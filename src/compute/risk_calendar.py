"""
src/compute/risk_calendar.py

Sowing-date risk calendar: for each district x crop x sowing date, in how
many of the 2001-2024 seasons did NASA data for the area around the field
show a problem (a sourced hazard threshold crossed at a sensitive stage),
plus water stress days and net irrigation mm from the FAO-56 balance.
This is a record of past seasons, not a forecast.

Weather: load_weather() (NASA GPM IMERG rain + NASA POWER temperature etc.)
with FAO-56 ET0 from water_balance.weather_table(). Water: water_balance.
crop_season() for upland crops; a ponded-paddy bucket for boro rice.
Researched values: data/reference/ via reference.load_reference() (strict:
PLACEHOLDER rows, non-http sources, malformed items and unparseable values
are skipped and reported). A hazard is evaluated ONLY where a kept row
gives its threshold and the stage it applies to; everything else is listed
as a data gap, never filled with a guessed value.

Season Y = 1 August Y to 31 July Y+1 (so a January boro transplanting
belongs to the season that started the previous August).

Stages (days after sowing):
  - Cited days for a stage come from <crop>.stage.days_to_<stage>
    (+ _low/_high). Several sources: the mean of each source's midpoint.
  - With a cited base temperature (<crop>.stage.base_temp) the stage is
    timed by growing degree days, GDD = sum(max(0, Tmean - Tbase)). The GDD
    target is calibrated so that a crop sown at the middle of the cited
    sowing window, in the district's 2001-2024 mean daily climate, reaches
    the stage in exactly the cited days. In a real season the stage comes
    when that season's GDD sum reaches the target (earlier when warm, later
    when cool). Without a base temperature the cited days are used as-is.

Modelling choices that are NOT from a source are in ASSUMPTIONS, labelled
"CropShift assumption", and the key ones get a sensitivity run.
"""

import calendar as _calendar
import functools
import math
import os

import numpy as np
import pandas as pd

from reference import citation, load_reference
from water_balance import (crop_season, kc_daily, load_crop_params, load_soil_params,
                           weather_table)
from weather import load_weather

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROCESSED_DIR = os.path.join(REPO_ROOT, "data", "processed")
KC_TABLE_PATH = os.path.join(PROCESSED_DIR, "kc_table.csv")
DISTRICT_METADATA_PATH = os.path.join(PROCESSED_DIR, "district_metadata.csv")
RISK_CALENDAR_PATH = os.path.join(PROCESSED_DIR, "risk_calendar.csv")

REFERENCE_FILES = ("crop_calendar.csv", "crop_thresholds.csv", "crop_params.csv",
                   "paddy_params.csv", "soil_params.csv", "crop_area_by_district.csv")
CROPS = ("wheat", "mustard", "lentil", "potato", "boro_rice")
SEASONS = tuple(range(2001, 2025))
SEASON_START_MONTH = 8
REFERENCE_SEASON = 2001      # non-leap season, used only to lay out MM-DD dates in order
HORIZON_DAYS = 240           # weather needed after sowing to time every stage
GRID_BEFORE_DAYS = 14        # calendar starts 2 weeks before window_start (task spec)
GRID_AFTER_DAYS = 28         # ...and ends 4 weeks after window_end (task spec)
WORST_FRACTION = 0.2         # "worst 20% of years" (CLAUDE.md ranking rule)

# ---------------- CropShift assumptions (not from a source) ----------------

