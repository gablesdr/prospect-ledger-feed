# Prospect Ledger data feed

A nightly job that pulls college football data from CollegeFootballData.com (CFBD) and saves it in `data/`. Claude reads these files when German says "refresh the ledger".

## What's in data/

| File | What it is |
| --- | --- |
| `player_season_YYYY.json` | Official passing, rushing and receiving season stats for every FBS player (current season and the two before) |
| `recruits_YYYY.json` | 247Sports composite recruiting rankings by high school class |
| `team_advanced_YYYY.json` | Team offense and defense efficiency (EPA/PPA, success rate, explosiveness), garbage time removed |
| `sp_ratings_YYYY.json` | SP+ team ratings |
| `games_YYYY.json` | Schedule and scores for FBS games |
| `gamelogs_YYYY.json` | Game-by-game passing, rushing and receiving box scores for every FBS player |
| `manifest.json` | When the last pull ran, backfill progress, and anything that failed |

## Setup (one time)

1. Get a free API key at collegefootballdata.com (Key Registration). It arrives by email.
2. In this repo: Settings > Secrets and variables > Actions > New repository secret. Name it `CFBD_API_KEY`, paste the key.
3. Actions tab > "Pull CFBD data" > Run workflow. After a few minutes, `data/` fills in.

The job then runs every morning. The repo stays public so Claude can read the files; the API key stays private in Secrets.

Data courtesy of CollegeFootballData.com.
