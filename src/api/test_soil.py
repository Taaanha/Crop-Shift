"""
Tests for GET /api/v1/soil and src/acquire/soilgrids_point.py. SoilGrids is
always mocked: no test touches the network.

Run from the repo root: python -m pytest src -q
"""

import json
import os
import sys

import pytest
import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import app as api  # noqa: E402
from test_app import assert_measure, is_measure, src_ids, walk  # noqa: E402

sg = api.soilgrids_point
client = TestClient(api.app)
URL = "/api/v1/soil?lat=23.45&lon=91.2"

# (property, d_factor, 0-5 cm, 5-15 cm, 15-30 cm) in SoilGrids' mapped units
LAYERS = [("sand", 10, 200, 210, 220), ("silt", 10, 300, 330, 350),
          ("clay", 10, 500, 460, 430), ("phh2o", 10, 50, 52, 52), ("soc", 10, 240, 160, 120)]
ALL = {name for name, *_ in LAYERS}


def payload(null=()):
    out = []
    for name, d, *vals in LAYERS:
        depths = [{"label": lab, "values": {"mean": None if name in null else v}}
                  for lab, v in zip(("0-5cm", "5-15cm", "15-30cm"), vals)]
        out.append({"name": name, "unit_measure": {"d_factor": d}, "depths": depths})
    return {"properties": {"layers": out}}


class Reply:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "CACHE_DIR", str(tmp_path))
    return tmp_path


def fake_get(monkeypatch, reply=None, error=None):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        if error:
            raise error
        return reply
    monkeypatch.setattr(sg.requests, "get", get)
    return calls


# ---------------- parsing ----------------

def test_depth_weighted_over_0_30_cm():
    soil = sg.parse_response(payload())
    assert soil["ph"] == pytest.approx((5.0 * 5 + 5.2 * 10 + 5.2 * 15) / 30)
    assert soil["organic_carbon_g_kg"] == pytest.approx((24.0 * 5 + 16.0 * 10 + 12.0 * 15) / 30)
    assert soil["sand"] + soil["silt"] + soil["clay"] == pytest.approx(100.0)
    clay = (50.0 * 5 + 46.0 * 10 + 43.0 * 15) / 30
    sand = (20.0 * 5 + 21.0 * 10 + 22.0 * 15) / 30
    silt = (30.0 * 5 + 33.0 * 10 + 35.0 * 15) / 30
    assert soil["clay"] == pytest.approx(clay * 100 / (clay + sand + silt))
    assert soil["texture"] == "clay"


def test_masked_point_gives_none():
    assert sg.parse_response(payload(null=ALL)) is None


def test_missing_texture_property_keeps_ph():
    soil = sg.parse_response(payload(null={"sand"}))
    assert soil["texture"] is None and soil["ph"] == pytest.approx(5.17, abs=0.01)


def test_garbage_json_is_a_soilgrids_error():
    with pytest.raises(sg.SoilGridsError):
        sg.parse_response({"unexpected": 1})


# ---------------- endpoint: success ----------------

def test_success_returns_field_estimate_with_soilgrids_provenance(monkeypatch):
    calls = fake_get(monkeypatch, Reply(payload()))
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    soil = body["data"]["soil"]
    assert soil["estimate"] == "field" and soil["depth"] == "0-30 cm"
    assert soil["texture"]["value"] == "clay" and soil["texture"]["bn"]
    assert soil["ph"]["value"] == 5.2 and soil["ph"]["unit"] == "pH (H2O)"
    assert soil["organic_carbon_g_kg"]["unit"] == "g/kg"
    assert body["source_mode"] == "live"
    prov = {p["id"]: p for p in body["provenance"]}
    assert prov["soilgrids"]["url"] == "https://soilgrids.org"
    assert prov["soilgrids"]["resolution"] == "250 m"
    assert prov["soilgrids"]["agency"] == "ISRIC - World Soil Information"
    assert "not a lab test" in prov["soilgrids"]["note"]
    assert "Upazila Agriculture Office" in prov["soilgrids"]["note"]
    assert calls[0]["timeout"] == 10
    assert any("still uses the district" in n["en"] for n in body["notices"])


def test_second_request_comes_from_disk_cache(monkeypatch):
    calls = fake_get(monkeypatch, Reply(payload()))
    client.get(URL)
    again = client.get(URL).json()
    assert len(calls) == 1
    assert again["source_mode"] == "cache" and again["data"]["soil"]["estimate"] == "field"


