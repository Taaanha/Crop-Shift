"""
app.py: the CropShift API, v1 (task 10a).

FastAPI over the existing deterministic code in src/compute. No new science
lives here: each endpoint calls compute functions, then packs their output
into the envelope from docs/api_contract.md. Every number is a
{value, unit, src} measure whose src ids point to a provenance entry with a
dataset and URL, and every narration is a plain template sentence built from
the data and checked by guard() (src/agents/guard.py).

Real endpoints: /districts, /advisory, /risk-calendar, /post-flood,
/field-twin, /soil. Not built yet (they serve the web/mock file with is_mock: true):
/enso-lens, /warnings, /ask.

Run locally (repo root): python -m uvicorn app:app --reload
then open http://127.0.0.1:8000/docs
"""

import datetime as _dt
import functools
import glob
import json
import math
import os
import re
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in (("src", "compute"), ("src", "agents"), ("src", "acquire")):
    _path = os.path.join(ROOT, *_sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import post_flood as pf  # noqa: E402
import soilgrids_point  # noqa: E402
from fetch_soilgrids import usda_texture  # noqa: E402
import risk_calendar as rc  # noqa: E402
from reference import load_reference  # noqa: E402
from guard import guard  # noqa: E402
from water_balance import crop_season, load_crop_params, load_soil_params  # noqa: E402
from weather import RAIN_CITATIONS  # noqa: E402

API_VERSION = "1.0"
PROCESSED = os.path.join(ROOT, "data", "processed")
REFERENCE = os.path.join(ROOT, "data", "reference")
MOCK_DIR = os.path.join(ROOT, "web", "mock")
REPO_URL = "https://github.com/Taaanha/Crop-Shift"
BD_TZ = _dt.timezone(_dt.timedelta(hours=6))
SOURCE_MODE = "cache"   # every real endpoint reads the processed archive in data/processed

DISTRICTS = {
    "cumilla": {"en": "Cumilla", "bn": "কুমিল্লা"},
    "feni": {"en": "Feni", "bn": "ফেনী"},
    "noakhali": {"en": "Noakhali", "bn": "নোয়াখালী"},
    "brahmanbaria": {"en": "Brahmanbaria", "bn": "ব্রাহ্মণবাড়িয়া"},
    "sylhet": {"en": "Sylhet", "bn": "সিলেট"},
}
POWER_CELL_SHARED = {"feni": ["noakhali"], "noakhali": ["feni"]}   # CLAUDE.md data facts
CROP_NAMES = {
    "wheat": {"en": "Wheat", "bn": "গম"},
    "mustard": {"en": "Mustard", "bn": "সরিষা"},
    "lentil": {"en": "Lentil", "bn": "মসুর"},
    "potato": {"en": "Potato", "bn": "আলু"},
    "boro_rice": {"en": "Boro rice", "bn": "বোরো ধান"},
}
HAZARD_BN = {
    "heat_anthesis": "ফুল ফোটার সময় গরম",
    "heat_grainfill": "দানা ভরার সময় গরম",
    "heat_flowering": "ফুল ফোটার সময় গরম",
    "waterlog": "জলাবদ্ধতা",
    "night_heat_tuber": "আলু গঠনের সময় রাতের গরম",
    "cold_booting": "থোড় আসার সময় ঠান্ডা",
}
# Crops the FAO-56 field-twin replay supports (boro uses the paddy bucket instead).
FIELD_TWIN_CROPS = tuple(c for c, m in rc.WATER_MODEL.items() if m == "fao56")
WATER_PARAM_CROP = {"boro_rice": "rice"}

# CropShift assumptions used only by this API (not from a source), shown in responses.
API_ASSUMPTIONS = {
    "dswx_scene_lead_days": {
        "value": 7, "unit": "days",
        "text": "DSWx-S1 scenes up to this many days before the flood date are used "
                "(a pass rarely falls on the exact flood day).",
        "why": "Radar passes come every few days, so a scene a week before the "
                "flood still shows the water that was already there."},
    "dswx_peak_max_lag_days": {
        "value": 30, "unit": "days",
        "text": "If the largest DSWx-S1 flood-water reading comes more than this many days "
                "after the flood date, it is not this flood (e.g. Noakhali's December "
                "peak), so the water signal is reported as inconclusive and only SMAP is used.",
        "why": "A peak a month or more after the flood is more likely a "
                "different event, and calling it 'inconclusive' is safer than "
                "guessing."},
    "dswx_dry_floor": {
        "value": "median flood_fraction, 2025-01-01 to 2025-03-31", "unit": "",
        "text": "Water the method sees with no flood (wet paddies, ponds, radar speckle). "
                "'Water mostly gone' = 90% of the water above this floor has drained "
                "(docs/results/post_flood.md).",
        "why": "Using the dry-season median (Jan-Mar 2025) as the floor means "
                "permanent water and speckle are not counted as flood."},
    "district_max_distance_km": {
        "value": 60, "unit": "km",
        "text": "A GPS point (lat, lon) is served by the nearest of the 5 district points "
                "(data/processed/district_metadata.csv) only if it lies within this distance; "
                "farther points get DISTRICT_NOT_COVERED.",
        "why": "The data are for the area around a district point, so a point "
                "far from every district would get numbers that do not describe "
                "it."},
    "twin_rain_week_mm": {
        "value": 20, "unit": "mm/week",
        "text": "In the field-twin replay a week is flagged 'rain' when IMERG rain that week "
                "is at least this much (for the animation only; not a risk rule).",
        "why": "It only picks which weeks of the animation show a rain icon; "
                "it feeds no risk number or ranking."},
}
IMAGE_DIR = os.path.join(ROOT, "docs", "results", "img")
IMAGE_RE = re.compile(r"^dswx_flood_([a-z]+)_(\d{4}-\d{2}-\d{2})\.png$")

NOT_ALL_CHECKED = {"en": "Not all risks for this crop are checked yet.",
                   "bn": "এই ফসলের সব ঝুঁকি এখনও যাচাই করা হয়নি।"}
AREA_NOTICE = {"level": "info",
               "en": "NASA data for the area around your field (rain ~0.1°, temperature ~0.5°, "
                     "soil moisture ~9 km), not the exact plot.",
               "bn": "আপনার জমির আশেপাশের এলাকার NASA তথ্য (বৃষ্টি ~০.১°, তাপমাত্রা ~০.৫°, "
                     "মাটির আর্দ্রতা ~৯ কিমি), নির্দিষ্ট জমির নয়।"}
PAST_NOT_FORECAST = {"level": "info",
                     "en": "These are counts over past seasons, not a forecast of this season.",
                     "bn": "এগুলো অতীতের মৌসুমের হিসাব, এই মৌসুমের পূর্বাভাস নয়।"}
WEATHER_RISKS_ONLY = {"level": "caution",
                      "en": "Only the weather risks listed for each crop are checked. Pests, "
                            "diseases, soil nutrients and prices are not.",
                      "bn": "প্রতিটি ফসলের জন্য শুধু তালিকাভুক্ত আবহাওয়াজনিত ঝুঁকি যাচাই করা "
                            "হয়েছে। পোকা, রোগ, মাটির পুষ্টি ও দাম যাচাই করা হয়নি।"}
ALL_WINDOWS_PASSED = {"level": "caution",
                      "en": "No crop's sowing window, including the next rabi season, can be "
                            "matched to this date.",
                      "bn": "পরের রবি মৌসুম সহ কোনো ফসলের বপনের সময় এই তারিখের সাথে মেলানো যায়নি।"}
SHARED_CELL_NOTICE = {"level": "caution",
                      "en": "Feni and Noakhali use the same NASA POWER temperature cell "
                            "(their IMERG rain cells are separate).",
                      "bn": "ফেনী ও নোয়াখালী একই NASA POWER তাপমাত্রা গ্রিড ব্যবহার করে "
                            "(IMERG বৃষ্টির গ্রিড আলাদা)।"}


# ---------------- small helpers ----------------

def bn_digits(text):
    return str(text).translate(str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯"))


def _num(value, decimals=0):
    """A JSON-safe rounded number, or None for missing values."""
    if value is None:
        return None
    value = float(value)
    if math.isnan(value):
        return None
    if decimals == 0:
        return int(round(value))
    return round(value, decimals)


def measure(value, unit, src, decimals=0):
    return {"value": _num(value, decimals), "unit": unit, "src": list(dict.fromkeys(src))}


def _fmt(value):
    """The number exactly as the data holds it (so guard() finds it)."""
    return f"{value:g}" if isinstance(value, float) else str(value)


def _clean(obj):
    """numpy / pandas values -> plain JSON types; NaN -> None."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(float(obj)) else float(obj)
    if isinstance(obj, pd.Timestamp):
        return str(obj.date())
    return obj


def _src_ids(obj, out):
    """Collects every src id used anywhere in data."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "src" and isinstance(v, list):
                out.extend(v)
            else:
                _src_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _src_ids(v, out)
    return out


class ApiError(Exception):
    STATUS = {"DISTRICT_NOT_COVERED": 400, "BAD_PARAMETER": 400,
              "NO_DATA_FOR_PERIOD": 404, "INTERNAL": 500}
    BN = {"DISTRICT_NOT_COVERED": "সার্ভে ক্রপস এখন কুমিল্লা, ফেনী, ব্রাহ্মণবাড়িয়া, নোয়াখালী ও "
                                  "সিলেট অঞ্চলে কাজ করে।",
          "BAD_PARAMETER": "অনুরোধের একটি তথ্য ঠিক নেই: {detail}",
          "NO_DATA_FOR_PERIOD": "এই সময়ের জন্য NASA তথ্য নেই: {detail}",
          "INTERNAL": "সার্ভারে একটি সমস্যা হয়েছে। আবার চেষ্টা করুন।"}

    def __init__(self, code, en, detail=""):
        super().__init__(en)
        self.code, self.en = code, en
        self.bn = self.BN[code].format(detail=detail or en)
        self.status = self.STATUS[code]


# ---------------- provenance ----------------

def _period(path, col="date"):
    dates = pd.read_csv(path, usecols=[col])[col]
    return f"{dates.min()}/{dates.max()}"


# "See the satellite data" links: NASA Worldview over the 5 districts. Each layer id
# is listed in the GIBS WMTS GetCapabilities (checked 2026-10-10); each date has
# data in that layer (IMERG: wettest day of the Aug-2024 flood in our IMERG files;
# OPERA: a day with a DSWx-S1 scene over Feni in data/processed/dswx_area_feni.csv).
AREA_BBOX = "90.8,22.7,92.4,25.3"   # lon/lat box around Cumilla, Feni, Brahmanbaria, Noakhali, Sylhet


def worldview_url(layer, date):
    layers = f"Reference_Labels_15m,Reference_Features_15m,Coastlines_15m,{layer}," \
             "VIIRS_SNPP_CorrectedReflectance_TrueColor"
    return f"https://worldview.earthdata.nasa.gov/?v={AREA_BBOX}&l={layers}&t={date}"


@functools.lru_cache(maxsize=1)
def fixed_provenance():
    smap = sorted(glob.glob(os.path.join(PROCESSED, "smap_*_2015_2026.csv")))
    dswx = sorted(glob.glob(os.path.join(PROCESSED, "dswx_area_*.csv")))
    return {
        "imerg": {"dataset": RAIN_CITATIONS["imerg"]["dataset"], "url": RAIN_CITATIONS["imerg"]["url"],
                  "view_url": worldview_url("IMERG_Precipitation_Rate", "2024-08-21"),
                  "agency": "NASA", "period": _period(os.path.join(PROCESSED, "imerg_cumilla_daily.csv")),
                  "resolution": "0.1° grid", "note": "Rain. Area around the field, not the exact plot."},
        "power": {"dataset": "NASA POWER Daily Point API (AG community): temperature, humidity, "
                             "wind, solar radiation", "url": "https://power.larc.nasa.gov/",
                  "view_url": "https://power.larc.nasa.gov/data-access-viewer/",
                  "agency": "NASA", "period": _period(os.path.join(PROCESSED, "power_cumilla_daily.csv")),
                  "resolution": "~0.5° grid",
                  "note": "Not used for rain. Feni and Noakhali share one cell."},
        "smap": {"dataset": "NASA SMAP L4 root-zone soil moisture (SPL4SMGP version 8)",
                 "url": "https://nsidc.org/data/spl4smgp/versions/8",
                 "view_url": worldview_url("SMAP_L4_Analyzed_Root_Zone_Soil_Moisture", "2024-08-25"),
                 "agency": "NASA (NSIDC DAAC)",
                 "period": _period(smap[0]) if smap else "", "resolution": "~9 km",
                 "note": "Downloaded through NASA AppEEARS (appeears.earthdatacloud.nasa.gov). Its rain "
                         "forcing is corrected to IMERG, so it is not independent of IMERG."},
        "opera": {"dataset": "NASA OPERA DSWx-S1 surface water (from Sentinel-1 radar)",
                  "url": "https://podaac.jpl.nasa.gov/dataset/OPERA_L3_DSWX-S1_V1",
                  "view_url": worldview_url("OPERA_L3_Dynamic_Surface_Water_Extent-Sentinel-1",
                                            "2024-08-28"),
                  "agency": "NASA JPL (Sentinel-1: ESA)",
                  "period": _period(dswx[0]) if dswx else "", "resolution": "30 m",
                  "note": "20 km x 20 km area around the district point; no scenes before 2024-08-21."},
        "fao56": {"dataset": "FAO-56 Crop evapotranspiration (Allen et al., 1998): Penman-Monteith "
                             "ET0, crop coefficients, root-zone water balance",
                  "url": "https://www.fao.org/4/x0490e/x0490e00.htm", "agency": "FAO",
                  "period": "", "resolution": "", "note": "Method reference; Kc in data/processed/kc_table.csv."},
        "calendar": {"dataset": "Survey Crops sowing-date risk calendar (data/processed/risk_calendar.csv)",
                     "url": f"{REPO_URL}/blob/main/docs/results/risk_calendar.md",
                     "agency": "Survey Crops analysis of NASA IMERG + POWER",
                     "period": f"seasons {rc.SEASONS[0]}-{rc.SEASONS[-1]}", "resolution": "",
                     "note": "Built by scripts/build_risk_calendar.py from the cited rows listed here."},
        "post_flood": {"dataset": "Survey Crops post-flood recovery method (docs/results/post_flood.md)",
                       "url": f"{REPO_URL}/blob/main/docs/results/post_flood.md",
                       "agency": "Survey Crops analysis of NASA SMAP + OPERA DSWx-S1",
                       "period": "", "resolution": "", "note": ""},
        "soilgrids": {"dataset": "ISRIC SoilGrids 2.0 (Poggio et al. 2021, SOIL 7:217-240): "
                                 "topsoil sand, silt, clay, pH (H2O), organic carbon",
                      "url": "https://soilgrids.org", "agency": "ISRIC - World Soil Information",
                      "period": "", "resolution": "250 m",
                      "note": "Model estimate from soil surveys + satellite data; not a lab test. "
                              "Test your soil at the Upazila Agriculture Office."},
        "assumptions": {"dataset": "Survey Crops modelling choices (not from a source; listed so "
                                   "they can be checked)",
                        "url": f"{REPO_URL}/blob/main/docs/assumptions.md",
                        "agency": "Survey Crops", "period": "", "resolution": "",
                        "note": "Where no source gave a number, the team chose one. Main choices are "
                                "re-run with other values (sensitivity runs)."},
    }


@functools.lru_cache(maxsize=1)
def reference_rows():
    """Every row of data/reference/*.csv with a stable 'ref_NN' id per
    (source_title, source_url)."""
    frames = []
    for path in sorted(glob.glob(os.path.join(REFERENCE, "*.csv"))):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        df = pd.read_csv(path, dtype=str)
        if not {"item", "source_title", "source_url"} <= set(df.columns):
            continue
        frames.append(df.assign(file=name))
    rows = pd.concat(frames, ignore_index=True).dropna(subset=["source_title", "source_url"])
    keys = sorted(set(zip(rows["source_title"], rows["source_url"])))
    ids = {key: f"ref_{i + 1:02d}" for i, key in enumerate(keys)}
    rows["ref_id"] = [ids[k] for k in zip(rows["source_title"], rows["source_url"])]
    return rows


BARE_DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/\S*)?")
FILE_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "htm", "html", "jpg", "png"}
LINK_PENDING = "Link pending: printed BRRI handbook / team to add the official URL"


