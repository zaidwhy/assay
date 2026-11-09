"""Films one scene of the live assay demo: a real run in headless Chromium, captured frame by frame through the DevTools
screencast, then re-timed to a constant frame rate.

  python video/capture.py <scene> [take]      scenes: normal, approval, longdoc, premise; each take keeps its own files

Nothing is staged: every question goes to the live service and the page renders whatever the run produces. The only
thing added to the page is a pointer, so a viewer can follow the clicks. Each scene also saves the run as the API reports
it (video/segs/<scene>.json) and the moments the edit lines narration up with (video/segs/<scene>.marks.json)."""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

URL = os.environ.get("ASSAY_URL", "https://zaidwhy--assay-web.us-east.modal.run").rstrip("/")
CHROME = os.environ.get("CHROME", "C:/Users/Asus/AppData/Local/ms-playwright/chromium-1252/chrome-win64/chrome.exe")
LLAMA_PDF = os.environ.get("LLAMA_PDF", "")
HERE = Path(__file__).resolve().parent
SEGS, FRAMES, PROFILE = HERE / "segs", HERE / "frames", HERE / "profile"
# A 1440x810 laptop layout drawn at 2x (2880x1620): sharp type, and the side panel fits. compose.py scales it down once.
VIEW_W, VIEW_H, SCALE, FPS = 1440, 810, 2, 30

# The pointer: an arrow that glides to each target before a click, and a ring on the click. It is not part of the app.
POINTER = """(() => {
  const make = () => {
    if (document.getElementById('film-pointer')) return;
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), path = document.createElementNS(ns, 'path');
    for (const [k, v] of Object.entries({ width: '22', height: '28', viewBox: '0 0 22 28' })) svg.setAttribute(k, v);
    for (const [k, v] of Object.entries({ d: 'M2 2 L2 22 L7.5 17 L11 26 L14.5 24.5 L11 16 L18 16 Z', fill: '#1f2b24', stroke: '#fff',
      'stroke-width': '1.6', 'stroke-linejoin': 'round' })) path.setAttribute(k, v);
    svg.append(path);
    const p = document.createElement('div');
    p.id = 'film-pointer';
    p.append(svg);
    Object.assign(p.style, { position: 'fixed', left: '760px', top: '600px', zIndex: 2147483647, pointerEvents: 'none',
      transition: 'left .7s cubic-bezier(.4,0,.2,1), top .7s cubic-bezier(.4,0,.2,1)', filter: 'drop-shadow(0 1px 2px rgba(0,0,0,.35))' });
    document.body.append(p);
  };
  window.__pointTo = (x, y) => { make(); const p = document.getElementById('film-pointer');
    // a modal dialog sits in the top layer, above everything in the body, so the pointer moves into it while it is open
    const home = document.querySelector('dialog[open]') || document.body;
    if (p.parentElement !== home) home.append(p);
    p.style.left = x + 'px'; p.style.top = y + 'px'; };
  window.__ring = (x, y) => { const r = document.createElement('div');
    Object.assign(r.style, { position: 'fixed', left: (x - 16) + 'px', top: (y - 16) + 'px', width: '32px', height: '32px', borderRadius: '50%',
      border: '2px solid #c9862a', zIndex: 2147483646, pointerEvents: 'none', transition: 'transform .45s ease-out, opacity .45s ease-out' });
    (document.querySelector('dialog[open]') || document.body).append(r);
    requestAnimationFrame(() => { r.style.transform = 'scale(1.8)'; r.style.opacity = '0'; }); setTimeout(() => r.remove(), 600); };
  addEventListener('DOMContentLoaded', make);
})();"""

GLIDE = """([to, ms]) => new Promise((done) => { const from = scrollY, t0 = performance.now();
  const step = (now) => { const k = Math.min(1, (now - t0) / ms), e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
    scrollTo(0, from + (to - from) * e); if (k < 1) requestAnimationFrame(step); else done(); }; requestAnimationFrame(step); })"""

# What the page shows right now, read the way a viewer would: the run state, the step times, the claim badges, the status line.
READ = """() => ({
  verdict: document.getElementById('verdict').textContent,
  status: document.getElementById('status').textContent,
  steps: [...document.querySelectorAll('.step')].map(s => (s.classList.contains('done') ? 'done' : s.classList.contains('active') ? 'active' : '-') + ' ' + s.querySelector('.t').textContent),
  badges: [...document.querySelectorAll('.claim .badge')].map(b => b.textContent),
  sources: document.querySelectorAll('.srcrow').length,
  approval: !document.getElementById('approval').hidden,
  run: new URLSearchParams(location.search).get('run'),
})"""

T0 = [0.0]
MARKS = {}


def mark(name):
    MARKS[name] = round(time.time() - T0[0], 2)
    print(f"  {MARKS[name]:6.2f}  {name}")


