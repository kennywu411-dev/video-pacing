#!/usr/bin/env python3
"""Pacing check for short videos.

Measures what a viewer feels before they decide to keep watching or swipe:

  1. the opening: frames at 0, 1, 2, 3 and 5 s, whether the video opens on a blank frame, and how soon the first new thing appears
  2. beats: moments where something new appears on screen (a cut, a caption, a label, an object). A beat is either one part of the
     picture changing far more than it had been, or the whole picture changing at once
  3. waits: stretches longer than --max-gap seconds in which no beat arrives, marked "still" (nothing moves) or "moving" (there is
     motion, and no beat was found in it)

It measures pixels, not meaning. A flagged stretch is a place to look; whether it reads as "nothing new" is for a person or a model to decide
from the frames this script saves next to its report.

Needs ffmpeg and ffprobe on PATH. No Python packages.

    python3 pacing.py video.mp4                      # report in ./pacing-out/
    python3 pacing.py video.mp4 --out dir --strict   # exit 1 when the opening is blank or a stretch is flagged
    python3 pacing.py --beats 0,1.5,4,9.5 --duration 14   # check a planned beat table before anything is rendered
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import struct
import subprocess
import sys
from pathlib import Path

SAMPLE_FPS = 4          # frames compared per second: a beat is placed to the nearest 0.25 s
GRID = 16               # the picture is cut into this many blocks along its long side, each judged on its own
BLOCK = 20              # pixels per block side once the frame is shrunk: 320x180 for a 16:9 video
FLOOR = 7.0             # a block must change by at least this much (0-255, averaged over the block) to count
RATIO = 3.0             # ...and by this many times what is usual for its part of the picture. Lower values were tried: at 1.5,
                        # ordinary camera drift turned into beats and a cut its maker called too slow came out cleaner than the fix
LOOKBACK = 2.0          # "usual" is taken over this many seconds before and after
BUSY = 15.0             # the whole picture changing by this much in one step is new content whatever came before: a cut, a whip pan, a montage
MERGE = 0.6             # changes closer together than this are one beat (an element easing in, a crossfade)
LONG_BEAT = 1.5         # a beat that goes on for longer than this is reported as a busy stretch
STILL = 0.35            # average change per sampled frame below this, over a whole stretch, is "still"; above it is "moving"
BLANK_SPREAD = 8.0      # an opening frame whose brightness varies less than this is blank (black, white or one flat colour)
HOOK_FRAMES = (0, 1, 2, 3, 4, 5)
SHEET_WIDTH = 1920      # every saved image is this wide, so its frames stay readable when a model opens it


class NotAVideo(Exception):
    pass


def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True)


def probe(path) -> dict:
    """Duration, size and whether there is sound."""
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    try:
        info = json.loads(r.stdout or b"{}")
    except ValueError:
        info = {}
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    if r.returncode or video is None:
        raise NotAVideo(f"{path}: ffprobe found no video stream")
    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list") or []:
        rotation = int(side.get("rotation") or rotation)
    if abs(rotation) in (90, 270):
        width, height = height, width
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise NotAVideo(f"{path}: the video has no duration")
    return {"duration": duration, "width": width, "height": height, "audio": any(s.get("codec_type") == "audio" for s in streams)}


def _grid(width: int, height: int) -> tuple:
    """How the picture is cut into blocks: GRID of them along the long side."""
    if width >= height:
        return GRID, max(4, round(GRID * height / width))
    return max(4, round(GRID * width / height)), GRID


def _changes(path, cols: int, rows: int, fps: float):
    """For each pair of neighbouring sampled frames, how much each block changed (0-255, averaged over the block and its three colours).

    ffmpeg does the arithmetic: shrink, take the difference of each frame from the one before, then average every block down to one
    pixel. Colour, because a cut from red to green has the same brightness on both sides; 16 bits, so that slow drift is not rounded
    away to zero."""
    vf = (f"fps={fps},scale={cols * BLOCK}:{rows * BLOCK}:flags=area,format=gbrp16le,tblend=all_mode=difference,"
          f"scale={cols}:{rows}:flags=area,format=rgb48le")
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    count = cols * rows * 3
    unpack = struct.Struct(f"<{count}H").unpack
    try:
        while True:
            buf = proc.stdout.read(count * 2)
            if len(buf) < count * 2:
                return
            v = unpack(buf)
            yield [(v[i] + v[i + 1] + v[i + 2]) / 771 for i in range(0, count, 3)]      # 771 = 3 colours x 257, back to 0-255
    finally:
        proc.stdout.close()
        proc.wait()


def _first_frame_spread(path) -> float:
    """How much the brightness varies across the very first frame. Near zero means black, white or one flat colour."""
    r = _run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "scale=64:36:flags=area,format=gray", "-f", "rawvideo", "-"])
    return statistics.pstdev(r.stdout) if len(r.stdout) > 1 else 0.0


def measure(path, fps: float = SAMPLE_FPS) -> dict:
    """Per sampled step: the change in every block since the frame before, and over the whole picture."""
    info = probe(path)
    cols, rows = _grid(info["width"], info["height"])
    blocks = list(_changes(path, cols, rows, fps))
    spread = _first_frame_spread(path)
    if not blocks and spread == 0.0 and info["duration"] > 1 / fps:
        raise NotAVideo(f"{path}: no frame could be decoded")
    return {**info, "fps": fps, "cols": cols, "blocks": blocks, "whole": [sum(b) / len(b) for b in blocks], "first_spread": spread}


def _around(step: list, cols: int) -> list:
    """For each block, the largest change among it and the blocks touching it."""
    rows = len(step) // cols
    out = []
    for j in range(rows):
        for i in range(cols):
            most = 0.0
            for jj in range(max(0, j - 1), min(rows, j + 2)):
                for v in step[jj * cols + max(0, i - 1):jj * cols + min(cols, i + 2)]:
                    if v > most:
                        most = v
            out.append(most)
    return out


def new_steps(blocks: list, whole: list, cols: int, fps: float, floor: float = FLOOR, ratio: float = RATIO) -> list:
    """For each sampled step, whether something new arrived in it.

    Either the whole picture changed at once (a cut, a fast move), or some block changed far more than its part of the picture had been
    changing or goes on to change. Each block is judged against the blocks around it over the seconds before and after: a busy corner
    does not hide a caption arriving somewhere else, and steady motion never adds up to a beat, because an edge sliding across the
    picture is still nearby a moment later while a caption appears once and then holds still."""
    look = max(1, int(round(LOOKBACK * fps)))
    near = [_around(step, cols) for step in blocks]
    out = []
    for k, now in enumerate(blocks):
        if whole[k] >= BUSY:
            out.append(True)
            continue
        before, after = near[max(0, k - look):k], near[k + 1:k + 1 + look]
        hit = False
        for b, change in enumerate(now):
            if change < floor:
                continue
            usual = max(statistics.median(p[b] for p in before) if before else 0.0,
                        statistics.median(p[b] for p in after) if after else 0.0)
            if change >= ratio * usual:
                hit = True
                break
        out.append(hit)
    return out


def spans(flags: list, fps: float) -> list:
    """Steps with something new, joined into beats: [start, end] in seconds. Most are one moment; a montage is one long beat."""
    out = []
    for k, new in enumerate(flags):
        if not new:
            continue
        t = (k + 1) / fps
        if out and t - out[-1][1] <= MERGE:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def arrivals(flags: list, whole: list, fps: float) -> list:
    """The moments the clock restarts for the viewer. A caption that takes half a second to ease in arrived once, when it started.
    A picture that is being replaced step after step (a montage, a fast move) keeps arriving."""
    out, last = [], None
    for k, new in enumerate(flags):
        if not new:
            continue
        t = (k + 1) / fps
        if whole[k] >= BUSY or last is None or t - last > MERGE:
            out.append(t)
        last = t
    return out


def waits(times: list, duration: float, max_gap: float) -> list:
    """Stretches longer than `max_gap` from one arrival to the next. The video's start and end are edges too."""
    edges = [0.0] + [t for t in times if 0 < t < duration] + [duration]
    return [{"start": round(a, 2), "end": round(b, 2), "seconds": round(b - a, 2)} for a, b in zip(edges, edges[1:]) if b - a > max_gap]