ASSUMPTIONS = {
    "flowering_window_days": {
        "value": 7, "sensitivity": [15],
        "text": "Heat at flowering/anthesis is counted over a window of this many days "
                "centred on the flowering date.",
        "why": "Flowering is not one day; a week around the flowering date is "
                "a plain, short window. A longer 15-day window is run as a "
                "check."},
    "hot_days": {
        "value": 3, "sensitivity": [1, 5],
        "text": "A season is a 'problem year' for a heat/cold hazard when at least this many "
                "days in the sensitive stage cross the cited threshold.",
        "why": "One hot day rarely ruins a crop, so a few days are required. "
                "Fewer (1) and more (5) days are run as a check."},
    "threshold_end": {
        "value": "onset", "sensitivity": ["severe"],
        "text": "When a threshold is cited as a range (e.g. mustard 25-29 C, potato night "
                "20-25 C) the onset end is used; the severe end is a sensitivity run.",
        "why": "The onset end of a cited range is the earlier, more careful "
                "warning point. The severe end is run as a check."},
    "waterlog_window_days": {
        "value": 30, "sensitivity": [],
        "text": "Waterlogging is checked in the first this-many days after sowing "
                "(from the task spec, not a source).",
        "why": "Young seedlings are the most exposed to standing water, so "
                "only the first month after sowing is checked. No sensitivity "
                "run exists."},
    "saturated_day": {
        "value": "Dr = 0 and deep percolation > 0", "sensitivity": [],
        "text": "A 'saturated' day in the FAO-56 balance: the root zone is at field capacity "
                "and surplus water is draining that day (the balance has no runoff term, so "
                "this includes water that would pond or run off).",
        "why": "It is the simplest daily test that follows from the water "
                "balance already used, and it needs no extra data."},
    "turnaround_days": {
        "value": 7, "sensitivity": [],
        "text": "Days between harvesting the previous crop and sowing the next one.",
        "why": "A short gap for harvesting, threshing and preparing land. No "
                "sensitivity run exists."},
    "combine_sources": {
        "value": "mean of per-source midpoints", "sensitivity": [],
        "text": "When several kept rows cite a stage duration or threshold, each source's "
                "midpoint is taken and the mean of those is used; the full range is shown.",
        "why": "Taking one midpoint per source stops a source with many rows "
                "from outweighing the others; the full range stays visible."},
    "gdd_no_upper_cap": {
        "value": "none", "sensitivity": [],
        "text": "GDD uses no upper temperature cap (none is cited).",
        "why": "Adding a cap would need a number that no kept source gives, so "
                "none is used."},
    "potato_main_season_window": {
        "value": "11-01 to 11-30", "sensitivity": [],
        "text": "Potato has three cited BARC windows. The North (Nov 1-7) and South "
                "(Nov 17-30) main-season windows are merged; the early-variety window "
                "(Sep 24-Oct 7) is excluded because its notes say it is a separate sub-type.",
        "why": "The two cited main-season windows are kept; the early-variety "
                "window is a different sub-type, so it is not mixed in."},
    "boro_transplant_window_start": {
        "value": "earliest seedbed_start + shortest seedling_age", "sensitivity": [],
        "text": "No kept row gives a boro transplanting window start, so it is derived from "
                "two cited values: the earliest seedbed sowing date plus the shortest "
                "seedling age. Boro 'sowing date' in this calendar means TRANSPLANTING date.",
        "why": "It uses only two cited numbers and invents none; it is a "
                "derived start, not a measured one."},
    "night_temperature": {
        "value": "POWER T2M_MIN", "sensitivity": [],
        "text": "The daily minimum 2 m temperature stands in for night temperature.",
        "why": "The daily minimum is the coolest reading of the day, which is "
                "the closest NASA POWER variable to night temperature."},
    "water_season_length": {
        "value": "kc_table stage lengths", "sensitivity": [],
        "text": "The water balance runs over the FAO-56 stage lengths in kc_table.csv "
                "(e.g. wheat 120 d), which differ from the cited maturity days.",
        "why": "The FAO-56 stage lengths are the ones the crop coefficients "
                "(Kc) were published for, so they stay consistent with each "
                "other."},
    "paddy_refill": {
        "value": "refill to ponding depth when the water layer is used up", "sensitivity": [],
        "text": "Boro paddy: after land preparation the field holds the cited ponding depth; "
                "each day rain adds and ETc + percolation remove water; water above the "
                "ponding depth spills; when the layer would go below zero it is refilled to "
                "the ponding depth (counted as irrigation). Season = kc_table rice stage "
                "lengths (150 d), from transplanting.",
        "why": "It keeps the paddy water layer at the cited ponding depth and "
                "counts every top-up as irrigation, which is how a farmer "
                "manages a boro field."},
    "irrigation_event_count": {
        "value": "one watering = one day the FAO-56 balance refills the root zone; "
                 "paddy: land preparation + each pond top-up", "sensitivity": [],
        "text": "Waterings per season are counted from the same FAO-56 balance that gives "
                "irrigation_mm: each day it adds irrigation counts as one watering. For "
                "boro paddy the land-preparation flooding counts as one watering and every "
                "later pond top-up as one more.",
        "why": "It turns the irrigation depth already computed into a number a farmer can "
                "compare with how many times they can bring water. Pump capacity is not "
                "modelled, so it is a count, not a volume per watering."},
    "paddy_percolation_class": {
        "value": "clay if clay_pct >= 40", "sensitivity": [],
        "text": "Percolation uses the cited clay rate when SoilGrids clay >= 40% (USDA clay "
                "class lower bound), otherwise the cited loam rate.",
        "why": "The 40% clay line is the lower bound of the USDA clay class, "
                "so the cited clay and loam rates are applied by a published "
                "rule."},
}
DEFAULT_PARAMS = {"flowering_window_days": ASSUMPTIONS["flowering_window_days"]["value"],
                  "hot_days": ASSUMPTIONS["hot_days"]["value"],
                  "threshold_end": ASSUMPTIONS["threshold_end"]["value"],
                  "waterlog_window_days": ASSUMPTIONS["waterlog_window_days"]["value"]}
TURNAROUND_DAYS = ASSUMPTIONS["turnaround_days"]["value"]
EXCLUDED_WINDOW_STARTS = {"potato": [(9, 24)]}   # see potato_main_season_window

# ---------------- hazards requested (evaluated only if sourced) ----------------

STAGE_ITEMS = {"flowering": "days_to_flowering", "maturity": "days_to_maturity",
               "panicle_initiation": "days_to_panicle_initiation",
               "tuber": "days_to_tuber_initiation"}
# Stage spans a hazard is checked over, and the stage timings each one needs.
STAGE_NEEDS = {"flowering": ["flowering"], "grain_fill": ["flowering", "maturity"],
               "tuber": ["tuber_start", "tuber_end"],
               "booting": ["panicle_initiation", "flowering"], "first_days": []}
HAZARDS = {
    "wheat": [
        {"hazard": "heat_anthesis", "kind": "temp", "variable": "temp_max_c", "op": ">",
         "item": "wheat.heat.anthesis_tmax", "stage": "flowering"},
        {"hazard": "heat_grainfill", "kind": "temp", "variable": "temp_max_c", "op": ">",
         "item": "wheat.heat.grainfill_tmax", "stage": "grain_fill"}],
    "mustard": [
        {"hazard": "heat_flowering", "kind": "temp", "variable": "temp_max_c", "op": ">",
         "item": "mustard.heat.flowering_tmax", "stage": "flowering"},
        {"hazard": "waterlog", "kind": "waterlog", "item": "mustard.wet.waterlog_days",
         "stage": "first_days"}],
    "lentil": [
        {"hazard": "heat_flowering", "kind": "temp", "variable": "temp_max_c", "op": ">",
         "item": "lentil.heat.flowering_tmax", "stage": "flowering"},
        {"hazard": "waterlog", "kind": "waterlog", "item": "lentil.wet.waterlog_days",
         "stage": "first_days"}],
    "potato": [
        {"hazard": "night_heat_tuber", "kind": "temp", "variable": "temp_min_c", "op": ">",
         "item": "potato.heat.tuber_tmin_night", "stage": "tuber"}],
    "boro_rice": [
        {"hazard": "heat_anthesis", "kind": "temp", "variable": "temp_max_c", "op": ">",
         "item": "boro_rice.heat.anthesis_tmax", "stage": "flowering"},
        {"hazard": "cold_booting", "kind": "temp", "variable": "temp_min_c", "op": "<",
         "item": "boro_rice.cold.booting_tmin", "stage": "booting"}],
}
WATER_MODEL = {"wheat": "fao56", "mustard": "fao56", "lentil": "fao56", "potato": "fao56",
               "boro_rice": "paddy"}


