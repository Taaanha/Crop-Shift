"""
Checks every link the API can put in a "Source" card: the url and view_url of
each fixed provenance entry (app.fixed_provenance) and the url of every
research source (app.ref_provenance over data/reference/*.csv). Each link is
requested once and its HTTP status reported. NASA and FAO links must answer
200 (after normal redirects); the script exits with code 1 if one does not.

It also lists every data/reference row whose source_url is not a real web
address (docs/results/broken_source_links.md), so the team can fix the CSVs.
The CSVs themselves are never edited here.

Run from the repo root:  python scripts/check_links.py
Writes docs/results/link_check.md and docs/results/broken_source_links.md.
"""

import csv
import glob
import os
import sys
from urllib.parse import urlsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)   # see check()

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import app  # noqa: E402

RESULTS = os.path.join(ROOT, "docs", "results")
HEADERS = {"User-Agent": "Mozilla/5.0 (CropShift link check; NASA Space Apps 2026)"}
MUST_PASS_HOSTS = ("nasa.gov", "fao.org")


def collect_links():
    """[(provenance id, field, url)] for every url/view_url the API can return."""
    links = []
    for pid, entry in app.fixed_provenance().items():
        for field in ("url", "view_url"):
            if entry.get(field):
                links.append((pid, field, entry[field]))
    for ref_id in sorted(set(app.reference_rows()["ref_id"])):
        url = app.ref_provenance(ref_id)["url"]
        if url:
            links.append((ref_id, "url", url))
    return links


def check(url):
    """(status, final url) for one GET with redirects. A server whose
    certificate chain Python cannot verify (browsers still open it) is retried
    without verification and reported as e.g. "200 (certificate warning)"."""
    try:
        with requests.get(url, headers=HEADERS, timeout=40, allow_redirects=True, stream=True) as r:
            return r.status_code, r.url
    except requests.exceptions.SSLError:
        try:
            with requests.get(url, headers=HEADERS, timeout=40, allow_redirects=True,
                              stream=True, verify=False) as r:
                return f"{r.status_code} (certificate warning)", r.url
        except requests.RequestException as err:
            return type(err).__name__, url
    except requests.RequestException as err:
        return type(err).__name__, url


def must_pass(url):
    host = urlsplit(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in MUST_PASS_HOSTS)


def broken_reference_rows():
    """Rows of data/reference/*.csv whose source_url is not exactly a real web address."""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "reference", "*.csv"))):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next((r for r in reader if any(c.strip() for c in r)), [])  # skip blank lines, like pandas
            if "source_url" not in header:
                continue
            line = reader.line_num
            for cells in reader:
                start, line = line + 1, reader.line_num
                if not any(c.strip() for c in cells):
                    continue
                row = dict(zip(header, cells))
                raw = (row.get("source_url") or "").strip()
                fixed = app.public_url(raw)
                if fixed == raw and raw:
                    continue
                problem = ("missing https:// (the API adds it)" if fixed and "://" not in raw
                           else "stray quote mark (the API removes it)" if fixed
                           else "empty" if not raw else "not a web address (shown as Link pending)")
                out.append({"file": name, "line": start, "item": row.get("item") or "",
                            "source_title": row.get("source_title") or "", "url": raw,
                            "problem": problem})
    return out


def cell(text, width=90):
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= width else text[:width - 1] + "…"


def main():
    links = collect_links()
    rows, failures, not_opening = [], [], []
    for pid, field, url in links:
        status, final = check(url)
        ok = status == 200
        if must_pass(url) and not ok:
            failures.append(url)
        if not ok and pid.startswith("ref_"):
            not_opening.append((pid, url, status))
        moved = "" if final.rstrip("/") == url.rstrip("/") else f" → {cell(final, 60)}"
        rows.append(f"| {pid} | {field} | {status}{' ✓' if ok else ''} | {cell(url)}{moved} |")
    report = ["# Link check", "",
              "Every `url` and `view_url` the API can return in `provenance`, requested by "
              "`scripts/check_links.py` (GET, redirects followed). NASA and FAO links must answer 200.",
              "", "| id | field | status | link |", "|---|---|---|---|", *rows, "",
              f"{len(links)} links checked; {sum(r.count(' ✓') for r in rows)} answered 200. "
              f"NASA/FAO failures: {len(failures)}."]
    with open(os.path.join(RESULTS, "link_check.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    broken = broken_reference_rows()
    lines = ["# Research rows without a working source link", "",
             "Rows in `data/reference/*.csv` whose `source_url` is not a real web address. The API "
             "shows them with no link and the note \"Link pending\" (or adds a missing `https://`). "
             "Please replace each with the official URL in the CSV; the API needs no change.",
             "Generated by `scripts/check_links.py`. Line = line number in the CSV file.", "",
             "| file | line | item | source_title | current url | problem |",
             "|---|---|---|---|---|---|"]
    lines += [f"| {r['file']} | {r['line']} | {cell(r['item'], 40)} | {cell(r['source_title'], 60)} | "
              f"{cell(r['url'], 60) or '(empty)'} | {r['problem']} |" for r in broken]
    lines += ["", f"{len(broken)} rows."]
    refs = app.reference_rows()
    lines += ["", "## Research links that did not answer 200 to the link check", "",
              "These are real web addresses, but a script could not open them cleanly. "
              "\"certificate warning\" opens in a browser (the server's certificate chain is "
              "incomplete); 403 means the publisher blocks scripts, so check it by hand; 404/422 "
              "means the page is gone or is an API query, so please replace it.", "",
              "| id | status | file | items | url |", "|---|---|---|---|---|"]
    for pid, url, status in not_opening:
        cited = refs[refs["ref_id"] == pid]
        lines.append(f"| {pid} | {status} | {', '.join(sorted(set(cited['file'])))} | "
                     f"{cell(', '.join(cited['item']), 60)} | {cell(url, 70)} |")
    with open(os.path.join(RESULTS, "broken_source_links.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(report))
    print(f"\n{len(broken)} research rows need a real URL: docs/results/broken_source_links.md")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