def public_url(raw):
    """The source_url as a clickable web address, or "" if it is not one.
    A bare domain ("czis.cropzoning.gov.bd") gets https://, stray quotes are
    dropped. Rejected: text with spaces, no http(s) scheme, or a host with no
    dot or ending in a file name ("https://adhunikDhanerChash.pdf")."""
    text = "" if pd.isna(raw) else str(raw).strip().strip("\"'")
    if "://" not in text and BARE_DOMAIN_RE.fullmatch(text):
        text = "https://" + text
    if re.search(r"\s", text):
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
    except ValueError:
        return ""
    tld = host.rsplit(".", 1)[-1]
    if parts.scheme not in ("http", "https") or "." not in host or not tld.isalpha() \
            or tld.lower() in FILE_EXTENSIONS:
        return ""
    return text


def ref_provenance(ref_id):
    rows = reference_rows()
    rows = rows[rows["ref_id"] == ref_id]
    if rows.empty:
        raise KeyError(ref_id)
    first = rows.iloc[0]
    files = ", ".join(sorted(set(rows["file"])))
    url = public_url(first["source_url"])
    note = f"Cited in data/reference/{files}" + ("" if url else f". {LINK_PENDING}")
    return {"dataset": first["source_title"], "url": url,
            "agency": "", "period": "" if pd.isna(first.get("year")) else str(first["year"]),
            "resolution": "", "note": note}


def ref_ids_for(citations):
    """ref ids for a list of citation dicts ({source_title, source_url, ...})."""
    rows = reference_rows()
    lookup = dict(zip(zip(rows["source_title"], rows["source_url"]), rows["ref_id"]))
    return [lookup[(c["source_title"], c["source_url"])] for c in citations
            if (c["source_title"], c["source_url"]) in lookup]


def ref_ids_for_items(items):
    rows = reference_rows()
    return list(dict.fromkeys(rows[rows["item"].isin(items)]["ref_id"]))


def build_provenance(data):
    fixed = fixed_provenance()
    out = []
    for pid in dict.fromkeys(_src_ids(data, [])):
        entry = fixed[pid] if pid in fixed else ref_provenance(pid)
        out.append({"id": pid, **entry})
    return out


# ---------------- cached inputs (loaded once) ----------------

@functools.lru_cache(maxsize=1)
def calendar_table():
    return pd.read_csv(rc.RISK_CALENDAR_PATH)


@functools.lru_cache(maxsize=1)
def crop_specs():
    return rc.build_crop_specs(rc.load_reference_for_calendar())


@functools.lru_cache(maxsize=1)
def district_metadata():
    return pd.read_csv(rc.DISTRICT_METADATA_PATH).set_index("district")


@functools.lru_cache(maxsize=None)
def weather(district):
    """IMERG rain + POWER temperature etc. + FAO-56 ET0 (~2 s per district)."""
    return rc.prepare_weather(district, district_metadata())


@functools.lru_cache(maxsize=None)
def smap(district):
    return pd.read_csv(os.path.join(PROCESSED, f"smap_{district}_2015_2026.csv"),
                       index_col="date", parse_dates=True)


@functools.lru_cache(maxsize=None)
def dswx_area(district):
    path = os.path.join(PROCESSED, f"dswx_area_{district}.csv")
    return pd.read_csv(path, parse_dates=["date"]) if os.path.exists(path) else None


@functools.lru_cache(maxsize=None)
def kc_table():
    return pd.read_csv(rc.KC_TABLE_PATH)


@functools.lru_cache(maxsize=256)
def smap_recovery(district, flood_date):
    return pf.smap_days_to_normal(smap(district), flood_date)


def crop_src(crop):
    """Provenance for a crop's calendar numbers: weather, the calendar method
    and every cited row (window, stage timing, thresholds)."""
    spec = crop_specs()[crop]
    cites = list((spec["window"] or {}).get("citations", []))
    for stage in spec["stage_days"].values():
        cites += stage["citations"]
    if spec["base_temp"] is not None:
        cites += spec["base_temp"]["citations"]
    for hazard in spec["hazards"]:
        cites += hazard["threshold"]["citations"]
    return list(dict.fromkeys(["imerg", "power", "calendar"] + ref_ids_for(cites)))


def water_src(crop, district):
    """Provenance for irrigation / stress numbers: weather, FAO-56 and the
    cited crop, soil (and paddy) rows."""
    param_crop = WATER_PARAM_CROP.get(crop, crop)
    items = [f"{param_crop}.{k}" for k in ("zr_min_m", "zr_max_m", "p")]
    items += [f"{district}.{k}" for k in ("theta_fc_m3m3", "theta_wp_m3m3", "clay_pct")]
    ids = ref_ids_for_items(items)
    if rc.WATER_MODEL[crop] == "paddy":
        rows = reference_rows()
        ids += list(dict.fromkeys(rows[rows["file"] == "paddy_params.csv"]["ref_id"]))
    return list(dict.fromkeys(["imerg", "power", "fao56"] + ids))


def window_src(crop):
    spec = crop_specs()[crop]
    return list(dict.fromkeys(ref_ids_for((spec["window"] or {}).get("citations", []))))


# ---------------- envelope ----------------

def envelope(endpoint, request, data, narration=None, notices=(), errors=(),
             provenance=None, is_mock=False, source_mode=SOURCE_MODE):
    data = _clean(data)
    return {
        "api_version": API_VERSION,
        "endpoint": endpoint,
        "request": {k: v for k, v in request.items() if v is not None},
        "generated_at": _dt.datetime.now(BD_TZ).isoformat(timespec="seconds"),
        "source_mode": source_mode,
        "is_mock": is_mock,
        "data": data,
        "narration": narration,
        "provenance": provenance if provenance is not None else
        (build_provenance(data) if data is not None else []),
        "notices": list(notices),
        "errors": list(errors),
    }


def error_response(endpoint, request, err):
    body = envelope(endpoint, request, None,
                    errors=[{"code": err.code, "en": err.en, "bn": err.bn}])
    return JSONResponse(body, status_code=err.status)


def narrate(texts, fallback, data):
    """texts / fallback: {"en", "bn", "sms_en", "sms_bn"}. Every number in
    texts must be found in the cited data (guard()); if one is not, the
    number-free fallback is used instead."""
    texts = {k: v[:160] if k.startswith("sms") else v for k, v in texts.items()}
    tool_result = {"data": _clean(data), "sources": build_provenance(_clean(data))}
    passed = all(guard(texts[k], [tool_result])["passed"] for k in texts)
    chosen = texts if passed else fallback
    return {**chosen,
            "narrator": "template",
            "provenance_check": "passed" if passed else "blocked_fallback_used"}


def respond(endpoint, request, build):
    """Runs build() -> (data, narration, notices); any failure becomes the
    error envelope, never an empty body."""
    try:
        data, narration, notices = build()
        return JSONResponse(envelope(endpoint, request, data, narration, notices))
    except ApiError as err:
        return error_response(endpoint, request, err)
    except Exception as exc:  # noqa: BLE001 - the contract forbids an empty body
        return error_response(endpoint, request, ApiError(
            "INTERNAL", f"Internal error: {type(exc).__name__}"))


# ---------------- parameter checks ----------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_district(district):
    if not district:
        raise ApiError("BAD_PARAMETER", "Missing parameter: district.", "district")
    if district.lower() not in DISTRICTS:
        raise ApiError("DISTRICT_NOT_COVERED",
                       "Survey Crops covers Cumilla, Feni, Brahmanbaria, Noakhali and Sylhet for now.")
    return district.lower()


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def nearest_district(lat, lon):
    """(district id, km) of the nearest district point in district_metadata.csv."""
    meta = district_metadata()
    dists = {d: haversine_km(lat, lon, float(meta.loc[d, "latitude"]), float(meta.loc[d, "longitude"]))
             for d in DISTRICTS}
    did = min(dists, key=dists.get)
    return did, dists[did]


def _coord(name, value, low, high):
    try:
        out = float(value)
    except ValueError:
        raise ApiError("BAD_PARAMETER", f"{name} must be a number, got {value!r}.", name) from None
    if not (low <= out <= high) or math.isnan(out):
        raise ApiError("BAD_PARAMETER", f"{name} must be between {low} and {high}.", name)
    return out


