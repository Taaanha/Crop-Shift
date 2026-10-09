"""
src/acquire/soilgrids_point.py

Topsoil (0-30 cm) at one field point from ISRIC SoilGrids 2.0 (250 m, a
model estimate from soil surveys + satellite data, not a lab test), for
GET /api/v1/soil.

One REST call returns sand / silt / clay / pH(H2O) / organic carbon at the
three topsoil depths (0-5, 5-15, 15-30 cm). They are thickness-weighted into
one 0-30 cm value, and sand/silt/clay are classified into a USDA texture
class. Answers are cached on disk (soil does not change from day to day).

SoilGrids masks some cells (towns, rivers, water): the API then returns null.
That is reported as "no value" (None), never guessed.
"""

import json
import os

import requests

from fetch_soilgrids import usda_texture

URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
PROPERTIES = ("sand", "silt", "clay", "phh2o", "soc")
DEPTHS = {"0-5cm": 5, "5-15cm": 10, "15-30cm": 15}   # label -> layer thickness (cm)
TIMEOUT_S = 10

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE_DIR = os.path.join(ROOT, "data", "processed", "soilgrids_cache")


class SoilGridsError(Exception):
    """SoilGrids did not give a usable answer (timeout, HTTP error, bad JSON)."""


def cache_path(lat, lon, cache_dir=None):
    return os.path.join(cache_dir or CACHE_DIR, f"{lat:.3f}_{lon:.3f}.json")


def parse_response(payload):
    """SoilGrids JSON -> {"sand","silt","clay" (%), "ph", "organic_carbon_g_kg",
    "texture"}, each 0-30 cm thickness-weighted. None for a property or the
    whole point when SoilGrids has no value there."""
    try:
        layers = payload["properties"]["layers"]
        raw = {}
        for layer in layers:
            d_factor = layer["unit_measure"]["d_factor"]
            total, thickness = 0.0, 0
            for depth in layer["depths"]:
                value = depth["values"]["mean"]
                if value is None:
                    total = None
                    break
                total += (value / d_factor) * DEPTHS[depth["label"]]
                thickness += DEPTHS[depth["label"]]
            raw[layer["name"]] = None if total is None else total / thickness
    except (KeyError, TypeError, ZeroDivisionError) as err:
        raise SoilGridsError(f"unexpected SoilGrids response ({type(err).__name__})") from err
    out = {"sand": raw.get("sand"), "silt": raw.get("silt"), "clay": raw.get("clay"),
           "ph": raw.get("phh2o"),
           # SoilGrids soc is dg/kg (d_factor 10 -> g/kg); organic carbon, not organic matter
           "organic_carbon_g_kg": raw.get("soc"), "texture": None}
    if out["sand"] is None and out["ph"] is None and out["organic_carbon_g_kg"] is None:
        return None
    if None not in (out["sand"], out["silt"], out["clay"]):
        scale = 100.0 / (out["sand"] + out["silt"] + out["clay"])   # fractions sum to 99-101
        for p in ("sand", "silt", "clay"):
            out[p] *= scale
        out["texture"] = usda_texture(out["sand"], out["silt"], out["clay"])
    return out


def fetch_point(lat, lon, get=None):
    """One SoilGrids call -> parse_response(). Raises SoilGridsError."""
    params = [("lon", lon), ("lat", lat), ("value", "mean")]
    params += [("property", p) for p in PROPERTIES] + [("depth", d) for d in DEPTHS]
    try:
        response = (get or requests.get)(URL, params=params, timeout=TIMEOUT_S)
    except requests.RequestException as err:
        raise SoilGridsError(type(err).__name__) from err
    if response.status_code != 200:
        raise SoilGridsError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as err:
        raise SoilGridsError("answer was not JSON") from err
    return parse_response(payload)


def soil_at(lat, lon, cache_dir=None, get=None):
    """-> (soil dict or None, source_mode "live"|"cache"). None = SoilGrids has no value
    at this point. Raises SoilGridsError if SoilGrids cannot be reached and
    nothing is cached."""
    path = cache_path(lat, lon, cache_dir)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["soil"], "cache"
    except (OSError, ValueError, KeyError):
        pass
    soil = fetch_point(lat, lon, get)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"lat": lat, "lon": lon, "soil": soil}, f)
    except OSError:
        pass   # a read-only disk only loses the cache
    return soil, "live"