# ---------------- season dates ----------------

def season_of(date):
    """Season a date belongs to (season Y runs 1 Aug Y - 31 Jul Y+1)."""
    date = pd.Timestamp(date)
    return date.year if date.month >= SEASON_START_MONTH else date.year - 1


def season_date(season, month, day):
    """The date with this month/day inside season `season` (Feb 29 -> Feb 28
    in non-leap years)."""
    year = season if month >= SEASON_START_MONTH else season + 1
    if month == 2 and day == 29 and not _calendar.isleap(year):
        day = 28
    return pd.Timestamp(year=year, month=month, day=day)


def season_offset(month, day):
    """Days from 1 August to month/day in the (non-leap) reference season."""
    if month == 2 and day == 29:
        day = 28
    return int((season_date(REFERENCE_SEASON, month, day)
                - season_date(REFERENCE_SEASON, SEASON_START_MONTH, 1)).days)


def mmdd_at_offset(offset):
    """(month, day) that is `offset` days after 1 August (reference season)."""
    date = (season_date(REFERENCE_SEASON, SEASON_START_MONTH, 1)
            + pd.Timedelta(days=int(offset) % 365))
    return date.month, date.day


def mmdd(month_day):
    return f"{month_day[0]:02d}-{month_day[1]:02d}"


def sowing_grid(window_start, window_end, before=GRID_BEFORE_DAYS, after=GRID_AFTER_DAYS,
                step=7):
    """Weekly (month, day) sowing dates from `before` days ahead of the
    window start to `after` days past the window end."""
    first = season_offset(*window_start) - before
    last = season_offset(*window_end) + after
    return [mmdd_at_offset(o) for o in range(first, last + 1, step)]


# ---------------- reference values -> crop specification ----------------

def skipped_note(ref, item):
    """'N row(s) skipped (reason xk)' for skipped rows of item/_low/_high."""
    names = {item, item + "_low", item + "_high"}
    rows = ref.skipped[ref.skipped["item"].isin(names)]
    if rows.empty:
        return ""
    counts = rows["reason"].value_counts()
    return (f"; {len(rows)} row(s) skipped in data/reference ("
            + ", ".join(f"{r} x{n}" for r, n in counts.items()) + ")")


def _by_source(rows):
    """Groups kept rows by (source_title, source_url) -> list of (low, high)."""
    groups = {}
    for _, row in rows.iterrows():
        groups.setdefault((row["source_title"], row["source_url"]), []).append(
            (float(row["low"]), float(row["high"])))
    return groups


def resolve_range(ref, item):
    """A cited number/range for item, combining item, item_low and item_high
    rows. Returns None if no kept row, else {"value" (mean of per-source
    midpoints), "low", "high", "n_sources", "citations"}."""
    exact, low_rows, high_rows = ref.rows(item), ref.rows(item + "_low"), ref.rows(item + "_high")
    if exact.empty and low_rows.empty and high_rows.empty:
        return None
    sources = _by_source(exact)
    for rows, which in ((low_rows, "low"), (high_rows, "high")):
        for _, row in rows.iterrows():
            v = float(row["value"])
            sources.setdefault((row["source_title"], row["source_url"]), []).append((v, v))
    midpoints, lows, highs = [], [], []
    for pairs in sources.values():
        lo, hi = min(p[0] for p in pairs), max(p[1] for p in pairs)
        midpoints.append((lo + hi) / 2)
        lows.append(lo)
        highs.append(hi)
    used = pd.concat([exact, low_rows, high_rows])
    return {"value": float(np.mean(midpoints)), "low": float(min(lows)),
            "high": float(max(highs)), "n_sources": len(sources),
            "citations": [dict(citation(r), item=r["item"]) for _, r in used.iterrows()]}


def resolve_window(ref, crop):
    """Cited sowing window {"start": (m, d), "end": (m, d), "method", "citations"}
    or None. Start/end rows are paired per source in file order; several
    windows are merged into earliest start .. latest end; excluded windows
    (EXCLUDED_WINDOW_STARTS) are dropped. If no start is cited but seedbed
    dates and seedling age are (boro), start = earliest seedbed_start +
    shortest seedling age (method "derived")."""
    starts, ends = ref.rows(f"{crop}.sow.window_start"), ref.rows(f"{crop}.sow.window_end")
    if ends.empty:
        return None
    excluded = EXCLUDED_WINDOW_STARTS.get(crop, [])
    pairs, used = [], []
    for key, s_rows in starts.groupby(["source_title", "source_url"], sort=False):
        e_rows = ends[(ends["source_title"] == key[0]) & (ends["source_url"] == key[1])]
        for (_, s), (_, e) in zip(s_rows.iterrows(), e_rows.iterrows()):
            start = (int(s["month"]), int(s["day"]))
            if start in excluded:
                continue
            pairs.append((start, (int(e["month"]), int(e["day"]))))
            used.extend([s, e])
    if pairs:
        start = min((p[0] for p in pairs), key=lambda md: season_offset(*md))
        end = max((p[1] for p in pairs), key=lambda md: season_offset(*md))
        method = "cited" if len(pairs) == 1 or len(set(pairs)) == 1 else "cited, merged"
        return {"start": start, "end": end, "method": method,
                "citations": [dict(citation(r), item=r["item"]) for r in used]}

    seedbed = ref.rows(f"{crop}.sow.seedbed_start")
    age = ref.rows(f"{crop}.stage.seedling_age")
    if seedbed.empty or age.empty:
        return None
    first = seedbed.loc[seedbed.apply(lambda r: season_offset(int(r["month"]), int(r["day"])),
                                      axis=1).idxmin()]
    youngest = age.loc[age["low"].astype(float).idxmin()]
    start_offset = season_offset(int(first["month"]), int(first["day"])) + int(youngest["low"])
    end_row = ends.iloc[0]
    end = max(((int(r["month"]), int(r["day"])) for _, r in ends.iterrows()),
              key=lambda md: season_offset(*md))
    return {"start": mmdd_at_offset(start_offset), "end": end, "method": "derived",
            "citations": [dict(citation(first), item=first["item"]),
                          dict(citation(youngest), item=youngest["item"]),
                          dict(citation(end_row), item=end_row["item"])]}