def resolve_location(district, lat, lon):
    """-> (district id, location block or None). With lat & lon, the backend
    (never the website) picks the nearest district point, up to
    district_max_distance_km away; lat/lon win over `district`."""
    if lat in (None, "") and lon in (None, ""):
        return check_district(district), None
    if lat in (None, "") or lon in (None, ""):
        raise ApiError("BAD_PARAMETER", "Give both lat and lon, or neither.", "lat/lon")
    la, lo = _coord("lat", lat, -90, 90), _coord("lon", lon, -180, 180)
    did, km = nearest_district(la, lo)
    limit = API_ASSUMPTIONS["district_max_distance_km"]["value"]
    if km > limit:
        raise ApiError("DISTRICT_NOT_COVERED",
                       f"This point is {km:.0f} km from the nearest Survey Crops district point "
                       f"({DISTRICTS[did]['en']}). Survey Crops covers points within {limit} km of "
                       f"Cumilla, Feni, Brahmanbaria, Noakhali and Sylhet.")
    return did, {"lat": round(la, 5), "lon": round(lo, 5), "district": did,
                 "district_name": DISTRICTS[did],
                 "distance_km": measure(km, "km to the district point", ["assumptions"], 1),
                 "method": {"en": f"Nearest of the 5 Survey Crops district points (within {limit} km).",
                            "bn": f"সার্ভে ক্রপস-এর ৫টি জেলা-বিন্দুর মধ্যে সবচেয়ে কাছেরটি "
                                  f"({bn_digits(limit)} কিমির মধ্যে)।"}}


def check_date(name, value, required=True):
    if value is None or value == "":
        if required:
            raise ApiError("BAD_PARAMETER", f"Missing parameter: {name} (YYYY-MM-DD).", name)
        return None
    try:
        if not DATE_RE.match(value):
            raise ValueError
        return pd.Timestamp(value)
    except ValueError:
        raise ApiError("BAD_PARAMETER", f"{name} must be a date written YYYY-MM-DD, got {value!r}.",
                       name) from None


def check_crop(crop, allowed=rc.CROPS):
    if not crop:
        raise ApiError("BAD_PARAMETER", "Missing parameter: crop.", "crop")
    if crop not in allowed:
        raise ApiError("BAD_PARAMETER", f"Unknown crop {crop!r}; choose one of {', '.join(allowed)}.",
                       "crop")
    return crop


def check_lang(lang):
    if lang not in ("en", "bn"):
        raise ApiError("BAD_PARAMETER", "lang must be 'en' or 'bn'.", "lang")
    return lang


WATER_LEVELS = ("rain_only", "limited", "regular", "plenty")
LIMITED_MAX_WATERINGS = 2     # "limited" = 1-2 irrigations a season (the farmer's own answer)

WATER_SAME_NOTICE = {"level": "info",
                     "en": "'Regular' and 'plenty' are treated the same: there is no sourced "
                           "pump-capacity number to tell them apart.",
                     "bn": "'নিয়মিত' ও 'প্রচুর' একইভাবে ধরা হয়েছে: দুটির পার্থক্য করার মতো "
                           "উৎসসহ পাম্প-ক্ষমতার সংখ্যা নেই।"}
WATERING_DEFINITION = {"level": "info",
                       "en": "A watering is one day the FAO-56 water balance refills the root "
                             "zone (for boro rice: land preparation plus each pond top-up). "
                             "Pump capacity is not modelled.",
                       "bn": "একবার সেচ মানে FAO-56 পানি-হিসাবে একটি দিন যেদিন শিকড়-অঞ্চল আবার "
                             "ভরতে হয় (বোরো ধানে: জমি তৈরি ও প্রতিবার পানি ভরা)। পাম্পের ক্ষমতা "
                             "হিসাবে নেই।"}


WATER_NARRATION = {
    "limited_none": {"en": "You said one or two irrigations, but every crop needs more than two "
                           "waterings in the worst 20% of years; see the reason under each crop.",
                     "bn": "আপনি এক বা দুইবার সেচের কথা বলেছেন, কিন্তু সবচেয়ে খারাপ ২০% বছরে "
                           "প্রতিটি ফসলে দুইবারের বেশি সেচ লাগে; প্রতিটি ফসলের নিচের কারণ দেখুন।"},
    "rain_only": {"en": "You said rain only, so crops are ranked by days of dry soil in the "
                        "worst 20% of years.",
                  "bn": "আপনি শুধু বৃষ্টির কথা বলেছেন, তাই সবচেয়ে খারাপ ২০% বছরের শুকনো মাটির "
                        "দিন দিয়ে ফসল সাজানো হয়েছে।"},
    "limited": {"en": "You said one or two irrigations, so crops needing more than two "
                      "waterings are left out.",
                "bn": "আপনি এক বা দুইবার সেচের কথা বলেছেন, তাই দুইবারের বেশি সেচ লাগে এমন "
                      "ফসল বাদ দেওয়া হয়েছে।"},
}
# (old, new) ending of data["method"] for rain_only
WATER_METHOD_TAIL = {
    "en": ("then by irrigation need in the worst 20% of years.",
           "then by days of dry soil without irrigation in the worst 20% of years."),
    "bn": ("বছরের সেচ দিয়ে সাজানো।", "বছরের সেচ ছাড়া শুকনো মাটির দিন দিয়ে সাজানো।"),
}


def check_water(water):
    if water is not None and water not in WATER_LEVELS:
        raise ApiError("BAD_PARAMETER",
                       "water must be one of: " + ", ".join(WATER_LEVELS) + ".", "water")
    return water


def _value(m):
    return None if m is None else m["value"]


def apply_water(ranked, filtered, water):
    """Applies the farmer's water answer to the ranked options.
    Returns (ranked, filtered, notices). None / 'regular' / 'plenty' leave the
    ranking untouched; 'rain_only' re-ranks by rainfed stress days in the worst
    20% of years; 'limited' moves crops needing more than 2 waterings (worst
    20% of years) to filtered_out as NEEDS_MORE_WATER."""
    notices = []
    if water in ("regular", "plenty"):
        return ranked, filtered, [WATER_SAME_NOTICE]
    if water == "rain_only":
        def stress(o):
            v = _value(o["water_stress_days_worst20"])
            return float("inf") if v is None else v
        ranked = sorted(ranked, key=lambda o: (_value(o["problem_years"]) or 0, stress(o)))
        for i, o in enumerate(ranked, 1):
            o["rank"] = i
            o["why"] = {"en": "Ranked by problem years first, then by days of dry soil without "
                              "irrigation in the worst 20% of years.",
                        "bn": "প্রথমে সমস্যার বছরের সংখ্যা, তারপর সেচ ছাড়া সবচেয়ে খারাপ ২০% "
                              "বছরের শুকনো মাটির দিন দিয়ে সাজানো।"}
            days, events = _value(o["water_stress_days_worst20"]), _value(o["irrigation_events_worst20"])
            if (days or 0) > 0 or (days is None and (events or 0) > 0):
                o["water_note"] = {"code": "NEEDS_IRRIGATION",
                                   "en": "Needs irrigation: with rain only, the soil was too dry "
                                         "in the worst 20% of years.",
                                   "bn": "সেচ লাগবে: শুধু বৃষ্টিতে সবচেয়ে খারাপ ২০% বছরে মাটি "
                                         "খুব শুকনো ছিল।"}
        return ranked, filtered, [WATERING_DEFINITION]
    if water == "limited":
        kept, dropped = [], []
        for o in ranked:
            events = _value(o["irrigation_events_worst20"])
            (dropped if events is not None and events > LIMITED_MAX_WATERINGS else kept).append(o)
        for o in dropped:
            n = round(_value(o["irrigation_events_worst20"]))
            o.update({"rank": None, "feasible": False, "why": None,
                      "reason_code": "NEEDS_MORE_WATER",
                      "reason": {"en": f"Needs more water than you can give: about {n} waterings "
                                       f"in the worst 20% of years, and you can give "
                                       f"{LIMITED_MAX_WATERINGS}.",
                                 "bn": f"আপনার দেওয়া সেচের চেয়ে বেশি লাগে: সবচেয়ে খারাপ ২০% বছরে "
                                       f"প্রায় {bn_digits(n)} বার সেচ, আপনি দিতে পারবেন "
                                       f"{bn_digits(LIMITED_MAX_WATERINGS)} বার।"}})
        for i, o in enumerate(kept, 1):
            o["rank"] = i
        return kept, filtered + dropped, [WATERING_DEFINITION]
    return ranked, filtered, notices


PRIORITIES = ("risk", "water", "more", "soil")
PRIORITY_LABELS = {
    "risk": {"en": "lowest risk", "bn": "সবচেয়ে কম ঝুঁকি"},
    "water": {"en": "saving water", "bn": "পানি বাঁচানো"},
    "more": {"en": "more crops a year (earliest harvest)", "bn": "বছরে বেশি ফসল (আগে ফসল তোলা)"},
    "soil": {"en": "healthier soil (legumes first)", "bn": "মাটির স্বাস্থ্য (ডাল জাতীয় ফসল আগে)"},
}
SOIL_PENDING_NOTICE = {"level": "info",
                       "en": "Healthier soil: research pending: legume source. No cited reference "
                             "row says which crops are legumes, so this priority changes nothing yet.",
                       "bn": "মাটির স্বাস্থ্য: গবেষণা চলছে: ডাল জাতীয় ফসলের উৎস। কোন ফসল ডাল জাতীয় "
                             "তা বলে এমন কোনো উৎসসহ সারি এখনও নেই, তাই এই পছন্দ এখনও কিছু বদলায় না।"}


def check_priority(priority):
    """'water,risk' -> ['water', 'risk'] (tap order, duplicates dropped). None or '' -> []."""
    if priority is None or not priority.strip():
        return []
    keys = [k.strip() for k in priority.split(",") if k.strip()]
    if any(k not in PRIORITIES for k in keys):
        raise ApiError("BAD_PARAMETER",
                       "priority must be a comma list of: " + ", ".join(PRIORITIES) + ".", "priority")
    return list(dict.fromkeys(keys))


@functools.lru_cache(maxsize=1)
def reference_all():
    return load_reference()


def legume_crops():
    """{crop: [reference items]} for crops with a kept data/reference row
    '<crop>.soil.legume' whose value is 'yes'. Empty until the team adds rows."""
    ref = reference_all()
    out = {}
    for crop in CROP_NAMES:
        item = f"{crop}.soil.legume"
        if any(str(v).strip().lower() == "yes" for v in ref.rows(item)["text"]):
            out[crop] = [item]
    return out


def ranked_by_block(keys, src=()):
    parts = [PRIORITY_LABELS[k] for k in keys]
    return {"keys": list(keys),
            "text": {"en": "Ranked by: " + ", then ".join(p["en"] for p in parts),
                     "bn": "সাজানো হয়েছে: " + ", তারপর ".join(p["bn"] for p in parts)},
            "src": list(src)}


def apply_priority(ranked, priority):
    """Re-sorts the ranked options by the farmer's priorities.
    Returns (ranked, ranked_by, notices). Nothing changes for no priority or
    'risk' alone. Each key is a tuple of numbers we already compute; the
    tapped keys' tuples are joined in tap order, then crop name. Crops whose
    sowing window has passed stay last. Risk is never hidden: if the new top
    crop has more problem years than the best crop under 'risk', a caution
    names both numbers."""
    keys = list(priority)
    notices = []
    legumes = {}
    if "soil" in keys:
        legumes = legume_crops()
        if not legumes:
            keys.remove("soil")
            notices.append(SOIL_PENDING_NOTICE)
    if not keys:
        keys = ["risk"]
    if keys == ["risk"]:
        return ranked, ranked_by_block(keys), notices
    inf = float("inf")

    def num(m):
        v = _value(m)
        return inf if v is None else v

    def part(k, o):
        if k == "risk":
            return (num(o["problem_years"]), num(o["irrigation_need_worst20"]))
        if k == "water":
            return (num(o["irrigation_need_worst20"]), num(o["problem_years"]))
        if k == "more":
            return (o["maturity_date"]["date"] if o["maturity_date"] else "9999-12-31",
                    num(o["problem_years"]))
        return (0 if o["crop"] in legumes else 1,)

    def key(o):
        late = (o["window_status"] or {}).get("code") == "late"
        return (late,) + tuple(x for k in keys for x in part(k, o)) + (o["crop"],)

    baseline = ranked[0] if ranked else None
    ranked = sorted(ranked, key=key)
    block = ranked_by_block(keys)
    for i, o in enumerate(ranked, 1):
        o["rank"] = i
        o["why"] = {"en": block["text"]["en"] + ".", "bn": block["text"]["bn"] + "।"}
    if baseline and ranked and num(ranked[0]["problem_years"]) > num(baseline["problem_years"]):
        top, safe = ranked[0], baseline
        t, b = _fmt(top["problem_years"]["value"]), _fmt(safe["problem_years"]["value"])
        notices.append({"level": "caution",
                        "en": f"Your top pick, {top['crop_name']['en']}, had problems in {t} years; "
                              f"the lowest-risk crop, {safe['crop_name']['en']}, had problems in "
                              f"{b} years.",
                        "bn": f"আপনার প্রথম পছন্দ {top['crop_name']['bn']}-এ {bn_digits(t)} বছর সমস্যা "
                              f"হয়েছে; সবচেয়ে কম ঝুঁকির ফসল {safe['crop_name']['bn']}-এ "
                              f"{bn_digits(b)} বছর।"})
    src = ref_ids_for_items([i for c in legumes.values() for i in c]) if "soil" in keys else []
    return ranked, ranked_by_block(keys, src), notices


