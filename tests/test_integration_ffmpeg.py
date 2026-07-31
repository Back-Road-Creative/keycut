"""End-to-end tests against a real ffmpeg, on a video generated on the fly.

These are the tests that matter: they demonstrate the ``-ss -c copy`` artefact
on an actual file, then show that keycut does not produce it. Skipped when
ffmpeg and ffprobe are not on PATH.

The fixture video is synthetic — ffmpeg's own ``testsrc2`` pattern plus a sine
tone. No sample media ships with this repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from keycut import extract_and_concat_ranges, probe_keyframes
from keycut.cli import main

pytestmark = requires_ffmpeg

FIXTURE_DURATION = 30.0
FIXTURE_FPS = 30
FIXTURE_GOP_FRAMES = 60  # a keyframe every 2.0 seconds
FIXTURE_GOP_SEC = FIXTURE_GOP_FRAMES / FIXTURE_FPS


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    """A 30 s, 320x240, 30 fps H.264 clip with a keyframe every 2.0 s exactly.

    Scene-cut keyframes are disabled so the GOP structure is predictable, which
    is what makes the assertions below arithmetic rather than approximate.
    """
    path = tmp_path_factory.mktemp("keycut-src") / "source.mp4"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size=320x240:rate={FIXTURE_FPS}:duration={FIXTURE_DURATION}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={FIXTURE_DURATION}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", str(FIXTURE_GOP_FRAMES), "-keyint_min", str(FIXTURE_GOP_FRAMES),
        "-x264-params", f"keyint={FIXTURE_GOP_FRAMES}:min-keyint={FIXTURE_GOP_FRAMES}"
                        ":scenecut=0:open-gop=0",
        "-c:a", "aac", "-b:a", "64k", "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return path


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, check=True, timeout=60,
    )
    return float(out.stdout.decode().strip())


def video_packets(path: Path) -> list[tuple[float, str]]:
    """Every video packet as ``(pts_seconds, flags)``.

    Packet-level truth, deliberately. A container's reported *duration* can
    disagree with the packets it holds, and in the case this suite is about, it
    does.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
        capture_output=True, check=True, timeout=120,
    )
    packets = []
    for line in out.stdout.decode().splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            try:
                packets.append((float(parts[0]), parts[1]))
            except ValueError:
                continue
    return packets


def concat_alone(path: Path, work: Path) -> Path:
    """Push one file through the concat demuxer unchanged."""
    work.mkdir(parents=True, exist_ok=True)
    manifest = work / "list.txt"
    manifest.write_text(f"file '{path.resolve()}'\n")
    out = work / "recat.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(manifest), "-c", "copy", str(out)],
        check=True, capture_output=True, timeout=120,
    )
    return out


