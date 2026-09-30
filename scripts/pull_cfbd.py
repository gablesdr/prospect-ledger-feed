"""Nightly CollegeFootballData.com pull for the Prospect Ledger (v5, 2026-09-30).

v5: advanced player data for scatter charts and model tests, all on the free tier:
  ppa_players_<Y>.json  (/ppa/players/season: EPA/PPA per play, pass/rush and by down; 2014+)
  usage_<Y>.json        (/player/usage: share of team plays overall, pass, rush, by down; 2014+)
  roster_<Y>.json       (/roster: official height, weight, class year, hometown; 2014+)
  returning_<Y>.json    (/player/returning: team returning production; 2014+)
Current season: PPA and usage re-pulled Sunday/Monday (after the week's games) and on first run; roster once a week (Monday);
returning once. History 2014+: ADV_PER_RUN calls a night, newest first, cached like everything else.

v4: (a) recruiting classes 2008-current are pulled first, once (one call per class, skill positions only for past
classes), so the model can test recruiting pedigree before the full backfill reaches them; (b) nfl_seasons.json:
season-by-season NFL half-PPR points and positional finish (rank among all players at the position that season)
for every QB/RB/WR/TE drafted 2010+, from nflverse stats_player (free, not a CFBD call).

Writes compact JSON to data/. Claude reads these files on "refresh the ledger".

Budget: CFBD's free tier is 1,000 calls a month. This script counts every call
in data/budget.json and stops for the month at MONTH_CAP, so it can never go over.

Priority each night (highest first):
  1. Current season: player stats, team ratings, schedule, recruits, transfer portal (about 6 calls).
  2. Current season game logs: any newly completed week (1 call per week).
  3. The two most recent past seasons, then history back to START_YEAR, newest
     first: season files first (stats, team advanced, schedule, recruits), then
     game-by-game logs one week per call. HISTORY_PER_RUN caps this per night.
Anything already pulled is cached and never pulled again. If CFBD has no data
for a season or week, that is recorded and skipped. Failures never stop the run;
they land in data/manifest.json.
"""
import json, os, sys, time, datetime, requests

KEY = os.environ.get("CFBD_API_KEY")
if not KEY:
    sys.exit("CFBD_API_KEY secret is missing. Add it under Settings > Secrets and variables > Actions.")
BASE = "https://api.collegefootballdata.com"
H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
NOW = datetime.datetime.utcnow()
SEASON = NOW.year if NOW.month >= 8 else NOW.year - 1
START_YEAR = 2000
MONTH_CAP = 900          # hard stop, leaves 100 calls of headroom under the free 1,000
HISTORY_PER_RUN = 22     # nightly calls for backfill; about 25 nights to reach 2000
ADV_START = 2014         # v5: advanced player files back to here (matches the PFF history)
ADV_PER_RUN = 10         # v5: nightly calls for advanced-file history (48 files, about 5 nights)
WEEKS = list(range(1, 17))
CATS = ("passing", "rushing", "receiving")
OUT = "data"
os.makedirs(OUT, exist_ok=True)

def load(name, default):
    p = os.path.join(OUT, name)
    return json.load(open(p)) if os.path.exists(p) else default

def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    manifest["files"][name] = len(obj) if isinstance(obj, (list, dict)) else 1

manifest = {"pulled_at": NOW.isoformat() + "Z", "season": SEASON, "files": {}, "errors": {}}
month = NOW.strftime("%Y-%m")
budget = load("budget.json", None)
if budget is None:  # first v3 run: count calls already made this month by v2
    prev = load("manifest.json", {})
    seed = prev.get("calls_this_run", 0) if prev.get("pulled_at", "").startswith(month) else 0
    budget = {"month": month, "calls": seed}
if budget.get("month") != month:
    budget = {"month": month, "calls": 0}
state = load("pull_state.json", {})   # key -> "done" | "empty"
run_calls = 0

class OutOfBudget(Exception):
    pass