def district_notices(district):
    notices = [AREA_NOTICE]
    if district in POWER_CELL_SHARED:
        notices.append(SHARED_CELL_NOTICE)
    return notices


# ---------------- soil (field point, SoilGrids) ----------------

TEXTURE_BN = {"sand": "বেলে", "loamy sand": "বেলে-দোআঁশ", "sandy loam": "বেলে দোআঁশ",
              "loam": "দোআঁশ", "silt loam": "পলি দোআঁশ", "silt": "পলি",
              "sandy clay loam": "বেলে এঁটেল দোআঁশ", "clay loam": "এঁটেল দোআঁশ",
              "silty clay loam": "পলি এঁটেল দোআঁশ", "sandy clay": "বেলে এঁটেল",
              "silty clay": "পলি এঁটেল", "clay": "এঁটেল"}
SOIL_TEST_ADVICE = {"en": "Test your soil at the Upazila Agriculture Office.",
                    "bn": "উপজেলা কৃষি অফিসে মাটি পরীক্ষা করান।"}
SOIL_RANKING_NOTICE = {"level": "info",
                       "en": "The crop ranking still uses the district's soil, not this field estimate.",
                       "bn": "ফসলের তালিকা এখনও জেলার মাটির তথ্য ব্যবহার করে, এই জমির অনুমান নয়।"}


def texture_block(name):
    if name is None:
        return None
    return {"value": name, "en": name.capitalize(), "bn": TEXTURE_BN[name]}


def district_soil_fallback(did):
    """Sand/silt/clay of the district from data/reference/soil_params.csv
    (SoilGrids, 0-100 cm, a few points around the district). No pH or carbon."""
    params = load_soil_params()[did]
    src = ref_ids_for_items([f"{did}.{k}" for k in ("clay_pct", "silt_pct", "sand_pct")])
    sand, silt, clay = (float(params[k]) for k in ("sand_pct", "silt_pct", "clay_pct"))
    return {"estimate": "district", "depth": "0-100 cm",
            "texture": texture_block(usda_texture(sand, silt, clay)),
            "sand_pct": measure(sand, "%", src, 1), "silt_pct": measure(silt, "%", src, 1),
            "clay_pct": measure(clay, "%", src, 1),
            "ph": measure(None, "pH (H2O)", src), "organic_carbon_g_kg": measure(None, "g/kg", src)}


def field_soil_block(soil):
    src = ["soilgrids"]
    return {"estimate": "field", "depth": "0-30 cm",
            "texture": texture_block(soil["texture"]),
            "sand_pct": measure(soil["sand"], "%", src, 1), "silt_pct": measure(soil["silt"], "%", src, 1),
            "clay_pct": measure(soil["clay"], "%", src, 1),
            "ph": measure(soil["ph"], "pH (H2O)", src, 1),
            "organic_carbon_g_kg": measure(soil["organic_carbon_g_kg"], "g/kg", src, 1)}


def soil_fallback_notice(why):
    reasons = {"no_value": ("SoilGrids has no value for this exact point (for example a river, "
                            "pond or built-up cell).",
                            "এই বিন্দুর জন্য SoilGrids-এ কোনো মান নেই (যেমন নদী, পুকুর বা বসতি)।"),
               "unreachable": ("SoilGrids did not answer in time.",
                               "SoilGrids সময়মতো উত্তর দেয়নি।")}[why]
    return {"level": "caution",
            "en": f"{reasons[0]} Showing the district soil instead (0-100 cm, texture only).",
            "bn": f"{reasons[1]} তাই জেলার মাটির তথ্য দেখানো হচ্ছে (০-১০০ সেমি, শুধু ধরন)।"}


# ---------------- crop option (advisory, post-flood) ----------------

def hazard_label(hazard):
    return {"en": rc.hazard_label(hazard).capitalize(), "bn": HAZARD_BN.get(hazard, hazard)}


def checked_text(hazards):
    if not hazards:
        return {"en": "No risk could be checked yet (no sourced threshold).",
                "bn": "এখনও কোনো ঝুঁকি যাচাই করা যায়নি (উৎসসহ সীমা নেই)।"}
    return {"en": "Checked: " + ", ".join(rc.hazard_label(h) for h in hazards) + ".",
            "bn": "যাচাই করা হয়েছে: " + ", ".join(HAZARD_BN.get(h, h) for h in hazards) + "।"}


def problem_line(option, n):
    """{en, bn} line that always says what was checked, e.g. "Problems in 0 of
    24 years. Checked: night heat." (never implies safety beyond that)."""
    if option["sowing_date"] is None:
        return {"en": "No sowing date: more than 4 weeks past the recommended window.",
                "bn": "বপনের তারিখ নেই: সুপারিশকৃত সময় পেরিয়ে ৪ সপ্তাহের বেশি।"}
    checked = checked_text(option["hazards_checked"])
    if not option["hazards_checked"]:
        return {"en": f"Not assessed. {checked['en']}", "bn": f"মূল্যায়ন হয়নি। {checked['bn']}"}
    py = _fmt(option["problem_years"])
    return {"en": f"Problems in {py} of {n} years. {checked['en']}",
            "bn": f"{bn_digits(n)} বছরের মধ্যে {bn_digits(py)} বছরে সমস্যা। {checked['bn']}"}


def aman_block(result):
    """The "aman transplanting still possible" note rotation_options() may add
    (task 6c), as {crop, crop_name, sowing_window, problem_line, coverage_notice};
    None when absent. It carries no risk numbers."""
    option = next((o for o in result["options"] if o["crop"] == "aman_rice"), None)
    if option is None:
        return None
    year = pd.Timestamp(result["earliest_sowing_date"]).year
    start, end = (tuple(int(x) for x in s.split("-")) for s in option["window"].split(" to "))
    src = list(dict.fromkeys(ref_ids_for(option.get("citations", [])))) or ["calendar"]
    end_date = str(pd.Timestamp(year, *end).date())
    return {
        "crop": "aman_rice",
        "crop_name": {"en": "Aman rice", "bn": "আমন ধান"},
        "sowing_window": {"start": str(pd.Timestamp(year, *start).date()), "end": end_date,
                          "src": src},
        "problem_line": {"en": option["problem_line"],
                         "bn": f"আমন রোপণ এখনও সম্ভব, {bn_digits(end_date)} পর্যন্ত। আমনের "
                               f"আবহাওয়া-ঝুঁকি এখনও যাচাই করা হয়নি।"},
        "coverage_notice": {"en": "Weather risk for aman is not assessed yet.",
                            "bn": "আমনের আবহাওয়া-ঝুঁকি এখনও যাচাই করা হয়নি।"},
    }


def threshold_measure(hazard_spec, src):
    value = rc.threshold_value(hazard_spec, "onset")
    if hazard_spec["kind"] == "waterlog":
        unit = "saturated days in a row (first 30 days after sowing)"
    elif hazard_spec["variable"] == "temp_max_c":
        unit = "°C daily maximum, problem above"
    elif hazard_spec["op"] == ">":
        unit = "°C daily minimum (night), problem above"
    else:
        unit = "°C daily minimum, problem below"
    return measure(value, unit, src, decimals=1)


def window_status(option):
    if option["sowing_date"] is None:
        return {"code": "closed",
                "en": "More than 4 weeks past the recommended sowing window.",
                "bn": "সুপারিশকৃত বপনের সময় পেরিয়ে ৪ সপ্তাহের বেশি হয়ে গেছে।"}
    if option["outside_recommended_window"]:
        return {"code": "late", "en": "After the recommended sowing window (late sowing).",
                "bn": "সুপারিশকৃত বপনের সময়ের পরে (দেরিতে বপন)।"}
    return {"code": "open", "en": "Inside the recommended sowing window.",
            "bn": "সুপারিশকৃত বপনের সময়ের মধ্যে।"}


def calendar_row(district, crop, sowing_date):
    if sowing_date is None:
        return None
    cal = calendar_table()
    mmdd = sowing_date[5:]
    rows = cal[(cal["district"] == district) & (cal["crop"] == crop) & (cal["sowing_mmdd"] == mmdd)]
    return None if rows.empty else rows.iloc[0]


def crop_option(district, option, season):
    """One rotation_options() option in the contract's shape."""
    crop = option["crop"]
    spec = crop_specs()[crop]
    csrc, wsrc = crop_src(crop), water_src(crop, district)
    row = calendar_row(district, crop, option["sowing_date"])
    n = option["n_years"]
    years_unit = f"of {n} years" if n else "years"
    start, end = (tuple(int(x) for x in s.split("-")) for s in option["window"].split(" to "))
    hazards = []
    for h in spec["hazards"]:
        hit = None if row is None else row.get(f"problem_years_{h['hazard']}")
        hazards.append({"hazard": h["hazard"], "label": hazard_label(h["hazard"]),
                        "years_hit": measure(hit, years_unit, csrc),
                        "threshold": threshold_measure(h, csrc)})
    stress_w20 = None if row is None else row.get("stress_days_worst20")
    maturity = option["maturity_date"]
    return {
        "rank": option["rank"],
        "crop": crop,
        "crop_name": CROP_NAMES[crop],
        "feasible": option["rank"] is not None,
        "sowing_date": ({"date": option["sowing_date"], "src": csrc}
                        if option["sowing_date"] else None),
        "sowing_window": {"start": str(rc.season_date(season, *start).date()),
                          "end": str(rc.season_date(season, *end).date()),
                          "src": window_src(crop) or csrc},
        "window_status": window_status(option),
        "problem_years": measure(option["problem_years"], years_unit, csrc),
        "problem_line": problem_line(option, n),
        "hazards_checked": hazards,
        "hazards_checked_text": checked_text(option["hazards_checked"]),
        "hazards_missing": [{"hazard": h, "label": hazard_label(h)} for h in option["hazards_missing"]],
        "coverage_notice": NOT_ALL_CHECKED if option["hazards_missing"] else None,
        "main_risks": [{"hazard": h["hazard"], "label": h["label"], "years_hit": h["years_hit"]}
                       for h in hazards if (h["years_hit"]["value"] or 0) > 0],
        "irrigation_need_avg": measure(option["irrigation_mm_mean"], "mm", wsrc),
        "irrigation_need_worst20": measure(option["irrigation_mm_worst20"], "mm", wsrc),
        "water_stress_days_avg": measure(option["stress_days_mean"], "days", wsrc),
        "water_stress_days_worst20": measure(stress_w20, "days", wsrc),
        "irrigation_events_avg": measure(None if row is None else row.get("irrigation_events_mean"),
                                         "waterings", wsrc, decimals=1),
        "irrigation_events_worst20": measure(
            None if row is None else row.get("irrigation_events_worst20"),
            "waterings", wsrc, decimals=1),
        "water_note": None,
        "maturity_date": {"date": maturity, "src": csrc} if maturity else None,
        "why": ({"en": "Ranked by problem years first, then by irrigation need in the worst "
                       "20% of years.",
                 "bn": "প্রথমে সমস্যার বছরের সংখ্যা, তারপর সবচেয়ে খারাপ ২০% বছরের সেচের "
                       "প্রয়োজন দিয়ে সাজানো।"} if option["rank"] else None),
    }


