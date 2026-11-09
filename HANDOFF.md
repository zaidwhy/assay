# assay handoff

Updated 2026-11-10 02:35 IST.

## State

- Live demo: https://zaidwhy--assay-web.us-east.modal.run (Modal, one container, secret `assay-keys`).
- Repo: https://github.com/zaidwhy/assay (public, main).
- Report: `docs/assay-report.pdf` (5 pages, rendered from `docs/report/report.html`).
- Tests: 53 (`uv run pytest`).
- Eval: Run A and Run B are in the README and the report; `eval/results.json` holds Run B.

## Submission form

| Field | Value |
|---|---|
| Project URL | https://zaidwhy--assay-web.us-east.modal.run |
| Repository URL | https://github.com/zaidwhy/assay |
| Video URL | pending: Zaid uploads `video/out/assay-walkthrough.mp4` (YouTube Unlisted or Loom) |

## In flight

- Walkthrough film: tooling in `video/` (narrate, capture, compose, overlays). Renders, frames and the browser profile are gitignored. The film is filmed from live runs, so it waits on Groq quota between takes.
- D-203: rerun the eval on a fresh Groq daily quota (200k tokens per model per day), then update the README tables and re-render the report PDF. Do not run it while the film is capturing; both draw on the same quota.

## Overnight decisions (review)

- 2026-11-10: the eval rerun waits until the film is captured, because both share the Groq daily token budget and a starved film run would show capacity abstentions on camera.
- Not done, needs Zaid: uploading the film (outward-facing), deleting the two unused GCP projects (`assay-research-zaid`, `assay-zaidsyed`; irreversible).