def get(path, **params):
    global run_calls
    if budget["calls"] >= MONTH_CAP:
        raise OutOfBudget()
    budget["calls"] += 1; run_calls += 1
    r = requests.get(BASE + path, headers=H, params=params, timeout=120)
    r.raise_for_status()
    time.sleep(0.6)
    return r.json()

def season_file(name, fn, cached):
    """Pull a season-level file unless it is cached."""
    if cached and (state.get(name) in ("done", "empty") or os.path.exists(os.path.join(OUT, name))):
        return 0
    rows = fn()
    if rows:
        save(name, rows); state[name] = "done"
    else:
        state[name] = "empty"
    return 1

def skill_stats(y):
    rows = get("/stats/player/season", year=y)
    return [r for r in rows if r.get("category") in CATS]

def compact(games, y, wk, stype):
    out = []
    for g in games:
        teams = g.get("teams", [])
        for t in teams:
            opp = next((o.get("team") for o in teams if o is not t), None)
            ath = {}
            for c in t.get("categories", []):
                if c.get("name") not in CATS: continue
                for ty in c.get("types", []):
                    for a in ty.get("athletes", []):
                        k = (a.get("id"), c["name"])
                        ath.setdefault(k, {"n": a.get("name")})[ty.get("name")] = a.get("stat")
            for (aid, cat), st in ath.items():
                name = st.pop("n")
                out.append({"g": g.get("id"), "y": y, "w": wk, "st": stype, "t": t.get("team"), "o": opp,
                            "ha": t.get("homeAway"), "pid": aid, "n": name, "c": cat, "s": st})
    return out

def gamelog_week(y, stype, wk, force=False):
    """One call covers every FBS game in a week. Rows replace that week in the season file."""
    k = f"gl|{y}|{stype}|{wk}"
    if state.get(k) in ("done", "empty") and not force:
        return 0
    params = {"year": y, "seasonType": stype}
    if wk: params["week"] = wk
    rows = compact(get("/games/players", **params), y, wk, stype)
    name = f"gamelogs_{y}.json"
    kept = [r for r in load(name, []) if not (r["st"] == stype and r["w"] == wk)]
    if rows or kept:
        save(name, kept + rows)
    state[k] = "done" if rows else "empty"
    return 1