def analyse(path, max_gap: float = 3.0, hook_seconds: float = 5.0, fps: float = SAMPLE_FPS) -> dict:
    m = measure(path, fps)
    duration = m["duration"]
    flags = new_steps(m["blocks"], m["whole"], m["cols"], fps)
    beat_spans = spans(flags, fps)
    arrived = arrivals(flags, m["whole"], fps)
    gaps = waits(arrived, duration, max_gap)
    for g in gaps:
        steps = m["whole"][int(round(g["start"] * fps)):int(round(g["end"] * fps)) - 1]     # between the two beats, not counting them
        g["kind"] = "still" if (sum(steps) / len(steps) if steps else 0.0) < STILL else "moving"
        g["ending"] = g["end"] >= round(duration, 2)
    beats = [round(a, 2) for a, _ in beat_spans]
    between = [b - a for a, b in zip([0.0] + arrived, arrived + [duration]) if b - a > 0]
    starts = [0.0] + [a for a, _ in beat_spans] + [duration]
    apart = [b - a for a, b in zip(starts, starts[1:]) if b - a > 0]      # beat to beat: a busy stretch counts once
    in_hook = [t for t in beats if t <= hook_seconds]
    return {
        "video": str(path), "duration": round(duration, 2), "width": m["width"], "height": m["height"], "audio": m["audio"],
        "max_gap": max_gap,
        "hook": {"seconds": hook_seconds, "opens_blank": m["first_spread"] < BLANK_SPREAD,
                 "first_beat": beats[0] if beats else None, "beats": len(in_hook),
                 "changing_seconds": round(sum(flags[:int(round(hook_seconds * fps))]) / fps, 2)},
        "beats": beats,
        "busy": [[round(a, 2), round(b, 2)] for a, b in beat_spans if b - a >= LONG_BEAT],
        "rhythm": {"beats": len(beats), "median_wait": round(statistics.median(apart), 2) if apart else None,
                   "longest_wait": round(max(between), 2) if between else None,
                   "seconds_in_flagged_stretches": round(sum(g["seconds"] for g in gaps), 1)},
        "gaps": gaps,
    }


