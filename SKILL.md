---
name: video-pacing
description: Use when a short video (Douyin, TikTok, Reels, Shorts, Bilibili) is about to be rendered or published, when a cut feels slow or draggy, when viewers swipe away in the first seconds or retention drops mid-video, when asked to check a video's hook, opening, pacing or rhythm, or when a storyboard's timing needs checking before anything is rendered. 短视频节奏、前五秒钩子、开头留不住人、拖沓、完播率、信息密度
---

# Video pacing

## Overview

Viewers leave when nothing new arrives. Two things decide most of it: what the first 5 seconds show, and the longest wait between one new thing and the next.

**Measure, then look, then judge.** The script measures. You look at the frames it saves. Only then say what is wrong. Pacing judged from the script, the storyboard or a feeling is a guess.

## The rules

| | Rule | Checked by |
|---|---|---|
| Opening | A reason to stay is on screen by 5 s, and the first frame already has a clear subject and readable text | `hook.jpg`, your eyes |
| Rhythm | Something new every 1.5 to 3 s | the script's beats |
| Waits | No stretch over 3 s with nothing new | the script's waits, then your eyes |

**New** means the viewer can read or see something they could not a moment ago:

- a new caption line replacing the one on screen, or more words added to it
- a new object, label or list item
- a number that jumps to a new value, or a readout that appears
- a reveal, a cut, a change of framing
- a change of state the viewer is meant to notice: something removed, crossed out, switched on

**Not new:**

- floating, breathing, particles, loops, a slow push-in. Motion is not information.
- a clock, counter or progress ring that runs at a steady rate. It was already running.
- an empty container: a panel, a frame or a bare heading with nothing in it yet. It is new when its content arrives.

**A hook is one of four things:** a claim that contradicts what the viewer believes; a question or puzzle they can answer on the spot; a situation they are in right now; something they have seen but cannot name. A title card, a logo, a slow establishing shot or a line that warms up is not a hook.

## Workflow

1. Run the script (needs `ffmpeg`; about 10 to 20 s for a two-minute video):

   ```bash
   python3 scripts/pacing.py VIDEO --out OUT_DIR
   ```

   `scripts/pacing.py` is next to this file. Write `OUT_DIR` somewhere outside the video's own folder.

2. Read the report it prints. The same text is saved as `OUT_DIR/report.md`.

3. Open `OUT_DIR/hook.jpg`: the frames at 0, 1, 2, 3, 4 and 5 s, in reading order, three to a row. Write down what the first frame says, which of the four hooks it is, and the first of those frames it is on. A hook that first shows in the 5 s frame counts, and is late. When a hook is a setup and a payoff (a claim at 0 s, its reversal at 4 s), give both seconds. If you cannot name the hook, there is none.

4. Open the image for **every** wait in the report. Its frames are all from inside the wait; the beat that ends it is not shown. The script compares pixels and cannot read. It under-detects a caption that changes over a moving background, and anything that fades in slowly.
   - A later frame shows a caption, number, label or object that the first frame does not: something new arrived. Strike this wait and note what arrived.
   - The frames say the same thing: confirmed wait.
   - Apply the lists above. A steadily running clock or an empty panel does not strike a wait.

5. Report in the format below.

**Before anything is rendered,** check the plan instead: list the time of every caption start, cut and on-screen event from the storyboard, then

```bash
python3 scripts/pacing.py --beats 0,1.5,4.2,6.0 --duration 150
```

Run `python3 scripts/pacing.py --help` for the other options (`--max-gap`, `--hook`, `--strict`, `--json`).

## What the report to the user contains

In this order:

1. **Opening.** One line: the hook, its type, and the second it lands. Or: no hook by 5 s, and what is on screen instead.
2. **Confirmed waits.** A table: from, to, seconds, what the viewer is looking at, the fix. Mark the ending and any deliberate pause as such. With none confirmed, say so in one line.
3. **Struck waits.** One line each: the wait and what arrived in it.
4. **Numbers.** From the script: beats, median time between beats, longest time with nothing new, and the waits it flagged with their share of the runtime. One you work out: seconds in confirmed waits divided by the duration at the bottom of the report. Give both shares and label them: the script's counts every flagged wait, yours only the confirmed ones.

Every measured number comes from `report.md` or `pacing.json`, as measured: when you strike a wait, the script's beat count and median stay as they are, and you say what it missed. Every judgement names the image it rests on.

## Fixing a confirmed wait

- Cut the stretch shorter.
- Add a beat inside it: split the caption so its second half arrives mid-way, bring in a label or a number, reveal a state.
- Reframe the same footage: a punch-in or a new crop every 3 s or less gives a change of framing without rendering again.
- Move the next beat earlier.
- The ending: a wait after the last beat is the tail. Cut it to the length of one beat, or put the closing line there.

Adding decorative motion fixes nothing.

## Common mistakes

| Mistake | Instead |
|---|---|
| Judging the rendered video from its script or timeline | Run the script on the file. Timings shift in a render; a caption planned for 0 s can be missing from frame 0 |
| Counting movement as something new | Ask what the viewer can read now that they could not before |
| Reporting the waits list without opening the images | Open each one; some are captions the script could not see |
| Dismissing a wait because the music or the voice carries it | Say so explicitly and let the user decide; do not strike it silently |
| Quoting thresholds from memory or from other guides | Use the three rules above and the numbers in the report |
| Building your own detector with ffmpeg filters | The script already did it, the same way every time |

## When the rules do not apply

- **Sound.** The script cannot hear, and `report.md` only says whether there is sound. If you cannot tell whether it is narration or music, say so in the report. With narration, a confirmed wait is a wait in the picture only: someone may be saying something new over it. Keep it in the table, labelled that way, and let the user decide.
- **Edits cut to music** (lyric videos, beat-synced montages) follow the beat, not the 1.5 to 3 s rule. Check the opening only, and say why.
- **A deliberate pause** (a quiz the viewer needs time to answer) is a wait the script will flag. Keep it in the confirmed table, marked as intended, with its length.