def build_crop_spec(ref, crop):
    """Everything the calendar needs for one crop, only from kept rows:
    window, base_temp, stage_days, stage_method, live hazards (with their
    threshold) and missing hazards (with the reason)."""
    spec = {"crop": crop, "window": resolve_window(ref, crop),
            "base_temp": resolve_range(ref, f"{crop}.stage.base_temp"),
            "stage_days": {}, "hazards": [], "missing": [], "water_model": WATER_MODEL[crop]}
    for key, suffix in STAGE_ITEMS.items():
        cited = resolve_range(ref, f"{crop}.stage.{suffix}")
        if cited is None:
            continue
        if key == "tuber":   # cited as the tuber-formation window "35-55 days"
            spec["stage_days"]["tuber_start"] = {**cited, "value": cited["low"]}
            spec["stage_days"]["tuber_end"] = {**cited, "value": cited["high"]}
        else:
            spec["stage_days"][key] = cited
    if not spec["stage_days"]:
        spec["stage_method"] = "none"
    elif spec["base_temp"] is not None and spec["window"] is not None:
        spec["stage_method"] = "gdd"
    else:
        spec["stage_method"] = "cited_days"

    if spec["window"] is None:
        spec["missing"].append({"crop": crop, "hazard": "(all)",
                                "reason": f"no sourced sowing window ({crop}.sow.window_start/"
                                          f"window_end){skipped_note(ref, f'{crop}.sow.window_start')}"})
    for hazard in HAZARDS[crop]:
        threshold = resolve_range(ref, hazard["item"])
        lacking = [s for s in STAGE_NEEDS[hazard["stage"]] if s not in spec["stage_days"]]
        if threshold is None:
            reason = f"no sourced threshold ({hazard['item']}){skipped_note(ref, hazard['item'])}"
        elif lacking:
            items = sorted({f"{crop}.stage.{STAGE_ITEMS.get(s.split('_')[0] if s.startswith('tuber') else s)}"
                            for s in lacking})
            reason = ("no sourced stage timing (" + ", ".join(items) + ")"
                      + "".join(skipped_note(ref, i) for i in items))
        else:
            spec["hazards"].append({**hazard, "threshold": threshold})
            continue
        spec["missing"].append({"crop": crop, "hazard": hazard["hazard"], "reason": reason})
    return spec


def build_crop_specs(ref, crops=CROPS):
    return {crop: build_crop_spec(ref, crop) for crop in crops}


HAZARD_LABELS = {
    "heat_anthesis": "heat at flowering",
    "heat_grainfill": "heat during grain fill",
    "heat_flowering": "heat at flowering",
    "waterlog": "waterlogging",
    "night_heat_tuber": "night heat",
    "cold_booting": "cold at booting",
}


def hazard_label(hazard):
    return HAZARD_LABELS.get(hazard, hazard.replace("_", " "))


def hazards_checked_text(hazards_checked):
    """'X only' / 'X, Y' for the plain-language problem line."""
    labels = [hazard_label(h) for h in hazards_checked]
    return f"{labels[0]} only" if len(labels) == 1 else ", ".join(labels)


def problem_line(sowing_date, problem_years, n_years, hazards_checked):
    """Plain-language line for a ranked crop that always says which hazards
    were checked, e.g. 'problems in 0 of 24 years (checked: night heat
    only)' (CLAUDE.md: never imply safety beyond what was checked)."""
    if sowing_date is None:
        return "no date: more than 4 weeks past the window"
    if not hazards_checked:
        return "not assessed (no sourced hazard)"
    return (f"problems in {problem_years} of {n_years} years "
            f"(checked: {hazards_checked_text(hazards_checked)})")


def threshold_value(hazard, end):
    """The threshold to use: 'onset' = the milder end of a cited range (low
    for heat and waterlogging, high for cold), 'severe' = the other end."""
    t = hazard["threshold"]
    cold = hazard.get("op") == "<"
    if end == "onset":
        return t["high"] if cold else t["low"]
    return t["low"] if cold else t["high"]


# ---------------- growing degree days ----------------

def mean_climate(tmean, seasons=SEASONS):
    """Mean daily temperature for each of the 365 days of a season (index =
    days after 1 August), averaged over `seasons`. Feb 29 is dropped."""
    start = season_date(seasons[0], SEASON_START_MONTH, 1)
    end = season_date(seasons[-1], SEASON_START_MONTH - 1, 31)
    t = tmean.loc[start:end]
    by_day = t.groupby([t.index.month, t.index.day]).mean()
    return np.array([by_day.loc[mmdd_at_offset(o)] for o in range(365)], dtype=float)


def gdd_target(clim, start_offset, days, base_temp):
    """GDD accumulated in the mean climate over `days` days from start_offset."""
    idx = (int(start_offset) + np.arange(int(round(days)))) % len(clim)
    return float(np.maximum(clim[idx] - base_temp, 0.0).sum())


def days_to_target(tmean_from_sowing, base_temp, target):
    """Number of days after sowing until cumulative GDD reaches target (the
    stage falls on sowing date + that many days), or None if never."""
    cum = np.cumsum(np.maximum(np.asarray(tmean_from_sowing, dtype=float) - base_temp, 0.0))
    hits = np.nonzero(cum >= target - 1e-9)[0]
    return int(hits[0] + 1) if len(hits) else None


