# assay handoff

Updated 2026-11-10 05:10 IST.

## State

- Live demo: https://zaidwhy--assay-web.us-east.modal.run (Modal, one container, secret `assay-keys`).
- Repo: https://github.com/zaidwhy/assay (public, main).
- Report: `docs/assay-report.pdf` (5 pages, rendered from `docs/report/report.html`).
- Tests: 54 (`uv run pytest`).
- Eval: Run A and Run B are in the README and the report; `eval/results.json` holds Run B.

## Submission form

| Field | Value |
|---|---|
| Project URL | https://zaidwhy--assay-web.us-east.modal.run |
| Repository URL | https://github.com/zaidwhy/assay |
| Video URL | pending: Zaid uploads `video/out/assay-walkthrough.mp4` (2:34, 1080p, 30.9 MB; YouTube Unlisted or Loom) |

## In flight

- Walkthrough film: done. `video/out/assay-walkthrough.mp4` (gitignored), built from six live runs; tooling and `video/VOICEOVER.md` are committed. In the Llama 2 PDF scene the held claims cite the arXiv copy and blog reviews, not the upload, and the narration says so.
- D-203 done 2026-11-11: Run C (15:47 IST, fresh quota) answered 12 of 14 answerable (gap closed), faithfulness 0.81, false premise 4 of 6, p50/p95 19.2/27.9 s. README table, report section 5 and `docs/assay-report.pdf` (5 pages) updated.
- Open from Run C: on u03 (Mars) and u04 (Atlantis) the verdict hedges ("cannot be determined") instead of rejecting the premise. Fixing the conclusion prompt is the next change; do not loosen the harness phrases to make them pass.

## Overnight decisions (review)

- 2026-11-10: the eval rerun waits until the film is captured, because both share the Groq daily token budget and a starved film run would show capacity abstentions on camera.
- 2026-11-10 05:00: D-203 postponed to about 01:00 IST on 11 Nov. Filming drove both gpt-oss models to about 199k of 200k daily tokens, and a 20-question eval needs well over that; running it now would repeat Run B's capacity failures.
- 2026-11-10 05:05: unchecked claims now read "Not checked" (amber) in the meter, claim note and evidence panel, and the meter total leaves them out, matching the abstention share; redeployed to Modal (`/health` 200, new app.js served). Not seen in a browser on a live run, since that needs Groq quota.
- 2026-11-10 12:30: the four UI issues from filming are resolved and deployed. The Evidence highlight anchors on the quoted words and drops page chrome before them; the Analyst prompt now asks for a recommendation, with a plain fallback line if it is still empty; at the approval gate the Critic shows done instead of pulsing; the "Normal" impact option is removed, because a request can raise impact but never lower it past the keyword and Analyst checks (the API still accepts "normal"). The amber Critic dot is the design accent, not a stuck state. Checked live: a fasting run held at the gate with a drafted recommendation, then approved and finished in 20.8 s.
- Not done, needs Zaid: uploading the film (outward-facing), deleting the two unused GCP projects (`assay-research-zaid`, `assay-zaidsyed`; irreversible).
