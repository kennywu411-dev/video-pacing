"""Each test builds a few seconds of video with a known answer and checks what pacing.py says about it."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pacing  # noqa: E402

SIZE = "320x180"
RATE = "20"


def make(path, filters, seconds, source=None):
    source = source or f"color=c=0x303030:s={SIZE}:r={RATE}"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(seconds)]
    if filters:
        cmd += ["-vf", filters]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def box(x, start, end, y=20, w=120, h=30):
    """A bright block that shows up at one place for a while: what a caption or a label does to the picture."""
    return f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill:enable='between(t,{start},{end})'"


DRIFT = f"smptebars=s=1280x180:r={RATE},crop=w=320:h=180:x='20+t*12':y=0"   # a still image sliding sideways: motion, nothing new


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    captions = ",".join(box(20 + 60 * (i % 3), 2 * i, 2 * i + 2) for i in range(1, 5))
    cuts = ("color=c=red:s={s}:r={r}:d=2[a];color=c=green:s={s}:r={r}:d=2[b];color=c=blue:s={s}:r={r}:d=2[c];"
            "color=c=white:s={s}:r={r}:d=2[d];[a][b][c][d]concat=n=4:v=1:a=0").format(s=SIZE, r=RATE)
    return {
        "still": make(d / "still.mp4", None, 8),
        "cuts": make(d / "cuts.mp4", None, 8, source=cuts),
        "captions": make(d / "captions.mp4", captions, 10),
        "drift": make(d / "drift.mp4", None, 8, source=DRIFT),
        "montage": make(d / "montage.mp4", None, 6, source=";".join(
            f"color=c={c}:s={SIZE}:r={RATE}:d=0.5[s{i}]" for i, c in enumerate(["red", "white", "blue", "yellow"] * 3))
            + ";" + "".join(f"[s{i}]" for i in range(12)) + "concat=n=12:v=1:a=0"),
        "paced": make(d / "paced.mp4", ",".join(box(20 + 60 * (i % 3), 2 * i, 2 * i + 2).replace("white", "black") for i in range(1, 4)), 8, source=DRIFT),
        "drift_caption": make(d / "drift_caption.mp4", box(40, 4, 8).replace("white", "black"), 8, source=DRIFT),
        "black_open": make(d / "black_open.mp4", "drawbox=x=0:y=0:w=320:h=180:color=black:t=fill:enable='lt(t,2)',"
                           + box(40, 2, 6), 6, source=f"color=c=0x808080:s={SIZE}:r={RATE}"),
        "busy_corner": make(d / "busy_corner.mp4", box(40, 4, 8, y=20), 8,
                            source=f"color=c=0x303030:s={SIZE}:r={RATE}[bg];color=c=yellow:s=40x40:r={RATE}[sq];[bg][sq]overlay=x='200+40*sin(8*t)':y=120"),
    }


def near(times, expected, tol=0.3):
    return len(times) == len(expected) and all(abs(a - b) <= tol for a, b in zip(times, expected))


def test_a_still_picture_has_no_beat_and_is_one_still_stretch(clips):
    r = pacing.analyse(clips["still"])
    assert r["beats"] == []
    assert [g["kind"] for g in r["gaps"]] == ["still"]
    assert r["gaps"][0]["start"] == 0 and r["gaps"][0]["end"] == pytest.approx(8, abs=0.3)


def test_hard_cuts_are_beats(clips):
    """Red to green is a cut between two colours of the same brightness: a grey-only comparison would miss it."""
    r = pacing.analyse(clips["cuts"])
    assert near(r["beats"], [2, 4, 6])
    assert r["gaps"] == []


def test_a_caption_sized_change_is_a_beat(clips):
    r = pacing.analyse(clips["captions"])
    assert near(r["beats"], [2, 4, 6, 8])
    assert r["gaps"] == []


def test_motion_with_nothing_new_is_one_moving_wait(clips):
    r = pacing.analyse(clips["drift"])
    assert r["beats"] == []
    assert [g["kind"] for g in r["gaps"]] == ["moving"]


def test_a_caption_over_moving_background_is_still_found(clips):
    r = pacing.analyse(clips["drift_caption"])
    assert near(r["beats"], [4])
    assert [(g["kind"], round(g["start"]), round(g["end"]), g["ending"]) for g in r["gaps"]] == [("moving", 0, 4, False), ("moving", 4, 8, True)]


def test_a_busy_corner_does_not_hide_a_caption_elsewhere(clips):
    r = pacing.analyse(clips["busy_corner"])
    assert near(r["beats"], [4])


def test_a_blank_opening_is_reported(clips):
    r = pacing.analyse(clips["black_open"])
    assert r["hook"]["opens_blank"] is True
    assert r["hook"]["first_beat"] == pytest.approx(2, abs=0.3)
    assert pacing.analyse(clips["captions"])["hook"]["opens_blank"] is True     # flat grey is blank too
    assert pacing.analyse(clips["drift"])["hook"]["opens_blank"] is False


def test_the_hook_window_counts_its_beats(clips):
    r = pacing.analyse(clips["captions"], hook_seconds=5)
    assert r["hook"]["beats"] == 2
    assert r["hook"]["changing_seconds"] == 0.5


def test_a_montage_is_one_busy_stretch_and_no_wait(clips):
    """Cuts every half second: no single moment stands out from its neighbours, and the picture is new the whole time."""
    r = pacing.analyse(clips["montage"])
    assert r["gaps"] == []
    assert len(r["busy"]) == 1 and r["busy"][0][1] - r["busy"][0][0] >= 4.5
    assert r["hook"]["changing_seconds"] >= 2


def test_something_that_eases_in_arrived_when_it_started():
    """A caption fading in over three steps is one arrival; the wait that follows runs from its first step."""
    calm, part = 1.0, 9.0
    flags = [False, True, True, True] + [False] * 14 + [True]
    assert pacing.arrivals(flags, [calm, part, part, part] + [calm] * 14 + [part], fps=4) == [0.5, 4.75]


def test_a_picture_replaced_step_after_step_keeps_arriving():
    flags = [True] * 8
    assert pacing.arrivals(flags, [40.0] * 8, fps=4) == [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def test_max_gap_decides_what_is_flagged(clips):
    assert pacing.analyse(clips["cuts"], max_gap=1.5)["gaps"] != []
    assert pacing.analyse(clips["still"], max_gap=10)["gaps"] == []


def test_waits_from_a_planned_beat_table():
    gaps = pacing.waits([0, 1.5, 4.0, 9.5, 11], duration=14, max_gap=3)
    assert [(g["start"], g["end"]) for g in gaps] == [(4.0, 9.5)]
    assert pacing.waits([], duration=5, max_gap=3) == [{"start": 0, "end": 5, "seconds": 5}]


def test_the_command_writes_a_report_frames_and_json(clips, tmp_path):
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "pacing.py"), str(clips["drift_caption"]), "--out", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    data = json.loads((out / "pacing.json").read_text())
    assert near(data["beats"], [4])
    text = (out / "report.md").read_text()
    assert text.count("| moving") == 2 and "the ending" in text
    assert (out / "hook.jpg").stat().st_size > 0
    assert len(list(out.glob("gap_*.jpg"))) == 2
    first, second = data["gaps"]
    assert first["start"] < first["frames_at"][0] and first["frames_at"][-1] <= first["end"] - 0.3      # no frame shows the beat that ends the wait
    assert second["frames_at"][-1] < second["end"]


def test_strict_fails_when_a_stretch_is_flagged(clips, tmp_path):
    run = lambda name: subprocess.run([sys.executable, str(ROOT / "scripts" / "pacing.py"), str(clips[name]), "--out", str(tmp_path / name),
                                       "--strict"], capture_output=True, text=True).returncode
    assert run("still") == 1
    assert run("cuts") == 1      # every cut lands in time, but it opens on one flat colour
    assert run("paced") == 0


def test_the_beat_table_command(tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "pacing.py"), "--beats", "0,1.5,4,9.5,11", "--duration", "14"],
                       capture_output=True, text=True)
    assert p.returncode == 0 and "4.0" in p.stdout and "9.5" in p.stdout


def test_a_file_that_is_not_a_video_is_an_error_with_a_reason(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "pacing.py"), str(bad)], capture_output=True, text=True)
    assert p.returncode == 2 and "video" in p.stderr.lower()
