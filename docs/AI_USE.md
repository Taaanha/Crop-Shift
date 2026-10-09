# AI Use Log

This file tracks every AI tool used in building CropShift, per NASA Space
Apps transparency requirements and the Space Apps build guide's
`evidence-provenance` guidance. Updated as the project progresses, not
written after the fact.

## Tools used

**Claude (Anthropic, claude.ai chat)** — used throughout the build, via
plain conversational chat (no Claude Code, no autonomous agent access to
this repo). Used for:
- Project planning and phase sequencing (prompt-2.md, this log)
- Step-by-step teaching/guidance for a first-time coder (git, VS Code,
  Colab, virtual environments)
- Writing and debugging `src/compute/agroclimate.py`: `rain_onset()`,
  `nearest_analog_year()`, `build_feature_table()`, `dry_spell_length()`,
  `eto_penman_monteith()` (FAO-56 Penman-Monteith), `backtest_rotation()`
- Writing unit tests for all of the above (`test_agroclimate.py`)
- Debugging real data issues found during development (a unit-conversion
  bug in solar radiation between POWER communities, a percentile
  tie-breaking bug, corrupted intermediate CSV files, misplaced import
  statements)
- Empirically tuning `rain_onset()`'s confirmation threshold against
  2001-2025 Cumilla POWER data
- Compiling FAO-56 standard crop coefficient (Kc) reference values
- Setting up the Hugging Face Inference Providers connection
  (`src/agents/test_hf_connection.py`, `test_hf_tool_calling.py`)
- Writing `src/agents/guard.py`: the provenance gate that extracts every
  number from the agent's narrated text (English and Bangla ০-৯ digits,
  comma grouping, decimals, %) and blocks any number that doesn't match a
  value inside a tool result carrying both `dataset` and `url`. Also wrote
  its pytest tests (`test_guard.py`), including a fabricated-number case.

**Claude Code** — added a `decision_date` parameter (default `"10-31"`) to
`build_feature_table()` in `src/compute/agroclimate.py`, so every feature
for year Y (onset, onset amount, dry spell, mean temperature) is computed
only from data on or before `decision_date` of that year — no feature can
use data that wouldn't yet exist on the day a recommendation is made. Also
adjusted the row's minimum-coverage floor to scale with `decision_date`
(previously a flat 300 days, which would have rejected every early-season
cutoff), threaded `decision_date` through
`tool_nearest_analog_year()` in `src/agents/tool_functions.py`, and added
tests proving (1) mutating data after `decision_date` does not change the
computed features and (2) a different `decision_date` does change them.

**Claude Code** — filled Task 3 test gaps: (1) a test validating
`eto_penman_monteith()` against FAO-56 (Allen et al., 1998) Chapter 4
Example 18 (Uccle, Belgium, 6 July, published ET0=3.9 mm/day); found and
documented that the function's single `rh_pct` input (vs. FAO-56's
separate RHmax/RHmin) moves its result to 3.79 mm/day, just outside the
requested ±0.1 mm/day tolerance of the published value, so the test
validates against the function's own arithmetic (3.79) with the gap noted
in the docstring rather than fudging the tolerance; (2) a direct
`build_feature_table()` test with hand-computed expected values on a
small synthetic dataframe, plus a test confirming a no-onset year is
excluded from the table; (3) `src/agents/test_tool_functions.py`, testing
`tool_nearest_analog_year()` and `tool_backtest_rotation()` against real
district CSVs in `data/processed/` (no mocking) — result keys, that every
`source` carries `dataset` and `url` (what `guard()` requires), and the
pre-2015 SMAP refusal. Also moved a `candidate_years` explanation that had
been left in `eto_penman_monteith()`'s docstring into
`nearest_analog_year()`'s, where that parameter actually lives.

