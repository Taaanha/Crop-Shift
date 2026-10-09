# CLAUDE.md: CropShift rules for Claude Code (keep this file short; it loads every session)

CropShift is Survey Crops' entry for NASA Space Apps 2026, Challenge 7, "Field Shift". It helps agriculture officers (SAAOs) and farmers in Cumilla, Feni, Brahmanbaria, Noakhali and Sylhet choose **which crop to plant and when**, with NASA data as the evidence. Event: 13–14 Nov 2026.

## Online-first
- The app is online-first: live → cache → fixture fallback, in that order. Only the **backend** ever fetches live data (NASA POWER near-real-time, SMAP latest, OPERA DSWx-S1 latest scenes, Open-Meteo, NOAA ENSO, FIRMS) — `web/` never calls out.
- Secrets (API keys, tokens) only via environment variables / HF Space secrets. Never hardcode, never commit.
- Cache is keyed per point per day, lives in `cache/` (gitignored). Demo fixtures for Aug-2024 and the 5 districts live in `demo_fixtures/` and are kept in git so the demo works with no network.
- Every response that used `safe_fetch` reports `source_mode` (`"live"`, `"cache"`, or `"fixture"`) so the UI/agent can be honest about where a number came from.

## Working with Tanha (a beginner)
- Before acting, say in 2–3 plain sentences what you will do and why. After acting, say what changed and how to check it.
- Save tokens:
  - Read only the files a task needs.
  - Never print whole data CSVs; use `head` or pandas summaries.
  - Run tests with `-q`.
  - Keep answers short unless asked.
- One task per session. When a task is done and committed, tell Tanha to start a fresh session.

## What CropShift says (the pivot; don't drift back)
- **Pitch = capability first:** a sowing-date risk map for every crop over decades of NASA data, a plot-level flood-recovery clock (OPERA radar 30 m + terrain + SMAP), and a flood → late-sowing → risk cascade, validated against SMAP and district yields. Honesty rules below keep it credible; they are not the headline.
- Recommendations come from **all years** of NASA data: for each crop × sowing date, "problems in X of N years", ranked by the average and by the worst 20% of years. Second line: irrigation need in mm.
- The closest analog year is the **"field twin": an example, never a forecast.** It is not used in the ranking.
- **Never** write that CropShift "predicts" the season. Say "NASA data for the area around your field", not "your field".
- El Niño lens (if built): all years vs El Niño years, always labelled "context, not forecast", with n shown.

## Non-negotiable architecture
1. `src/compute/`: deterministic Python, pytest for every function, no AI calls.
2. `src/ml/`: a real model only, with honest held-out accuracy.
3. `src/agents/`: a free Hugging Face open model (never a paid API), a plain loop with a max of 6 steps, **`guard()` blocks any number not found in a tool result that has a `dataset` and `url`**, a template fallback, and never empty JSON.
4. `web/`: static; renders API JSON only; never computes; never calls NASA or HF.
5. Every shown number is cited. Free tiers only (if something costs money, stop and tell Tanha). Raw rasters never go in git. Apache-2.0.
6. **Never cut:** the analog backtest (field twin), the Kc-weighted water comparison, `guard()`, and the hindcast.

## Data facts
- `data/processed/`: POWER daily 1981-01-01→2026-08-31 (`rainfall_mm, temp_mean_c, temp_max_c, temp_min_c, rh_pct, wind_speed_ms, solar_rad_mj_m2`); IMERG daily rain 2001-01-01→2025-09-30 (`imerg_<district>_daily.csv`, cells in `imerg_cells.csv`); SMAP root-zone 2015→2026; `kc_table.csv`; `district_metadata.csv`.
- **Rain source = NASA GPM IMERG V07 Final Run** (0.1°). Always load weather with `load_weather(district)` (`src/compute/weather.py`): IMERG rain + POWER temperature/RH/wind/solar. POWER rain has step changes (~1997 and ~2014-15, `docs/results/rain_source_check.md`); never use it for recommendations, rankings or trends.
- **Feni and Noakhali share one POWER cell** (temperature etc.). With IMERG their rain comes from separate cells, but Noakhali is only ~1-4% wetter: don't headline a contrast.
- OPERA DSWx-S1 exists over Feni/Cumilla from 21 Aug 2024 only.
- Researched values: `data/reference/*.csv` with the columns `item,value,unit,source_title,source_url,page,year,notes`. No source means no row.
- Primary window = seasons 2001-2024 (IMERG starts 2001; its Final Run ends 2025-09-30). POWER 1981-2025 is appendix-only.
- SMAP L4's rain forcing is corrected to IMERG, so model-vs-SMAP r is not independent evidence for IMERG.

## Task order (one per session; each with tests; commit on a branch and open a PR)
1. `src/agents/guard.py`: extract numbers (including Bangla digits ০–৯) and allow a number only if it matches a cited tool-result value (with rounding tolerance). Tests must include **a fabricated number being blocked**.
2. Add `decision_date` to `build_feature_table()` so no feature uses data after that date. Test it.
3. Test gaps: ET0 vs a worked FAO-56 example; `build_feature_table`; the agent tools. Move the misplaced `candidate_years` text out of the `eto_penman_monteith` docstring.
4. `water_balance.py`: FAO-56 daily root-zone balance (Kc × ET0, soil capacity cited) → stress days and irrigation mm. Validate against SMAP 2015+ and report r.
5. `hindcast.py` + `docs/results/hindcast.md`: leave one year out vs climatology; onset sensitivity; Mann-Kendall/Theil-Sen trends; El Niño-years test (after POWER 1981–2000 is added).
6. `risk_calendar.py`: crop × sowing date × hazard (GDD stages; thresholds only from `data/reference/`) over all years → feasibility filter → ranking by the average and by the worst 20% of years.
7. `post_flood.py`: SMAP days-to-normal (80th percentile ±15 days, held 7 days; also test the 70th and 90th) + DSWx-S1 persistence → earliest sowing date → **cascade**: re-read the risk calendar at the new date and report how the risk changed.
8. `validation.py`: water/heat-stress indices vs BBS district yield anomalies (from `data/reference/`); report r honestly.
9. `terrain.py`: HAND at 30 m from NASADEM around a point + DSWx-S1 flood frequency → plot-level flood/drainage class.
10. FastAPI matching `docs/api_contract.md` + contract tests against `web/mock/*.json`.
11. Agent loop with the free HF model, Bangla parsing, template fallback.
7b. Live season + live flood check for any GPS point inside the 5 districts.

## Folders
- **Tanha:** `src/`, `data/processed/`, `app.py`, `scripts/`.
- **Teammates:** `web/` (not `web/mock/`), `data/reference/`, `docs/research/`.
- **Shared (both agree, in its own PR):** `docs/api_contract.md`, `web/mock/`, `CLAUDE.md`, `CONTRIBUTING.md`.

## Commands (Windows, venv active, from the repo root)
- Tests (current layout): `cd src/compute; python -m pytest -q; cd ../..`. If you add tests elsewhere, make `python -m pytest src -q` work from the root (add `conftest.py` or package `__init__.py` files).
- Before a commit: tests pass, check `git status`, no file over 50 MB, `docs/AI_USE.md` updated if AI wrote code.