def decodes_cleanly(path: Path) -> bool:
    """True when ffmpeg can decode the whole file without emitting an error."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, timeout=300,
    )
    return result.returncode == 0 and not result.stderr.strip()


@pytest.fixture(scope="module")
def naive_copy(source, tmp_path_factory) -> Path:
    """``ffmpeg -ss 10.5 -to 12.5 -i src -c copy`` — the advice from every forum.

    2.0 seconds requested, starting mid-GOP.
    """
    path = tmp_path_factory.mktemp("keycut-naive") / "naive.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "10.5", "-to", "12.5",
         "-i", str(source), "-c", "copy", str(path)],
        check=True, capture_output=True, timeout=120,
    )
    return path


class TestTheArtefact:
    """The bug, on a real file, at packet level."""

    def test_the_fixture_has_the_keyframe_layout_the_tests_assume(self, source):
        keyframes = probe_keyframes(source)
        assert keyframes is not None
        assert keyframes[0] == pytest.approx(0.0, abs=1e-3)
        assert keyframes[1] == pytest.approx(FIXTURE_GOP_SEC, abs=1e-3)
        assert len(keyframes) == pytest.approx(FIXTURE_DURATION / FIXTURE_GOP_SEC, abs=1)

    def test_the_naive_copy_carries_frames_from_before_the_cut(self, naive_copy):
        """2.0 s was asked for; 2.5 s of frames come back.

        ``-c copy`` cannot start anywhere but a keyframe, so it backs up from
        10.5 to 10.0 and the extra 15 frames are in the file.
        """
        packets = video_packets(naive_copy)
        expected = int(2.0 * FIXTURE_FPS)
        assert len(packets) == expected + FIXTURE_GOP_FRAMES // 4, (
            f"{len(packets)} frames for a {expected}-frame request"
        )

    def test_the_extra_frames_are_hidden_behind_an_edit_list(self, naive_copy):
        """Why this ships to production so often.

        The mp4 muxer gives the pre-cut frames negative timestamps and a
        discard flag, and writes an edit list so players skip them. Check the
        duration and the file looks perfect — 2.0 s, exactly as ordered. The
        frames are still there.
        """
        packets = video_packets(naive_copy)
        assert packets[0][0] < 0, "the pre-cut frames are timestamped before zero"
        assert "D" in packets[0][1], "and flagged discard"
        assert probe_duration(naive_copy) == pytest.approx(2.0, abs=0.1), (
            "the reported duration is the edit list's answer, not the packets'"
        )

    def test_the_hidden_frames_come_back_the_moment_you_concatenate(
        self, naive_copy, tmp_path
    ):
        """And concatenating is the entire point of multi-range extraction.

        The concat demuxer reads packets. It does not read edit lists. Every
        frame the edit list was hiding is played.
        """
        rejoined = concat_alone(naive_copy, tmp_path / "work")
        assert probe_duration(rejoined) == pytest.approx(2.5, abs=0.2), (
            "the half-second that was hidden is now audible and visible"
        )
        assert len(video_packets(rejoined)) == len(video_packets(naive_copy))

    def test_keycut_emits_no_frames_from_before_the_cut(self, source, tmp_path):
        """The fix. Nothing to hide, so nothing to resurface.

        Every packet has a non-negative timestamp and none is flagged discard —
        the signature of the artefact is simply absent.
        """
        work = tmp_path / "work"
        out = extract_and_concat_ranges(
            source, [(10.5, 12.5)], tmp_path / "out.mp4", work, keep_scratch=True
        )
        assert out is not None

        piece = next(work.glob("keycut_*.mp4"))
        for pts, flags in video_packets(piece):
            assert pts >= 0.0, "a frame from before the requested start survived"
            assert "D" not in flags, "a discarded frame is still a frame in the file"

        rejoined = concat_alone(piece, work / "c")
        assert probe_duration(out) == pytest.approx(probe_duration(rejoined), abs=0.1), (
            "what you get is what the packets say, before and after concatenation"
        )
        assert decodes_cleanly(out)

    def test_the_cost_of_the_fix_is_omission_never_inclusion(self, source, tmp_path):
        """The honest trade.

        10.5 is mid-GOP, so the start moves forward to the keyframe at 12.0 and
        the output is the half-second [12.0, 12.5) rather than the two seconds
        asked for. keycut gives you less than you requested; it never gives you
        something you excluded.
        """
        out = extract_and_concat_ranges(
            source, [(10.5, 12.5)], tmp_path / "out.mp4", tmp_path / "work"
        )
        assert out is not None
        assert probe_duration(out) == pytest.approx(0.5, abs=0.15)


class TestRealExtractions:
    def test_contiguous_ranges_become_one_join_free_extraction(self, source, tmp_path):
        work = tmp_path / "work"
        out = extract_and_concat_ranges(
            source, [(4.0, 8.0), (8.0, 12.0)], tmp_path / "out.mp4", work,
            keep_scratch=True,
        )
        assert out is not None
        pieces = sorted(p.name for p in work.glob("keycut_*.mp4"))
        assert pieces == ["keycut_000.mp4"], "the internal join must be removed"
        assert probe_duration(out) == pytest.approx(8.0, abs=0.35)
        assert decodes_cleanly(out)

    def test_disjoint_ranges_concat_to_the_sum_of_their_lengths(self, source, tmp_path):
        out = extract_and_concat_ranges(
            source, [(2.0, 6.0), (16.0, 20.0)], tmp_path / "out.mp4", tmp_path / "work"
        )
        assert out is not None
        assert probe_duration(out) == pytest.approx(8.0, abs=0.35)
        assert decodes_cleanly(out)

    def test_a_sub_gop_gap_is_absorbed_rather_than_faked(self, source, tmp_path):
        # A 1.0s gap inside a 2.0s GOP. There is no keyframe to cut on, so the
        # gap is kept: 4.0 -> 9.0 continuous, not 4.0s + 4.0s.
        out = extract_and_concat_ranges(
            source, [(4.0, 8.0), (9.0, 13.0)], tmp_path / "out.mp4", tmp_path / "work"
        )
        assert out is not None
        assert probe_duration(out) == pytest.approx(9.0, abs=0.35)

    def test_the_reencode_fallback_concats_with_its_stream_copied_neighbour(
        self, source, tmp_path
    ):
        """The compatibility claim, verified.

        Range one is 1.0s starting at 4.5 — shorter than a GOP and containing
        no keyframe, so it cannot be aligned, and a copy would replay [4.0,
        4.5). Second 4 is excluded, so that range must be re-encoded. Range two
        starts on a keyframe and is copied. The two then have to concat, which
        only works if the re-encode matched the source's codec and pixel format.
        """
        exclude = [False] * 31
        exclude[4] = True
        out = extract_and_concat_ranges(
            source, [(4.5, 5.5), (20.0, 24.0)], tmp_path / "out.mp4", tmp_path / "work",
            exclude_mask=exclude,
        )
        assert out is not None
        assert probe_duration(out) == pytest.approx(5.0, abs=0.35)
        assert decodes_cleanly(out), "the re-encoded piece did not splice cleanly"

    def test_an_accurate_seek_re_encode_delivers_the_exact_span(self, source, tmp_path):
        """``keyframes=[]`` declares them unusable, so every range re-encodes.

        This is the one path that returns exactly what was asked for — 60
        frames, no more and no fewer — and it pins the semantics keycut relies
        on: with ``-ss``/``-to`` placed after ``-i``, both are still read in the
        input's timeline, so ``-to 12.5`` means 12.5 in the source and not 12.5
        seconds after the seek.
        """
        out = extract_and_concat_ranges(
            source, [(10.5, 12.5)], tmp_path / "out.mp4", tmp_path / "work",
            keyframes=[],
        )
        assert out is not None
        assert len(video_packets(out)) == int(2.0 * FIXTURE_FPS)
        assert decodes_cleanly(out)


class TestCliAgainstRealFfmpeg:
    def test_the_command_line_produces_a_playable_file(self, source, tmp_path, capsys):
        out = tmp_path / "cli.mp4"
        code = main([str(source), str(out), "-r", "0:02", "0:06", "-r", "0:16", "0:20"])
        assert code == 0
        assert capsys.readouterr().out.strip() == str(out)
        assert probe_duration(out) == pytest.approx(8.0, abs=0.35)
        assert decodes_cleanly(out)

    def test_dry_run_reports_the_measured_gop(self, source, tmp_path, capsys):
        code = main([str(source), str(tmp_path / "o.mp4"), "-r", "4", "8", "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert "keyframes: 15" in out
        assert "merge gap: 2.1" in out, "measured from the fixture's real 2.0s GOP"