# ------------------------------------------------------------------ frames to look at
def _grab(path, t: float, out: Path, width: int) -> bool:
    r = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2", str(out)])
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def _sheet(path, times: list, out: Path, tmp: Path, per_row: int) -> bool:
    """One image with the frames at `times`, in reading order, `per_row` to a row."""
    tmp.mkdir(parents=True, exist_ok=True)
    width = SHEET_WIDTH // per_row // 2 * 2
    shots = []
    for i, t in enumerate(times):
        shot = tmp / f"{out.stem}_{i}.jpg"
        if _grab(path, t, shot, width):
            shots.append(shot)
    if not shots:
        return False
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for shot in shots:
        cmd += ["-i", str(shot)]
    rows = [shots[i:i + per_row] for i in range(0, len(shots), per_row)]
    if len(shots) == 1:
        graph = "null"
    elif len(rows) == 1 or len(rows[-1]) != per_row:      # a ragged last row cannot be stacked under the others: one row instead
        graph = f"hstack=inputs={len(shots)}"
    else:
        parts, n = [], 0
        for r, row in enumerate(rows):
            parts.append("".join(f"[{n + i}]" for i in range(len(row))) + f"hstack=inputs={len(row)}[r{r}]")
            n += len(row)
        graph = ";".join(parts) + ";" + "".join(f"[r{r}]" for r in range(len(rows))) + f"vstack=inputs={len(rows)}"
    cmd += ["-filter_complex", graph, "-frames:v", "1", "-q:v", "3", str(out)]
    return _run(cmd).returncode == 0


