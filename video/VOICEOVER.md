# How the walkthrough is made and voiced

Every run in the film is a real run on the live demo (https://zaidwhy--assay-web.us-east.modal.run), filmed in headless
Chromium as it happened. The only thing added to the page is a pointer, so a viewer can follow the clicks. Where a run
came out differently from the plan, the narration says what really happened.

- **Narrator:** Microsoft's neural voice "Andrew" (`en-US-AndrewMultilingualNeural`) through edge-tts, pitched down 4 Hz,
  long pauses cut to 0.3 s, broadcast EQ. The end card says so: "Narration: AI voice (Microsoft Andrew, edge-tts)".
- **Numbers:** every number spoken or captioned is on screen at that moment or stated in the README.

## Narration

| Shot | Recording | Line |
|---|---|---|
| Title | card | This is assay. Every claim it makes is checked against the exact passage it cites. |
| Run | normal-3 | Ask it a decision question. |
| | | The Researcher plans searches and calls its tools: web search, Wikipedia and arXiv. |
| | | Hybrid search ranks the passages, keyword plus dense, then a cross-encoder reranks them. |
| | | The Critic checks each claim against only the passage it cites. |
| | | Impact said Normal, but the Analyst flagged it as high impact, so a person approves it. |
| Review | normal-3 | The decision is built only from the claims that held. Removed claims stay on the page, with the reason they failed. |
| | | Click a verified claim, and its quoted sentence is highlighted in the source. |
| | | Every source carries a reliability score. |
| Trace | normal-3 | Every agent step, model call and tool call is traced: latency, tokens, the model that answered, and any fallback. This run used three. |
| Approval | approval-1 | Now a health question, with Impact left on auto. |
| | | Every model is open-weight: gpt-oss on Groq, with Qwen as the fallback. Each run gets one thirty second deadline, with a budget for every stage. |
| | | The words weight loss mark it as medical, so the report waits for approval. |
| | | Approved and released. Six of six claims held. |
| Upload | longdoc-1 (the 12 s upload wait cut) | Long documents work too. The seventy seven page Llama 2 paper is uploaded to this session. |
| Long document | longdoc-1 | Then ask how many tokens Llama 2 was pretrained on. The upload is now source number one. |
| | | Two trillion tokens. The claims that held cite the paper's arXiv copy and reviews, not the upload. |
| Premise | premise-1 | Last, a question with a false premise: the fifth moon of Venus. A system that has to quote its sources cannot invent a name. |
| | | It rejects the premise. Venus has no known moons. And every run is scored automatically. |
| End | card | assay is open source on GitHub, and the demo is live. |

The lines and their slots live in `narrate.py` (`LINES`); where each one lands, and the captions, in `compose.py` (`SHOTS`).

## Rebuild

Python with `uv`, ffmpeg on the path, and a Chromium build (set `CHROME` if it is not at the Playwright default).

    uv run --no-project --with playwright python video/capture.py normal 3       # one real run per scene and take
    uv run --no-project --with playwright python video/capture.py approval 1
    LLAMA_PDF=path/to/llama2.pdf uv run --no-project --with playwright python video/capture.py longdoc 1
    uv run --no-project --with playwright python video/capture.py premise 1
    uv run --no-project --with playwright python video/overlays.py              # title, captions, end card
    uv run --no-project --with edge-tts python video/narrate.py                  # the narrator's lines, fitted to slots
    uv run --no-project --with numpy python video/compose.py                     # -> video/out/assay-walkthrough.mp4

The live demo runs on Groq's free tier; its rate-limit replies on 2026-11-10 gave a daily limit of 200,000 tokens per
model, refilling continuously, and a full run used about 8,000 of the large model's. Once the day's quota is low, space
the takes about an hour apart, or runs come back with claims marked "Not checked" for model capacity (take normal-1 did).