async def pause(page, seconds):
    await page.wait_for_timeout(int(seconds * 1000))


async def point(page, selector, click=True, settle=0.8):
    """Glide the pointer to an element, then click it the way a visitor would."""
    box = await page.locator(selector).first.bounding_box()
    x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
    await page.evaluate("([x, y]) => window.__pointTo(x, y)", [x, y])
    await pause(page, settle)
    if click:
        await page.evaluate("([x, y]) => window.__ring(x, y)", [x, y])
        await page.locator(selector).first.click()


async def scroll_to(page, y, seconds=1.2):
    await page.evaluate(GLIDE, [y, seconds * 1000])


async def ask(page, question, impact):
    await point(page, "#question")
    await page.locator("#question").fill("")
    await page.locator("#question").press_sequentially(question, delay=32)
    await pause(page, 0.4)
    await point(page, "#impact", settle=0.6)
    await page.select_option("#impact", impact)
    await pause(page, 0.6)
    await point(page, "#go", click=False, settle=0.7)
    mark("submit")
    await page.locator("#go").click()


async def follow(page, until, limit=75):
    """Poll the page until the run reaches a state, marking each step and the first verdicts as they appear."""
    seen = set()
    start = time.time()
    while time.time() - start < limit:
        view = await page.evaluate(READ)
        for i, step in enumerate(view["steps"]):
            for state in ("active", "done"):
                if step.startswith(state) and f"{i}{state}" not in seen:
                    seen.add(f"{i}{state}")
                    mark(f"step{i + 1}-{state}")
        if view["sources"] and "sources" not in seen:
            seen.add("sources")
            mark("first-source")
        if view["badges"] and "claims" not in seen:
            seen.add("claims")
            mark("first-claim")
        if any(b != "Checking" for b in view["badges"]) and "verdict" not in seen:
            seen.add("verdict")
            mark("first-verdict")
        if until == "approval" and view["approval"]:
            mark("approval")
            return view
        status = view["status"]
        if status.startswith("Finished") or status.startswith("Failed") or status.startswith("Rejected"):
            mark("done")
            return view
        await pause(page, 0.15)
    mark("timeout")
    return await page.evaluate(READ)


async def save_run(page, name):
    run_id = (await page.evaluate(READ))["run"]
    if not run_id:
        return None
    data = await page.evaluate("async (id) => { const r = await fetch('/runs/' + id); return r.json(); }", run_id)
    trace = await page.evaluate("async (id) => { const r = await fetch('/runs/' + id + '/trace'); return r.json(); }", run_id)
    SEGS.mkdir(exist_ok=True)
    (SEGS / f"{name}.json").write_text(json.dumps({"run": data, "trace": trace}, indent=1), encoding="utf-8")
    return data


async def click_claim(page, kind):
    """Click the first claim with this badge ('Removed' matches 'Removed: partial'); returns whether one was there."""
    rows = page.locator(".claim", has=page.locator(".badge", has_text=kind))
    if not await rows.count():
        return False
    row = rows.first
    await row.scroll_into_view_if_needed()
    box = await row.bounding_box()
    x, y = box["x"] + box["width"] * 0.45, box["y"] + 14
    await page.evaluate("([x, y]) => window.__pointTo(x, y)", [x, y])
    await pause(page, 0.8)
    await page.evaluate("([x, y]) => window.__ring(x, y)", [x, y])
    await row.click()
    return True


# ---------- the scenes ----------

async def normal(page):
    await pause(page, 1.5)
    await ask(page, "Should a city replace sodium street lights with LED fixtures?", "normal")
    view = await follow(page, "approval")
    if view["approval"]:
        # Impact "Normal" is the request's own label; the Analyst can still flag the decision as high impact, and then it waits
        await pause(page, 5.0)
        await point(page, "#approve")
        mark("approved")
        await follow(page, "done", limit=30)
    await pause(page, 2.5)
    mark("claims")
    if await click_claim(page, "Removed"):
        mark("removed-click")
        await pause(page, 5.5)
    if await click_claim(page, "Verified"):
        mark("verified-click")
        await pause(page, 5.5)
    # the sources and their reliability, further down the side panel
    mark("sources-scroll")
    await scroll_to(page, 520, 1.6)
    await pause(page, 3.5)
    await scroll_to(page, 0, 1.2)
    await pause(page, 0.6)
    await point(page, "#traceBtn")
    mark("trace-open")
    await pause(page, 3.5)
    await page.evaluate("""() => { const b = document.querySelector('.traceBody'); const end = b.scrollHeight - b.clientHeight, t0 = performance.now();
      const step = (now) => { const k = Math.min(1, (now - t0) / 6000); b.scrollTop = end * (k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2); if (k < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step); }""")
    await pause(page, 7.5)
    await point(page, "#traceClose")
    mark("trace-closed")
    await pause(page, 1.5)