def save_frames(path, result: dict, out: Path, most: int = 12) -> None:
    for old in list(out.glob("gap_*.jpg")) + [out / "hook.jpg"]:      # frames from an earlier run would not match this report
        old.unlink(missing_ok=True)
    tmp = out / ".frames"
    last = max(0.0, result["duration"] - 0.05)
    result["hook"]["frames_at"] = [t for t in HOOK_FRAMES if t <= last] or [0]
    _sheet(path, result["hook"]["frames_at"], out / "hook.jpg", tmp, per_row=3)
    step = 1 / SAMPLE_FPS
    longest = sorted(result["gaps"], key=lambda g: -g["seconds"])[:most]
    for n, g in enumerate(result["gaps"], 1):
        if g in longest:
            g["frames"] = f"gap_{n:02d}_{g['start']:.1f}-{g['end']:.1f}s.jpg"
            # every frame is inside the wait: after the beat that opened it has landed, and a full step before the one that ends it
            a, b = g["start"] + 0.15, min(last, g["end"] - (0.1 if g["ending"] else step + 0.1))
            cuts = 5 if g["seconds"] >= 6 else 3      # a long wait gets more frames, so a caption that changed inside it shows
            g["frames_at"] = [round(a + (b - a) * i / (cuts - 1), 2) for i in range(cuts)]
            _sheet(path, g["frames_at"], out / g["frames"], tmp, per_row=cuts)
    shutil.rmtree(tmp, ignore_errors=True)


