# assay handoff

Updated 2026-11-10 05:10 IST.

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
| Video URL | pending: Zaid uploads `video/out/assay-walkthrough.mp4` (2:34, 1080p, 30.9 MB; YouTube Unlisted or Loom) |

## In flight

- Walkthrough film: done. `video/out/assay-walkthrough.mp4` (gitignored), built from six live runs; tooling and `video/VOICEOVER.md` are committed. In the Llama 2 PDF scene the held claims cite the arXiv copy and blog reviews, not the upload, and the narration says so.
- D-203: rerun the eval on a fresh Groq daily quota (200k tokens per model per day, rolling), then update the README tables and re-render the report PDF.

## Known UI issues (from filming, not fixed)

- Impact "Normal" does not stop the approval gate when the Analyst flags high impact (by design, but surprising).
- The held decision card shows "Draft ready for review." when the Analyst returned no recommendation.
- The Evidence highlight can include page chrome around the matched sentence (seen on a Hacker News page).
- The Critic step dot can stay amber after the run is done.

## Overnight decisions (review)

- 2026-11-10: the eval rerun waits until the film is captured, because both share the Groq daily token budget and a starved film run would show capacity abstentions on camera.
- 2026-11-10 05:00: D-203 postponed to about 01:00 IST on 11 Nov. Filming drove both gpt-oss models to about 199k of 200k daily tokens, and a 20-question eval needs well over that; running it now would repeat Run B's capacity failures.
- 2026-11-10 05:05: unchecked claims now read "Not checked" (amber) in the meter, claim note and evidence panel, and the meter total leaves them out, matching the abstention share; redeployed to Modal (`/health` 200, new app.js served). Not seen in a browser on a live run, since that needs Groq quota.
- Not done, needs Zaid: uploading the film (outward-facing), deleting the two unused GCP projects (`assay-research-zaid`, `assay-zaidsyed`; irreversible).
