"""The film's narrator: an AI voice (Microsoft's neural "Andrew", through edge-tts). The end card says so.

  python video/narrate.py [line ...]     -> video/narration/<line>.wav, each fitted to its slot in LINES

Each line is generated, its long pauses cut (TTS rests at every comma, which is what makes it sound read), and if it still
overruns its slot it is generated again a little faster, 4 percent at a time, up to 16 percent. Every number spoken is one
the screen shows at that moment or the README states."""
import asyncio
import subprocess
import sys
from pathlib import Path

import edge_tts

HERE = Path(__file__).resolve().parent
OUT = HERE / "narration"
VOICE, PITCH = "en-US-AndrewMultilingualNeural", "-4Hz"
MAX_RATE = 16

# line id -> (slot in seconds, text). Third person, short sentences, written to be heard. compose.py places each one.
LINES = {
    "title": (5.6, "This is assay. Every claim it makes is checked against the exact passage it cites."),
    "ask": (3.6, "Ask it a decision question."),
    "research": (6.2, "The Researcher plans searches and calls its tools: web search, Wikipedia and arXiv."),
    "rank": (6.3, "Hybrid search ranks the passages, keyword plus dense, then a cross-encoder reranks them."),
    "critic": (4.3, "The Critic checks each claim against only the passage it cites."),
    "flag": (5.8, "Impact said Normal, but the Analyst flagged it as high impact, so a person approves it."),
    "held": (8.8, "The decision is built only from the claims that held. Removed claims stay on the page, with the reason they failed."),
    "cite": (5.4, "Click a verified claim, and its quoted sentence is highlighted in the source."),
    "sources": (4.8, "Every source carries a reliability score."),
    "trace": (11.5, "Every agent step, model call and tool call is traced: latency, tokens, the model that answered, and any fallback. This run used three."),
    "health": (5.0, "Now a health question, with Impact left on auto."),
    "models": (12.0, "Every model is open-weight: gpt-oss on Groq, with Qwen as the fallback. Each run gets one thirty second deadline, with a budget for every stage."),
    "medical": (6.8, "The words weight loss mark it as medical, so the report waits for approval."),
    "released": (4.4, "Approved and released. Six of six claims held."),
    "upload": (7.2, "Long documents work too. The seventy seven page Llama 2 paper is uploaded to this session."),
    "tokens": (13.0, "Then ask how many tokens Llama 2 was pretrained on. The upload is now source number one."),
    "arxiv": (6.4, "Two trillion tokens. The claims that held cite the paper's arXiv copy and reviews, not the upload."),
    "venus": (13.0, "Last, a question with a false premise: the fifth moon of Venus. A system that has to quote its sources cannot invent a name."),
    "nomoon": (6.2, "It rejects the premise. Venus has no known moons. And every run is scored automatically."),
    "end": (6.0, "assay is open source on GitHub, and the demo is live."),
}

# Broadcast chain: rumble cut, chest warmth, less mud, presence, air, de-ess, gentle compression, a ceiling.
POLISH = ("highpass=f=70,equalizer=f=110:t=q:w=1:g=3,equalizer=f=320:t=q:w=1:g=-2.5,equalizer=f=3200:t=q:w=1:g=2,"
          "treble=g=1.5:f=9000,deesser=i=0.4,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120,"
          "alimiter=limit=0.89,loudnorm=I=-18:TP=-2")
TIGHTEN = "silenceremove=stop_periods=-1:stop_duration=0.42:stop_threshold=-45dB:stop_silence=0.3"

# How the voice is told to say a word where its own guess is wrong; the captions keep the spelling.
SAY = {"Zaid": "Zade", "arXiv": "archive", "BM25": "B M 25", "LLM": "L L M", "gpt-oss": "G P T O S S"}


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


async def speak(text, rate, path):
    for written, spoken in SAY.items():
        text = text.replace(written, spoken)
    await edge_tts.Communicate(text, VOICE, rate=f"+{rate}%", pitch=PITCH).save(str(path))


def make(key, slot, text):
    raw, final = OUT / f"{key}-raw.mp3", OUT / f"{key}.wav"
    for rate in range(0, MAX_RATE + 1, 4):
        asyncio.run(speak(text, rate, raw))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-af", f"{TIGHTEN},{POLISH}", "-ar", "48000", "-ac", "2",
                        str(final)], check=True)
        length = duration(final)
        if length <= slot:
            break
    raw.unlink()
    flag = "" if length <= slot else f"  STILL {length - slot:.1f} s LONG: shorten the line"
    print(f"{key:>10}: {length:4.1f} s in a {slot:4.1f} s slot at +{rate}%{flag}")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for key, (slot, text) in LINES.items():
        if len(sys.argv) == 1 or key in sys.argv[1:]:
            make(key, slot, text)
