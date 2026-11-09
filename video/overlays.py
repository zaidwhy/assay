"""Draws the title card, the lower-thirds and the end card as transparent 1920x1080 PNGs in the app's own look (IBM Plex,
forest ink, amber for the Critic), by rendering small HTML cards in Chromium. Output: video/work/overlays/<name>.png

  python video/overlays.py

Each caption names the requirement of the brief that the shot above it shows. The recording sits in a frame with a band
under it (compose.py: SCREEN), and captions are set in that band, so they never cover the app."""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent / "work" / "overlays"
CHROME = os.environ.get("CHROME", "C:/Users/Asus/AppData/Local/ms-playwright/chromium-1252/chrome-win64/chrome.exe")
DEMO = "zaidwhy--assay-web.us-east.modal.run"

STYLE = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Serif:wght@400;500&display=swap');
html, body { margin: 0; width: 1920px; height: 1080px; background: transparent; font-family: 'IBM Plex Sans', sans-serif; color: #eef0ea; }
.lower { position: absolute; left: 112px; right: 112px; top: 978px; height: 102px; display: flex; align-items: center; gap: 26px; }
.lower .kicker { font: 500 19px/1 'IBM Plex Mono', monospace; letter-spacing: .14em; text-transform: uppercase; color: #e3b25e;
  padding: 9px 14px; border: 1px solid rgba(227,178,94,.55); border-radius: 4px; white-space: nowrap; }
.lower .line { font: 400 27px/1.25 'IBM Plex Sans', sans-serif; color: #eef0ea; }
.full { position: absolute; inset: 0; background: #eef0ea; color: #1f2b24; display: flex; flex-direction: column; justify-content: center; padding-left: 220px; }
.brand { display: flex; align-items: center; gap: 30px; font: 400 150px/1 'IBM Plex Serif', serif; letter-spacing: -.02em; }
.brand svg { width: 112px; height: 112px; margin-top: 18px; fill: none; stroke: #1f2b24; stroke-width: 1.5; }
.tag { margin-top: 44px; font: 400 44px/1.3 'IBM Plex Sans', sans-serif; color: #2d3a32; max-width: 1400px; }
.tag b { font-weight: 500; color: #a46a0a; }
.sub { margin-top: 30px; font: 400 24px/1.5 'IBM Plex Mono', monospace; color: #6b7669; letter-spacing: .02em; }
.end .brand { margin-bottom: 36px; }
.end .row { display: flex; gap: 28px; align-items: baseline; margin-top: 26px; }
.end .row span { width: 190px; font: 500 18px/1 'IBM Plex Mono', monospace; letter-spacing: .16em; text-transform: uppercase; color: #6b7669; }
.end .row b { font: 400 38px/1.2 'IBM Plex Sans', sans-serif; color: #2f4a3c; }
.end .note { margin-top: 64px; font: 400 21px/1.6 'IBM Plex Mono', monospace; color: #6b7669; }
"""

MARK = '<svg viewBox="0 0 20 20"><path d="M4 17 L10 3 L16 17 M6.6 11 H13.4"/></svg>'

# requirement -> what the shot shows
LOWER = {
    "agents": ("Multi-agent pipeline", "Researcher, Analyst, Critic, Report Generator"),
    "tools": ("Tool calling", "Web search, Wikipedia and arXiv, called by the Researcher"),
    "hybrid": ("Hybrid search + rerank", "BM25 + dense embeddings, fused, then a cross-encoder rerank"),
    "checked": ("No unsupported claims", "Each claim judged against only the passage it cites"),
    "removed": ("No unsupported claims", "Removed claims stay on the page, with the reason"),
    "cite": ("Citations to the exact sentence", "The quoted words, found in the source passage"),
    "approval": ("Human approval", "High-impact decisions wait for a person"),
    "trace": ("Observability", "Every step traced: latency, tokens, model, fallback"),
    "longdoc": ("Long documents", "A 77-page PDF joins this session's sources"),
    "premise": ("No unsupported claims", "A false premise gets no made-up answer"),
    "budget": ("30 s budget", "One deadline per run, with a budget for each stage"),
}

FULL = {
    "title": ('<div class="full"><div class="brand">' + MARK + 'assay</div>'
              '<div class="tag">Every claim <b>checked against the exact passage it cites.</b></div>'
              '<div class="sub">An autonomous multi-agent research and decision system</div></div>'),
    "end": ('<div class="full end"><div class="brand">' + MARK + 'assay</div>'
            '<div class="row"><span>Repo</span><b>github.com/zaidwhy/assay</b></div>'
            f'<div class="row"><span>Live demo</span><b>{DEMO}</b></div>'
            '<div class="note">Open-weight models only. Every run in this film is a real run on the live demo.<br>'
            'Narration: AI voice (Microsoft Andrew, edge-tts)</div></div>'),
}


def cards():
    out = dict(FULL)
    for key, (kicker, line) in LOWER.items():
        out[key] = f'<div class="lower"><div class="kicker">{kicker}</div><div class="line">{line}</div></div>'
    return out


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        for name, body in cards().items():
            await page.set_content(f"<!doctype html><html><head><meta charset='utf-8'><style>{STYLE}</style></head><body>{body}</body></html>")
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(250)
            await page.screenshot(path=str(OUT / f"{name}.png"), omit_background=True)
        await browser.close()
    print(f"{len(cards())} overlays -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
