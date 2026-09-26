"""Nightly CollegeFootballData.com pull for the Prospect Ledger (v2).

Writes compact JSON to data/. Claude reads these files on "refresh the ledger".
- Core files refresh daily (about 6 calls).
- Game-by-game box scores are pulled by conference and week, so every FBS
  player is covered without keeping a prospect list. Past seasons backfill a
  little each night (BACKFILL_BUDGET calls) and are then cached forever.
  The current season pulls each new week once, and re-pulls the latest two weeks on Sundays and Mondays.
Free CFBD tier: 1,000 calls a month. This stays well under it.
One failed call never stops the rest; failures land in data/manifest.json.
"""
import json, os, sys, time, datetime, requests

KEY = os.environ.get("CFBD_API_KEY")
if not KEY:
    sys.exit("CFBD_API_KEY secret is missing. Add it under Settings > Secrets and variables > Actions.")
BASE = "https://api.collegefootballdata.com"
H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
NOW = datetime.datetime.utcnow()
SEASON = NOW.year if NOW.month >= 8 else NOW.year - 1
PAST = [SEASON - 1, SEASON - 2]
CONFS = ["SEC", "B1G", "ACC", "B12", "PAC", "AAC", "MWC", "SBC", "CUSA", "MAC", "Ind"]
WEEKS = range(1, 17)
BACKFILL_BUDGET = 40
CATS = ("passing", "rushing", "receiving")
OUT = "data"
os.makedirs(OUT, exist_ok=True)
calls = 0
manifest = {"pulled_at": NOW.isoformat() + "Z", "season": SEASON, "files": {}, "errors": {}}

def get(path, **params):
    global calls
    calls += 1
    r = requests.get(BASE + path, headers=H, params=params, timeout=90)
    r.raise_for_status()
    time.sleep(0.6)
    return r.json()

def load(name, default):
    p = os.path.join(OUT, name)
    return json.load(open(p)) if os.path.exists(p) else default

def save(name, obj):
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    manifest["files"][name] = len(obj) if isinstance(obj, (list, dict)) else 1

def job(name, fn, cache=False):
    if cache and os.path.exists(os.path.join(OUT, name)):
        manifest["files"][name] = "cached"; return
    try:
        save(name, fn())
    except Exception as e:
        manifest["errors"][name] = str(e)[:300]

# ---------- core season files
for y in [SEASON] + PAST:
    job(f"player_season_{y}.json",
        lambda y=y: [r for r in get("/stats/player/season", year=y) if r.get("category") in CATS],
        cache=(y != SEASON))
    job(f"team_advanced_{y}.json", lambda y=y: get("/stats/season/advanced", year=y, excludeGarbageTime="true"), cache=(y != SEASON))
    job(f"games_{y}.json", lambda y=y: get("/games", year=y, classification="fbs"), cache=(y != SEASON))
job(f"sp_ratings_{SEASON}.json", lambda: get("/ratings/sp", year=SEASON))
for y in range(SEASON - 4, SEASON + 1):
    job(f"recruits_{y}.json", lambda y=y: get("/recruiting/players", year=y, classification="HighSchool"), cache=(y < SEASON))

# ---------- game-by-game box scores
def compact(games, y, wk, stype):
    rows = []
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
                rows.append({"g": g.get("id"), "y": y, "w": wk, "st": stype, "t": t.get("team"), "o": opp,
                             "ha": t.get("homeAway"), "pid": aid, "n": name, "c": cat, "s": st})
    return rows

state = load("gamelog_state.json", {})  # "Y|stype|conf|week" -> "done"
def pull_week(y, stype, conf, wk, force=False):
    k = f"{y}|{stype}|{conf}|{wk or 0}"
    if state.get(k) == "done" and not force: return None
    params = {"year": y, "conference": conf, "seasonType": stype}
    if wk: params["week"] = wk
    games = get("/games/players", **params)
    state[k] = "done"
    return compact(games, y, wk or 0, stype)

def merge_rows(y, new_rows, replace_keys):
    name = f"gamelogs_{y}.json"
    rows = [r for r in load(name, []) if (r["st"], r["w"], r["t"]) not in replace_keys]
    rows += new_rows
    save(name, rows)

# current season: re-pull the two most recent completed weeks
try:
    sched = load(f"games_{SEASON}.json", [])
    done_weeks = sorted({g.get("week") for g in sched if g.get("completed")})
    # re-pull the last two weeks on Sunday and Monday (UTC) to catch stat corrections
    recent = done_weeks[-2:] if (done_weeks and NOW.weekday() in (6, 0)) else []
    for wk in done_weeks:  # anything never pulled yet
        for conf in CONFS:
            if f"{SEASON}|regular|{conf}|{wk}" not in state and wk not in recent: recent.append(wk)
    for wk in sorted(set(recent)):
        batch = []
        for conf in CONFS:
            try:
                rows = pull_week(SEASON, "regular", conf, wk, force=True) or []
                batch += rows
            except Exception as e:
                manifest["errors"][f"gamelog {SEASON} wk{wk} {conf}"] = str(e)[:200]
        merge_rows(SEASON, batch, {(r["st"], r["w"], r["t"]) for r in batch})
except Exception as e:
    manifest["errors"]["gamelog current"] = str(e)[:300]

# past seasons: backfill within budget
spent = 0
for y in PAST:
    batch = []
    for stype, weeks in (("regular", WEEKS), ("postseason", [None])):
        for wk in weeks:
            for conf in CONFS:
                if spent >= BACKFILL_BUDGET: break
                k = f"{y}|{stype}|{conf}|{wk or 0}"
                if state.get(k) == "done": continue
                try:
                    rows = pull_week(y, stype, conf, wk)
                    spent += 1
                    if rows: batch += rows
                except Exception as e:
                    manifest["errors"][f"gamelog {k}"] = str(e)[:200]; spent += 1
    if batch:
        merge_rows(y, batch, set())
save("gamelog_state.json", state)
total = len(PAST) * (len(WEEKS) + 1) * len(CONFS)
manifest["backfill"] = {"done": sum(1 for k in state if int(k.split("|")[0]) in PAST), "of": total}
manifest["calls_this_run"] = calls
save("manifest.json", manifest)
print(json.dumps(manifest, indent=1))
