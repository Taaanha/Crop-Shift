"""
Contract tests for app.py (task 10a): each real endpoint's response has the
same top-level keys and measure shape as its web/mock file, every src id
exists in provenance (with a dataset and URL), narration passes guard(),
errors use the error envelope, and the not-built endpoints say so.

Run from the repo root: python -m pytest src -q
"""

import json
import os
import re
import sys
from urllib.parse import urlsplit

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import app as api  # noqa: E402
from guard import guard  # noqa: E402

MOCK_DIR = os.path.join(ROOT, "web", "mock")
client = TestClient(api.app)   # no lifespan: caches fill lazily, so tests don't pay the warm-up

REAL = {
    "districts": "/api/v1/districts",
    "advisory": "/api/v1/advisory?district=cumilla&prev_harvest=2024-11-10&lang=bn",
    "risk_calendar": "/api/v1/risk-calendar?district=cumilla&crop=wheat",
    "post_flood": "/api/v1/post-flood?district=feni&flood_date=2024-08-21",
    "field_twin": "/api/v1/field-twin?district=cumilla&year=2019&crop=mustard&sow_date=2019-11-10",
}
EXTRA = {   # same shape rules, other paths through the code
    "advisory_flood": "/api/v1/advisory?district=feni&prev_harvest=2024-11-01&flood_ready=2024-11-20",
    "risk_calendar_boro": "/api/v1/risk-calendar?district=sylhet&crop=boro_rice",
    "risk_calendar_mustard": "/api/v1/risk-calendar?district=noakhali&crop=mustard",
    "post_flood_noakhali": "/api/v1/post-flood?district=noakhali&flood_date=2024-08-21",
    "post_flood_sylhet": "/api/v1/post-flood?district=sylhet&flood_date=2022-06-17",
}