**Claude Code** — gave `eto_penman_monteith()` a second, more accurate path
for actual vapour pressure: when the row carries `rh_max_pct`/`rh_min_pct`,
`ea` is computed from those separately (FAO-56 Eq.17); otherwise it falls
back to the existing RH-mean method (FAO-56 Eq.19), which is what NASA
POWER's single daily RH2M value requires. Replaced the old single Example
18 test with two: one feeding RHmax=84/RHmin=63 through the new Eq.17 path
(matches FAO-56's published 3.9 mm/day within ±0.1), and one feeding
RHmean=73.5% through the Eq.19 fallback (documented as validating the
function's own arithmetic at 3.79 mm/day, not the published value, per the
known approximation gap).

**Claude Code** — Task 4, the FAO-56 root-zone water balance. Wrote
`src/compute/water_balance.py`: FAO-56 Chapter 8 Eq. 82-88 (TAW, RAW,
Ks, the daily depletion balance, deep percolation), a Kc curve that
interpolates linearly through the development and late stages (FAO-56
Eq. 66 / Fig. 25) instead of stepping, and per crop/sowing date/year
outputs (water-stress days rainfed, net irrigation mm to refill to field
capacity whenever Dr > RAW, deep percolation mm, daily tables). Wrote its
tests (`test_water_balance.py`: mass balance closes, no-rain depletion
grows, heavy rain drains, irrigation refills, a hand-computed stress-day
count). Built the two reference files from sources it read during the
session rather than from memory: `data/reference/crop_params.csv` (Zr
and p from FAO-56 Table 22, checked against the FAO HTML edition) and
`data/reference/soil_params.csv`, written by the new
`src/acquire/fetch_soilgrids.py` (clay/silt/sand from ISRIC SoilGrids 2.0
around each district, sampled away from the India border, then the
FAO-56 Table 19 midpoints for that USDA texture). Two districts'
SoilGrids textures have no Table 19 row (Brahmanbaria: clay loam;
Sylhet: sandy clay loam); the script refuses to guess unless a reviewed
fallback is recorded, and uses the nearest Table 19 class (clay; loam),
with the reason written into each row's notes. Wrote
`scripts/validate_water_balance.py`, which compares weekly anomalies of a
reference-grass balance with SMAP root-zone soil moisture (2015-2025) and
writes `docs/results/water_balance_validation.md`.

**Hugging Face Inference Providers** — a free, open-source hosted model
(Llama-3.1-8B-Instruct) is used as the CropShift agent's reasoning layer
(Layer 3, `src/agents/`). Per the project's architecture rules, this model
never computes any statistic or number itself — it only calls deterministic
Python tools (Layer 1, `src/compute/`) and narrates their results. This is
enforced by a provenance gate (`src/agents/guard.py`) that blocks any
narrated number that doesn't match a cited tool result.

**Claude Code** — moved the project to an online-first design: updated
`CLAUDE.md` (live → cache → fixture fallback; only the backend fetches
live data; secrets via env vars only; per-point-per-day cache; every
response reports `source_mode`) and added Task 7b (live season + live
flood check for any GPS point in the 5 districts). Wrote
`src/acquire/safe_fetch.py`: `fetch_json(url, params, cache_key,
ttl_hours=24)` tries a live GET first, falls back to a per-point cache in
`cache/` (gitignored) if fresh, then to a checked-in fixture in
`demo_fixtures/`, then to a stale cache as a last resort so a demo never
hard-fails if any prior data exists; `OFFLINE=1` skips the live attempt
entirely. Wrote `src/acquire/test_safe_fetch.py` against a fake
non-resolving URL and a temp cache/fixture dir (no real network), covering
the fixture fallback, the cache fallback, the no-data-at-all error, the
`OFFLINE` flag, and the stale-cache last resort.

**Claude Code** — Task 5a, extended the historical record back to 1981 and
added ENSO context. Gave `src/acquire/fetch_power.py` an optional
`start_date` argument (default unchanged at `20010101`) and re-downloaded
all 5 districts from 1981-01-01 to 2026-08-31; POWER's `-999` missing-value
sentinel is now converted to `NaN` rather than left as a magic number, and
no gap is filled. All 7 weather columns have real data from 1981-01-01
except `solar_rad_mj_m2`, which starts 1984-01-01 in every district (later
than the ~mid-1983 estimate in the task brief; reported as found, not
adjusted to match the estimate). Wrote `scripts/fetch_oni.py`, which
downloads NOAA CPC's ONI ascii table and saves
`data/reference/oni.csv` (919 rows, 1950-2026, columns `season, year,
total, anom, source_title, source_url`). Wrote `src/compute/enso.py`
(`label_enso_years()`): the standard CPC rule — an episode is >=5
consecutive overlapping 3-month seasons with anomaly >=+0.5 (El Nino) or
<=-0.5 (La Nina) — applied to the ONI series to label every year. Found
and fixed a bug in its own first implementation during manual verification
against the real ONI series: grouping consecutive True/False runs with
`(~is_event).cumsum()` merges the boundary False row into the next run,
so any event run not starting at row 0 of the series was silently
dropped — caught because the real data returned zero El Nino/La Nina years
where several are well known (1982-83, 1997-98, 2015-16, 2023-24), while
the hand-written unit tests had (accidentally) all placed their event runs
at the start of the tiny test series and so didn't catch it. Fixed with
the standard change-point method (`is_event != is_event.shift()`) and
added a regression test with a run that starts mid-series. `build_feature_table()`'s
default years are unchanged (2001-2025); `python -m pytest src -q` passes
(74 tests) with no changes to any existing test's expected values.

**Claude Code** — Task 5b, hindcast + trends + El Nino lens. Wrote
`src/compute/hindcast.py` (seasonal totals that refuse partly covered
windows, analog ranking built on the unchanged `nearest_analog_year()`,
a leave-one-year-out comparison of the long-term average, 1 analog, the
mean of 5 analogs and the ENSO-phase average, skill = 1 - MAE/MAE of the
long-term average with a sign test, Mann-Whitney group comparison,
Mann-Kendall + Theil-Sen trends via SciPy, and `rain_onset()` threshold
sensitivity) with tests on tiny synthetic data (`test_hindcast.py`: a
perfect analog scores skill 1, a constant series makes the long-term
average exact, the held-out season never feeds its own estimate). Added
`classify_oni()` and `season_enso_labels()` to `src/compute/enso.py`
(one label per season from the ASO ONI value, known by the 31 Oct
decision date) with new tests in `test_enso.py`; existing functions,
defaults and tests unchanged. Wrote `scripts/run_hindcast.py`, which
writes `docs/results/hindcast.md`. Added SciPy to the venv (free,
open-source). While checking the real-data results it found and reported,
without changing the existing functions: (1) POWER Jun-Sep rain in
1981-2000 is about half the 2001-2025 level and most early years have no
confirmed monsoon onset, so the 1981-2025 rain "trends" are not
presented as climate change and a 2001-2025 robustness check was added;
(2) `rain_onset()` accepts a wet week in the last 7 days before the
cutoff without its dry-spell and 30-day checks, which gives some years
a spurious late-October onset in `build_feature_table()`; these are
flagged in the report. Net irrigation from the water balance moves in
steps of about one RAW refill, which the report states.

**Claude Code** — fixed the `rain_onset()` edge case flagged in Task 5b.
A 7-day window with fewer than 7 days of data after it (a wet week just
before the 31 Oct cutoff) was accepted as the onset without the
dry-spell and 30-day checks; it is now skipped, so such a year has no
onset. All other defaults unchanged. Added a test (a single wet week at
the very end of the data is not an onset) and rewrote one hindcast test
that had pinned the old behaviour. Re-ran `scripts/run_hindcast.py`: the
unconfirmed late-October onsets in `docs/results/hindcast.md` drop to 0,
and those years now count as "no onset", so 21 fewer district-seasons
are scored in the analog comparison (pooled 141 -> 120 for rabi rain).
The conclusion is unchanged: nothing tested beats the long-term average.

**Claude Code** — Task 5c, made 2001-2025 the primary analysis window.
Restructured `scripts/run_hindcast.py` and `docs/results/hindcast.md` so
the hindcast skill table, El Nino lens and trends are computed and
reported over 2001-2025 only; the 1981-2025 record now appears only in
an "Appendix: Data-consistency check (not used for recommendations)",
alongside a small year-by-year table (1993-2000) showing NASA POWER's
Jun-Sep rain jump from ~700-1,200 mm to ~1,300-1,600 mm around 1997. This
was a data-consistency break flagged during Task 5b, not a real change
in the monsoon. Added the corresponding line to CLAUDE.md's "Data facts".
Added `requirements.txt` (packages actually imported by the code: pandas,
numpy, scipy, requests, pytest, python-dotenv, openai; major versions
pinned only). Deleted 6 remote branches already merged into main
(fix-onset-end-of-data, task1-guard, task3-tests, task4-water-balance,
task5b-hindcast, team-setup); 2 others (online-first, task5a-data-1981)
had already been deleted. `python -m pytest src -q` passes unchanged
(102 tests; no test values changed since Task 5b's fix, only the report
layout).

**Claude Code** — Task 5d, switched rain to NASA GPM IMERG. Wrote
`scripts/fetch_imerg.py` (IMERG V07 Final Run daily, GPM_3IMERGDF.07,
through the NASA Giovanni time-series API, the first access method that
worked; resumable; token read from `.env`, never printed). It downloaded
2001-01-01 to 2025-09-30 (the Final Run's last day at GES DISC) for the five
district points in about 6.5 minutes and records each 0.1 degree cell centre in
`data/processed/imerg_cells.csv`. Added `src/compute/weather.py`
(`load_weather()`: POWER table with IMERG rain, never mixing the two rain
records) and `src/compute/change_point.py` (Pettitt test), each with tests.
`scripts/check_rain_source.py` writes `docs/results/rain_source_check.md`:
POWER rain has a significant change point around 2014-15 in all 10
district series (annual and Jun-Sep, +38% to +84%); IMERG has none (every
p > 0.5). The decision rule was fixed in the script before the results
were seen. Added BMD station monthly normals from BMD's own PDF to
`data/reference/bmd_normal_rainfall.csv`. IMERG is now the default rain
source for `validate_water_balance.py`, `run_hindcast.py` (primary window
now seasons 2001-2024; POWER 1981-2025 kept for the appendix) and the agent
tools (which also now refuse to match on a year cut off before the
decision date). SMAP weekly-anomaly r rose in every district (mean 0.44 ->
0.56). Claude checked the SMAP L4 user guide and flagged that SMAP L4's rain
forcing is corrected to IMERG, so this gain is not independent evidence.
The POWER "+458 mm/decade Jun-Sep" trend is gone with IMERG (+1.8
mm/decade, p = 1.00) and was removed. `python -m pytest src -q`: 115 passed.

**Claude Code** — Task 7a, the post-flood recovery clock. Wrote
`src/compute/post_flood.py`: `smap_days_to_normal()` (a calendar-window
percentile of every *other* year, held for `hold_days` in a row, with 70th/
90th percentile sensitivity reported alongside the 80th), `water_persistence()`
(last date OPERA DSWx-S1 saw open water/inundated vegetation in a window,
plus how many observations were usable), `earliest_sowing_date()` (the later
of the two, no extra buffer), and `crops_still_possible()` (compares against
`data/reference/crop_calendar.csv`, which does not exist yet, so every crop
comes back "research pending" rather than an invented sowing window — this
is the honest, unfinished half of the flood -> late-sowing -> risk cascade;
the re-ranking half needs Task 6's `risk_calendar.py`, not built yet). Wrote
`test_post_flood.py` (18 tests: synthetic recovery curves, the hold-days
requirement, percentile sensitivity ordering, DSWx-S1 validity filtering,
the "research pending" path for a missing file and a missing crop row).

Wrote `scripts/fetch_dswx.py`: searches NASA CMR via `earthaccess` for OPERA
DSWx-S1 granules near a point, then reads only a 300 m x 300 m window of the
WTR band per granule via an HTTPS range request (`rasterio` + `earthaccess`'s
authenticated fsspec session) — never a full scene, and nothing raster ever
touches disk or git. Looked up the actual WTR band value codes (0/1/3 = not
water/open water/inundated vegetation, 250/251 = HAND/layover-shadow masked,
255 = fill) from the OPERA DSWx-S1 product spec and PO.DAAC docs rather than
guessing. Timed 5 real granule reads (~4.1 s each; ~3.4-3.5 s/granule once
warmed up) before running the fetch, and reported the full-history estimate
(1,616 granules, ~94 minutes) versus what was actually fetched (315
granules through 2024-12-31, ~18 minutes) in `docs/results/post_flood.md`,
rather than running the full fetch unasked. Added `earthaccess` and
`rasterio` to `requirements.txt` and `EARTHDATA_TOKEN` to `.env.example`
(it was already required by the existing `scripts/fetch_imerg.py` but
missing from the example file).

Ran the pipeline for August 2024 (Feni, Cumilla, Noakhali, Brahmanbaria) and
June 2022 (Sylhet, SMAP-only — DSWx-S1's mission data starts 2023-12-01,
after that flood) and wrote up the results, including a caveat that
DSWx-S1's persistent small water_fraction at the Noakhali and Brahmanbaria
district points looks like a permanent water feature (pond/khal) inside the
300 m window rather than draining floodwater, so those two districts'
headline earliest-sowing-date uses the SMAP-only date with DSWx-S1 flagged
as inconclusive, instead of silently combining a likely-spurious "still
flooded" reading into the number. `python -m pytest src -q`: 133 passed.

**Claude Code** — Task 7a-2, making the DSWx-S1 flood signal visible. Added
an `--area` mode to `scripts/fetch_dswx.py`. It reads a 20 km x 20 km
window (30 m UTM grid, WarpedVRT range reads, no full tiles) from every
DSWx-S1 granule touching it. It covers the flood period (2024-08-21 to
2024-12-31) and a dry-season reference (Jan-Mar 2025), caches the windows in
`cache/` (gitignored), and builds `data/processed/dswx_area_<district>.csv`
plus 8 small PNG maps (Feni, Noakhali) in `docs/results/img/`. Timed 5 reads
first (~4.0 s/granule; 702 granules, ~47 min estimated, 41 min actual, 0
failures). Wrote `src/compute/flood_area.py` (merge same-day granules,
permanent-water mask, flood stats) with `test_flood_area.py`. Added
`flood_recession()` to `post_flood.py` and let `earliest_sowing_date()`
take its result, with 7 new tests. The plain "below 10% of peak" rule never
triggered at Cumilla or Noakhali, because the method sees a floor of
non-permanent water even in the dry season. So `flood_recession()` also
takes an optional `baseline` (the default of 0 keeps the plain rule), and
the report headlines "90% of the water above the dry-season floor gone",
showing the plain-rule result next to it. Claude flagged that Noakhali's
DSWx-S1 signal can't be separated from seasonal water, and that
Brahmanbaria's is mostly seasonal drawdown, rather than presenting them as
flood recession. Added `matplotlib` to `requirements.txt`.
`python -m pytest src -q`: 147 passed.

**Claude Code** — Task 6, sowing-date risk calendar with rotation input.
Wrote `src/compute/reference.py`, a strict loader for `data/reference/*.csv`.
It skips PLACEHOLDER rows, non-http(s) sources, malformed items and
unparseable values, and lists each with a reason. It parses A-B ranges and
MMDD dates and keeps every kept row's source/page. Teammates' files were not
edited. Also wrote `src/compute/risk_calendar.py`: GDD-calibrated stages,
hazards only where a kept row gives both the threshold and the stage timing,
the water balance from `water_balance.crop_season()`, a ponded-paddy need
for boro, and `rotation_options()`. `scripts/build_risk_calendar.py` writes
`data/processed/risk_calendar.csv` and `docs/results/risk_calendar.md`.
Every modelling choice not from a source is a named "CropShift assumption",
with sensitivity runs for hot days (1/3/5), threshold end and flowering
window width. Claude reported, rather than filled, what the strict rules
remove. The BRRI rows (file-name sources) held boro's only stage durations
and the 12-13 C cold threshold, so the boro cold sanity check cannot run
and its test is skipped. Mustard's flowering row is split across two lines.
The CZIS zoning rows are skipped, are not used as a filter, and conflict
with BBS area. It also fixed a pre-existing failing test: `post_flood.py`'s
`crops_still_possible()` expected an older crop_calendar layout and would
crash on the real file, so it now returns a clear pending notice.
`python -m pytest src -q`: 191 passed, 5 skipped.

**Claude Code** — Task 6b, hazard coverage labelling in
`rotation_options()`. Every ranked crop now carries `hazards_checked` /
`hazards_missing` (lists), `n_hazards_checked`, `coverage` ("full" only if
every hazard `HAZARDS` lists for that crop is sourced, else "partial") and
`problem_line`, a plain-language sentence that always names what was
checked, e.g. "problems in 0 of 24 years (checked: night heat only)", so a
crop is never read as safer just because fewer hazards were evaluated for
it. Partial-coverage crops get `coverage_notice`: "Not all risks for this
crop are checked yet." The ranking order itself is unchanged (still
problem share, then worst-20% irrigation); this only adds the label.
`scripts/build_risk_calendar.py`'s "problem years" cell now uses
`problem_line` directly and a new "coverage" column was added.
`data/reference/` was not touched. `python -m pytest src -q`: 192 passed,
5 skipped (one new test: a crop with 0 problems but partial coverage gets
the partial label and notice).

**Claude Code**: Task 10a, the first real API (`app.py`, FastAPI). The endpoints
`/districts`, `/advisory` (`prev_harvest`, optional `flood_ready`), `/risk-calendar`,
`/post-flood` and `/field-twin` are built only from existing `src/compute`
functions (`rotation_options`, the `risk_calendar.csv` rows, `smap_days_to_normal`,
`flood_recession`, `earliest_sowing_date`, `load_weather` + `crop_season`). Each
response uses the contract envelope. Every number is a `{value, unit, src}`
measure that points to a provenance entry with a dataset and URL: IMERG, POWER,
SMAP, OPERA DSWx-S1, FAO-56, and each `data/reference/` source by id. Narration is
a template sentence checked by `guard()`, with a number-free fallback.
Coverage is always stated as "Checked: …" plus "Not all risks for this crop are
checked yet.", so `rotation_options`' "full"/"partial" label is never sent.
`/enso-lens`, `/warnings` and `/ask` serve the mock with `is_mock: true`. One
compute change: `post_flood._days_to_normal()` now stops at the first qualifying
7-day run instead of scanning to the end of the SMAP record. The answer is the same
(the Aug-2024 values for all 4 districts still match `docs/results/post_flood.md`)
and it runs in ~2 s instead of ~31 s. Weather and the demo floods are
warmed at startup (~13 s), so warm requests take 0.02-0.06 s, and a new flood date
takes ~2 s once. New contract tests are in `src/api/test_app.py` (81). Contract
differences are proposed in `docs/contract_change_proposals.md`; the shared
`docs/api_contract.md` and `web/mock/` were not edited. Bangla strings need
review by a Bangla speaker. Known limitation (compute, fixed in task 6c
below): `rotation_options()` put a June/July ready date at the end of the
previous Aug-Jul season, so every rabi window read as passed (e.g. Sylhet
flood of 2022-06-17). `python -m pytest src -q`: 273 passed, 5 skipped.

**Claude Code** — Task 6c, the season-rollover bug: a June/July earliest
sowing date (e.g. Sylhet flood 2022-06-17, ready 2022-07-07) reads as the
tail end of the previous Aug-Jul season, past every rabi window
(Nov-Jan), so `rotation_options()` reported `window_passed=True` for
every rabi crop with no sowing date. Reproduced with
`rc.rotation_options('sylhet', '2022-06-17', '2022-07-07')`.

Before the fix, every option (wheat, mustard, lentil, potato, boro_rice)
had `window_passed=True, sowing_date=None, rank=None`. After: if every
rabi crop's window (plus the 4-week grid tail) has already passed for the
season the earliest date falls in, `rotation_options()` now rolls over to
the next rabi season instead (new `season`/`next_season_used`/
`next_season_note` fields on the result); for the Sylhet example it picks
mustard (2022-10-15, rank 1), lentil (2022-10-24, rank 2), wheat
(2022-11-15, rank 3), potato (2022-11-01, rank 4), boro_rice
(2022-12-05, not ranked: no live hazard) — none `window_passed`.
Separately, if the earliest date falls inside or before the sourced
`aman_rice.sow.window_start/window_end` (data/reference/crop_calendar.csv,
BARC crop calendar, loaded via `reference.py`; that file was not edited),
an `aman_rice` option is added at the front of `options` — "Aman
transplanting still possible until 2022-07-30 (Crop Calendar - Aman Rice
...). Weather risk for aman is not assessed yet." — with the citation in
its `citations` field and no invented risk numbers (`rank: None`,
`hazards_checked: []`).

`app.py`: `split_options()` and the `/post-flood` cascade loop now skip
the `aman_rice` note (it is not one of app.py's `CROP_NAMES` crops and
carries no risk numbers, so it is not rendered through the per-crop
contract shape yet — a follow-up task, not this one). `crop_option()`'s
sowing-window display dates now use the `season` `rotation_options()`
actually used, so a rolled-over rabi season shows the correct year.
`ALL_WINDOWS_PASSED`'s text and trigger condition (`/advisory` and
`/post-flood`) were updated to fire only when every crop is truly
unavailable (every option's `sowing_date` is `None`), since a rolled-over
season almost always has *something* available now.

New pytest coverage in `src/compute/test_risk_calendar.py`: the Sylhet
2022 rollover case (every rabi crop gets a real sowing date, none
`window_passed`, ranking unchanged from the equivalent November case);
the aman_rice option's exact text and citation; a normal November harvest
date behaves exactly as before (no rollover, no aman option).
`python -m pytest src -q`: 276 passed, 5 skipped (3 new tests).

## Data sources
All NASA/scientific data used is from NASA POWER, NASA GPM IMERG (via
Giovanni), NASA SMAP (via AppEEARS), OPERA DSWx-S1 (via NASA CMR/earthaccess),
BMD station normals, and FAO-56 (Allen et al., 1998) reference values — see
README and inline docstrings in `src/compute/agroclimate.py` for exact
citations per number.
## Review log

- **External review by a second Claude session** of the agent-layer code
  (tools.json, tool_functions.py, backtest_rotation). It found: cite_check
  exposed as a model-callable tool (moved inside guard(), so the check
  can't be skipped); analog_year being ignored by backtest_rotation(); a
  missing result key on empty results; misspelled crops silently dropped;
  and empty 0-of-0 results for pre-2015 analog years. Each fix was checked
  before being applied, and one proposed fix was changed (it would have
  broken rotations that cross into the next year).
- **Real-data checks with Claude** then found further issues: inconsistent
  column names across district files, a stale Sylhet file, and the
  whole-year soil-moisture ranking penalizing every dry-season crop. That
  ranking was replaced with a seasonal percentile, validated against
  independent POWER rainfall (scripts/check_smap_years.py).

**Claude Code (task 10b)** — wrote `Dockerfile`, `.dockerignore`, and
`scripts/deploy_hf_space.py` (huggingface_hub upload script) to deploy
`app.py` to a Hugging Face Space. Running the deploy script surfaced that
HF now requires a PRO subscription to create a Docker-SDK Space even on
free `cpu-basic` hardware (`402 Payment Required` on a plain free
account) — per CLAUDE.md's free-tiers-only rule, this was reported instead
of proceeding on a paid tier. Asked, the user chose to redeploy on
Render's free tier instead. Wrote `render.yaml` (Docker runtime,
`plan: free`) reusing the same Dockerfile, and changed the Dockerfile's
`CMD` to read `$PORT` (falling back to 7860) so the same image works on
both Render (which assigns `PORT`) and a future HF Space. Neither the
Render MCP connector nor a `RENDER_API_KEY` was available in this
session, so the actual deploy (creating the service, polling the live
URL, timing the three test calls) is left for the user or a follow-up
session — see the "Hosted API" section of README.md. Verified locally
instead: `python -m pytest src -q` (276 passed, 5 skipped) and
`uvicorn app:app` serving `/api/v1/districts`, `/api/v1/advisory` and
`/api/v1/post-flood` with 200 responses.

**Claude Code (task 12)** — connected the prototype website (`web/app/`)
to the API. In `web/app/index.html` it removed the hard-coded sample data
(crop, plan, flood and source constants) and the in-browser ranking
(`scorePlan` etc.) and rewrote the data layer so every number shown comes
from `/advisory`, `/post-flood`, `/field-twin` or `/districts`, each with a
source link to the response's provenance. It kept the look, screens,
animations, EN/Bangla, voice and navigation; added the source-mode badge,
notices, errors in the chosen language, a "waking up the server" state with
retry, and localStorage "offline copies". API text is read aloud with the
phone's Bangla voice when there is no recorded clip. It wrote
`web/app/config.js`, removed the direct NASA GIBS map layer, and marked
water access, priorities and soil as "coming soon". On the backend it added
optional `lat`/`lon` (nearest district within 60 km), `problem_line`,
`aman_option`, the SMAP recovery curve (`post_flood.smap_recovery_curve()`,
with tests), flood images via `GET /api/v1/images/{name}`, per-week twin
events, and `GET /` → `/docs`; added a Render static site to `render.yaml`
and copied the images into the Docker image. Checked with
`python -m pytest src -q` and in a browser against a local uvicorn, the
mock files and the live API. Bangla strings were written with AI help and
need a review by a Bangla speaker.



**Claude Code (task 14, back button)** — navigation only, in
`web/app/index.html`: every screen change in `go()` is now a browser-history
entry (`history.pushState({screen, depth}, "", "#" + screen)`, not pushed
when the screen did not change); a "‹ Back" / "‹ ফিরে যান" button (44 px)
at the left of the header calls `history.back()` and is hidden on the start
screen and when there is nothing to go back to; the phone/browser back
button (`popstate`) closes any modal, stops speech and shows the saved
screen via `go(s, {fromHistory: true})`; opening the site with a hash
(`#flood`, also `#plans` → crop, `#farm` → prev) opens that screen as the
first entry (`history.replaceState`). On phones up to 460 px wide the
"Survey Crops" words are hidden while Back shows so the header fits. No
data, API calls or numbers changed. Checked in a browser against a local
uvicorn: start → field → farm → plans → twin, then Back ×4 with the button
and with the browser back button, a reload on `#plans`, and the header at
375 px in Bangla. The Bangla label needs a review by a Bangla speaker.

**Claude Code (task 13, source links)** — made every "Source" card open a
real page. In `app.py` the IMERG, POWER, SMAP and OPERA provenance entries
got an optional `view_url` ("See the satellite data"): NASA Worldview over
the 5 districts with GIBS layers it confirmed in the WMTS GetCapabilities
(`IMERG_Precipitation_Rate` 2024-08-21, `SMAP_L4_Analyzed_Root_Zone_Soil_Moisture`
2024-08-25, `OPERA_L3_Dynamic_Surface_Water_Extent-Sentinel-1` 2024-08-28),
and the POWER Data Access Viewer. SMAP's `url` now points to the NSIDC page
for SPL4SMGP version 8 (AppEEARS moved to the note). It added
`public_url()`, so research rows whose `source_url` is not a web address
are sent with `url: ""` and a "Link pending" note (no URLs were invented
and the CSVs were not edited). It updated `srcCard` in
`web/app/index.html` ("Dataset page ↗", "See the satellite data ↗", "Link
pending"), wrote `scripts/check_links.py` (writes `docs/results/link_check.md`
and `docs/results/broken_source_links.md`), and added tests in
`src/api/test_app.py`. Checked with `python -m pytest src -q`, the link
check, and in a browser against a local uvicorn. The Bangla link labels
need a review by a Bangla speaker.

**Claude Code (task 15, rename to Survey Crops)** — replaced "CropShift"
with "Survey Crops" (Bangla: "সার্ভে ক্রপস") in every user-visible string
in `web/app/index.html` and in the text `app.py` returns (error messages,
notices, method/basis text, provenance dataset names, `sms_en`/`sms_bn`,
the FastAPI title). Comments, file names, identifiers and URLs were not
changed, and neither were the shared `web/mock/*.json` files (they need
their own PR). The 6th bottom tab is now "My plan" / "আমার পরিকল্পনা" (the
icon and "Pictures only" switch are unchanged). Compared the recorded
Bangla sentences before and after with the page's own `__collectBn()`
helper: none of the 61 recorded sentences changed, so no clip falls back
to the phone voice. Added tests that no API response contains "CropShift"
and that the error text and API title use the new name. Checked with
`python -m pytest src -q` and in a browser (Bangla tab bar at 375 px).
The Bangla wording needs a review by a Bangla speaker.

**Claude Code (task 16, Survey Crops name + assumptions doc)** — the
registered team name is now "Survey Crops" (it was "Team Regolith"). Replaced
the team name in `app.py` (agency fields), `CLAUDE.md`, `CONTRIBUTING.md` and
`scripts/deploy_hf_space.py`; member names, URLs, the repo name, git history
and the shared `web/mock/*.json` were not touched (the mock needs its own
PR). Past entries in this file are history and keep the old name. Added a
`why` line to every entry of `ASSUMPTIONS` (`src/compute/risk_calendar.py`)
and `API_ASSUMPTIONS` (`app.py`), wrote `scripts/build_assumptions_doc.py`
which generates `docs/assumptions.md` from them, and added
`src/api/test_assumptions_doc.py`, which fails if the doc is stale. The
"assumptions" provenance entry in `app.py` now links to that doc. Checked
with `python -m pytest src -q`. The `why` sentences are the AI's wording of
the reasons; the team should check them.

**Claude Code (task 17, mock team name)** — changed one string in
`web/mock/advisory.json` ("Team Regolith analysis of NASA POWER" -> "Survey
Crops analysis of NASA POWER") to match the registered team name. Checked
with `python -m pytest src -q`.

**Claude Code (task 17 follow-up)** — resolved the `docs/AI_USE.md` merge
conflict with main (kept both entries). Made
`src/api/test_assumptions_doc.py` read `docs/assumptions.md` with universal
newlines, so the stale-doc check does not fail on Windows CRLF checkouts.

**Claude Code (water question)** — made "How much water can you give?" real.
`src/compute/risk_calendar.py` now counts waterings per season from the same
FAO-56 balance as `irrigation_mm` (new `irrigation_events_mean` and
`irrigation_events_worst20` in `data/processed/risk_calendar.csv`; for boro
rice, land preparation counts as one watering plus each pond top-up; new
`irrigation_event_count` assumption, `docs/assumptions.md` regenerated).
`app.py` `/api/v1/advisory` takes an optional `water` (`rain_only`, `limited`,
`regular`, `plenty`; absent = today's result), documented in
`docs/api_contract.md`. `web/app/index.html` sends `water` once the farmer
answers and drops the "coming soon" pill from that question only. The two
new calendar columns were merged into the committed CSV; a full rebuild
would also change mustard and boro rows because newer reference data exists,
so that refresh is left for its own PR. The `limited` cut-off (at most 2
waterings in the worst 20% of years) is the user's rule, not a sourced value.
Checked with `python -m pytest src -q` and by driving the page against a
local API.

## Task: "What matters most to you?" made real (`priority` on /advisory)

Claude (Sonnet 5.5) wrote `check_priority`, `legume_crops`, `apply_priority` and
`ranked_by_block` in `app.py`, the `data.ranked_by` block, the caution and
"research pending: legume source" notices, the narration sentence, the
`priority` section of `docs/api_contract.md`, the website change in
`web/app/index.html` (sends `priority` in tap order, shows the "Ranked by"
line, removes "coming soon" from that question only, and starts with no
priority selected instead of a pre-ticked placeholder), the `_TEMPLATE.csv`
example row for `<crop>.soil.legume`, and the tests in `src/api/test_app.py`.
The ranking keys are the user's rules; no new numbers or reference values
were invented. There are no `soil.legume` rows yet, so the soil priority does
nothing until the team adds a cited row. Checked with `python -m pytest src -q`
and by driving the page against a local API.


## Task: "Your soil" made real (`/api/v1/soil`, ISRIC SoilGrids)

Claude (Sonnet 5.5) wrote `src/acquire/soilgrids_point.py` (one SoilGrids call,
0-30 cm depth-weighting, USDA texture via the existing `usda_texture`, disk
cache), the `/api/v1/soil` endpoint, the `soilgrids` provenance entry and the
district fallback in `app.py`, `web/mock/soil.json`, the `docs/api_contract.md`
section, the Farm screen soil box in `web/app/index.html` (texture, pH, organic
carbon with Source buttons, field record shown next to the estimate, "coming
soon" removed from that question only), and `src/api/test_soil.py` (SoilGrids
mocked; no test uses the network). The ranking is unchanged: field soil is
shown only. The SoilGrids layer names, units (g/kg, pH x10, dg/kg) and the 5
requests/minute limit were checked against the live API, which answered on
2026-10-10. Checked with `python -m pytest src -q` and by driving the page
against a local API.