async def approval(page):
    await pause(page, 1.0)
    await ask(page, "Is intermittent fasting more effective than calorie restriction for weight loss?", "")
    view = await follow(page, "approval")
    if not view["approval"]:
        await pause(page, 4)
        return
    await pause(page, 6.0)
    await point(page, "#approve")
    mark("approved")
    await follow(page, "done", limit=30)
    await pause(page, 6.0)


async def longdoc(page):
    await pause(page, 1.0)
    await point(page, "label.upload", click=False)
    mark("upload")
    await page.set_input_files("#upload", LLAMA_PDF)
    start = time.time()
    status = ""
    while time.time() - start < 120:
        status = await page.evaluate("document.getElementById('status').textContent")
        if status.startswith("Added") or ("Uploading" not in status and status):
            break
        await pause(page, 0.2)
    mark("uploaded")
    print("  status:", status)
    await pause(page, 3.0)
    await ask(page, "According to the Llama 2 paper, how many tokens of data were the Llama 2 models pretrained on?", "normal")
    await follow(page, "done")
    await pause(page, 2.0)
    if await click_claim(page, "Verified"):
        mark("verified-click")
    await pause(page, 6.0)


async def premise(page):
    await pause(page, 1.0)
    await ask(page, "What is the name of the fifth moon of Venus?", "")
    await follow(page, "done")
    await pause(page, 2.0)
    if await click_claim(page, "Removed") or await click_claim(page, "Verified"):
        mark("claim-click")
    await pause(page, 6.0)


SCENES = {"normal": normal, "approval": approval, "longdoc": longdoc, "premise": premise}


async def record(scene, take):
    name = f"{scene}-{take}"
    frames_dir = FRAMES / name
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    stamps = []
    async with async_playwright() as pw:
        # one persistent profile, so every scene runs in the same browser session (an upload and the question about it share it)
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE), executable_path=CHROME, headless=True, viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=SCALE,
            args=["--hide-scrollbars", f"--force-device-scale-factor={SCALE}", "--font-render-hinting=none"])
        page = context.pages[0] if context.pages else await context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.add_init_script(POINTER)
        await page.goto(URL + "/", wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        await pause(page, 1.0)
        cdp = await context.new_cdp_session(page)

        async def on_frame(event):
            index = len(stamps)
            stamps.append(event["metadata"]["timestamp"])
            (frames_dir / f"{index:05d}.jpg").write_bytes(base64.b64decode(event["data"]))
            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

        cdp.on("Page.screencastFrame", lambda event: asyncio.ensure_future(on_frame(event)))
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 96, "maxWidth": VIEW_W * SCALE, "maxHeight": VIEW_H * SCALE, "everyNthFrame": 1})
        T0[0] = time.time()
        # a still page sends no frames; a 1 px nudge makes the first one arrive at once, so marks line up with frame time
        await page.evaluate("scrollTo(0, 1)")
        await page.evaluate("scrollTo(0, 0)")
        await SCENES[scene](page)
        data = await save_run(page, name)
        await cdp.send("Page.stopScreencast")
        await pause(page, 0.4)
        await context.close()
    if errors:
        print("page errors:", errors[:3])
    first = min(stamps)
    # screencast stamps share the wall clock with the marks; marks are re-based on the first frame
    offset = first - T0[0]
    marks = {k: round(v - offset, 2) for k, v in MARKS.items()}
    order = sorted(range(len(stamps)), key=stamps.__getitem__)
    lines = []
    for k, i in enumerate(order):
        hold = (stamps[order[k + 1]] - stamps[i]) if k + 1 < len(order) else 1.0
        lines += [f"file '{frames_dir.as_posix()}/{i:05d}.jpg'", f"duration {hold:.6f}"]
    lines.append(f"file '{frames_dir.as_posix()}/{order[-1]:05d}.jpg'")
    concat = frames_dir / "frames.txt"
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    SEGS.mkdir(exist_ok=True)
    out = SEGS / f"{name}.mp4"
    # near-lossless intermediate at the captured 2880x1620
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "8", str(out)], check=True)
    (SEGS / f"{name}.marks.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")
    span = max(stamps) - first
    print(f"{name}: {len(stamps)} frames over {span:.1f} s -> {out}")
    if data:
        report = data.get("report") or {}
        print("state:", data.get("state"), "| latency_ms:", data.get("latency_ms"), "| verdict:", report.get("verdict"),
              "| abstained:", report.get("abstained"), "|", report.get("abstain_reason"))


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in SCENES:
        sys.exit(f"one of: {', '.join(SCENES)} [take]")
    asyncio.run(record(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "1"))