# ---------------- endpoint: failures fall back, never broken JSON ----------------

@pytest.mark.parametrize("failure", [
    {"error": requests.Timeout("slow")},
    {"error": requests.ConnectionError("down")},
    {"reply": Reply({}, 503)},
    {"reply": Reply({}, 429)},
    {"reply": Reply(ValueError("not json"))},
    {"reply": Reply({"unexpected": 1})},
])
def test_failure_falls_back_to_district_soil_with_notice(monkeypatch, failure):
    fake_get(monkeypatch, **failure)
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    soil = body["data"]["soil"]
    assert soil["estimate"] == "district" and soil["depth"] == "0-100 cm"
    assert soil["clay_pct"]["value"] == 42.4          # cumilla.clay_pct in soil_params.csv
    assert soil["ph"]["value"] is None and soil["organic_carbon_g_kg"]["value"] is None
    assert body["source_mode"] == "fixture"
    assert any(n["level"] == "caution" and "district soil" in n["en"] for n in body["notices"])
    assert "soilgrids" not in src_ids(body["data"])


def test_masked_point_falls_back_and_says_so(monkeypatch):
    fake_get(monkeypatch, Reply(payload(null=ALL)))
    body = client.get(URL).json()
    assert body["data"]["soil"]["estimate"] == "district"
    assert any("no value" in n["en"] for n in body["notices"])


def test_failure_is_not_cached(monkeypatch):
    fake_get(monkeypatch, error=requests.Timeout("slow"))
    client.get(URL)
    calls = fake_get(monkeypatch, Reply(payload()))
    assert client.get(URL).json()["data"]["soil"]["estimate"] == "field"
    assert len(calls) == 1


def test_district_only_uses_district_values_without_calling_soilgrids(monkeypatch):
    calls = fake_get(monkeypatch, Reply(payload()))
    body = client.get("/api/v1/soil?district=sylhet").json()
    assert calls == []
    assert body["data"]["soil"]["estimate"] == "district"
    assert body["data"]["soil"]["sand_pct"]["value"] == 48.4
    assert body["data"]["location"] is None


# ---------------- endpoint: bad input ----------------

@pytest.mark.parametrize("query", [
    "lat=abc&lon=91.2", "lat=95&lon=91.2", "lat=23.4&lon=300", "lat=23.4", "lon=91.2", "",
    "district=dhaka", "lat=0&lon=0"])
def test_bad_coordinates_use_the_error_envelope(monkeypatch, query):
    calls = fake_get(monkeypatch, Reply(payload()))
    r = client.get("/api/v1/soil?" + query)
    body = r.json()
    assert r.status_code == 400 and calls == []
    assert body["data"] is None
    assert body["errors"][0]["code"] in ("BAD_PARAMETER", "DISTRICT_NOT_COVERED")
    assert body["errors"][0]["en"] and body["errors"][0]["bn"]


# ---------------- contract shape ----------------

def mock_soil():
    with open(os.path.join(ROOT, "web", "mock", "soil.json"), encoding="utf-8") as f:
        return json.load(f)


def shape(obj):
    """Key structure only: dict keys recursively, a list as its first item's shape."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0])] if obj else []
    return None


@pytest.mark.parametrize("mode", ["field", "district"])
def test_contract_shape_matches_mock(monkeypatch, mode):
    if mode == "field":
        fake_get(monkeypatch, Reply(payload()))
    else:
        fake_get(monkeypatch, error=requests.Timeout("slow"))
    body = client.get(URL).json()
    mock = mock_soil()
    assert set(body) == set(mock) - {"mock_note"}
    assert shape(body["data"]) == shape(mock["data"])
    assert mock["is_mock"] is True and body["is_mock"] is False
    assert body["endpoint"] == "/api/v1/soil" and body["errors"] == []
    for key, value in walk(body["data"]):
        if is_measure(value):
            assert_measure(value, key)
    ids = {p["id"] for p in body["provenance"]}
    assert set(src_ids(body["data"])) <= ids
    assert all(p["dataset"] and p["url"] for p in body["provenance"])
    for n in body["notices"]:
        assert n["level"] in ("info", "caution") and n["en"] and n["bn"]
