"""
Unit tests for src/compute/risk_calendar.py on synthetic weather and a
synthetic reference table.
Run from the repo root with: python -m pytest src -q
"""

import numpy as np
import pandas as pd
import pytest

from reference import load_file, ReferenceData
from risk_calendar import (RISK_CALENDAR_PATH, build_crop_spec, calibrate, count_days, crop_calendar, days_to_target,
                           district_area, evaluate_season, gdd_target, max_run, mean_climate,
                           mmdd_at_offset, paddy_percolation, paddy_water_need, resolve_range,
                           resolve_window, rotation_options, saturated_days, season_date,
                           season_of, season_offset, sowing_grid, stage_span, summarise,
                           threshold_value, worst_mean, DEFAULT_PARAMS)

URL = "https://example.org/source.pdf"
HEADER = "item,value,unit,source_title,source_url,page,year,notes\n"


def make_ref(tmp_path, lines):
    path = tmp_path / "ref.csv"
    path.write_text(HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
    kept, skipped = load_file(path)
    return ReferenceData(kept, skipped)


def row(item, value, unit, title="Paper A", url=URL):
    return f"{item},{value},{unit},{title},{url},1,2020,"


WHEAT_LINES = [
    row("wheat.sow.window_start", "1115", "MMDD"), row("wheat.sow.window_end", "1130", "MMDD"),
    row("wheat.stage.days_to_flowering", "55-60", "days"),
    row("wheat.stage.days_to_maturity_low", "100", "days"),
    row("wheat.stage.days_to_maturity_high", "106", "days"),
    row("wheat.stage.base_temp", "0", "degC"),
    row("wheat.heat.anthesis_tmax", "31", "degC"),
    row("wheat.heat.grainfill_tmax", "35", "degC", title="PLACEHOLDER - NOT A REAL SOURCE",
        url="n/a"),
]


def synthetic_weather(start="2000-08-01", end="2026-07-31", tmean=20.0, spread=5.0,
                      seasonal=0.0):
    """Daily weather: tmean (+ a seasonal cosine of amplitude `seasonal`,
    coolest in mid-January), tmax = tmean + spread, tmin = tmean - spread."""
    idx = pd.date_range(start, end, freq="D")
    doy = idx.dayofyear.to_numpy()
    t = tmean - seasonal * np.cos(2 * np.pi * (doy - 15) / 365.25)
    return pd.DataFrame({"temp_mean_c": t, "temp_max_c": t + spread, "temp_min_c": t - spread,
                         "rainfall_mm": 0.0, "et0_mm": 3.0}, index=idx)


# ---------------- season dates and grid ----------------

def test_season_dates():
    assert season_of("2024-11-15") == 2024
    assert season_of("2025-01-20") == 2024
    assert season_date(2024, 1, 20) == pd.Timestamp("2025-01-20")
    assert season_date(2002, 2, 29) == pd.Timestamp("2003-02-28")   # 2003 is not a leap year
    assert season_offset(8, 1) == 0
    assert season_offset(1, 30) == 182
    assert mmdd_at_offset(season_offset(12, 15)) == (12, 15)


def test_sowing_grid_spans_two_weeks_before_to_four_weeks_after():
    grid = sowing_grid((11, 15), (11, 30))
    assert grid[0] == (11, 1)
    assert grid[-1] == (12, 27)            # last weekly step on or before Dec 28
    assert len(grid) == 9
    assert sowing_grid((12, 5), (1, 30))[0] == (11, 21)


# ---------------- reference -> spec ----------------

def test_resolve_range_combines_low_high_and_sources(tmp_path):
    ref = make_ref(tmp_path, WHEAT_LINES + [row("wheat.stage.days_to_flowering", "57", "days",
                                                title="Paper B")])
    flowering = resolve_range(ref, "wheat.stage.days_to_flowering")
    assert flowering["n_sources"] == 2
    assert flowering["value"] == pytest.approx((57.5 + 57) / 2)
    assert (flowering["low"], flowering["high"]) == (55.0, 60.0)
    maturity = resolve_range(ref, "wheat.stage.days_to_maturity")
    assert (maturity["value"], maturity["low"], maturity["high"]) == (103.0, 100.0, 106.0)
    assert resolve_range(ref, "wheat.heat.grainfill_tmax") is None       # PLACEHOLDER skipped


def test_spec_keeps_only_sourced_hazards(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    assert spec["stage_method"] == "gdd"
    assert [h["hazard"] for h in spec["hazards"]] == ["heat_anthesis"]
    missing = {m["hazard"]: m["reason"] for m in spec["missing"]}
    assert "no sourced threshold" in missing["heat_grainfill"]
    assert "placeholder x1" in missing["heat_grainfill"]
    for hazard in spec["hazards"]:
        assert all(c["source_url"].startswith("http") for c in hazard["threshold"]["citations"])


def test_spec_without_base_temp_uses_cited_days(tmp_path):
    lines = [l for l in WHEAT_LINES if "base_temp" not in l]
    spec = build_crop_spec(make_ref(tmp_path, lines), "wheat")
    assert spec["stage_method"] == "cited_days"


def test_spec_without_stage_days_reports_stage_gap(tmp_path):
    lines = [row("boro_rice.sow.window_start", "1205", "MMDD"),
             row("boro_rice.sow.window_end", "0130", "MMDD"),
             row("boro_rice.heat.anthesis_tmax", "35", "degC"),
             row("boro_rice.stage.days_to_maturity", "140", "days", url="brridhan28.pdf")]
    spec = build_crop_spec(make_ref(tmp_path, lines), "boro_rice")
    assert spec["stage_method"] == "none" and spec["hazards"] == []
    reasons = " ".join(m["reason"] for m in spec["missing"])
    assert "no sourced stage timing" in reasons


def test_window_merge_exclusion_and_derived_start(tmp_path):
    potato = [row("potato.sow.window_start", "1101", "MMDD"),
              row("potato.sow.window_end", "1107", "MMDD"),
              row("potato.sow.window_start", "1117", "MMDD"),
              row("potato.sow.window_end", "1130", "MMDD"),
              row("potato.sow.window_start", "0924", "MMDD"),   # early-variety, excluded
              row("potato.sow.window_end", "1007", "MMDD")]
    w = resolve_window(make_ref(tmp_path, potato), "potato")
    assert (w["start"], w["end"], w["method"]) == ((11, 1), (11, 30), "cited, merged")

    boro = [row("boro_rice.sow.seedbed_start", "1031", "MMDD"),
            row("boro_rice.sow.seedbed_start", "1115", "MMDD", title="Paper B"),
            row("boro_rice.stage.seedling_age", "35-45", "days"),
            row("boro_rice.sow.window_end", "0130", "MMDD")]
    w = resolve_window(make_ref(tmp_path, boro), "boro_rice")
    assert (w["start"], w["end"], w["method"]) == ((12, 5), (1, 30), "derived")


def test_threshold_value_ends():
    heat = {"op": ">", "threshold": {"low": 25.0, "high": 29.0}}
    cold = {"op": "<", "threshold": {"low": 12.0, "high": 13.0}}
    assert threshold_value(heat, "onset") == 25.0 and threshold_value(heat, "severe") == 29.0
    assert threshold_value(cold, "onset") == 13.0 and threshold_value(cold, "severe") == 12.0


# ---------------- growing degree days ----------------

def test_gdd_constant_temperature():
    clim = np.full(365, 20.0)
    target = gdd_target(clim, 100, 57.5, 0.0)         # round(57.5) = 58 days
    assert target == 58 * 20.0
    assert days_to_target(np.full(200, 20.0), 0.0, target) == 58
    assert days_to_target(np.full(200, 25.0), 0.0, target) == 47   # warmer -> earlier
    assert days_to_target(np.full(200, 15.0), 0.0, target) == 78   # cooler -> later
    assert days_to_target(np.full(10, 20.0), 0.0, target) is None


def test_calibration_reproduces_cited_days_in_mean_climate(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    weather = synthetic_weather(seasonal=6.0)
    clim = mean_climate(weather["temp_mean_c"], seasons=tuple(range(2001, 2025)))
    targets = calibrate(spec, clim)
    middle = (season_offset(11, 15) + season_offset(11, 30)) // 2
    from_middle = np.concatenate([clim[middle:], clim[:middle]])
    assert days_to_target(from_middle, 0.0, targets["flowering"]) == 58
    assert days_to_target(from_middle, 0.0, targets["maturity"]) == 103


def test_stage_spans():
    stages = {"flowering": 50, "maturity": 100}
    assert stage_span("flowering", stages, DEFAULT_PARAMS) == (47, 53)
    assert stage_span("grain_fill", stages, DEFAULT_PARAMS) == (54, 100)
    assert stage_span("first_days", {}, DEFAULT_PARAMS) == (0, 29)
    assert stage_span("flowering", {"flowering": None}, DEFAULT_PARAMS) is None
    assert stage_span("tuber", {"tuber_start": 35, "tuber_end": 55}, DEFAULT_PARAMS) == (35, 55)


# ---------------- hazards ----------------

def test_count_days_and_runs():
    assert count_days([30, 31, 32, 33], 31, ">") == 2
    assert count_days([7, 8, 9], 8, "<") == 1
    assert max_run([True, True, False, True, True, True, False]) == 3
    assert max_run([]) == 0


def test_saturated_days():
    daily = pd.DataFrame({"depletion_mm": [0.0, 0.0, 5.0], "deep_percolation_mm": [3.0, 0.0, 0.0]})
    assert list(saturated_days(daily)) == [True, False, False]


def test_evaluate_season_counts_hot_days_at_flowering(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    weather = synthetic_weather(tmean=20.0, spread=5.0)           # tmax 25, never > 31
    targets = calibrate(spec, mean_climate(weather["temp_mean_c"]))
    sow = pd.Timestamp("2010-11-22")
    base = evaluate_season(spec, targets, weather, sow)
    assert base["flowering_days"] == 58 and base["heat_anthesis_days_onset"] == 0
    hot = weather.copy()
    # 4 hot days (tmax 33) around flowering; tmean unchanged so timing is unchanged
    flowering_date = sow + pd.Timedelta(days=58)
    hot.loc[flowering_date - pd.Timedelta(days=1): flowering_date + pd.Timedelta(days=2),
            "temp_max_c"] = 33.0
    row = evaluate_season(spec, targets, hot, sow)
    assert row["heat_anthesis_days_onset"] == 4
    assert evaluate_season(spec, targets, weather, pd.Timestamp("2026-06-01")) is None


def test_summarise_problem_years_and_hot_day_sensitivity(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    rows = [{"heat_anthesis_days_onset": n, "heat_anthesis_days_severe": n,
             "flowering_days": 58, "maturity_days": 103,
             "net_irrigation_mm": irr, "water_stress_days": 10}
            for n, irr in [(0, 100), (1, 100), (3, 120), (5, 300), (0, 100)]]
    s3 = summarise(rows, spec, DEFAULT_PARAMS)
    assert (s3["n_years"], s3["problem_years"], s3["problem_share"]) == (5, 2, 0.4)
    assert summarise(rows, spec, {**DEFAULT_PARAMS, "hot_days": 1})["problem_years"] == 3
    assert summarise(rows, spec, {**DEFAULT_PARAMS, "hot_days": 5})["problem_years"] == 1
    assert s3["irrigation_mm_mean"] == 144.0
    assert s3["irrigation_mm_worst20"] == 300.0          # worst 1 of 5 years
    assert s3["maturity_days_median"] == 103


def test_summarise_irrigation_events_mean_and_worst20(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    rows = [{"heat_anthesis_days_onset": 0, "heat_anthesis_days_severe": 0,
             "flowering_days": 58, "maturity_days": 103,
             "net_irrigation_mm": 100, "water_stress_days": 10, "irrigation_events": n}
            for n in (2, 3, 3, 4, 8)]
    s = summarise(rows, spec, DEFAULT_PARAMS)
    assert s["irrigation_events_mean"] == 4.0
    assert s["irrigation_events_worst20"] == 8.0          # worst 1 of 5 years
    no_water = summarise([{k: v for k, v in rows[0].items() if k != "irrigation_events"}],
                         spec, DEFAULT_PARAMS)
    assert np.isnan(no_water["irrigation_events_mean"])
    assert np.isnan(no_water["irrigation_events_worst20"])


def test_summarise_with_no_live_hazard_is_not_zero_risk():
    spec = {"hazards": []}
    s = summarise([{"flowering_days": None, "maturity_days": None}], spec, DEFAULT_PARAMS)
    assert s["problem_years"] is None and np.isnan(s["problem_share"])


def test_worst_mean():
    assert worst_mean([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 9.5
    assert worst_mean([4.0]) == 4.0
    assert np.isnan(worst_mean([]))


def test_crop_calendar_flags_outside_window(tmp_path):
    spec = build_crop_spec(make_ref(tmp_path, WHEAT_LINES), "wheat")
    rows = crop_calendar("testland", spec, synthetic_weather(), seasons=tuple(range(2001, 2004)))
    by_date = {r["sowing_mmdd"]: r for r in rows}
    assert not by_date["11-22"]["outside_window"]
    assert by_date["11-01"]["outside_window"] and by_date["12-27"]["outside_window"]
    assert by_date["11-22"]["n_years"] == 3
    assert by_date["11-22"]["stage_method"] == "gdd"
    for col in ("problem_years_hot1", "problem_years_hot5", "problem_years_severe",
                "problem_years_window15"):
        assert col in by_date["11-22"]


def test_committed_calendar_has_sane_irrigation_events():
    cal = pd.read_csv(RISK_CALENDAR_PATH)
    assert {"irrigation_events_mean", "irrigation_events_worst20"} <= set(cal.columns)
    assert cal["irrigation_events_mean"].notna().all()
    assert (cal["irrigation_events_mean"] >= 0).all()
    assert (cal["irrigation_events_worst20"] >= cal["irrigation_events_mean"] - 1e-6).all()


# ---------------- paddy ----------------

def test_paddy_water_need_dry_season():
    need = paddy_water_need(np.zeros(30), np.full(30, 5.0), np.ones(30),
                            land_prep_mm=200, percolation_mm_day=2, ponding_depth_mm=100)
    # 7 mm/day loss: the 100 mm layer runs out on day 15 and day 30 -> 2 refills of 105 mm
    assert need["season_irrigation_mm"] == pytest.approx(210.0)
    assert need["total_mm"] == pytest.approx(410.0)
    assert need["etc_mm"] == 150.0 and need["percolation_mm"] == 60.0


def test_paddy_counts_land_prep_and_each_top_up_as_waterings():
    need = paddy_water_need(np.zeros(30), np.full(30, 5.0), np.ones(30), 200, 2, 100)
    assert need["irrigation_events"] == 3                  # land prep + 2 refills
    wet = paddy_water_need(np.full(30, 20.0), np.full(30, 5.0), np.ones(30), 200, 2, 100)
    assert wet["irrigation_events"] == 1                   # land prep only
    assert paddy_water_need(np.full(10, 20.0), np.ones(10), np.ones(10), 0, 2,
                            100)["irrigation_events"] == 0


def test_paddy_rain_fills_layer_and_spills():
    rain = np.zeros(10)
    rain[0] = 500.0
    need = paddy_water_need(rain, np.full(10, 5.0), np.ones(10), 200, 2, 100)
    assert need["season_irrigation_mm"] == 0.0
    assert need["spill_mm"] == pytest.approx(493.0)


def test_paddy_percolation_class():
    paddy = {"percolation_clay": {"value": 2.0}, "percolation_loam": {"value": 4.18}}
    assert paddy_percolation({"clay_pct": 42.4}, paddy) == (2.0, "clay")
    assert paddy_percolation({"clay_pct": 29.0}, paddy) == (4.18, "loam")


# ---------------- rotation ----------------

def _calendar():
    def cal_row(crop, md, share, irr, start, end, outside=False, assessed="heat",
                maturity=100.0):
        return {"district": "testland", "crop": crop, "sowing_mmdd": md,
                "sow_offset": season_offset(*(int(x) for x in md.split("-"))),
                "window_start": start, "window_end": end, "outside_window": outside,
                "hazards_assessed": assessed, "hazards_missing": "", "n_years": 24,
                "problem_years": None if share != share else round(share * 24),
                "problem_share": share, "irrigation_mm_mean": irr, "irrigation_mm_worst20": irr,
                "stress_days_mean": 5.0, "maturity_days_median": maturity}
    nan = float("nan")
    return pd.DataFrame([
        cal_row("wheat", "11-08", 0.00, 150, "11-15", "11-30", outside=True),
        cal_row("wheat", "11-15", 0.10, 150, "11-15", "11-30"),
        cal_row("wheat", "11-22", 0.20, 150, "11-15", "11-30"),
        cal_row("wheat", "12-20", 0.50, 150, "11-15", "11-30", outside=True),
        cal_row("lentil", "11-15", 0.10, 90, "10-24", "11-15"),
        cal_row("lentil", "11-22", 0.10, 90, "10-24", "11-15", outside=True),
        cal_row("potato", "11-15", 0.30, 200, "11-01", "11-30"),
        cal_row("potato", "11-22", 0.30, 200, "11-01", "11-30"),
        cal_row("boro_rice", "12-05", nan, 900, "12-05", "01-30", assessed=nan, maturity=nan),
    ])


def test_rotation_ranks_by_share_then_worst_irrigation():
    out = rotation_options("testland", "2024-11-08", calendar=_calendar())
    assert out["earliest_sowing_date"] == "2024-11-15"
    ranked = [(o["crop"], o["rank"]) for o in out["options"]]
    assert ranked[:3] == [("lentil", 1), ("wheat", 2), ("potato", 3)]  # tie 0.10 -> lower irrigation
    boro = [o for o in out["options"] if o["crop"] == "boro_rice"][0]
    assert boro["rank"] is None                                        # no live hazard: not ranked
    assert boro["sowing_date"] == "2024-12-05"


def test_rotation_marks_passed_windows_and_uses_ready_date():
    out = rotation_options("testland", "2024-11-08", earliest_ready_date="2024-11-20",
                           calendar=_calendar())
    assert out["earliest_sowing_date"] == "2024-11-20"
    assert "post-flood" in out["basis"]
    lentil = [o for o in out["options"] if o["crop"] == "lentil"][0]
    assert lentil["window_passed"] and lentil["outside_recommended_window"]
    assert lentil["sowing_date"] == "2024-11-22"
    assert out["options"][0]["crop"] == "wheat"          # passed windows rank after open ones


def test_rotation_turnaround_and_next_crop_fit():
    out = rotation_options("testland", "2024-11-01", turnaround_days=14,
                           planned_next_crop="boro_rice", calendar=_calendar())
    assert out["earliest_sowing_date"] == "2024-11-15"
    wheat = [o for o in out["options"] if o["crop"] == "wheat"][0]
    assert wheat["maturity_date"] == "2025-02-23"                       # Nov 15 + 100 days
    assert wheat["fits_before_next_crop"] is False                      # boro window ends Jan 30


def test_district_area_is_information(tmp_path):
    ref = make_ref(tmp_path, [row("area.feni.wheat", "52", "ha"),
                              row("area.cumilla.wheat", "496", "ha")])
    assert district_area(ref, "feni") == {"wheat": {"ha": 52.0, "source_title": "Paper A",
                                                    "source_url": URL, "page": "1"}}


def test_partial_coverage_crop_is_labelled_even_with_zero_problems():
    def cal_row(crop, md, assessed, missing, start="11-01", end="11-30"):
        return {"district": "testland", "crop": crop, "sowing_mmdd": md,
                "sow_offset": season_offset(*(int(x) for x in md.split("-"))),
                "window_start": start, "window_end": end, "outside_window": False,
                "hazards_assessed": assessed, "hazards_missing": missing, "n_years": 24,
                "problem_years": 0, "problem_share": 0.0,
                "irrigation_mm_mean": 100.0, "irrigation_mm_worst20": 100.0,
                "stress_days_mean": 5.0, "maturity_days_median": 100.0}
    cal = pd.DataFrame([
        cal_row("mustard", "11-08", "waterlog", "heat_flowering"),  # one hazard missing
        cal_row("potato", "11-08", "night_heat_tuber", ""),         # every hazard sourced
    ])
    out = rotation_options("testland", "2024-11-01", calendar=cal)
    mustard = [o for o in out["options"] if o["crop"] == "mustard"][0]
    potato = [o for o in out["options"] if o["crop"] == "potato"][0]

    assert mustard["problem_share"] == 0.0                          # 0 problems, but...
    assert mustard["coverage"] == "partial"                         # ...not shown as fully safe
    assert mustard["coverage_notice"] == "Not all risks for this crop are checked yet."
    assert mustard["hazards_checked"] == ["waterlog"]
    assert mustard["hazards_missing"] == ["heat_flowering"]
    assert mustard["n_hazards_checked"] == 1
    assert mustard["problem_line"] == "problems in 0 of 24 years (checked: waterlogging only)"

    assert potato["coverage"] == "full" and potato["coverage_notice"] is None
    assert potato["hazards_missing"] == []


def test_rotation_waits_for_the_window_to_open():
    out = rotation_options("testland", "2024-11-01", calendar=_calendar())
    assert out["earliest_sowing_date"] == "2024-11-08"
    wheat = [o for o in out["options"] if o["crop"] == "wheat"][0]
    assert wheat["sowing_date"] == "2024-11-15" and not wheat["outside_recommended_window"]


# ---------------- season rollover (task 6c) ----------------

def _empty_ref():
    return ReferenceData(pd.DataFrame(columns=["item", "source_title", "source_url", "page"]),
                         pd.DataFrame(columns=["item"]))


def test_rotation_rolls_over_to_next_season_when_every_rabi_window_has_passed():
    # Sylhet 2022 flood: harvest 2022-06-17, ready 2022-07-07 -> in the old
    # (Aug-Jul) season this is the tail end, past every rabi window (all in
    # Nov-Jan) -> every option used to read window_passed=True.
    out = rotation_options("testland", "2022-06-17", earliest_ready_date="2022-07-07",
                           calendar=_calendar(), ref=_empty_ref())
    assert out["earliest_sowing_date"] == "2022-07-07"
    assert out["next_season_used"] is True and out["season"] == 2022
    assert out["next_season_note"]
    rabi = [o for o in out["options"] if o["crop"] != "aman_rice"]
    assert not any(o["window_passed"] for o in rabi)
    assert all(o["sowing_date"] is not None for o in rabi)
    ranked = [(o["crop"], o["rank"]) for o in out["options"] if o["rank"] is not None]
    assert ranked[:3] == [("lentil", 1), ("wheat", 2), ("potato", 3)]
    lentil = [o for o in out["options"] if o["crop"] == "lentil"][0]
    assert lentil["sowing_date"] == "2022-11-15"                    # next (2022) rabi season


def test_rotation_normal_november_date_still_uses_the_same_season():
    out = rotation_options("testland", "2024-11-08", calendar=_calendar(), ref=_empty_ref())
    assert out["next_season_used"] is False and out["season"] == 2024
    assert out["next_season_note"] is None
    ranked = [(o["crop"], o["rank"]) for o in out["options"] if o["rank"] is not None]
    assert ranked[:3] == [("lentil", 1), ("wheat", 2), ("potato", 3)]


def test_rotation_adds_aman_option_when_sourced_window_is_still_open(tmp_path):
    lines = [row("aman_rice.sow.window_start", "0615", "MMDD", title="BARC Aman Calendar",
                url="https://example.org/aman.pdf"),
             row("aman_rice.sow.window_end", "0715", "MMDD", title="BARC Aman Calendar",
                url="https://example.org/aman.pdf")]
    ref = make_ref(tmp_path, lines)

    out = rotation_options("testland", "2022-06-17", earliest_ready_date="2022-07-07",
                           calendar=_calendar(), ref=ref)
    aman = out["options"][0]
    assert aman["crop"] == "aman_rice" and aman["rank"] is None
    assert aman["problem_line"] == ("Aman transplanting still possible until 2022-07-15 "
                                    "(BARC Aman Calendar). Weather risk for aman is not "
                                    "assessed yet.")
    assert aman["citations"][0]["source_url"] == "https://example.org/aman.pdf"

    out_nov = rotation_options("testland", "2024-11-08", calendar=_calendar(), ref=ref)
    assert out_nov["options"][0]["crop"] != "aman_rice"                 # window long closed