try:
    # 1. current season core (daily)
    for name, fn in ((f"player_season_{SEASON}.json", lambda: skill_stats(SEASON)),
                     (f"team_advanced_{SEASON}.json", lambda: get("/stats/season/advanced", year=SEASON, excludeGarbageTime="true")),
                     (f"games_{SEASON}.json", lambda: get("/games", year=SEASON, classification="fbs")),
                     (f"sp_ratings_{SEASON}.json", lambda: get("/ratings/sp", year=SEASON)),
                     (f"recruits_{SEASON}.json", lambda: get("/recruiting/players", year=SEASON, classification="HighSchool")),
                     (f"portal_{SEASON}.json", lambda: get("/player/portal", year=SEASON))):
        try: season_file(name, fn, cached=False)
        except OutOfBudget: raise
        except Exception as e: manifest["errors"][name] = str(e)[:300]

    # 2. current season game logs: new weeks, plus re-pull last two weeks on Sun/Mon (stat corrections)
    sched = load(f"games_{SEASON}.json", [])
    done_weeks = sorted({g.get("week") for g in sched if g.get("completed") and g.get("seasonType", "regular") == "regular"})
    redo = set(done_weeks[-2:]) if NOW.weekday() in (6, 0) else set()
    for wk in done_weeks:
        try: gamelog_week(SEASON, "regular", wk, force=(wk in redo))
        except OutOfBudget: raise
        except Exception as e: manifest["errors"][f"gamelog {SEASON} wk{wk}"] = str(e)[:200]

    # 2b. v4: recruiting classes back to 2008, once each (skill positions + athletes for past classes)
    SKILLPOS = {"PRO", "DUAL", "QB", "RB", "APB", "WR", "TE", "ATH"}
    for y in range(SEASON - 1, 2007, -1):
        name = f"recruits_{y}.json"
        if state.get(name) in ("done", "empty") or os.path.exists(os.path.join(OUT, name)): continue
        try:
            rows = [r for r in get("/recruiting/players", year=y, classification="HighSchool") if (r.get("position") or "").upper() in SKILLPOS]
            if rows: save(name, rows); state[name] = "done"
            else: state[name] = "empty"
        except OutOfBudget: raise
        except Exception as e: manifest["errors"][name] = str(e)[:200]

    # 2c. v5: advanced player files (PPA, usage, roster, returning). Current season weekly, history 2014+ capped per night.
    ADV = lambda y: [(f"ppa_players_{y}.json", lambda y=y: get("/ppa/players/season", year=y, excludeGarbageTime="true")),
                     (f"usage_{y}.json", lambda y=y: get("/player/usage", year=y, excludeGarbageTime="true")),
                     (f"roster_{y}.json", lambda y=y: [{k: r.get(k) for k in ("id", "firstName", "lastName", "team", "height", "weight", "jersey", "year", "position", "homeCity", "homeState", "recruitIds")}
                                                        for r in get("/roster", year=y) if (r.get("position") or "") in ("QB", "RB", "WR", "TE", "FB", "ATH")]),
                     (f"returning_{y}.json", lambda y=y: get("/player/returning", year=y))]
    weekly = NOW.weekday() in (6, 0)
    for name, fn in ADV(SEASON):
        fresh = (name.startswith("ppa_") or name.startswith("usage_")) and weekly or name.startswith("roster_") and NOW.weekday() == 0
        try: season_file(name, fn, cached=not fresh)
        except OutOfBudget: raise
        except Exception as e: manifest["errors"][name] = str(e)[:300]
    adv_spent = 0
    for y in range(SEASON - 1, ADV_START - 1, -1):
        for name, fn in ADV(y):
            if adv_spent >= ADV_PER_RUN: break
            try: adv_spent += season_file(name, fn, cached=True)
            except OutOfBudget: raise
            except Exception as e: manifest["errors"][name] = str(e)[:200]; adv_spent += 1
        if adv_spent >= ADV_PER_RUN: break
    manifest["advanced"] = {"start_year": ADV_START, "files_done": sum(1 for k, v in state.items() if v in ("done", "empty") and k.startswith(("ppa_", "usage_", "roster_", "returning_"))),
                            "files_total": 4 * (SEASON - ADV_START + 1)}

    # 3. backfill, newest season first, within the nightly allowance
    spent = 0
    for y in range(SEASON - 1, START_YEAR - 1, -1):
        plan = [(f"player_season_{y}.json", lambda y=y: skill_stats(y)),
                (f"team_advanced_{y}.json", lambda y=y: get("/stats/season/advanced", year=y, excludeGarbageTime="true")),
                (f"games_{y}.json", lambda y=y: get("/games", year=y, classification="fbs")),
                (f"recruits_{y}.json", lambda y=y: get("/recruiting/players", year=y, classification="HighSchool"))]
        if y >= 2021: plan.append((f"portal_{y}.json", lambda y=y: get("/player/portal", year=y)))
        for name, fn in plan:
            if spent >= HISTORY_PER_RUN: break
            try: spent += season_file(name, fn, cached=True)
            except OutOfBudget: raise
            except Exception as e: manifest["errors"][name] = str(e)[:200]; spent += 1
        for stype, wk in [("regular", w) for w in WEEKS] + [("postseason", 1)]:
            if spent >= HISTORY_PER_RUN: break
            try: spent += gamelog_week(y, stype, wk)
            except OutOfBudget: raise
            except Exception as e: manifest["errors"][f"gamelog {y} {stype} {wk}"] = str(e)[:200]; spent += 1
        if spent >= HISTORY_PER_RUN: break
except OutOfBudget:
    manifest["errors"]["budget"] = f"Monthly cap of {MONTH_CAP} calls reached; resumes next month."

