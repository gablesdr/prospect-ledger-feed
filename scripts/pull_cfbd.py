"""Nightly CollegeFootballData.com pull for the Prospect Ledger (v3).

Writes compact JSON to data/. Claude reads these files on "refresh the ledger".

Budget: CFBD's free tier is 1,000 calls a month. This script counts every call
in data/budget.json and stops for the month at MONTH_CAP, so it can never go over.

Priority each night (highest first):
  1. Current season: player stats, team ratings, schedule, recruits (about 5 calls).
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
                     (f"recruits_{SEASON}.json", lambda: get("/recruiting/players", year=SEASON, classification="HighSchool"))):
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

    # 3. backfill, newest season first, within the nightly allowance
    spent = 0
    for y in range(SEASON - 1, START_YEAR - 1, -1):
        plan = [(f"player_season_{y}.json", lambda y=y: skill_stats(y)),
                (f"team_advanced_{y}.json", lambda y=y: get("/stats/season/advanced", year=y, excludeGarbageTime="true")),
                (f"games_{y}.json", lambda y=y: get("/games", year=y, classification="fbs")),
                (f"recruits_{y}.json", lambda y=y: get("/recruiting/players", year=y, classification="HighSchool"))]
        for name, fn in plan:
            if spent >= HISTORY_PER_RUN: break
            try: spent += season_file(name, fn, cached=True)
            except OutOfBudget: raise
            except Exception as e: manifest["errors"][name] = str(e)[:200]; spent += 1
        for stype, wk in [("regular", w) for w in WEEKS] + [("postseason", 0)]:
            if spent >= HISTORY_PER_RUN: break
            try: spent += gamelog_week(y, stype, wk)
            except OutOfBudget: raise
            except Exception as e: manifest["errors"][f"gamelog {y} {stype} {wk}"] = str(e)[:200]; spent += 1
        if spent >= HISTORY_PER_RUN: break
except OutOfBudget:
    manifest["errors"]["budget"] = f"Monthly cap of {MONTH_CAP} calls reached; resumes next month."

# progress report
hist = list(range(SEASON - 1, START_YEAR - 1, -1))
per_season = 4 + len(WEEKS) + 1
done = sum(1 for k in state if not k.startswith("gl|") and any(k.endswith(f"_{y}.json") for y in hist)) + \
       sum(1 for k in state if k.startswith("gl|") and int(k.split("|")[1]) in hist)
manifest["backfill"] = {"start_year": START_YEAR, "steps_done": done, "steps_total": len(hist) * per_season,
                        "empty": sorted(k for k, v in state.items() if v == "empty")[:50]}
manifest["budget"] = {"month": budget["month"], "calls_used": budget["calls"], "cap": MONTH_CAP, "calls_this_run": run_calls}
save("pull_state.json", state)
save("budget.json", budget)
save("manifest.json", manifest)
print(json.dumps(manifest, indent=1))
