# CropShift API contract, v1.0 (26 Sep 2026)

This is the handshake between the backend (`src/`, `app.py`) and the website (`web/`). **Both sides build to this document.**
- The website reads the example files in `web/mock/` until the backend is live.
- The backend has a contract test that checks its real responses have the same keys and types as those files.

**Changing this contract:** open a GitHub Issue titled "Contract change: …". Both sides agree first. Then update this file **and** the matching mock file in one Pull Request, and raise `api_version`.

---

## 1. Rules for every response

**Base URL:** `{API_BASE}/api/v1/…`. The website keeps `API_BASE` in one config line (`web/config.js`), set to `mock` during development.

Every response has the same **envelope**:

```json
{
  "api_version": "1.0",
  "endpoint": "/api/v1/advisory",
  "request": { "district": "cumilla", "lang": "bn" },
  "generated_at": "2026-11-13T09:00:00+06:00",
  "source_mode": "cache",
  "is_mock": false,
  "data": { },
  "narration": {
    "en": "…", "bn": "…",
    "sms_en": "… (≤160 characters)", "sms_bn": "…",
    "narrator": "hf_model | template",
    "provenance_check": "passed | blocked_fallback_used"
  },
  "provenance": [
    { "id": "p1", "dataset": "NASA POWER Daily Point API (AG)", "url": "https://power.larc.nasa.gov/",
      "agency": "NASA", "period": "2001-01-01/2026-08-31", "resolution": "~0.5°", "note": "" }
  ],
  "notices": [ { "level": "info | caution", "en": "…", "bn": "…" } ],
  "errors": []
}
```

| Field | Meaning |
|---|---|
| `source_mode` | `live`, `cache` or `fixture`. **Always shown as a badge.** |
| `is_mock` | `true` only in `web/mock/` files. When true, the website shows "MOCK DATA". |
| `narration` | Text written by the agent (or the template fallback), already checked by the provenance gate. **Display only; never parse numbers out of it.** It is `null` for `/districts` and for error responses. |
| `provenance` | The list of sources. Every number in `data` points to one or more of these by `id`. `url` is the dataset's official page; it is `""` (with a "Link pending" `note`) when a research source has no web address yet. Optional `view_url` (NASA datasets): a page that shows the data as satellite images for our area, e.g. NASA Worldview. Show `url` as "Dataset page ↗" and `view_url` as "See the satellite data ↗". |
| `notices` | Honest caveats, e.g. "area-level data", "Feni and Noakhali share one weather cell". |
| `errors` | Empty when OK. See section 3. |

**A "measure" object.** Every headline number in `data` uses this shape, so the website can open its receipt:

```json
{ "value": 49, "unit": "days", "src": ["p2"] }
```

- For large arrays (charts, calendars) the `src` sits at the array's parent level.
- Units are always given. Dates use `YYYY-MM-DD`.
- Language: all farmer-facing strings come as `{ "en": "…", "bn": "…" }`.

---

## 2. Endpoints

| Method and path | Purpose | Mock file |
|---|---|---|
| `GET /api/v1/districts` | Supported districts, their coverage, and caveats | `districts.json` |
| `GET /api/v1/advisory?district=&decision_date=&irrigation=yes\|no&lang=` | **Main screen:** ranked crop options with sowing windows, risks and reasons | `advisory.json` |
| `GET /api/v1/risk-calendar?district=&crop=` | Crop × sowing-date × hazard grid over all years | `risk_calendar.json` |
| `GET /api/v1/field-twin?district=&year=&crop=&sow_date=` | Week-by-week replay of one past year (an explanation, not a forecast) | `field_twin.json` |
| `GET /api/v1/post-flood?district=&flood_date=&sand=yes\|no` | Water persistence, days until soil is back to normal, earliest sowing date, crops still possible | `post_flood.json` |
| `GET /api/v1/warnings?district=` | Short-range forecast warnings with actions (rule table) | `warnings.json` |
| `GET /api/v1/enso-lens?district=&crop=` | **PENDING confirmation.** El Niño years vs all years | `enso_lens.json` |
| `POST /api/v1/ask` body `{ "question": "…", "district": "…", "lang": "bn" }` | Free-text question → agent answer, tools used, provenance check | `ask.json` |

**Errors** use the same envelope, with `data: null` and for example:
`"errors": [{ "code": "DISTRICT_NOT_COVERED", "en": "…", "bn": "…" }]` (see `error.json`).

Error codes: `DISTRICT_NOT_COVERED`, `BAD_PARAMETER`, `NO_DATA_FOR_PERIOD`, `MODEL_UNAVAILABLE` (the narration then falls back to the template; `data` is still returned), `INTERNAL`.

---

## 3. What the website must do
1. Render only what the JSON contains. No calculations, no unit conversions.
2. Every `measure` shows a small "source" link that opens its `provenance` entries.
3. Always show the `source_mode` badge, and "MOCK DATA" when `is_mock` is true.
4. Show every `notices` item; never hide caveats.
5. If `errors` isn't empty, show the message in the chosen language, and never a blank page.
6. Bangla uses Noto Sans Bengali (a local copy in `web/fonts/`).

## 4. What the backend must do
1. Return exactly these keys and types (checked by the contract test).
2. Every number in `data` has a `src` pointing to an existing `provenance` id.
3. `narration` passes `guard()`. If it doesn't, use the template text and set `provenance_check: "blocked_fallback_used"`.
4. Never return an empty body. On failure, return the error envelope.