# ---------- NFL outcomes for the comp engine (nflverse, free; not a CFBD call)
try:
    import csv, io
    txt = requests.get("https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv", timeout=90).text
    keep = ["season", "round", "pick", "age", "pfr_player_name", "position", "college", "to", "games", "pass_yards", "rush_yards", "rec_yards", "receptions", "pass_tds", "rush_tds", "rec_tds"]
    rows = [{k: r.get(k) for k in keep} for r in csv.DictReader(io.StringIO(txt)) if r.get("position") in ("QB", "RB", "WR", "TE") and r.get("season", "0").isdigit() and int(r["season"]) >= 2010]
    if rows: save("nfl_outcomes.json", rows)
except Exception as e:
    manifest["errors"]["nfl_outcomes"] = str(e)[:200]

# ---------- v4: NFL season-by-season fantasy finishes (nflverse stats_player, free; not a CFBD call)
try:
    dp = {r.get("gsis_id"): r for r in csv.DictReader(io.StringIO(requests.get("https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv", timeout=90).text))
          if r.get("gsis_id") and r.get("position") in ("QB", "RB", "WR", "TE") and r.get("season", "0").isdigit() and int(r["season"]) >= 2010}
    out, miss = [], []
    for y in range(2010, SEASON):
        try:
            t = requests.get(f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_reg_{y}.csv", timeout=120)
            t.raise_for_status()
        except Exception as e:
            miss.append(y); continue
        rows = list(csv.DictReader(io.StringIO(t.text)))
        pts = []
        for r in rows:
            pos = r.get("position") or r.get("position_group")
            if pos not in ("QB", "RB", "WR", "TE"): continue
            try: ppr = float(r.get("fantasy_points_ppr") or 0); rec = float(r.get("receptions") or 0); g = int(float(r.get("games") or 0))
            except ValueError: continue
            pts.append((pos, r.get("player_id"), ppr - 0.5 * rec, g))
        for pos in ("QB", "RB", "WR", "TE"):
            L = sorted([p for p in pts if p[0] == pos], key=lambda p: -p[2])
            for i, (_, pid, hp, g) in enumerate(L):
                d = dp.get(pid)
                if d: out.append([d.get("pfr_player_name"), int(d["season"]), y, pos, g, round(hp, 1), i + 1])
    if out: save("nfl_seasons.json", {"fields": ["pfr_player_name", "draft_season", "season", "pos", "games", "half_ppr", "pos_finish"], "rows": out})
    if miss: manifest["errors"]["nfl_seasons"] = "no stats_player_reg file for " + ",".join(map(str, miss))
except Exception as e:
    manifest["errors"]["nfl_seasons"] = str(e)[:200]

# ---------- Combine measurables for comps (nflverse, free; not a CFBD call)
try:
    txt = requests.get("https://github.com/nflverse/nflverse-data/releases/download/combine/combine.csv", timeout=90).text
    keep = ["season", "player_name", "pos", "school", "ht", "wt", "forty", "bench", "vertical", "broad_jump", "cone", "shuttle", "draft_year", "draft_round", "draft_ovr"]
    rows = [{k: r.get(k) for k in keep} for r in csv.DictReader(io.StringIO(txt)) if r.get("pos") in ("QB", "RB", "WR", "TE") and r.get("season", "0").isdigit() and int(r["season"]) >= 2010]
    if rows: save("combine.json", rows)
except Exception as e:
    manifest["errors"]["combine"] = str(e)[:200]

# progress report
hist = list(range(SEASON - 1, START_YEAR - 1, -1))
per_season = 4 + len(WEEKS) + 1
ADVP = ("ppa_", "usage_", "roster_", "returning_")
done = sum(1 for k in state if not k.startswith("gl|") and not k.startswith(ADVP) and any(k.endswith(f"_{y}.json") for y in hist)) + \
       sum(1 for k in state if k.startswith("gl|") and int(k.split("|")[1]) in hist)
manifest["backfill"] = {"start_year": START_YEAR, "steps_done": done, "steps_total": len(hist) * per_season,
                        "empty": sorted(k for k, v in state.items() if v == "empty")[:50]}
manifest["budget"] = {"month": budget["month"], "calls_used": budget["calls"], "cap": MONTH_CAP, "calls_this_run": run_calls}
save("pull_state.json", state)
save("budget.json", budget)
save("manifest.json", manifest)
print(json.dumps(manifest, indent=1))
