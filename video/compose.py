"""Cuts the film from the scene recordings (video/segs/), lays the narrator's lines (video/narration/) at their marks over
a quiet pad, and encodes the delivery file once.

  python video/compose.py          -> video/out/assay-walkthrough.mp4 (1920x1080, 30 fps, H.264 + AAC), timeline.json

Every shot is cut to its own near-lossless clip first: the 2880x1620 recording scaled once to the SCREEN rectangle on a
dark frame, with the caption band under it. Clips are joined with short dissolves; nothing in a recording is re-timed
unless a shot says "speed", and such a shot carries a visible tag saying so."""
import json
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SEGS, WORK, OUT, VO = HERE / "segs", HERE / "work", HERE / "out", HERE / "narration"
OVER = WORK / "overlays"
FPS, W, H = 30, 1920, 1080
# where the recording sits in the frame; the band below it holds the captions
SCREEN = (112, 24, 1696, 954)
FRAME_COLOR = "0x1f2b24"
DISSOLVE = 0.4

# What the edit is made of, in order. src/in/out are a recording and seconds in it; card is a full-frame overlay;
# over = [(overlay, start, end)] in seconds into the shot; say = [(narration line, seconds into the shot)].
SHOTS = [
    {"name": "title", "card": 6.5, "over": [("title", 0.0, 6.5)], "say": [("title", 0.6)]},
    # A normal run, live: typed, researched, ranked, drafted, checked, held for approval by the Analyst's flag, approved.
    {"name": "run", "src": "normal-3", "in": 4.0, "out": 29.8,
     "over": [("agents", 0.3, 4.1), ("tools", 4.3, 9.0), ("hybrid", 9.2, 15.5), ("checked", 15.7, 19.8), ("approval", 20.0, 25.8)],
     "say": [("ask", 0.3), ("research", 2.8), ("rank", 9.2), ("critic", 15.6), ("flag", 20.0)]},
    {"name": "review", "src": "normal-3", "in": 30.0, "out": 50.0,
     "over": [("removed", 0.4, 8.8), ("cite", 9.1, 14.4)],
     "say": [("held", 0.6), ("cite", 9.2), ("sources", 14.8)]},
    {"name": "trace", "src": "normal-3", "in": 51.2, "out": 64.8, "over": [("trace", 1.0, 13.4)], "say": [("trace", 1.4)]},
    # High impact by keyword, Impact left on auto: held, approved, released.
    {"name": "approval", "src": "approval-1", "in": 6.0, "out": 36.5,
     "over": [("budget", 5.6, 17.8), ("approval", 18.8, 30.3)],
     "say": [("health", 0.2), ("models", 5.8), ("medical", 18.9), ("released", 26.0)]},
    # The 77-page PDF uploaded into the session, then a question about it.
    # The upload took 12 s with nothing changing on screen but "Uploading llama2.pdf..."; the wait is cut, not sped up.
    {"name": "upload", "src": "longdoc-1", "in": 0.8, "out": 5.2, "over": [("longdoc", 0.5, 4.8)], "say": [("upload", 0.4)]},
    {"name": "uploaded", "src": "longdoc-1", "in": 13.0, "out": 16.8, "over": [("longdoc", 0.0, 3.6)]},
    {"name": "longdoc", "src": "longdoc-1", "in": 22.0, "out": 45.0, "over": [("cite", 16.8, 22.8)], "say": [("tokens", 1.0), ("arxiv", 16.4)]},
    # A false premise.
    {"name": "premise", "src": "premise-1", "in": 2.5, "out": 25.0, "over": [("premise", 15.8, 22.4)], "say": [("venus", 0.3), ("nomoon", 16.0)]},
    {"name": "end", "card": 8.0, "over": [("end", 0.0, 8.0)], "say": [("end", 0.6)]},
]


def run(args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)], check=True)


def length_of(shot):
    return shot["card"] if "card" in shot else (shot["out"] - shot["in"]) / shot.get("speed", 1)


def cut(index, shot):
    length = length_of(shot)
    clip = WORK / "shots" / f"{index:02d}-{shot['name']}.mkv"
    x, y, w, h = SCREEN
    if "card" in shot:
        args = ["-f", "lavfi", "-i", f"color=c=0xeef0ea:s={W}x{H}:r={FPS}:d={length:.3f}"]
        graph = ["[0:v]format=yuv420p[v0]"]
    else:
        speed = shot.get("speed", 1)
        args = ["-f", "lavfi", "-i", f"color=c={FRAME_COLOR}:s={W}x{H}:r={FPS}:d={length:.3f}",
                "-ss", shot["in"], "-to", shot["out"], "-i", SEGS / f"{shot['src']}.mp4"]
        retime = f"setpts=PTS/{speed}," if speed != 1 else ""
        # a still page sends no frames, so a recording ends at its last change; the last frame is held to the shot's end
        graph = [f"[1:v]{retime}fps={FPS},tpad=stop_mode=clone:stop_duration=30,scale={w}:{h}:flags=lanczos[s]",
                 f"[0:v][s]overlay={x}:{y}:shortest=1,format=yuv420p,trim=duration={length:.3f}[v0]"]
    last, inputs = "v0", 1 if "card" in shot else 2
    for k, (name, start, end) in enumerate(shot.get("over", [])):
        args += ["-loop", "1", "-t", f"{length:.3f}", "-i", OVER / f"{name}.png"]
        fade = f"fade=t=in:st={start}:d=0.35:alpha=1,fade=t=out:st={max(start, end - 0.35)}:d=0.35:alpha=1"
        graph.append(f"[{inputs + k}:v]format=rgba,{fade}[o{k}]")
        graph.append(f"[{last}][o{k}]overlay=0:0:format=auto[v{k + 1}]")
        last = f"v{k + 1}"
    run([*args, "-filter_complex", ";".join(graph), "-map", f"[{last}]", "-t", f"{length:.3f}", "-an",
         "-c:v", "libx264", "-preset", "slow", "-crf", "8", "-r", FPS, clip])
    return clip, length