def _clock(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def report(result: dict) -> str:
    hook, rhythm, gaps = result["hook"], result["rhythm"], result["gaps"]
    lines = [f"# Pacing: {Path(result['video']).name}", "",
             f"{_clock(result['duration'])} long, {result['width']}x{result['height']}, {'with' if result['audio'] else 'no'} sound.", "",
             f"## Opening (first {hook['seconds']:g} s)", ""]
    lines.append("- Opens on a blank frame: **yes**. The first frame is one flat colour." if hook["opens_blank"]
                 else "- Opens on a blank frame: no.")
    lines.append(f"- First new thing on screen: {hook['first_beat']:.2f} s." if hook["first_beat"] is not None
                 else "- First new thing on screen: never. Nothing changes in the whole video.")
    lines.append(f"- Beats that start in the first {hook['seconds']:g} s: {hook['beats']}.")
    if "frames_at" in hook:
        lines.append(f"- `hook.jpg` shows the frames at {', '.join(f'{t} s' for t in hook['frames_at'])}, in reading order, three to a row. "
                     "Look at it: is there a reason to stay, readable in the first frame?")
    lines.append("- Sound: " + ("yes. This script cannot hear, so it cannot tell narration from music." if result["audio"] else "none."))
    lines += ["", "## Rhythm", "",
              f"- Beats found: {rhythm['beats']}. A beat is something new arriving: a cut, a caption, a new element. "
              "Most are one moment. A montage or a fast move is one long beat, and something new keeps arriving all through it.",
              f"- Median time from the start of one beat to the start of the next: {rhythm['median_wait']} s.",
              f"- Longest time with nothing new arriving: {rhythm['longest_wait']} s.",
              f"- Waits over {result['max_gap']:g} s: {len(gaps)}, {rhythm['seconds_in_flagged_stretches']} s in all "
              f"({100 * rhythm['seconds_in_flagged_stretches'] / result['duration']:.0f}% of the video). "
              "This counts every wait the script flagged, before anyone has looked at them."]
    if gaps:
        lines += ["", f"## Waits over {result['max_gap']:g} s", "",
                  "A wait runs from the last moment something new arrived to the next one. After a long beat it starts where that beat ends.",
                  "`still`: nothing moves. `moving`: there is motion, and the script found no beat in it. That may be a slow push or floating "
                  "particles, or it may be a caption or a number the script could not see.",
                  "Each image shows frames from inside the wait, first to last, left to right (3 frames, or 5 for a wait of 6 s or more). "
                  "None of them is the beat that ends the wait. If the frames say the same thing, the viewer waited.", "",
                  "| # | From | To | Seconds | Kind | Frames | Frames at |", "|---|---|---|---|---|---|---|"]
        for n, g in enumerate(gaps, 1):
            kind = g["kind"] + (", the ending" if g["ending"] else "")
            at = ", ".join(f"{t:g} s" for t in g.get("frames_at", []))
            lines.append(f"| {n} | {_clock(g['start'])} | {_clock(g['end'])} | {g['seconds']:.1f} | {kind} | {('`' + g['frames'] + '`') if g.get('frames') else ''} | {at} |")
    long_beats = {a: b for a, b in result["busy"]}
    listed = [f"{_clock(t)}-{_clock(long_beats[t])}" if t in long_beats else _clock(t) for t in result["beats"]]
    lines += ["", "## Beats", "",
              "Start of each beat. A long beat is written start-end. For waits, use the table above, not the gaps in this list.", "", " ".join(listed) or "none", "",
              f"Duration for your own sums: {result['duration']:g} s.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the pacing of a short video: the opening, how often something new appears, and where nothing does.")
    ap.add_argument("video", nargs="?", help="the video file")
    ap.add_argument("--out", default="pacing-out", help="folder for report.md, pacing.json and the frames (default: pacing-out)")
    ap.add_argument("--max-gap", type=float, default=3.0, help="longest stretch without a beat that is not flagged, in seconds (default: 3)")
    ap.add_argument("--hook", type=float, default=5.0, help="how many seconds count as the opening (default: 5)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when the opening is blank, nothing new arrives in it, or any wait is flagged")
    ap.add_argument("--json", action="store_true", help="print the result as JSON instead of the report")
    ap.add_argument("--beats", help="check a planned beat table instead of a video: comma-separated seconds, or a file with one time per line")
    ap.add_argument("--duration", type=float, help="with --beats: how long the video will be, in seconds")
    args = ap.parse_args(argv)

    if args.beats is not None:
        text = Path(args.beats).read_text() if Path(args.beats).is_file() else args.beats
        try:
            beats = sorted(float(x) for x in text.replace("\n", ",").split(",") if x.strip())
        except ValueError:
            print("--beats takes seconds, like 0,1.5,4.2", file=sys.stderr)
            return 2
        duration = args.duration if args.duration is not None else (beats[-1] if beats else 0)
        gaps = waits(beats, duration, args.max_gap)
        if args.json:
            print(json.dumps({"beats": beats, "duration": duration, "gaps": gaps}))
        else:
            print(f"{len(beats)} beats over {duration:g} s. Stretches over {args.max_gap:g} s with no beat: {len(gaps)}.")
            for g in gaps:
                print(f"  {g['start']:.1f} s to {g['end']:.1f} s: {g['seconds']:.1f} s")
        return 1 if args.strict and gaps else 0

    if not args.video:
        ap.error("give a video file, or --beats")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"{tool} is not on PATH. Install ffmpeg first.", file=sys.stderr)
            return 2
    try:
        result = analyse(args.video, args.max_gap, args.hook)
    except NotAVideo as e:
        print(f"not a video this can read: {e}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    save_frames(args.video, result, out)
    text = report(result)
    (out / "report.md").write_text(text, encoding="utf-8")
    (out / "pacing.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False) if args.json else text + f"\nSaved to {out}/")
    failed = result["hook"]["opens_blank"] or result["hook"]["changing_seconds"] == 0 or bool(result["gaps"])
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    sys.exit(main())