def calibrate(spec, clim):
    """{stage: GDD target} so that sowing at the middle of the cited window in
    the mean climate reaches each stage in its cited days. {} unless the
    crop's stage_method is 'gdd'."""
    if spec["stage_method"] != "gdd":
        return {}
    w = spec["window"]
    middle = (season_offset(*w["start"]) + season_offset(*w["end"])) // 2
    tbase = spec["base_temp"]["value"]
    return {stage: gdd_target(clim, middle, cited["value"], tbase)
            for stage, cited in spec["stage_days"].items()}


def season_stages(spec, targets, tmean_from_sowing):
    """{stage: days after sowing} for one season (None where not reached)."""
    if spec["stage_method"] == "gdd":
        tbase = spec["base_temp"]["value"]
        return {s: days_to_target(tmean_from_sowing, tbase, t) for s, t in targets.items()}
    return {s: int(round(c["value"])) for s, c in spec["stage_days"].items()}


def stage_span(stage, stages, params):
    """(first_day, last_day), inclusive, in days after sowing, or None."""
    if stage == "first_days":
        return 0, params["waterlog_window_days"] - 1
    if any(stages.get(s) is None for s in STAGE_NEEDS[stage]):
        return None
    if stage == "flowering":
        width = params["flowering_window_days"]
        first = stages["flowering"] - width // 2
        return first, first + width - 1
    if stage == "grain_fill":
        return stage_span("flowering", stages, params)[1] + 1, stages["maturity"]
    if stage == "tuber":
        return stages["tuber_start"], stages["tuber_end"]
    if stage == "booting":
        return stages["panicle_initiation"], stages["flowering"] - 1
    raise ValueError(f"unknown stage {stage!r}")


# ---------------- hazards and water ----------------

def count_days(values, threshold, op):
    values = np.asarray(values, dtype=float)
    return int(np.sum(values > threshold) if op == ">" else np.sum(values < threshold))


def max_run(flags):
    """Longest run of consecutive True values."""
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def saturated_days(daily):
    """Boolean per day of an FAO-56 rainfed run: root zone at field capacity
    with surplus draining (the 'saturated_day' assumption)."""
    return (daily["depletion_mm"] <= 1e-9) & (daily["deep_percolation_mm"] > 0)


def paddy_water_need(rain_mm, et0_mm, kc, land_prep_mm, percolation_mm_day, ponding_depth_mm):
    """Ponded-paddy water need (see the 'paddy_refill' assumption).
    Returns land_prep_mm, season_irrigation_mm, irrigation_events (land
    preparation + pond top-ups), total_mm, etc_mm,
    percolation_mm, rain_mm, spill_mm."""
    rain = np.asarray(rain_mm, dtype=float)
    etc = np.asarray(kc, dtype=float) * np.asarray(et0_mm, dtype=float)
    if not len(rain) == len(etc):
        raise ValueError("rain_mm, et0_mm and kc must have the same length")
    pond, irrigation, spill, events = float(ponding_depth_mm), 0.0, 0.0, 0
    for r, e in zip(rain, etc):
        pond += r - e - percolation_mm_day
        if pond > ponding_depth_mm:
            spill += pond - ponding_depth_mm
            pond = float(ponding_depth_mm)
        if pond < 0:
            irrigation += ponding_depth_mm - pond
            pond = float(ponding_depth_mm)
            events += 1
    return {"land_prep_mm": float(land_prep_mm), "season_irrigation_mm": irrigation,
            "irrigation_events": events + (1 if land_prep_mm > 0 else 0),
            "total_mm": float(land_prep_mm) + irrigation, "etc_mm": float(etc.sum()),
            "percolation_mm": float(percolation_mm_day) * len(rain),
            "rain_mm": float(rain.sum()), "spill_mm": spill}


def resolve_paddy(ref):
    """Cited paddy parameters or None if any is missing."""
    names = ("land_prep_water", "percolation_clay", "percolation_loam", "ponding_depth")
    out = {}
    for name in names:
        rows = ref.rows(f"paddy.{name}")
        if rows.empty:
            return None
        out[name] = {"value": float(rows["value"].astype(float).mean()),
                     "citations": [dict(citation(r), item=r["item"]) for _, r in rows.iterrows()]}
    return out


def paddy_percolation(soil, paddy):
    """(mm/day, class) for a district's soil (see paddy_percolation_class)."""
    if soil.get("clay_pct", 0.0) >= 40.0:
        return paddy["percolation_clay"]["value"], "clay"
    return paddy["percolation_loam"]["value"], "loam"


def worst_mean(values, fraction=WORST_FRACTION, highest_is_worst=True):
    """Mean of the worst `fraction` of values (at least one value)."""
    v = np.sort(np.asarray([x for x in values if x is not None and not pd.isna(x)], dtype=float))
    if len(v) == 0:
        return float("nan")
    k = max(1, math.ceil(fraction * len(v)))
    return float((v[-k:] if highest_is_worst else v[:k]).mean())


# ---------------- one season, one sowing date ----------------

def evaluate_season(spec, targets, weather, sow, params=DEFAULT_PARAMS):
    """Stage days and per-hazard day counts for one sowing date in one season.
    weather: daily DataFrame (temp_mean_c, temp_max_c, temp_min_c) with a
    DatetimeIndex. Returns None if the weather does not cover HORIZON_DAYS
    after sowing or a needed stage is never reached."""
    sow = pd.Timestamp(sow)
    days = weather.loc[sow: sow + pd.Timedelta(days=HORIZON_DAYS - 1)]
    if len(days) < HORIZON_DAYS:
        return None
    stages = season_stages(spec, targets, days["temp_mean_c"].to_numpy())
    row = {"sowing_date": sow, "flowering_days": stages.get("flowering"),
           "maturity_days": stages.get("maturity")}
    for hazard in spec["hazards"]:
        if hazard["kind"] != "temp":
            continue
        span = stage_span(hazard["stage"], stages, params)
        if span is None:
            return None
        values = days[hazard["variable"]].to_numpy()[span[0]: span[1] + 1]
        for end in ("onset", "severe"):
            row[f"{hazard['hazard']}_days_{end}"] = count_days(
                values, threshold_value(hazard, end), hazard["op"])
    return row


