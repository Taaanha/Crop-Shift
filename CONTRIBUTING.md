# How we work together on CropShift

This guide is for everyone on Survey Crops. It keeps our work from clashing, so everything fits together at the end.

## 1. One-time setup (about 15 minutes)
1. Make a GitHub account (use your own; your commits count for our Teamwork score).
2. Send your GitHub username to Tanha. She adds you: repo **Settings → Collaborators → Add people**. Accept the email invite.
3. Install **GitHub Desktop**: https://desktop.github.com
4. In GitHub Desktop: **File → Clone repository → URL**, paste `https://github.com/Taaanha/Crop-Shift.git`, choose a folder, click **Clone**.

## 2. Every time you work
1. Open GitHub Desktop. Click **Fetch origin**, then **Pull origin**, to get everyone's latest work.
2. **Make a branch for your task.** Click **Current branch → New branch**. Name it after the task, e.g. `web-advisory-page` or `research-crop-calendar`.
3. Do your work in **your folders only** (see section 3).
4. Save your work in small steps. In GitHub Desktop, write a short summary (e.g. "Add sowing windows for mustard and lentil") and click **Commit**.
5. Click **Push origin**, then **Create Pull Request**. On the GitHub page, describe what you did and click **Create pull request**.
6. Another teammate reviews it and clicks **Merge**. Never commit straight to `main`.

## 3. Who owns which folder (this prevents conflicts)
| Folder | Owner | Contents |
|---|---|---|
| `src/`, `data/processed/`, `app.py` | Tanha | Science code, the AI agent, the API |
| `web/` | Frontend teammate | The website: HTML, CSS, JavaScript, Bangla text, fonts |
| `data/reference/`, `docs/research/` | Research teammate | Tables of facts we found (crop calendars, thresholds, costs) and notes |
| `docs/api_contract.md`, `web/mock/` | **Everyone, by agreement** | The "handshake" between the website and the backend |

If you need something changed in a folder you don't own, **ask the owner.** Don't edit it yourself.

## 4. Building the website against the mock files
The backend isn't finished yet, so the website reads **mock files** in `web/mock/`. They have exactly the shape the real backend will send (see `docs/api_contract.md`).

- The website should load data with `fetch("mock/advisory.json")` and so on. Later we change a single `API_BASE` setting to the real URL, and nothing else changes.
- **The website never calculates anything.** It only shows what the JSON says: every number, label and warning.
- Every number shown needs a way to open its **source** (the `provenance` list in the JSON). This is our "receipt" drawer.
- Show the **`source_mode` badge** (live, cache or fixture), and show "MOCK DATA" whenever `is_mock` is true.
- Use the **Noto Sans Bengali** font for Bangla. Keep a copy in `web/fonts/` so it works offline.
- If you need a field that isn't in the mock, **don't add it quietly.** Open a GitHub Issue titled "Contract change: …" and we'll agree on it together.

## 5. Research rules (for anything in `data/reference/`)
Every fact goes in a CSV with these exact columns:

```
item,value,unit,source_title,source_url,page,year,notes
```

Example:
```
mustard_sowing_window_start,2025-11-01,date,"BARI Krishi Projukti Hatboi",https://...,112,2019,"Cumilla region"
```

- **No source, no row.** Never estimate or guess a number. If you can't find it, write it in `docs/research/open-questions.md`.
- Prefer official sources: BARI, BRRI, DAE, BBS, SRDI, FAO, and journal papers.
- Save the PDF link and page number, so judges can check.

Current research tasks, in order:
1. Bangla interface text and a glossary (crop names, বিঘা/শতাংশ, warning texts)
2. Crop calendars and hazard thresholds: sowing windows, crop duration, and heat/cold/flood limits per crop
3. SRDI Upazila Nirdeshika (land type) for the upazilas in our districts
4. BBS district crop yields by year
5. Later: crop cost data (primary DAE and BWMRI documents)
6. Test the Bangla voice and font on the demo laptop
7. Talk to 2–3 adult farmers or an agriculture officer (with their permission) and write notes

## 6. Using AI tools (ChatGPT, Claude, and so on)
It's allowed, but **you must log it** in `docs/AI_USE.md`: which tool, what you asked, what it produced, and what you checked or changed yourself. The NASA rules require this.

## 7. Never do these
- Don't put passwords, API keys or the `.env` file in the repo.
- Don't upload big data files (anything over 50 MB, satellite images, and so on).
- Don't include the name, voice or face of anyone under 18 in any file or video.
- Don't change `main` directly or force-push.

## 8. Staying in sync
- A short weekly call: what I finished, what's next, what's blocking me.
- The task board is the GitHub **Issues** tab. One issue per task, with an owner.