def mock(name):
    with open(os.path.join(MOCK_DIR, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def responses():
    out = {}
    for name, url in {**REAL, **EXTRA}.items():
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
        out[name] = r.json()
    return out


def is_measure(obj):
    return isinstance(obj, dict) and "value" in obj and "src" in obj


def walk(obj, key=None):
    """Yields (key, value) for every dict entry, recursively."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)


def paths(obj, prefix=""):
    """{path: value} with list items collapsed to '[]' (e.g. options[].problem_years)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.setdefault(f"{prefix}.{k}", []).append(v)
            for p, vs in paths(v, f"{prefix}.{k}").items():
                out.setdefault(p, []).extend(vs)
    elif isinstance(obj, list):
        for v in obj:
            for p, vs in paths(v, f"{prefix}[]").items():
                out.setdefault(p, []).extend(vs)
    return out


def src_ids(obj):
    return [i for k, v in walk(obj) if k == "src" for i in v]


def assert_measure(m, where):
    assert set(m) == {"value", "unit", "src"}, where
    assert m["value"] is None or (isinstance(m["value"], (int, float))
                                  and not isinstance(m["value"], bool)), where
    assert isinstance(m["unit"], str) and m["unit"], where
    assert isinstance(m["src"], list) and m["src"], where
    assert all(isinstance(s, str) for s in m["src"]), where


# ---------------- envelope and measure shape ----------------

@pytest.mark.parametrize("name", list(REAL))
def test_top_level_keys_match_mock(responses, name):
    expected = set(mock(name)) - {"mock_note"}
    assert set(responses[name]) == expected


@pytest.mark.parametrize("name", list(REAL) + list(EXTRA))
def test_envelope_values(responses, name):
    body = responses[name]
    assert body["api_version"] == "1.0"
    assert body["source_mode"] in ("live", "cache", "fixture")
    assert body["is_mock"] is False
    assert body["errors"] == []
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", body["generated_at"])
    for notice in body["notices"]:
        assert notice["level"] in ("info", "caution") and notice["en"] and notice["bn"]


@pytest.mark.parametrize("name", list(REAL) + list(EXTRA))
def test_every_measure_has_the_contract_shape(responses, name):
    for key, value in walk(responses[name]["data"]):
        if is_measure(value):
            assert_measure(value, f"{name}.{key}")


@pytest.mark.parametrize("name", list(REAL))
def test_mock_measures_are_measures_here_too(responses, name):
    """Any path that holds a measure in the mock holds a measure here too
    (when the real response has that path and it is not null)."""
    real = paths(responses[name]["data"])
    shared = 0
    for path, values in paths(mock(name)["data"]).items():
        if any(is_measure(v) for v in values) and path in real:
            shared += 1
            for v in real[path]:
                assert v is None or is_measure(v), f"{name}{path} should be a measure"
    assert shared > 0 or name == "districts"


@pytest.mark.parametrize("name", list(REAL) + list(EXTRA))
def test_every_src_id_is_in_provenance_with_dataset_and_url(responses, name):
    body = responses[name]
    provenance = {p["id"]: p for p in body["provenance"]}
    assert len(provenance) == len(body["provenance"]), "duplicate provenance ids"
    for key, value in walk(body["data"]):
        if key == "src":
            assert len(value) == len(set(value)), f"{name}: duplicate src ids {value}"
    for pid in src_ids(body["data"]):
        assert pid in provenance, f"{name}: src {pid} missing from provenance"
    for p in body["provenance"]:
        assert p["dataset"] and "url" in p, p
        assert is_web_url(p["url"]) or (p["url"] == "" and "Link pending" in p["note"]), p
        assert is_web_url(p.get("view_url", "https://x.org")), p
        assert {"id", "dataset", "url", "agency", "period", "resolution", "note"} <= set(p)
        assert set(p) <= {"id", "dataset", "url", "view_url", "agency", "period", "resolution", "note"}


def is_web_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and "." in host and " " not in url \
        and host.rsplit(".", 1)[-1].isalpha() and host.rsplit(".", 1)[-1] != "pdf"


def test_nasa_sources_have_a_dataset_page_and_a_satellite_view():
    fixed = api.fixed_provenance()
    for pid in ("imerg", "power", "smap", "opera"):
        assert is_web_url(fixed[pid]["url"]) and is_web_url(fixed[pid]["view_url"]), pid
    for pid in ("imerg", "smap", "opera"):
        assert fixed[pid]["view_url"].startswith("https://worldview.earthdata.nasa.gov/?v=")
    assert "nsidc.org/data/spl4smgp" in fixed["smap"]["url"]
    assert "AppEEARS" in fixed["smap"]["note"]


@pytest.mark.parametrize("raw, expected", [
    ("https://www.fao.org/4/x0490e/x0490e00.htm", "https://www.fao.org/4/x0490e/x0490e00.htm"),
    ("czis.cropzoning.gov.bd", "https://czis.cropzoning.gov.bd"),
    ('https://example.org/a.pdf"', "https://example.org/a.pdf"),
    ("https://n/a - fabricated for prototype", ""),
    ("n/a - fabricated for prototype/backend development only", ""),
    ("https://adhunikDhanerChash.pdf (BRRI official handbook)", ""),
    ("adhunikDhanerChash.pdf", ""),
    ("brridhan28.pdf / brridhan29.pdf (BRRI official fact sheets)", ""),
    ("The variety required 37 to 44 days", ""),
    ("", ""),
    (float("nan"), ""),
])
def test_public_url_keeps_only_real_web_addresses(raw, expected):
    assert api.public_url(raw) == expected


def test_every_reference_source_is_a_real_link_or_says_link_pending():
    for ref_id in sorted(set(api.reference_rows()["ref_id"])):
        p = api.ref_provenance(ref_id)
        assert is_web_url(p["url"]) or (p["url"] == "" and "Link pending" in p["note"]), p
    pending = [r for r in set(api.reference_rows()["ref_id"]) if not api.ref_provenance(r)["url"]]
    assert pending, "the BRRI handbook rows should still be pending until the team adds URLs"


@pytest.mark.parametrize("name", [n for n in list(REAL) + list(EXTRA) if n != "districts"])
def test_narration_passes_guard(responses, name):
    body = responses[name]
    narration = body["narration"]
    assert narration["narrator"] == "template"
    assert narration["provenance_check"] == "passed"
    assert len(narration["sms_en"]) <= 160 and len(narration["sms_bn"]) <= 160
    cited = [{"data": body["data"], "sources": body["provenance"]}]
    for key in ("en", "bn", "sms_en", "sms_bn"):
        assert narration[key]
        assert guard(narration[key], cited)["passed"], (key, narration[key])


@pytest.mark.parametrize("name", list(REAL) + list(EXTRA))
def test_the_public_name_is_survey_crops(responses, name):
    """The app is called Survey Crops (সার্ভে ক্রপস); the old name must not reach users."""
    for key, value in walk(responses[name]):
        if isinstance(value, str) and key != "url":
            assert "CropShift" not in value, (name, key, value)


def test_error_and_api_title_use_the_public_name():
    assert api.app.title == "Survey Crops API"
    body = client.get("/api/v1/advisory?district=dhaka&prev_harvest=2024-11-10").json()
    assert "Survey Crops" in body["errors"][0]["en"]
    assert "সার্ভে ক্রপস" in body["errors"][0]["bn"]


def test_districts_narration_is_null(responses):
    assert responses["districts"]["narration"] is None
    assert [d["id"] for d in responses["districts"]["data"]["districts"]] == list(api.DISTRICTS)


# ---------------- content rules ----------------

def user_strings(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("url", "id", "hazard", "crop", "code", "reason_code"):
                yield from user_strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from user_strings(v, key)
    elif isinstance(obj, str):
        yield obj


@pytest.mark.parametrize("name", list(REAL) + list(EXTRA))
def test_never_says_full(responses, name):
    body = responses[name]
    for text in user_strings({k: body[k] for k in ("data", "narration", "notices")}):
        assert not re.search(r"\bfull\b", text, re.IGNORECASE), text
    for key, value in walk(body["data"]):
        assert value != "full", key


def test_advisory_options_say_what_was_checked(responses):
    data = responses["advisory"]["data"]
    everything = data["options"] + data["filtered_out"]
    assert {o["crop"] for o in everything} == set(api.CROP_NAMES)
    for o in everything:
        assert "coverage" not in o   # rotation_options' "full"/"partial" label is never sent
        assert o["hazards_checked_text"]["en"]
        assert o["window_status"]["code"] in ("open", "late", "closed")
        if o["hazards_missing"]:
            assert o["coverage_notice"]["en"] == "Not all risks for this crop are checked yet."
        else:
            assert o["coverage_notice"] is None
    ranks = [o["rank"] for o in data["options"]]
    assert ranks == list(range(1, len(ranks) + 1))
    mustard = next(o for o in everything if o["crop"] == "mustard")   # heat hazard unsourced
    assert mustard["coverage_notice"] is not None
    boro = next(o for o in data["filtered_out"] if o["crop"] == "boro_rice")
    assert boro["reason_code"] == "NO_RISK_CHECKED" and boro["rank"] is None


def test_advisory_flood_ready_moves_the_earliest_date(responses):
    data = responses["advisory_flood"]["data"]
    assert data["earliest_sowing_date"]["date"] == "2024-11-20"
    assert "smap" in data["earliest_sowing_date"]["src"]


def test_post_flood_chain(responses):
    data = responses["post_flood"]["data"]
    # docs/results/post_flood.md: Feni soil 49 days (70th 52, 90th 47), water gone 2024-09-23.
    assert data["soil_back_to_normal"]["days_after_flood"]["value"] == 49
    assert {s["pct"]: s["days_after_flood"]["value"]
            for s in data["soil_back_to_normal"]["sensitivity"]} == {70: 52, 80: 49, 90: 47}
    assert data["water_on_ground"]["water_gone_date"] == "2024-09-23"
    assert data["water_on_ground"]["curve"]["rows"]
    assert data["earliest_sowing_date"]["date"] == "2024-10-09"
    assert data["still_possible"] and data["cascade"]["rows"]


def test_post_flood_inconclusive_water_uses_soil_only(responses):
    water = responses["post_flood_noakhali"]["data"]["water_on_ground"]
    assert water["available"] and not water["conclusive"]
    assert responses["post_flood_noakhali"]["data"]["earliest_sowing_date"]["date"] == "2024-10-11"
    sylhet = responses["post_flood_sylhet"]["data"]
    assert sylhet["water_on_ground"]["available"] is False
    assert sylhet["earliest_sowing_date"]["date"] == "2022-07-07"


def test_field_twin_is_an_example_not_a_forecast(responses):
    body = responses["field_twin"]
    data = body["data"]
    assert data["is_forecast"] is False
    assert "not a forecast" in data["label"]["en"]
    assert "not a forecast" in body["narration"]["en"]
    rows = data["weekly"]["rows"]
    assert rows[0]["week_start"] == "2019-11-10"
    assert sum(r["stress_days"] for r in rows) == data["totals"]["stress_days"]["value"]
    assert data["smap_check"]["available"] is True


def test_risk_calendar_rows_line_up(responses):
    data = responses["risk_calendar"]["data"]
    n = len(data["sowing_dates"])
    assert n > 0
    assert len(data["grid"]["any_problem_years"]) == n
    for row in data["grid"]["rows"]:
        assert len(row["years_hit"]) == n
    boro = responses["risk_calendar_boro"]["data"]
    assert boro["grid"]["rows"] == [] and boro["coverage_notice"] is not None


# ---------------- guard fallback ----------------

def test_a_fabricated_number_triggers_the_fallback():
    data = {"x": api.measure(12, "days", ["smap"])}
    texts = {"en": "It took 99 days.", "bn": "১২ দিন", "sms_en": "12 days", "sms_bn": "১২ দিন"}
    fallback = {k: "See below." for k in texts}
    out = api.narrate(texts, fallback, data)
    assert out["provenance_check"] == "blocked_fallback_used"
    assert out["en"] == "See below."
    ok = api.narrate({**texts, "en": "It took 12 days."}, fallback, data)
    assert ok["provenance_check"] == "passed"


# ---------------- errors ----------------

ERRORS = [
    ("/api/v1/advisory?district=dhaka&prev_harvest=2024-11-10", 400, "DISTRICT_NOT_COVERED"),
    ("/api/v1/advisory?district=cumilla", 400, "BAD_PARAMETER"),
    ("/api/v1/advisory?district=cumilla&prev_harvest=2024-13-40", 400, "BAD_PARAMETER"),
    ("/api/v1/advisory?district=cumilla&prev_harvest=2024-11-10&lang=fr", 400, "BAD_PARAMETER"),
    ("/api/v1/risk-calendar?district=cumilla&crop=banana", 400, "BAD_PARAMETER"),
    ("/api/v1/post-flood?district=feni&flood_date=2010-08-01", 404, "NO_DATA_FOR_PERIOD"),
    ("/api/v1/field-twin?district=cumilla&crop=wheat&sow_date=2026-11-10", 404, "NO_DATA_FOR_PERIOD"),
    ("/api/v1/field-twin?district=cumilla&crop=boro_rice&sow_date=2019-01-10", 400, "BAD_PARAMETER"),
    ("/api/v1/field-twin?district=cumilla&year=2015&crop=wheat&sow_date=2019-11-10", 400,
     "BAD_PARAMETER"),
    ("/api/v1/no-such-endpoint", 404, "BAD_PARAMETER"),
    # task 12: GPS points. Dhaka is ~74 km from the nearest district point.
    ("/api/v1/advisory?lat=23.81&lon=90.41&prev_harvest=2024-11-10", 400, "DISTRICT_NOT_COVERED"),
    ("/api/v1/post-flood?lat=23.81&lon=90.41&flood_date=2024-08-21", 400, "DISTRICT_NOT_COVERED"),
    ("/api/v1/field-twin?lat=23.81&lon=90.41&crop=wheat&sow_date=2019-11-10", 400,
     "DISTRICT_NOT_COVERED"),
    ("/api/v1/advisory?lat=23.4&prev_harvest=2024-11-10", 400, "BAD_PARAMETER"),
    ("/api/v1/advisory?lat=abc&lon=91.1&prev_harvest=2024-11-10", 400, "BAD_PARAMETER"),
    ("/api/v1/advisory?lat=123&lon=91.1&prev_harvest=2024-11-10", 400, "BAD_PARAMETER"),
    ("/api/v1/images/app.py", 404, "NO_DATA_FOR_PERIOD"),
    ("/api/v1/images/dswx_flood_feni_2030-01-01.png", 404, "NO_DATA_FOR_PERIOD"),
]


@pytest.mark.parametrize("url,status,code", ERRORS)
def test_errors_use_the_error_envelope(url, status, code):
    r = client.get(url)
    assert r.status_code == status
    body = r.json()
    assert set(body) == set(mock("error")) - {"mock_note"}
    assert body["data"] is None and body["narration"] is None
    assert [e["code"] for e in body["errors"]] == [code]
    assert body["errors"][0]["en"] and body["errors"][0]["bn"]


# ---------------- task 12: GPS points, images, website fields ----------------

def test_nearest_district_and_distance():
    assert api.nearest_district(23.47, 91.15)[0] == "cumilla"
    assert api.nearest_district(24.9, 91.87)[0] == "sylhet"
    did, km = api.nearest_district(23.0159, 91.3976)   # the Feni point itself
    assert did == "feni" and km < 0.01
    assert api.haversine_km(23.0, 91.0, 24.0, 91.0) == pytest.approx(111.2, abs=0.5)


@pytest.mark.parametrize("url", [
    "/api/v1/advisory?lat=23.47051&lon=91.15637&prev_harvest=2024-11-10",
    "/api/v1/post-flood?lat=23.47051&lon=91.15637&flood_date=2024-08-21",
    "/api/v1/field-twin?lat=23.47051&lon=91.15637&year=2019&crop=mustard&sow_date=2019-11-10",
])
def test_lat_lon_is_turned_into_a_district_by_the_backend(url):
    body = client.get(url).json()
    assert body["errors"] == []
    loc = body["data"]["location"]
    assert loc["district"] == "cumilla" and body["data"]["district"] == "cumilla"
    assert loc["distance_km"]["value"] < 60
    ids = {p["id"] for p in body["provenance"]}
    assert "assumptions" in ids


def test_lat_lon_wins_over_district_and_matches_the_district_answer():
    by_point = client.get("/api/v1/advisory?district=sylhet&lat=23.0159&lon=91.3976"
                          "&prev_harvest=2024-11-10").json()["data"]
    by_name = client.get("/api/v1/advisory?district=feni&prev_harvest=2024-11-10").json()["data"]
    assert by_point["district"] == "feni"
    assert by_point["options"] == by_name["options"]
    assert by_name["location"] is None


def test_advisory_problem_line_and_aman(responses):
    for o in responses["advisory"]["data"]["options"]:
        n = responses["advisory"]["data"]["years_used"]["value"]
        assert o["problem_line"]["en"].startswith(f"Problems in {o['problem_years']['value']} of {n}")
        assert o["hazards_checked_text"]["en"] in o["problem_line"]["en"]
        assert o["problem_line"]["bn"]
    assert responses["advisory"]["data"]["aman_option"] is None      # November: aman is over
    july = client.get("/api/v1/advisory?district=feni&prev_harvest=2026-07-10").json()["data"]
    aman = july["aman_option"]
    assert aman["crop"] == "aman_rice" and aman["coverage_notice"]["en"]
    assert aman["sowing_window"]["src"] and "not assessed" in aman["problem_line"]["en"]


def test_post_flood_smap_curve_and_images(responses):
    data = responses["post_flood"]["data"]
    curve = data["soil_back_to_normal"]["curve"]
    rows = {r["date"]: r for r in curve["rows"]}
    assert curve["src"] == ["smap", "post_flood"]
    assert "2024-08-21" in rows and data["soil_back_to_normal"]["date"] in rows
    normal = rows[data["soil_back_to_normal"]["date"]]
    assert normal["value"] <= normal["threshold"]
    assert [i["date"] for i in data["flood_images"]] == ["2024-08-21", "2024-09-02", "2024-09-26",
                                                         "2024-10-15"]
    assert responses["post_flood_sylhet"]["data"]["flood_images"] == []


def test_image_endpoint_serves_only_whitelisted_pngs():
    r = client.get("/api/v1/images/dswx_flood_feni_2024-08-21.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[1:4] == b"PNG"


def test_field_twin_weeks_carry_events(responses):
    weekly = responses["field_twin"]["data"]["weekly"]
    allowed = {"heat", "night_heat", "cold", "rain", "dry", "irrigation", "ok"}
    for row in weekly["rows"]:
        assert row["events"] and set(row["events"]) <= allowed
        assert ("dry" in row["events"]) == (row["stress_days"] > 0)
        assert ("irrigation" in row["events"]) == (row["irrigation_events"] > 0)
    assert weekly["events_rule"]["rain_week"]["value"] == 20


def test_root_redirects_to_docs():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/docs"


# ---------------- not built yet ----------------

@pytest.mark.parametrize("method,url,name", [
    ("get", "/api/v1/enso-lens?district=cumilla&crop=wheat", "enso_lens"),
    ("get", "/api/v1/warnings?district=feni", "warnings"),
    ("post", "/api/v1/ask", "ask"),
])
def test_unbuilt_endpoints_serve_the_mock(method, url, name):
    kwargs = {"json": {"question": "?", "district": "feni", "lang": "bn"}} if method == "post" else {}
    body = getattr(client, method)(url, **kwargs).json()
    assert body["is_mock"] is True
    assert body["notices"][0]["en"].startswith("Not built yet")
    assert body["data"] == mock(name)["data"]


def test_cors_allows_any_origin():
    r = client.get("/api/v1/districts", headers={"Origin": "https://example.org"})
    assert r.headers["access-control-allow-origin"] == "*"


# ---------------- advisory: the "how much water can you give?" answer ----------------

ADVISORY = "/api/v1/advisory?district=cumilla&prev_harvest=2024-11-10"


def advisory_with(water=None, url=ADVISORY):
    r = client.get(url + (f"&water={water}" if water else ""))
    assert r.status_code == 200, r.text[:300]
    return r.json()


def test_advisory_water_absent_equals_regular_without_the_notice():
    base, regular = advisory_with(), advisory_with("regular")
    assert base["data"] == regular["data"]
    assert "water" not in base["request"] and regular["request"]["water"] == "regular"
    assert not any("pump-capacity" in n["en"] for n in base["notices"])


@pytest.mark.parametrize("level", ["regular", "plenty"])
def test_advisory_regular_and_plenty_are_the_same_and_say_so(level):
    body = advisory_with(level)
    assert body["data"] == advisory_with()["data"]
    assert any("treated the same" in n["en"] and "pump-capacity" in n["en"]
               for n in body["notices"])
    assert any(n["bn"] for n in body["notices"] if "treated the same" in n["en"])


def test_advisory_rain_only_ranks_by_rainfed_stress_days():
    body = advisory_with("rain_only")
    opts = body["data"]["options"]
    assert body["request"]["water"] == "rain_only"
    assert [o["rank"] for o in opts] == list(range(1, len(opts) + 1))
    keys = [(o["problem_years"]["value"], o["water_stress_days_worst20"]["value"]) for o in opts]
    assert keys == sorted(keys)
    for o in opts:     # every crop here has dry-soil days in the worst 20% of years
        assert o["water_stress_days_worst20"]["value"] > 0
        assert o["water_note"]["code"] == "NEEDS_IRRIGATION"
        assert o["water_note"]["en"] and o["water_note"]["bn"]
    assert {o["crop"] for o in opts} == {o["crop"] for o in advisory_with()["data"]["options"]}
    assert body["narration"]["provenance_check"] == "passed"
    assert "rain only" in body["narration"]["en"]


def test_advisory_limited_filters_crops_needing_more_than_two_waterings():
    body = advisory_with("limited")
    data = body["data"]
    base = advisory_with()["data"]
    dropped = [o for o in data["filtered_out"] if o["reason_code"] == "NEEDS_MORE_WATER"]
    assert dropped
    for o in dropped:
        assert o["irrigation_events_worst20"]["value"] > 2
        assert o["rank"] is None and o["feasible"] is False
        n = round(o["irrigation_events_worst20"]["value"])
        assert f"about {n} waterings" in o["reason"]["en"] and o["reason"]["bn"]
    for o in data["options"]:
        assert o["irrigation_events_worst20"]["value"] <= 2
    assert [o["rank"] for o in data["options"]] == list(range(1, len(data["options"]) + 1))
    assert ({o["crop"] for o in data["options"]} | {o["crop"] for o in dropped}
            == {o["crop"] for o in base["options"]})
    # other filtered_out reasons are kept as they were
    assert [o for o in data["filtered_out"] if o["reason_code"] != "NEEDS_MORE_WATER"] \
        == base["filtered_out"]
    assert body["narration"]["provenance_check"] == "passed"


def test_apply_water_limited_boundary():
    def opt(events):
        return {"irrigation_events_worst20": {"value": events}, "rank": 1, "feasible": True,
                "why": None}
    ranked, filtered, _ = api.apply_water([opt(2.0), opt(2.4), opt(None)], [], "limited")
    assert [o["irrigation_events_worst20"]["value"] for o in ranked] == [2.0, None]
    assert [o["irrigation_events_worst20"]["value"] for o in filtered] == [2.4]
    assert filtered[0]["reason_code"] == "NEEDS_MORE_WATER"
    assert "about 2 waterings" in filtered[0]["reason"]["en"]


def test_advisory_water_contract_shape(responses):
    body = advisory_with("limited")
    assert set(body["data"]) == set(responses["advisory"]["data"])
    for name in ("irrigation_events_avg", "irrigation_events_worst20"):
        for o in body["data"]["options"] + body["data"]["filtered_out"]:
            assert set(o[name]) == {"value", "unit", "src"}
            assert o[name]["unit"] == "waterings"
            assert {"imerg", "power", "fao56"} <= set(o[name]["src"])
    assert set(advisory_with("rain_only")) == set(responses["advisory"])
    bn = client.get(ADVISORY + "&water=limited&lang=bn").json()
    assert any(o["reason"]["bn"] for o in bn["data"]["filtered_out"]
               if o["reason_code"] == "NEEDS_MORE_WATER")


def test_advisory_rejects_unknown_water_level():
    r = client.get(ADVISORY + "&water=lots")
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "BAD_PARAMETER"