def summarise(rows, spec, params=DEFAULT_PARAMS, water=True):
    """One calendar row from the per-season rows of one sowing date."""
    rows = pd.DataFrame(rows)
    problems, assessed = {}, []
    for hazard in spec["hazards"]:
        name = hazard["hazard"]
        if hazard["kind"] == "temp":
            problems[name] = rows[f"{name}_days_{params['threshold_end']}"] >= params["hot_days"]
        elif water and "waterlog_run" in rows:
            problems[name] = rows["waterlog_run"] >= threshold_value(hazard, params["threshold_end"])
        else:
            continue
        assessed.append(name)
    out = {"n_years": int(len(rows)), "hazards_assessed": ";".join(assessed)}
    if assessed:
        any_problem = pd.DataFrame(problems).any(axis=1)
        out["problem_years"] = int(any_problem.sum())
        out["problem_share"] = round(float(any_problem.mean()), 3) if len(rows) else float("nan")
    else:
        out["problem_years"], out["problem_share"] = None, float("nan")
    for name, flags in problems.items():
        out[f"problem_years_{name}"] = int(flags.sum())
    for col, key in (("net_irrigation_mm", "irrigation_mm"), ("water_stress_days", "stress_days"),
                     ("irrigation_events", "irrigation_events")):
        if col in rows and rows[col].notna().any():
            out[f"{key}_mean"] = round(float(rows[col].mean()), 1)
            out[f"{key}_worst20"] = round(worst_mean(rows[col]), 1)
        else:
            out[f"{key}_mean"] = out[f"{key}_worst20"] = float("nan")
    for col in ("flowering_days", "maturity_days"):
        values = rows[col].dropna() if col in rows else pd.Series(dtype=float)
        out[f"{col}_median"] = float(values.median()) if len(values) else float("nan")
    return out


# ---------------- whole calendar ----------------

def prepare_weather(district, metadata=None):
    """IMERG rain + POWER temperature/RH/wind/solar (load_weather) with
    FAO-56 ET0 added."""
    if metadata is None:
        metadata = pd.read_csv(DISTRICT_METADATA_PATH).set_index("district")
    raw = load_weather(district)
    meta = metadata.loc[district]
    water = weather_table(raw, float(meta["latitude"]), float(meta["elevation_m"]))
    return raw[["temp_mean_c", "temp_max_c", "temp_min_c"]].join(water)


def crop_calendar(district, spec, weather, seasons=SEASONS, params=DEFAULT_PARAMS,
                  water=None, sow_dates=None, sensitivity=True):
    """Calendar rows for one district and crop.

    weather: prepare_weather() output. water: None (temperature hazards
    only; fast) or a dict with kc_table, crop_params, soil and paddy for
    the water balance, waterlogging and paddy need. sow_dates: list of
    (month, day); default is the weekly sowing_grid() of the cited window.
    """
    window = spec["window"]
    if window is None:
        return []
    if sow_dates is None:
        sow_dates = sowing_grid(window["start"], window["end"])
    targets = calibrate(spec, mean_climate(weather["temp_mean_c"], seasons))
    start_off, end_off = season_offset(*window["start"]), season_offset(*window["end"])
    widths = ASSUMPTIONS["flowering_window_days"]["sensitivity"] if sensitivity else []
    out = []
    for md in sow_dates:
        per_season, per_width = [], {w: [] for w in widths}
        for season in seasons:
            sow = season_date(season, *md)
            row = evaluate_season(spec, targets, weather, sow, params)
            if row is None:
                continue
            row["season"] = season
            if water is not None and not _add_water(row, spec, weather, sow, water, params):
                continue
            per_season.append(row)
            for w in widths:   # only the temperature counts change with the window width
                alt = evaluate_season(spec, targets, weather, sow,
                                      {**params, "flowering_window_days": w})
                per_width[w].append({**row, **alt} if alt is not None else row)
        has_water = water is not None
        summary = summarise(per_season, spec, params, water=has_water)
        if sensitivity:
            for hot in ASSUMPTIONS["hot_days"]["sensitivity"]:
                s = summarise(per_season, spec, {**params, "hot_days": hot}, has_water)
                summary[f"problem_years_hot{hot}"] = s["problem_years"]
            s = summarise(per_season, spec, {**params, "threshold_end": "severe"}, has_water)
            summary["problem_years_severe"] = s["problem_years"]
            for w in widths:
                s = summarise(per_width[w], spec, {**params, "flowering_window_days": w},
                              has_water)
                summary[f"problem_years_window{w}"] = s["problem_years"]
        offset = season_offset(*md)
        out.append({"district": district, "crop": spec["crop"], "sowing_mmdd": mmdd(md),
                    "sow_offset": offset, "window_start": mmdd(window["start"]),
                    "window_end": mmdd(window["end"]), "window_method": window["method"],
                    "outside_window": bool(offset < start_off or offset > end_off),
                    "stage_method": spec["stage_method"],
                    "hazards_missing": ";".join(m["hazard"] for m in spec["missing"]),
                    **summary})
    return out


