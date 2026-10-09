"""
scripts/deploy_hf_space.py: deploy the CropShift API to a free Hugging Face
Space (task 10b).

Reads HF_TOKEN from .env, creates the Space if it does not exist yet
(Docker SDK, free CPU basic hardware, public), and uploads exactly the
files the Dockerfile needs: app.py, src/, data/processed/, data/reference/,
web/mock/, Dockerfile, requirements.txt. A Space-only README.md (with the
sdk/app_port front-matter HF needs to build the Space) is generated and
uploaded too -- the GitHub README.md is never touched.

Run from the repo root: python scripts/deploy_hf_space.py
"""

import os
import sys

from dotenv import load_dotenv
from huggingface_hub import HfApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPACE_ID = "Tanha01/cropshift-api"

ALLOW_PATTERNS = [
    "app.py",
    "Dockerfile",
    "requirements.txt",
    "src/**",
    "data/processed/**",
    "data/reference/**",
    "web/mock/**",
]
IGNORE_PATTERNS = [
    "**/__pycache__/**",
    "**/*.pyc",
    "**/test_*.py",
    "**/*_test.py",
    "**/.ipynb_checkpoints/**",
]

SPACE_README = """---
title: CropShift API
emoji: 🌾
sdk: docker
app_port: 7860
license: apache-2.0
---

# CropShift API

Which crop to plant and when, with NASA data as the evidence. FastAPI
service for Survey Crops' NASA Space Apps 2026 (Challenge 7, "Field
Shift") entry, covering Cumilla, Feni, Brahmanbaria, Noakhali and Sylhet.

Source: https://github.com/Taaanha/Crop-Shift

Docs: `/docs` on this Space's URL. Endpoints: `/api/v1/districts`,
`/api/v1/advisory`, `/api/v1/risk-calendar`, `/api/v1/post-flood`,
`/api/v1/field-twin`.

Free tier: this Space sleeps when idle; the first request after a sleep
can take about a minute while it wakes up.
"""


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def files_to_upload():
    """Every file under ROOT that the allow/ignore patterns keep, mirroring
    what the Dockerfile COPYs, so the size check below matches the real upload."""
    import fnmatch

    keep = []
    for sub in ("src", "data/processed", "data/reference", "web/mock"):
        base = os.path.join(ROOT, sub)
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel, pat) for pat in IGNORE_PATTERNS):
                    continue
                keep.append(path)
    for extra in ("app.py", "Dockerfile", "requirements.txt"):
        keep.append(os.path.join(ROOT, extra))
    return keep


def check_upload_size(limit_mb=500):
    paths = files_to_upload()
    sized = sorted(((os.path.getsize(p), p) for p in paths), reverse=True)
    total = sum(s for s, _ in sized)
    print(f"Upload contents: {len(paths)} files, {human_size(total)} total")
    print("Biggest files:")
    for size, path in sized[:10]:
        print(f"  {human_size(size):>10}  {os.path.relpath(path, ROOT)}")
    if total > limit_mb * 1024 * 1024:
        print(f"\nSTOP: upload is over {limit_mb} MB ({human_size(total)}). Not uploading.")
        sys.exit(1)
    return total


def main():
    load_dotenv(os.path.join(ROOT, ".env"))
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("STOP: HF_TOKEN not found in .env")
        sys.exit(1)

    check_upload_size()

    api = HfApi(token=token)
    api.create_repo(repo_id=SPACE_ID, repo_type="space", space_sdk="docker", exist_ok=True)
    print(f"Space ready: https://huggingface.co/spaces/{SPACE_ID}")

    readme_path = os.path.join(ROOT, "scripts", "_hf_space_readme.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(SPACE_README)

    api.upload_file(
        path_or_fileobj=readme_path,
        path_in_repo="README.md",
        repo_id=SPACE_ID,
        repo_type="space",
    )
    os.remove(readme_path)
    print("Uploaded Space README.md")

    api.upload_folder(
        folder_path=ROOT,
        repo_id=SPACE_ID,
        repo_type="space",
        allow_patterns=ALLOW_PATTERNS,
        ignore_patterns=IGNORE_PATTERNS,
        commit_message="Deploy CropShift API (task 10b)",
    )
    print("Upload complete. Space will now build:")
    print(f"  https://huggingface.co/spaces/{SPACE_ID}")
    print(f"  live URL once built: https://tanha01-cropshift-api.hf.space")


if __name__ == "__main__":
    main()