def split_options(district, result):
    """rotation_options() result -> (ranked options, filtered_out).

    Skips the "aman_rice" note rotation_options() may add at the front of
    options (task 6c): it is not one of app.py's CROP_NAMES crops and
    carries no risk numbers, so it is not rendered through the per-crop
    contract shape here yet."""
    season = result.get("season", rc.season_of(pd.Timestamp(result["earliest_sowing_date"])))
    ranked, filtered = [], []
    for option in result["options"]:
        if option["crop"] not in CROP_NAMES:
            continue
        item = crop_option(district, option, season)
        if option["rank"] is not None:
            ranked.append(item)
        elif option["sowing_date"] is None:
            filtered.append({**item, "reason_code": "TOO_LATE",
                             "reason": {"en": "Too late: more than 4 weeks past the recommended "
                                              "sowing window.",
                                        "bn": "অনেক দেরি: সুপারিশকৃত বপনের সময় পেরিয়ে ৪ সপ্তাহের "
                                              "বেশি।"}})
        else:
            filtered.append({**item, "reason_code": "NO_RISK_CHECKED",
                             "reason": {"en": "Not ranked: none of its risks can be checked yet "
                                              "(no sourced threshold and stage timing).",
                                        "bn": "র‍্যাঙ্ক করা হয়নি: এর কোনো ঝুঁকি এখনও যাচাই করা "
                                              "যায় না (উৎসসহ সীমা নেই)।"}})
    return ranked, filtered


def coverage_notices(options):
    partial = [o["crop_name"] for o in options if o["coverage_notice"]]
    if not partial:
        return []
    return [{"level": "caution",
             "en": "Not all risks are checked yet for: " + ", ".join(p["en"] for p in partial) + ".",
             "bn": "এই ফসলগুলোর সব ঝুঁকি এখনও যাচাই করা হয়নি: "
                   + ", ".join(p["bn"] for p in partial) + "।"}]


def earliest_block(result, flood_ready_used):
    if flood_ready_used:
        basis = {"en": "The post-flood ready date (soil back to normal and flood water gone).",
                 "bn": "বন্যার পর জমি প্রস্তুতের তারিখ (মাটি স্বাভাবিক ও পানি নেমে গেছে)।"}
        src = ["smap", "opera", "post_flood"]
    else:
        days = rc.TURNAROUND_DAYS
        basis = {"en": f"Harvest date + {days} days to prepare the field (Survey Crops assumption).",
                 "bn": f"ফসল কাটার তারিখ + জমি তৈরির {bn_digits(days)} দিন (সার্ভে ক্রপস অনুমান)।"}
        src = ["assumptions"]
    return {"date": result["earliest_sowing_date"], "basis": basis, "src": src}


# ---------------- the app ----------------

def warm_caches():
    """Loads everything slow once, so requests stay under ~3 s."""
    calendar_table()
    crop_specs()
    reference_rows()
    fixed_provenance()
    for district in DISTRICTS:
        weather(district)
        smap(district)
    for district in ("feni", "cumilla", "noakhali", "brahmanbaria"):
        soil = smap_recovery(district, "2024-08-21")   # the Aug-2024 demo flood
        smap_curve_block(district, pd.Timestamp("2024-08-21"), soil["normal_date"])


@asynccontextmanager
async def lifespan(_app):
    warm_caches()
    yield