def _add_water(row, spec, weather, sow, water, params):
    """Adds water columns to a season row. False if weather is too short."""
    if spec["water_model"] == "paddy":
        if water.get("paddy") is None:
            return True
        kc = kc_daily(water["kc_table"], "rice")
        w = weather.loc[sow: sow + pd.Timedelta(days=len(kc) - 1)]
        if len(w) < len(kc):
            return False
        perc, _ = paddy_percolation(water["soil"], water["paddy"])
        need = paddy_water_need(w["rainfall_mm"], w["et0_mm"], kc,
                                water["paddy"]["land_prep_water"]["value"], perc,
                                water["paddy"]["ponding_depth"]["value"])
        row["net_irrigation_mm"] = need["total_mm"]
        row["water_stress_days"] = float("nan")
        row["irrigation_events"] = need["irrigation_events"]
        return True
    try:
        result = crop_season(spec["crop"], sow, weather, water["kc_table"],
                             water["crop_params"], water["soil"])
    except ValueError as err:
        if "does not cover" in str(err):
            return False
        raise
    row["net_irrigation_mm"] = result["net_irrigation_mm"]
    row["water_stress_days"] = result["water_stress_days"]
    row["irrigation_events"] = result["irrigation_events"]
    first = saturated_days(result["daily"]).to_numpy()[:params["waterlog_window_days"]]
    row["waterlog_run"] = max_run(first)
    return True


def build_calendar(districts, specs, ref, seasons=SEASONS, params=DEFAULT_PARAMS,
                   with_water=True, weather_by_district=None):
    """The full calendar DataFrame for every district and crop."""
    kc_table = pd.read_csv(KC_TABLE_PATH)
    crop_params, soils = load_crop_params(), load_soil_params()
    paddy = resolve_paddy(ref)
    rows = []
    for district in districts:
        weather = (weather_by_district or {}).get(district)
        if weather is None:
            weather = prepare_weather(district)
        water = ({"kc_table": kc_table, "crop_params": crop_params, "soil": soils[district],
                  "paddy": paddy} if with_water else None)
        for spec in specs.values():
            rows.extend(crop_calendar(district, spec, weather, seasons, params, water))
    return pd.DataFrame(rows)


# ---------------- rotation ----------------

def _rank_key(option):
    share = option["problem_share"]
    irr = option["irrigation_mm_worst20"]
    return (option["hazards_assessed"] == "", option["window_passed"],
            option["sowing_date"] is None,
            1.0 if pd.isna(share) else share, float("inf") if pd.isna(irr) else irr)


def _rabi_window_still_open(cal, earliest_offset):
    """True if at least one crop in `cal` still has an open sowing slot this
    season: earliest_offset is on or before that crop's window_end plus the
    grid tail (GRID_AFTER_DAYS)."""
    for _, rows in cal.groupby("crop", sort=False):
        window_end = tuple(int(x) for x in rows.iloc[0]["window_end"].split("-"))
        if earliest_offset <= season_offset(*window_end) + GRID_AFTER_DAYS:
            return True
    return False


def _aman_option(ref, earliest):
    """Extra rotation option: aman (kharif) transplanting is still possible
    if `earliest` falls inside or before the sourced aman_rice sowing
    window (same calendar year as `earliest`). Returns None if there is no
    sourced aman_rice.sow window or the window has already closed. Carries
    no risk numbers: aman weather risk is not assessed yet (CLAUDE.md:
    never invent a number without a cited tool result)."""
    window = resolve_window(ref, "aman_rice")
    if window is None:
        return None
    end = pd.Timestamp(year=earliest.year, month=window["end"][0], day=window["end"][1])
    if earliest > end:
        return None
    source = window["citations"][0]["source_title"].split(",")[0]
    note = (f"Aman transplanting still possible until {end.date()} ({source}). "
            "Weather risk for aman is not assessed yet.")
    return {"crop": "aman_rice",
           "window": f"{mmdd(window['start'])} to {mmdd(window['end'])}",
           "window_passed": False, "sowing_date": None, "outside_recommended_window": None,
           "n_years": None, "problem_years": None, "problem_share": float("nan"),
           "hazards_assessed": "", "hazards_checked": [], "hazards_missing": [],
           "n_hazards_checked": 0, "coverage": "not_assessed",
           "coverage_notice": "Weather risk for aman is not assessed yet.",
           "irrigation_mm_mean": float("nan"), "irrigation_mm_worst20": float("nan"),
           "stress_days_mean": float("nan"), "maturity_date": None,
           "fits_before_next_crop": None, "rank": None, "problem_line": note,
           "citations": window["citations"]}