def join(clips):
    args, graph = [], []
    for clip, _ in clips:
        args += ["-i", clip]
    offset, last = clips[0][1], "0:v"
    for i in range(1, len(clips)):
        d = SHOTS[i].get("dissolve", DISSOLVE)
        offset -= d
        graph.append(f"[{last}][{i}:v]xfade=transition=fade:duration={d}:offset={offset:.3f}[v{i}]")
        last = f"v{i}"
        offset += clips[i][1]
    joined = WORK / "joined.mkv"
    run([*args, "-filter_complex", ";".join(graph), "-map", f"[{last}]", "-c:v", "libx264", "-preset", "slow", "-crf", "8", joined])
    return joined, offset


def pad(total):
    """A quiet bed made from scratch: two slow, detuned chords that breathe in and out, low and soft under the voice."""
    rate = 48000
    t = np.arange(int(total * rate)) / rate
    chords = [(146.83, 220.0, 293.66, 369.99), (130.81, 196.0, 261.63, 329.63)]   # D and C, open voicings
    out = np.zeros((len(t), 2), np.float32)
    span = 8.0
    for k, chord in enumerate(chords):
        # each chord swells for 8 s, alternating, with a cosine crossfade
        phase = (t / span - k) % 2
        env = np.where(phase < 1, np.sin(np.pi * phase) ** 2, 0) * 0.5 + 0.08
        for n, f in enumerate(chord):
            for ch, detune in ((0, 0.997), (1, 1.003)):
                out[:, ch] += (0.05 / (n + 1)) * env * np.sin(2 * np.pi * f * detune * t + n)
    fade = np.minimum(1, np.minimum(t / 3, (total - t) / 4))[:, None]
    out *= fade
    path = WORK / "pad.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(rate), "-ac", "2", "-i", "-",
                    "-af", "lowpass=f=1800", str(path)], input=out.tobytes(), check=True)
    return path


def voice_track(total, starts):
    """Every narration line at its mark, on one track the length of the film."""
    args, graph, labels = [], [], []
    for i, shot in enumerate(SHOTS):
        for key, at in shot.get("say", []):
            delay = int((starts[i] + at) * 1000)
            args += ["-i", VO / f"{key}.wav"]
            graph.append(f"[{len(labels)}:a]adelay={delay}|{delay}[l{len(labels)}]")
            labels.append(f"[l{len(labels)}]")
    graph.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,apad,atrim=duration={total:.3f}[vo]")
    path = WORK / "voice.wav"
    run([*args, "-filter_complex", ";".join(graph), "-map", "[vo]", "-ar", "48000", "-ac", "2", path])
    return path


def main():
    (WORK / "shots").mkdir(parents=True, exist_ok=True)
    OUT.mkdir(exist_ok=True)
    clips = []
    for i, shot in enumerate(SHOTS):
        clips.append(cut(i, shot))
        print(f"cut {i:02d} {shot['name']:<14} {clips[-1][1]:5.1f} s")
    joined, total = join(clips)
    starts, at = [], 0.0
    for i, (_, length) in enumerate(clips):
        starts.append(at)
        at += length - (SHOTS[i + 1].get("dissolve", DISSOLVE) if i + 1 < len(SHOTS) else 0)
    (OUT / "timeline.json").write_text(json.dumps({s["name"]: round(v, 2) for s, v in zip(SHOTS, starts)}, indent=1), encoding="utf-8")
    voice, bed = voice_track(total, starts), pad(total)
    graph = ["[1:a]asplit=2[voice][key]",
             "[2:a]volume=0.55[bed]",
             # the pad steps back under the voice and comes up again in the gaps
             "[bed][key]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=700[ducked]",
             "[ducked][voice]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[mix]"]
    # the one delivery encode, straight from the near-lossless cut
    run(["-i", joined, "-i", voice, "-i", bed, "-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[mix]",
         "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "film", "-x264-params", "aq-mode=3:aq-strength=1.0",
         "-profile:v", "high", "-g", "60", "-bf", "2", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
         "-metadata", "title=assay: a walkthrough", OUT / "assay-walkthrough.mp4"])
    print(f"out/assay-walkthrough.mp4: {total:.1f} s")


if __name__ == "__main__":
    main()