app = FastAPI(title="Survey Crops API", version=API_VERSION, lifespan=lifespan,
              description="Which crop to plant and when, with NASA data as the evidence. "
                          "Every number carries its source. Records of past seasons, not forecasts.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                   allow_headers=["*"])


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    err = ApiError("BAD_PARAMETER", f"Unknown endpoint or method: {request.method} {request.url.path}.",
                   request.url.path)
    body = envelope(request.url.path, dict(request.query_params), None,
                    errors=[{"code": err.code, "en": err.en, "bn": err.bn}])
    return JSONResponse(body, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return error_response(request.url.path, dict(request.query_params),
                          ApiError("BAD_PARAMETER", f"Bad parameter: {exc.errors()[0].get('msg', '')}"))


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    return error_response(request.url.path, dict(request.query_params),
                          ApiError("INTERNAL", f"Internal error: {type(exc).__name__}"))


# ---------------- /districts ----------------

@app.get("/api/v1/districts")
def districts():
    endpoint = "/api/v1/districts"

    def build():
        meta = district_metadata()
        crops = sorted(calendar_table()["crop"].unique())
        out = []
        for did, name in DISTRICTS.items():
            has_dswx = dswx_area(did) is not None
            out.append({
                "id": did, "name": name,
                "lat": float(meta.loc[did, "latitude"]), "lon": float(meta.loc[did, "longitude"]),
                "power_cell_shared_with": POWER_CELL_SHARED.get(did, []),
                "covered": True,
                "coverage": {
                    "rain": {"period": fixed_provenance()["imerg"]["period"], "src": ["imerg"]},
                    "temperature": {"period": fixed_provenance()["power"]["period"], "src": ["power"]},
                    "soil_moisture": {"period": fixed_provenance()["smap"]["period"], "src": ["smap"]},
                    "flood_maps": ({"period": fixed_provenance()["opera"]["period"], "src": ["opera"]}
                                   if has_dswx else None),
                    "risk_calendar_crops": crops,
                },
            })
        notices = [AREA_NOTICE, SHARED_CELL_NOTICE,
                   {"level": "info",
                    "en": "Radar flood maps (OPERA DSWx-S1) exist from 2024-08-21 and not for Sylhet.",
                    "bn": "রাডার বন্যা-মানচিত্র (OPERA DSWx-S1) ২০২৪-০৮-২১ থেকে আছে, সিলেটের জন্য নেই।"}]
        return {"districts": out}, None, notices

    return respond(endpoint, {}, build)


# ---------------- /advisory ----------------

@app.get("/api/v1/advisory")
def advisory(district: str = None, prev_harvest: str = None, flood_ready: str = None,
             lang: str = "en", lat: str = None, lon: str = None, water: str = None,
             priority: str = None):
    endpoint = "/api/v1/advisory"
    request = {"district": district, "prev_harvest": prev_harvest, "flood_ready": flood_ready,
               "lang": lang, "lat": lat, "lon": lon, "water": water, "priority": priority}

    def build():
        did, location = resolve_location(district, lat, lon)
        harvest = check_date("prev_harvest", prev_harvest)
        ready = check_date("flood_ready", flood_ready, required=False)
        check_lang(lang)
        check_water(water)
        keys = check_priority(priority)
        result = rc.rotation_options(did, harvest, ready, calendar=calendar_table())
        ranked, filtered = split_options(did, result)
        ranked, filtered, water_notices = apply_water(ranked, filtered, water)
        ranked, ranked_by, priority_notices = apply_priority(ranked, keys)
        n = next((o["n_years"] for o in result["options"] if o["n_years"]), len(rc.SEASONS))
        data = {
            "district": did,
            "prev_harvest": str(harvest.date()),
            "flood_ready": str(ready.date()) if ready is not None else None,
            "earliest_sowing_date": earliest_block(result, result["basis"].startswith("post-flood")),
            "years_used": measure(n, "years", ["imerg", "power", "calendar"]),
            "method": {"en": f"Each crop was checked at its sowing date against every season "
                             f"{rc.SEASONS[0]}-{rc.SEASONS[-1]} of NASA data for the area around "
                             f"your field (IMERG rain, POWER temperature). Ranked by problem years, "
                             f"then by irrigation need in the worst 20% of years.",
                       "bn": f"{bn_digits(rc.SEASONS[0])}-{bn_digits(rc.SEASONS[-1])} সালের প্রতিটি "
                             f"মৌসুমের NASA তথ্যে (IMERG বৃষ্টি, POWER তাপমাত্রা) প্রতিটি ফসল তার "
                             f"বপনের তারিখে যাচাই করা হয়েছে। সমস্যার বছর, তারপর সবচেয়ে খারাপ ২০% "
                             f"বছরের সেচ দিয়ে সাজানো।"},
            "ranked_by": ranked_by,
            "options": ranked,
            "filtered_out": filtered,
            "aman_option": aman_block(result),
            "location": location,
        }
        name = DISTRICTS[did]
        earliest = result["earliest_sowing_date"]
        if ranked:
            top = ranked[0]
            py, irr, irr_w = (top["problem_years"]["value"], top["irrigation_need_avg"]["value"],
                              top["irrigation_need_worst20"]["value"])
            crop_en, crop_bn = top["crop_name"]["en"], top["crop_name"]["bn"]
            sow = top["sowing_date"]["date"]
            chk = top["hazards_checked_text"]
            extra_en = f" {NOT_ALL_CHECKED['en']}" if top["coverage_notice"] else ""
            extra_bn = f" {NOT_ALL_CHECKED['bn']}" if top["coverage_notice"] else ""
            texts = {
                "en": f"{name['en']}: earliest sowing {earliest}. Top-ranked: {crop_en} sown {sow} "
                      f"had problems in {_fmt(py)} of {n} years here. {chk['en']}{extra_en} "
                      f"Irrigation need about {_fmt(irr)} mm on average, {_fmt(irr_w)} mm in the "
                      f"worst 20% of years.",
                "bn": f"{name['bn']}: সবচেয়ে আগে বপন {bn_digits(earliest)}। প্রথম পছন্দ: {crop_bn}, "
                      f"{bn_digits(sow)} তারিখে বুনলে এখানে {bn_digits(n)} বছরের মধ্যে "
                      f"{bn_digits(_fmt(py))} বছরে সমস্যা হয়েছে। {chk['bn']}{extra_bn} সেচ: গড়ে প্রায় "
                      f"{bn_digits(_fmt(irr))} মিমি, সবচেয়ে খারাপ ২০% বছরে {bn_digits(_fmt(irr_w))} মিমি।",
                "sms_en": f"{crop_en} sow {sow}: problems {_fmt(py)}/{n} yrs, irrigation "
                          f"~{_fmt(irr)} mm (NASA data, past years).",
                "sms_bn": f"{crop_bn} বপন {bn_digits(sow)}: {bn_digits(n)} বছরে {bn_digits(_fmt(py))} "
                          f"বছর সমস্যা, সেচ ~{bn_digits(_fmt(irr))} মিমি (NASA)।",
            }
        else:
            texts = {"en": f"{name['en']}: earliest sowing {earliest}. No crop can be ranked for "
                           f"this date.",
                     "bn": f"{name['bn']}: সবচেয়ে আগে বপন {bn_digits(earliest)}। এই তারিখে কোনো "
                           f"ফসল র‍্যাঙ্ক করা যায়নি।",
                     "sms_en": f"{name['en']}: no crop can be ranked for {earliest}.",
                     "sms_bn": f"{name['bn']}: {bn_digits(earliest)} তারিখে কোনো ফসল র‍্যাঙ্ক করা যায়নি।"}
        if ranked_by["keys"] != ["risk"]:
            for lk, tail, end in (("en", "Ranked by problem years, then by irrigation need in the worst 20% of years.", "."),
                                  ("bn", "সমস্যার বছর, তারপর সবচেয়ে খারাপ ২০% বছরের সেচ দিয়ে সাজানো।", "।")):
                data["method"][lk] = data["method"][lk].replace(tail, ranked_by["text"][lk] + end)
        texts["en"] += " " + ranked_by["text"]["en"] + "."
        texts["bn"] += " " + ranked_by["text"]["bn"] + "।"
        water_text = WATER_NARRATION.get(water)
        if water == "limited" and not ranked and any(
                o.get("reason_code") == "NEEDS_MORE_WATER" for o in filtered):
            water_text = WATER_NARRATION["limited_none"]
        if water_text:
            texts["en"] += " " + water_text["en"]
            texts["bn"] += " " + water_text["bn"]
            if water == "rain_only":
                data["method"] = {
                    k: v.replace(WATER_METHOD_TAIL[k][0], WATER_METHOD_TAIL[k][1])
                    for k, v in data["method"].items()}
        fallback = {"en": "See the ranked crop options below; each number shows its NASA source.",
                    "bn": "নিচে র‍্যাঙ্ক করা ফসলগুলো দেখুন; প্রতিটি সংখ্যার NASA উৎস দেওয়া আছে।",
                    "sms_en": "Survey Crops: see crop options (NASA data).",
                    "sms_bn": "সার্ভে ক্রপস: ফসলের তালিকা দেখুন (NASA তথ্য)।"}
        notices = (district_notices(did) + [PAST_NOT_FORECAST, WEATHER_RISKS_ONLY]
                   + water_notices + priority_notices + coverage_notices(ranked + filtered))
        if not ranked and filtered and all(o["sowing_date"] is None for o in filtered):
            notices.append(ALL_WINDOWS_PASSED)
        return data, narrate(texts, fallback, data), notices

    return respond(endpoint, request, build)


# ---------------- /risk-calendar ----------------

@app.get("/api/v1/soil")
def soil(district: str = None, lat: str = None, lon: str = None):
    endpoint = "/api/v1/soil"
    request = {"district": district, "lat": lat, "lon": lon}
    mode = {"value": SOURCE_MODE}

    def build():
        did, location = resolve_location(district, lat, lon)
        notices = []
        block = None
        if location is not None:
            try:
                found, mode["value"] = soilgrids_point.soil_at(location["lat"], location["lon"])
            except soilgrids_point.SoilGridsError:
                found, why = None, "unreachable"
            else:
                why = "no_value"
            if found is not None:
                block = field_soil_block(found)
            else:
                notices.append(soil_fallback_notice(why))
        if block is None:
            block = district_soil_fallback(did)
            mode["value"] = "fixture"
        notices.append(SOIL_RANKING_NOTICE)
        data = {"district": did, "location": location, "soil": block,
                "test_advice": SOIL_TEST_ADVICE}
        return data, None, notices

    try:
        data, narration, notices = build()
        return JSONResponse(envelope(endpoint, request, data, narration, notices,
                                     source_mode=mode["value"]))
    except ApiError as err:
        return error_response(endpoint, request, err)
    except Exception as exc:  # noqa: BLE001 - the contract forbids an empty body
        return error_response(endpoint, request, ApiError(
            "INTERNAL", f"Internal error: {type(exc).__name__}"))


@app.get("/api/v1/risk-calendar")
def risk_calendar(district: str = None, crop: str = None):
    endpoint = "/api/v1/risk-calendar"
    request = {"district": district, "crop": crop}

    def build():
        did = check_district(district)
        c = check_crop(crop)
        cal = calendar_table()
        rows = cal[(cal["district"] == did) & (cal["crop"] == c)].sort_values("sow_offset")
        if rows.empty:
            raise ApiError("NO_DATA_FOR_PERIOD", f"No risk calendar rows for {c} in {did}.")
        spec = crop_specs()[c]
        csrc, wsrc = crop_src(c), water_src(c, did)
        n = int(rows["n_years"].max())
        years_unit = f"years with the problem (of {n})"
        checked = [h["hazard"] for h in spec["hazards"]]
        missing = [m["hazard"] for m in spec["missing"] if m["hazard"] != "(all)"]

        def col(name, decimals=0):
            if name not in rows:
                return [None] * len(rows)
            return [_num(v, decimals) for v in rows[name]]

        first = rows.iloc[0]
        data = {
            "district": did, "crop": c, "crop_name": CROP_NAMES[c],
            "years_used": measure(n, "years", ["imerg", "power", "calendar"]),
            "recommended_window": {"start": first["window_start"], "end": first["window_end"],
                                   "format": "MM-DD", "method": first["window_method"],
                                   "src": window_src(c) or csrc},
            "hazards": [{"id": h["hazard"], "label": hazard_label(h["hazard"]),
                         "threshold": threshold_measure(h, csrc)} for h in spec["hazards"]],
            "hazards_checked_text": checked_text(checked),
            "hazards_missing": [{"hazard": h, "label": hazard_label(h)} for h in missing],
            "coverage_notice": NOT_ALL_CHECKED if missing else None,
            "sowing_dates": list(rows["sowing_mmdd"]),
            "sowing_dates_format": "MM-DD, the same day in every season",
            "grid": {
                "src": csrc, "unit": years_unit,
                "rows": [{"hazard": h, "years_hit": col(f"problem_years_{h}")} for h in checked],
                "any_problem_years": col("problem_years"),
                "n_years": col("n_years"),
                "outside_window": [bool(v) for v in rows["outside_window"]],
            },
            "water": {
                "src": wsrc,
                "units": {"irrigation_mm_mean": "mm", "irrigation_mm_worst20": "mm",
                          "stress_days_mean": "days", "stress_days_worst20": "days"},
                "irrigation_mm_mean": col("irrigation_mm_mean"),
                "irrigation_mm_worst20": col("irrigation_mm_worst20"),
                "stress_days_mean": col("stress_days_mean"),
                "stress_days_worst20": col("stress_days_worst20"),
            },
            "sensitivity": {
                "src": csrc + ["assumptions"], "unit": years_unit,
                "rows": [
                    {"id": "hot1", "label": {"en": "Problem year if 1 day crosses the threshold (main: 3)",
                                             "bn": "১ দিন সীমা পার হলেই সমস্যার বছর (মূল: ৩)"},
                     "any_problem_years": col("problem_years_hot1")},
                    {"id": "hot5", "label": {"en": "Problem year only if 5 days cross it (main: 3)",
                                             "bn": "৫ দিন সীমা পার হলে সমস্যার বছর (মূল: ৩)"},
                     "any_problem_years": col("problem_years_hot5")},
                    {"id": "severe", "label": {"en": "Severe end of the cited threshold range",
                                               "bn": "উৎসের সীমার তীব্র প্রান্ত"},
                     "any_problem_years": col("problem_years_severe")},
                    {"id": "window15", "label": {"en": "Flowering window 15 days wide (main: 7)",
                                                 "bn": "ফুল ফোটার সময়কাল ১৫ দিন (মূল: ৭)"},
                     "any_problem_years": col("problem_years_window15")},
                ],
            },
        }
        name, cname = DISTRICTS[did], CROP_NAMES[c]
        values = [v for v in data["grid"]["any_problem_years"] if v is not None]
        d0, d1 = data["sowing_dates"][0], data["sowing_dates"][-1]
        if values and checked:
            lo, hi = min(values), max(values)
            extra_en = f" {NOT_ALL_CHECKED['en']}" if missing else ""
            extra_bn = f" {NOT_ALL_CHECKED['bn']}" if missing else ""
            chk = checked_text(checked)
            texts = {
                "en": f"{cname['en']} in {name['en']}, sown between {d0} and {d1} (month-day): "
                      f"problems in {lo} to {hi} of {n} years. {chk['en']}{extra_en}",
                "bn": f"{name['bn']}-এ {cname['bn']}, {bn_digits(d0)} থেকে {bn_digits(d1)} (মাস-দিন) "
                      f"বুনলে {bn_digits(n)} বছরের মধ্যে {bn_digits(lo)} থেকে {bn_digits(hi)} বছরে "
                      f"সমস্যা। {chk['bn']}{extra_bn}",
                "sms_en": f"{cname['en']} {d0} to {d1}: problems in {lo}-{hi} of {n} yrs (NASA data).",
                "sms_bn": f"{cname['bn']} {bn_digits(d0)} থেকে {bn_digits(d1)}: {bn_digits(n)} বছরে "
                          f"{bn_digits(lo)}-{bn_digits(hi)} বছর সমস্যা (NASA)।",
            }
        else:
            texts = {"en": f"{cname['en']} in {name['en']}: no risk can be checked yet (no sourced "
                           f"threshold). {NOT_ALL_CHECKED['en']}",
                     "bn": f"{name['bn']}-এ {cname['bn']}: এখনও কোনো ঝুঁকি যাচাই করা যায়নি। "
                           f"{NOT_ALL_CHECKED['bn']}",
                     "sms_en": f"{cname['en']}: risks not checked yet.",
                     "sms_bn": f"{cname['bn']}: ঝুঁকি এখনও যাচাই হয়নি।"}
        fallback = {"en": "See the calendar below; each number shows its NASA source.",
                    "bn": "নিচের ক্যালেন্ডার দেখুন; প্রতিটি সংখ্যার NASA উৎস দেওয়া আছে।",
                    "sms_en": "Survey Crops: see the sowing calendar (NASA data).",
                    "sms_bn": "সার্ভে ক্রপস: বপন ক্যালেন্ডার দেখুন (NASA তথ্য)।"}
        notices = district_notices(did) + [PAST_NOT_FORECAST, WEATHER_RISKS_ONLY]
        if missing:
            notices.append({"level": "caution", **NOT_ALL_CHECKED})
        return data, narrate(texts, fallback, data), notices

    return respond(endpoint, request, build)


# ---------------- /post-flood ----------------

def flood_water(district, flood):
    """OPERA DSWx-S1 flood-area curve and recession, or None with a reason."""
    area = dswx_area(district)
    reason = None
    if area is None:
        reason = {"en": "No radar flood maps (OPERA DSWx-S1) were fetched for this district.",
                  "bn": "এই জেলার জন্য রাডার বন্যা-মানচিত্র (OPERA DSWx-S1) নেই।"}
    else:
        lead = pd.Timedelta(days=API_ASSUMPTIONS["dswx_scene_lead_days"]["value"])
        lag = pd.Timedelta(days=API_ASSUMPTIONS["dswx_peak_max_lag_days"]["value"])
        if area["date"].min() > flood + lag or area["date"].max() < flood:
            reason = {"en": f"Radar flood maps here cover {area['date'].min().date()} to "
                            f"{area['date'].max().date()} only.",
                      "bn": f"এখানে রাডার বন্যা-মানচিত্র শুধু {bn_digits(area['date'].min().date())} "
                            f"থেকে {bn_digits(area['date'].max().date())} পর্যন্ত আছে।"}
    if reason is not None:
        return {"available": False, "reason": reason}, {}

    floor_rows = area[(area["date"] >= "2025-01-01") & (area["date"] <= "2025-03-31")]
    floor = float(floor_rows["flood_fraction"].median()) if len(floor_rows) else 0.0
    used = area[area["date"] >= flood - lead]
    try:
        rec = pf.flood_recession(used, value_col="flood_fraction", baseline=floor)
    except ValueError:   # no scene with enough of the area observed
        return {"available": False,
                "reason": {"en": "No radar pass saw enough of the area after this date.",
                           "bn": "এই তারিখের পর কোনো রাডার পাসে এলাকার যথেষ্ট অংশ দেখা যায়নি।"}}, {}
    peak_date = pd.Timestamp(rec["peak_date"])
    conclusive = peak_date <= flood + lag
    peak_km2 = float(used.loc[used["date"] == peak_date, "flood_km2"].iloc[0])
    src = ["opera", "post_flood"]
    block = {
        "available": True,
        "conclusive": bool(conclusive),
        "peak_date": rec["peak_date"],
        "peak_flood_share": measure(rec["peak_value"] * 100, "% of the observed area", src, 1),
        "peak_flood_area": measure(peak_km2, "km²", src, 1),
        "dry_season_floor": measure(floor * 100, "% of the observed area", src + ["assumptions"], 2),
        "water_gone_date": rec["water_gone_date"] if conclusive else None,
        "days_peak_to_gone": measure(rec["days_peak_to_gone"] if conclusive else None, "days", src),
        "scenes_used": measure(rec["scenes_used"], "scenes", src),
        "rule": {"en": "Flood water 'mostly gone' when 90% of the water above the dry-season floor "
                       "has drained, for 2 radar passes in a row.",
                 "bn": "শুকনো মৌসুমের স্তরের উপরের ৯০% পানি টানা ২টি রাডার পাসে নেমে গেলে "
                       "'পানি প্রায় নেমে গেছে'।"},
        "resolution_note": {"en": "30 m water maps from radar (sees through clouds).",
                            "bn": "৩০ মিটার রাডার পানি-মানচিত্র (মেঘ ভেদ করে দেখে)।"},
        "curve": {
            "src": ["opera"],
            "units": {"flood_km2": "km²", "flood_pct": "% of the observed area",
                      "observed_pct": "% of the 20 km area seen that day"},
            "rows": [{"date": str(r.date.date()), "flood_km2": _num(r.flood_km2, 1),
                      "flood_pct": _num(r.flood_fraction * 100, 1),
                      "observed_pct": _num(r.valid_fraction * 100, 0),
                      "used": bool(r.valid_fraction >= 0.5)} for r in used.itertuples()],
        },
    }
    if not conclusive:
        block["reason"] = {"en": "The largest water reading comes long after this flood (seasonal "
                                 "water, not this flood), so only soil moisture is used.",
                           "bn": "সবচেয়ে বেশি পানি এই বন্যার অনেক পরে দেখা গেছে (মৌসুমি পানি), "
                                 "তাই শুধু মাটির আর্দ্রতা ব্যবহার করা হয়েছে।"}
        return block, {}
    return block, rec


@functools.lru_cache(maxsize=64)
def smap_curve(district, start, end):
    curve = pf.smap_recovery_curve(smap(district), start, end)
    return [{"date": str(r.date.date()), "value": _num(r.value, 3), "threshold": _num(r.threshold, 3)}
            for r in curve.itertuples()]


def smap_curve_block(district, flood, normal_date):
    """SMAP root-zone moisture and its 'normal' threshold, from 2 weeks before
    the flood to 3 weeks after the soil was back to normal (or 90 days)."""
    series = smap(district)
    end = pd.Timestamp(normal_date) if normal_date else flood + pd.Timedelta(days=90)
    end = min(end + pd.Timedelta(days=21), series.index.max())
    start = max(flood - pd.Timedelta(days=14), series.index.min())
    return {"src": ["smap", "post_flood"],
            "units": {"value": "m³/m³ root-zone soil moisture (SMAP)",
                      "threshold": "m³/m³, 80th percentile of other years (same dates ±15 days)"},
            "rows": smap_curve(district, str(start.date()), str(end.date()))}


def image_files():
    """Whitelist: only docs/results/img/dswx_flood_<district>_<date>.png."""
    if not os.path.isdir(IMAGE_DIR):
        return {}
    return {name: IMAGE_RE.match(name).groups() for name in sorted(os.listdir(IMAGE_DIR))
            if IMAGE_RE.match(name)}


def flood_images(district, flood):
    """OPERA DSWx-S1 flood-water maps for this district from 7 days before to
    120 days after the flood date, served by /api/v1/images/{name}."""
    name = DISTRICTS[district]
    out = []
    for fname, (did, date) in image_files().items():
        day = pd.Timestamp(date)
        if did != district or not (flood - pd.Timedelta(days=7) <= day <= flood + pd.Timedelta(days=120)):
            continue
        out.append({"date": date, "path": f"/api/v1/images/{fname}", "src": ["opera"],
                    "caption": {"en": f"Flood water seen by radar (NASA OPERA DSWx-S1) around the "
                                      f"{name['en']} point, {date}.",
                                "bn": f"রাডারে দেখা বন্যার পানি (NASA OPERA DSWx-S1), {name['bn']} "
                                      f"বিন্দুর চারপাশে, {bn_digits(date)}।"}})
    return out


@app.get("/api/v1/post-flood")
def post_flood(district: str = None, flood_date: str = None, lang: str = "en",
               lat: str = None, lon: str = None):
    endpoint = "/api/v1/post-flood"
    request = {"district": district, "flood_date": flood_date, "lang": lang, "lat": lat, "lon": lon}

    def build():
        did, location = resolve_location(district, lat, lon)
        flood = check_date("flood_date", flood_date)
        check_lang(lang)
        series = smap(did)
        hold = pd.Timedelta(days=7)
        if flood < series.index.min() or flood > series.index.max() - hold:
            raise ApiError("NO_DATA_FOR_PERIOD",
                           f"SMAP soil moisture here covers {series.index.min().date()} to "
                           f"{series.index.max().date()}; flood_date must fall inside it.",
                           f"SMAP {series.index.min().date()} – {series.index.max().date()}")
        soil = smap_recovery(did, str(flood.date()))
        water, water_result = flood_water(did, flood)
        earliest = pf.earliest_sowing_date(soil, water_result)
        ssrc = ["smap", "post_flood"]
        sensitivity = [{"pct": int(k.split("_")[1]), "date": v["date"],
                        "days_after_flood": measure(v["days"], "days", ssrc)}
                       for k, v in soil["sensitivity"].items()]
        data = {
            "district": did,
            "flood_date": str(flood.date()),
            "water_on_ground": water,
            "soil_back_to_normal": {
                "date": soil["normal_date"],
                "days_after_flood": measure(soil["days_to_normal"], "days", ssrc),
                "rule": {"en": "Root-zone moisture at or below the 80th percentile of other years "
                               "(same dates ±15 days) for 7 days in a row.",
                         "bn": "টানা ৭ দিন মাটির আর্দ্রতা অন্য বছরের একই সময়ের (±১৫ দিন) "
                               "৮০তম শতাংশের সমান বা নিচে।"},
                "sensitivity": sensitivity,
                "curve": smap_curve_block(did, flood, soil["normal_date"]),
            },
            "earliest_sowing_date": ({"date": earliest,
                                      "basis": {"en": "The later of: soil back to normal, flood "
                                                      "water mostly gone.",
                                                "bn": "মাটি স্বাভাবিক হওয়া ও পানি নেমে যাওয়া—এর "
                                                      "মধ্যে যেটি পরে।"},
                                      "src": ssrc + (["opera"] if water_result else [])}
                                     if earliest else None),
            "still_possible": [], "no_longer_possible": [], "cascade": None,
            "flood_images": flood_images(did, flood),
            "location": location,
            "soil_test_advice": {"en": "Satellites cannot measure soil pH or nutrients. Get a soil "
                                       "test at SRDI or your Upazila Agriculture Office.",
                                 "bn": "স্যাটেলাইট মাটির pH বা পুষ্টি মাপতে পারে না। SRDI বা "
                                       "উপজেলা কৃষি অফিসে মাটি পরীক্ষা করান।"},
        }
        notices = district_notices(did) + [PAST_NOT_FORECAST]
        if not water["available"] or not water.get("conclusive", True):
            notices.append({"level": "caution", **water["reason"]})
        if earliest:
            # The flood -> planting chain: re-read the risk calendar at the ready date.
            after = rc.rotation_options(did, flood, earliest, calendar=calendar_table())
            at_once = rc.rotation_options(did, flood, calendar=calendar_table())
            ranked, filtered = split_options(did, after)
            data["still_possible"] = ranked + [o for o in filtered if o["reason_code"] != "TOO_LATE"]
            data["no_longer_possible"] = [o for o in filtered if o["reason_code"] == "TOO_LATE"]
            before = {o["crop"]: o for o in at_once["options"]}
            rows = []
            for o in after["options"]:
                if o["crop"] not in CROP_NAMES or o["crop"] not in before:
                    continue                     # e.g. the aman_rice note: not one of app.py's crops
                b = before[o["crop"]]
                csrc = crop_src(o["crop"])
                unit_a = f"of {o['n_years']} years" if o["n_years"] else "years"
                unit_b = f"of {b['n_years']} years" if b["n_years"] else "years"
                if o["sowing_date"] is None and b["sowing_date"] is not None:
                    change = {"code": "lost", "en": "The flood delay closes this crop's window.",
                              "bn": "বন্যার দেরিতে এই ফসলের সময় শেষ।"}
                elif (o["problem_years"] or 0) > (b["problem_years"] or 0):
                    change = {"code": "riskier", "en": "More problem years at the later date.",
                              "bn": "দেরিতে বুনলে সমস্যার বছর বেশি।"}
                elif o["sowing_date"] == b["sowing_date"] or o["problem_years"] == b["problem_years"]:
                    change = {"code": "same", "en": "No change in problem years.",
                              "bn": "সমস্যার বছরে পরিবর্তন নেই।"}
                else:
                    change = {"code": "fewer", "en": "Fewer problem years at the later date.",
                              "bn": "দেরিতে বুনলে সমস্যার বছর কম।"}
                rows.append({
                    "crop": o["crop"], "crop_name": CROP_NAMES[o["crop"]],
                    "if_ready_at_once": {"sowing_date": b["sowing_date"],
                                         "problem_years": measure(b["problem_years"], unit_b, csrc)},
                    "after_flood": {"sowing_date": o["sowing_date"],
                                    "problem_years": measure(o["problem_years"], unit_a, csrc)},
                    "change": change,
                    "coverage_notice": NOT_ALL_CHECKED if o["hazards_missing"] else None,
                })
            if not data["still_possible"] and data["no_longer_possible"]:
                notices.append(ALL_WINDOWS_PASSED)
            data["cascade"] = {
                "src": ["calendar", "smap", "post_flood"],
                "compares": {"en": f"Sowing if the field were ready {rc.TURNAROUND_DAYS} days after "
                                   f"the flood vs sowing at the post-flood ready date.",
                             "bn": f"বন্যার {bn_digits(rc.TURNAROUND_DAYS)} দিন পরে জমি প্রস্তুত হলে "
                                   f"বনাম বন্যার পর প্রকৃত প্রস্তুতের তারিখে বপন।"},
                "rows": rows}
            notices += [WEATHER_RISKS_ONLY] + coverage_notices(data["still_possible"])
        else:
            notices.append({"level": "caution",
                            "en": "Soil moisture or flood water did not return to normal in the "
                                  "data, so no sowing date can be given.",
                            "bn": "তথ্যে মাটির আর্দ্রতা বা পানি স্বাভাবিক হয়নি, তাই বপনের তারিখ "
                                  "দেওয়া যাচ্ছে না।"})

        name = DISTRICTS[did]
        days = data["soil_back_to_normal"]["days_after_flood"]["value"]
        fd = data["flood_date"]
        if earliest and days is not None:
            gone = water.get("water_gone_date")
            water_en = f" Flood water (radar) mostly gone by {gone}." if gone else ""
            water_bn = f" বন্যার পানি (রাডার) {bn_digits(gone)} নাগাদ প্রায় নেমে গেছে।" if gone else ""
            top = data["still_possible"][0] if data["still_possible"] else None
            top_en = top_bn = ""
            if top and top["rank"]:
                top_en = (f" Top-ranked then: {top['crop_name']['en']}, problems in "
                          f"{_fmt(top['problem_years']['value'])} {top['problem_years']['unit']}.")
                top_bn = (f" তখন প্রথম পছন্দ: {top['crop_name']['bn']}, "
                          f"{bn_digits(top['problem_years']['unit'].split()[1])} বছরের মধ্যে "
                          f"{bn_digits(_fmt(top['problem_years']['value']))} বছরে সমস্যা।")
            texts = {
                "en": f"After the flood of {fd}, SMAP soil moisture near {name['en']} took {days} days "
                      f"to return to normal.{water_en} Earliest sowing date: {earliest}.{top_en}",
                "bn": f"{bn_digits(fd)} বন্যার পর {name['bn']} এলাকায় SMAP মাটির আর্দ্রতা স্বাভাবিক হতে "
                      f"{bn_digits(days)} দিন লেগেছে।{water_bn} সবচেয়ে আগে বপন: {bn_digits(earliest)}।{top_bn}",
                "sms_en": f"Flood {fd}: soil normal after {days} days; sow from {earliest} (NASA data).",
                "sms_bn": f"বন্যা {bn_digits(fd)}: মাটি {bn_digits(days)} দিনে স্বাভাবিক; বপন "
                          f"{bn_digits(earliest)} থেকে (NASA)।",
            }
        else:
            texts = {"en": f"After the flood of {fd}, soil moisture near {name['en']} did not return "
                           f"to normal in the data.",
                     "bn": f"{bn_digits(fd)} বন্যার পর {name['bn']} এলাকায় মাটির আর্দ্রতা তথ্যে "
                           f"স্বাভাবিক হয়নি।",
                     "sms_en": f"Flood {fd}: soil not back to normal in the data.",
                     "sms_bn": f"বন্যা {bn_digits(fd)}: তথ্যে মাটি স্বাভাবিক হয়নি।"}
        fallback = {"en": "See the recovery clock below; each number shows its NASA source.",
                    "bn": "নিচে পুনরুদ্ধারের হিসাব দেখুন; প্রতিটি সংখ্যার NASA উৎস দেওয়া আছে।",
                    "sms_en": "Survey Crops: see flood recovery (NASA data).",
                    "sms_bn": "সার্ভে ক্রপস: বন্যার পর জমির অবস্থা দেখুন (NASA)।"}
        return data, narrate(texts, fallback, data), notices

    return respond(endpoint, request, build)


# ---------------- /field-twin ----------------

def stage_names(crop, days):
    kc = kc_table()
    rows = kc[kc["crop"] == crop]
    names = []
    for stage, length in zip(rows["stage"], rows["length_days"]):
        names += [stage] * int(length)
    return (names + [names[-1]] * days)[:days]


def twin_heat_limits(crop):
    """The crop's sourced temperature thresholds (from data/reference via the
    risk calendar), used only to flag weeks in the replay."""
    csrc = crop_src(crop)
    out = []
    for h in crop_specs()[crop]["hazards"]:
        if h["kind"] != "temp":
            continue
        event = ("heat" if h["variable"] == "temp_max_c" else
                 "night_heat" if h["op"] == ">" else "cold")
        out.append({"event": event, "hazard": h["hazard"], "label": hazard_label(h["hazard"]),
                    "limit": threshold_measure(h, csrc)})
    return out


def twin_week_events(row, heat_limits):
    """Adds row["events"]: what to animate this week, most important first."""
    events = []
    for lim in heat_limits:
        v = lim["limit"]["value"]
        if lim["event"] == "heat" and (row["tmax_highest_c"] or 0) > v:
            events.append("heat")
        elif lim["event"] == "night_heat" and (row["tmin_mean_c"] or 0) > v:
            events.append("night_heat")
        elif lim["event"] == "cold" and row["tmin_mean_c"] is not None and row["tmin_mean_c"] < v:
            events.append("cold")
    if (row["rain_mm"] or 0) >= API_ASSUMPTIONS["twin_rain_week_mm"]["value"]:
        events.append("rain")
    if row["stress_days"] > 0:
        events.append("dry")
    if row["irrigation_events"] > 0:
        events.append("irrigation")
    row["events"] = list(dict.fromkeys(events)) or ["ok"]
    return row


def twin_events_rule(heat_limits):
    return {
        "text": {"en": "Week flags for the replay: heat = hottest day above the crop's heat "
                       "threshold; night heat / cold = mean night temperature past the threshold; "
                       "rain = a wet week; dry = rainfed water-stress days; irrigation = a "
                       "watering was needed.",
                 "bn": "সাপ্তাহিক চিহ্ন: গরম = সবচেয়ে গরম দিন ফসলের সীমার উপরে; রাতের গরম / ঠান্ডা = "
                       "রাতের গড় তাপমাত্রা সীমা পেরিয়েছে; বৃষ্টি = ভেজা সপ্তাহ; শুকনো = সেচ ছাড়া "
                       "পানির অভাবের দিন; সেচ = পানি দিতে হয়েছে।"},
        "thresholds": heat_limits,
        "rain_week": measure(API_ASSUMPTIONS["twin_rain_week_mm"]["value"], "mm/week",
                             ["imerg", "assumptions"]),
    }


@app.get("/api/v1/field-twin")
def field_twin(district: str = None, year: str = None, crop: str = None, sow_date: str = None,
               lang: str = "en", lat: str = None, lon: str = None):
    endpoint = "/api/v1/field-twin"
    request = {"district": district, "year": year, "crop": crop, "sow_date": sow_date, "lang": lang,
               "lat": lat, "lon": lon}

    def build():
        did, location = resolve_location(district, lat, lon)
        if crop == "boro_rice":
            raise ApiError("BAD_PARAMETER", "The week-by-week replay for boro rice (ponded paddy) is "
                                            "not built yet.", "crop")
        c = check_crop(crop, FIELD_TWIN_CROPS)
        sow = check_date("sow_date", sow_date)
        check_lang(lang)
        yr = sow.year
        if year not in (None, ""):
            if not re.fullmatch(r"\d{4}", year):
                raise ApiError("BAD_PARAMETER", "year must be a 4-digit year.", "year")
            if int(year) not in (sow.year, rc.season_of(sow)):
                raise ApiError("BAD_PARAMETER", f"year {year} does not match sow_date {sow_date}.",
                               "year")
            yr = int(year)
        w = weather(did)
        try:
            result = crop_season(c, sow, w, kc_table(), load_crop_params(), load_soil_params()[did])
        except ValueError as err:
            if "does not cover" not in str(err):
                raise
            raise ApiError("NO_DATA_FOR_PERIOD",
                           f"IMERG rain covers {w.index.min().date()} to {w.index.max().date()}; "
                           f"a {c} season sown {sow.date()} (with the soil spin-up from 1 August) "
                           f"does not fit inside it.",
                           f"IMERG {w.index.min().date()} – {w.index.max().date()}") from None
        daily, irrigated = result["daily"], result["daily_irrigated"]
        season_w = w.loc[daily.index]
        stages = stage_names(c, len(daily))
        heat_limits = twin_heat_limits(c)
        sm = smap(did)["sm_rootzone"]
        sm_season = sm.reindex(daily.index)
        smap_ok = bool(sm_season.notna().any())
        weeks = []
        for start in range(0, len(daily), 7):
            sl = slice(start, start + 7)
            d, irr, ww = daily.iloc[sl], irrigated.iloc[sl], season_w.iloc[sl]
            weeks.append(twin_week_events({
                "week_start": str(d.index[0].date()), "days": len(d),
                "stage": stages[start],
                "rain_mm": _num(d["rain_mm"].sum(), 1),
                "et0_mm": _num(d["et0_mm"].sum(), 1),
                "crop_demand_mm": _num(d["etc_mm"].sum(), 1),
                "tmax_mean_c": _num(ww["temp_max_c"].mean(), 1),
                "tmax_highest_c": _num(ww["temp_max_c"].max(), 1),
                "tmin_mean_c": _num(ww["temp_min_c"].mean(), 1),
                "soil_water_pct": _num(d["relative_soil_water"].iloc[-1] * 100),
                "stress_days": int(d["water_stress"].sum()),
                "irrigation_mm": _num(irr["irrigation_mm"].sum(), 1),
                "irrigation_events": int((irr["irrigation_mm"] > 0).sum()),
                "smap_rootzone_m3m3": (_num(sm_season.iloc[sl].mean(), 3)
                                       if sm_season.iloc[sl].notna().any() else None),
            }, heat_limits))
        wsrc = water_src(c, did)
        data = {
            "district": did, "year": yr, "crop": c, "crop_name": CROP_NAMES[c],
            "sow_date": str(sow.date()), "season_end": result["harvest_date"],
            "location": location,
            "is_forecast": False,
            "label": {"en": f"What happened in {yr} with NASA data for the area around your field. "
                            f"An example, not a forecast.",
                      "bn": f"{bn_digits(yr)} সালে আপনার জমির আশেপাশের এলাকার NASA তথ্যে যা ঘটেছিল। "
                            f"উদাহরণ, পূর্বাভাস নয়।"},
            "weekly": {
                "src": wsrc + (["smap"] if smap_ok else []),
                "units": {"rain_mm": "mm/week", "et0_mm": "mm/week", "crop_demand_mm": "mm/week",
                          "tmax_mean_c": "°C", "tmax_highest_c": "°C", "tmin_mean_c": "°C",
                          "soil_water_pct": "% of available water, end of week (rainfed)",
                          "stress_days": "days/week (rainfed)",
                          "irrigation_mm": "mm/week (if irrigated)",
                          "irrigation_events": "waterings/week (if irrigated)",
                          "smap_rootzone_m3m3": "m³/m³ (SMAP, for comparison)"},
                "events_rule": twin_events_rule(heat_limits),
                "rows": weeks,
            },
            "totals": {
                "irrigation_need": measure(result["net_irrigation_mm"], "mm", wsrc),
                "irrigation_events": measure(result["irrigation_events"], "waterings", wsrc),
                "stress_days": measure(result["water_stress_days"], "days", wsrc),
                "rain": measure(result["rain_mm"], "mm", ["imerg"]),
                "crop_water_demand": measure(result["etc_mm"], "mm", ["power", "fao56"]),
            },
            "smap_check": {
                "available": smap_ok,
                "note": {"en": "SMAP root-zone soil moisture is shown next to the model for "
                               "comparison. Its rain input is corrected to IMERG, so it is not an "
                               "independent check.",
                         "bn": "তুলনার জন্য SMAP মাটির আর্দ্রতা দেখানো হয়েছে। এর বৃষ্টির তথ্য "
                               "IMERG দিয়ে সংশোধিত, তাই এটি স্বাধীন যাচাই নয়।"}
                if smap_ok else {"en": "SMAP starts 2015-03-31, so there is no soil-moisture "
                                       "comparison for this season.",
                                 "bn": "SMAP ২০১৫-০৩-৩১ থেকে শুরু, তাই এই মৌসুমে তুলনা নেই।"},
            },
        }
        t = data["totals"]
        name, cname = DISTRICTS[did], CROP_NAMES[c]
        irr, ev, st, rain = (t["irrigation_need"]["value"], t["irrigation_events"]["value"],
                             t["stress_days"]["value"], t["rain"]["value"])
        sd = data["sow_date"]
        texts = {
            "en": f"An example from a past year, not a forecast: {cname['en']} sown {sd} in the "
                  f"{name['en']} area. Without irrigation it had {st} water-stress days; keeping it "
                  f"out of stress needed about {irr} mm of irrigation "
                  f"({ev} watering{'' if ev == 1 else 's'}). Season "
                  f"rain: {rain} mm.",
            "bn": f"অতীতের একটি উদাহরণ, পূর্বাভাস নয়: {name['bn']} এলাকায় {bn_digits(sd)} তারিখে বোনা "
                  f"{cname['bn']}। সেচ ছাড়া {bn_digits(st)} দিন পানির অভাব ছিল; তা এড়াতে {bn_digits(ev)} "
                  f"বারে প্রায় {bn_digits(irr)} মিমি সেচ লেগেছিল। মৌসুমের বৃষ্টি: {bn_digits(rain)} মিমি।",
            "sms_en": f"{yr} replay: {cname['en']} sown {sd} needed ~{irr} mm irrigation "
                      f"(example, not forecast).",
            "sms_bn": f"{bn_digits(yr)}: {bn_digits(sd)} বোনা {cname['bn']}-এ ~{bn_digits(irr)} মিমি সেচ "
                      f"লেগেছিল (উদাহরণ)।",
        }
        fallback = {"en": "A past-year example, not a forecast. See the weekly table below.",
                    "bn": "অতীতের উদাহরণ, পূর্বাভাস নয়। নিচের সাপ্তাহিক তালিকা দেখুন।",
                    "sms_en": "Survey Crops: past-year example (not a forecast).",
                    "sms_bn": "সার্ভে ক্রপস: অতীতের উদাহরণ (পূর্বাভাস নয়)।"}
        notices = district_notices(did) + [{"level": "info",
                                            "en": "An example from a past year, never a forecast.",
                                            "bn": "অতীতের একটি উদাহরণ, কখনও পূর্বাভাস নয়।"}]
        return data, narrate(texts, fallback, data), notices

    return respond(endpoint, request, build)


# ---------------- images and the root ----------------

@app.get("/api/v1/images/{name}")
def images(name: str):
    """NASA OPERA DSWx-S1 flood maps made in docs/results/img/ (whitelist only)."""
    if name not in image_files():
        return error_response("/api/v1/images", {"name": name},
                              ApiError("NO_DATA_FOR_PERIOD", f"No such image: {name!r}.", name))
    return FileResponse(os.path.join(IMAGE_DIR, name), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


# ---------------- not built yet: serve the mock ----------------

NOT_BUILT = {"level": "caution",
             "en": "Not built yet: this is example data (MOCK DATA), not a result.",
             "bn": "এখনও তৈরি হয়নি: এটি উদাহরণ তথ্য (মক ডেটা), ফলাফল নয়।"}


def mock_response(name, endpoint, request):
    with open(os.path.join(MOCK_DIR, f"{name}.json"), encoding="utf-8") as f:
        body = json.load(f)
    body.update({"endpoint": endpoint, "request": request, "is_mock": True,
                 "generated_at": _dt.datetime.now(BD_TZ).isoformat(timespec="seconds")})
    body["notices"] = [NOT_BUILT] + list(body.get("notices", []))
    return JSONResponse(body)


@app.get("/api/v1/enso-lens")
def enso_lens(request: Request):
    return mock_response("enso_lens", "/api/v1/enso-lens", dict(request.query_params))


@app.get("/api/v1/warnings")
def warnings(request: Request):
    return mock_response("warnings", "/api/v1/warnings", dict(request.query_params))


@app.post("/api/v1/ask")
async def ask(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return mock_response("ask", "/api/v1/ask", body if isinstance(body, dict) else {})