def rotation_options(district, previous_crop_harvest_date, earliest_ready_date=None,
                     turnaround_days=TURNAROUND_DAYS, planned_next_crop=None, calendar=None,
                     ref=None):
    """
    What to sow after the previous crop (e.g. aman) is harvested.

    earliest sowing = max(harvest + turnaround_days, earliest_ready_date),
    where earliest_ready_date is the post-flood ready date if one is known
    (post_flood.earliest_sowing_date). For each crop the first calendar
    sowing date on or after max(earliest sowing, the crop's window start)
    is used: nobody sows before the recommended window opens.

    If every rabi crop's sowing window (plus the 4-week grid tail) has
    already passed for the season `earliest` falls in (e.g. a flood ready
    date in June/July, near the end of the Aug-Jul season), the NEXT
    season's windows are used instead; next_season_used is set and
    next_season_note explains it in plain language.

    If a sourced aman_rice.sow window (data/reference, via `ref`) covers or
    is still ahead of `earliest`, an "aman_rice" option is added at the
    front of "options" saying transplanting is still possible; it carries
    no risk numbers (aman weather risk is not assessed yet).

    Crops are ranked by problem-year share, tie-broken by worst-20% net
    irrigation. Crops with no sourced hazard are listed but not ranked, and
    crops whose cited window has passed are marked and ranked after the
    rest. If planned_next_crop is given, each option is checked: does it
    reach (median) maturity before that crop's window_end?

    Every option carries hazards_checked/hazards_missing (lists),
    n_hazards_checked, coverage ("full" only if every hazard HAZARDS lists
    for that crop is sourced, else "partial" with coverage_notice set), and
    problem_line, a plain-language sentence that always names what was
    checked so a crop is never read as "safer" just because fewer hazards
    were evaluated for it.

    calendar: a build_calendar() DataFrame (default: data/processed/
    risk_calendar.csv). ref: a ReferenceData for the aman_rice check
    (default: load_reference_for_calendar()).
    """
    if calendar is None:
        calendar = pd.read_csv(RISK_CALENDAR_PATH)
    if ref is None:
        ref = load_reference_for_calendar()
    cal = calendar[calendar["district"] == district]
    if cal.empty:
        raise ValueError(f"No risk calendar rows for district {district!r}")
    harvest = pd.Timestamp(previous_crop_harvest_date)
    earliest = harvest + pd.Timedelta(days=int(turnaround_days))
    basis = f"harvest {harvest.date()} + {int(turnaround_days)} days turnaround (CropShift assumption)"
    if earliest_ready_date is not None and pd.Timestamp(earliest_ready_date) > earliest:
        earliest = pd.Timestamp(earliest_ready_date)
        basis = f"post-flood ready date {earliest.date()}"
    season = season_of(earliest)
    earliest_offset = season_offset(earliest.month, earliest.day)

    next_season_used = False
    next_season_note = None
    if not _rabi_window_still_open(cal, earliest_offset):
        season += 1
        earliest_offset = -1   # before every crop's window: nobody sows before it opens anyway
        next_season_used = True
        next_season_note = (f"Every rabi (winter) crop's sowing window for the {harvest.year}"
                            f"-{harvest.year + 1} season had already passed by {earliest.date()}; "
                            "showing the next rabi season's windows instead.")

    next_end = None
    if planned_next_crop is not None:
        rows = cal[cal["crop"] == planned_next_crop]
        if rows.empty:
            raise ValueError(f"No calendar rows for planned next crop {planned_next_crop!r}")
        next_end = tuple(int(x) for x in rows.iloc[0]["window_end"].split("-"))

    options = []
    for crop, rows in cal.groupby("crop", sort=False):
        rows = rows.sort_values("sow_offset")
        window_start = tuple(int(x) for x in rows.iloc[0]["window_start"].split("-"))
        window_end = tuple(int(x) for x in rows.iloc[0]["window_end"].split("-"))
        later = rows[rows["sow_offset"] >= max(earliest_offset, season_offset(*window_start))]
        hazards_assessed_str = (rows.iloc[0]["hazards_assessed"]
                                if isinstance(rows.iloc[0]["hazards_assessed"], str) else "")
        hazards_missing_str = (rows.iloc[0]["hazards_missing"]
                               if isinstance(rows.iloc[0]["hazards_missing"], str) else "")
        hazards_checked = [h for h in hazards_assessed_str.split(";") if h]
        hazards_missing = [h for h in hazards_missing_str.split(";") if h]
        coverage = "full" if not hazards_missing else "partial"
        coverage_notice = ("Not all risks for this crop are checked yet."
                           if coverage == "partial" else None)
        option = {"crop": crop, "window": f"{rows.iloc[0]['window_start']} to {rows.iloc[0]['window_end']}",
                  "window_passed": bool(earliest_offset > season_offset(*window_end)),
                  "sowing_date": None, "outside_recommended_window": None,
                  "n_years": None, "problem_years": None, "problem_share": float("nan"),
                  "hazards_assessed": hazards_assessed_str,
                  "hazards_checked": hazards_checked, "hazards_missing": hazards_missing,
                  "n_hazards_checked": len(hazards_checked),
                  "coverage": coverage, "coverage_notice": coverage_notice,
                  "irrigation_mm_mean": float("nan"), "irrigation_mm_worst20": float("nan"),
                  "stress_days_mean": float("nan"), "maturity_date": None,
                  "fits_before_next_crop": None}
        if not later.empty:
            row = later.iloc[0]
            sow = season_date(season, *(int(x) for x in row["sowing_mmdd"].split("-")))
            option.update({
                "sowing_date": str(sow.date()),
                "outside_recommended_window": bool(row["outside_window"]),
                "n_years": int(row["n_years"]),
                "problem_years": None if pd.isna(row["problem_years"]) else int(row["problem_years"]),
                "problem_share": row["problem_share"],
                "irrigation_mm_mean": row["irrigation_mm_mean"],
                "irrigation_mm_worst20": row["irrigation_mm_worst20"],
                "stress_days_mean": row["stress_days_mean"]})
            if not pd.isna(row["maturity_days_median"]):
                maturity = sow + pd.Timedelta(days=int(row["maturity_days_median"]))
                option["maturity_date"] = str(maturity.date())
                if next_end is not None and crop != planned_next_crop:
                    end_date = season_date(season, *next_end)
                    if end_date <= sow:
                        end_date = season_date(season + 1, *next_end)
                    option["fits_before_next_crop"] = bool(maturity <= end_date)
        option["problem_line"] = problem_line(option["sowing_date"], option["problem_years"],
                                              option["n_years"], hazards_checked)
        options.append(option)

    options.sort(key=_rank_key)
    rank = 0
    for option in options:
        if option["hazards_assessed"] and option["sowing_date"] is not None:
            rank += 1
            option["rank"] = rank
        else:
            option["rank"] = None

    aman_option = _aman_option(ref, earliest)
    if aman_option is not None:
        options.insert(0, aman_option)

    return {"district": district, "earliest_sowing_date": str(earliest.date()),
            "basis": basis, "season": season, "next_season_used": next_season_used,
            "next_season_note": next_season_note, "planned_next_crop": planned_next_crop,
            "options": options}


# ---------------- information only ----------------

def district_area(ref, district):
    """{crop: {"ha", source_title, source_url, page}} from BBS area rows.
    Information only: never used to filter or rank crops."""
    out = {}
    for _, row in ref.with_prefix(f"area.{district}.").iterrows():
        out[row["item"].split(".", 2)[2]] = {"ha": row["value"], **citation(row)}
    return out


@functools.lru_cache(maxsize=1)
def load_reference_for_calendar():
    return load_reference(files=list(REFERENCE_FILES))
