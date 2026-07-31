"""Keyframe probing, alignment, and GOP measurement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from keycut import (
    DEFAULT_GOP_DURATION_SEC,
    clear_keyframe_cache,
    estimate_gop_duration,
    keyframe_at_or_before,
    probe_keyframes,
    snap_start_to_keyframe,
)

KFS = [0.0, 2.002, 4.004, 6.006]


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_keyframe_cache()
    yield
    clear_keyframe_cache()


class TestSnapStartToKeyframe:
    """A cut start moves FORWARD to a keyframe — never backward, which is what
    ``-ss … -c copy`` does on its own and is precisely the artefact."""

    def test_mid_gop_start_moves_forward(self):
        assert snap_start_to_keyframe(2.5, 100.0, KFS) == 4.004

    def test_start_already_on_a_keyframe_is_unchanged(self):
        assert snap_start_to_keyframe(2.002, 100.0, KFS) == 2.002

    def test_no_keyframe_before_the_end_limit_keeps_the_original_start(self):
        # Snapping to 4.004 would put the start past the end of the range.
        # A loose cut beats an empty one.
        assert snap_start_to_keyframe(2.5, 3.5, KFS) == 2.5

    def test_unknown_keyframes_keep_the_original_start(self):
        assert snap_start_to_keyframe(2.5, 100.0, None) == 2.5
        assert snap_start_to_keyframe(2.5, 100.0, []) == 2.5

    def test_a_start_a_hair_past_a_keyframe_is_left_alone(self):
        # Within a millisecond of a keyframe counts as already aligned. Moving
        # it forward would throw away a whole GOP to avoid a sub-millisecond
        # snap-back, and moving it back is never allowed.
        assert snap_start_to_keyframe(2.0021, 100.0, KFS) == 2.0021


class TestKeyframeAtOrBefore:
    def test_returns_the_keyframe_a_copy_would_land_on(self):
        assert keyframe_at_or_before(3.0, KFS) == 2.002
        assert keyframe_at_or_before(2.002, KFS) == 2.002

    def test_none_before_the_first_keyframe_or_without_keyframes(self):
        assert keyframe_at_or_before(-1.0, KFS) is None
        assert keyframe_at_or_before(5.0, None) is None


class TestEstimateGopDuration:
    def test_measures_a_regular_two_second_gop(self):
        keyframes = [i * 2.002 for i in range(100)]
        assert estimate_gop_duration(keyframes) == pytest.approx(2.002 * 1.05, rel=1e-6)

    def test_scene_cut_keyframes_do_not_shrink_the_estimate(self):
        # A regular 2s GOP with extra keyframes crammed in at scene changes.
        # Those create SHORT gaps; the estimate must still describe the long ones.
        keyframes = sorted([i * 2.0 for i in range(50)] + [10.4, 10.9, 22.3])
        assert estimate_gop_duration(keyframes) == pytest.approx(2.0 * 1.05, rel=0.05)

    def test_falls_back_when_too_few_keyframes(self):
        assert estimate_gop_duration([]) == DEFAULT_GOP_DURATION_SEC
        assert estimate_gop_duration([1.0]) == DEFAULT_GOP_DURATION_SEC
        assert estimate_gop_duration(None) == DEFAULT_GOP_DURATION_SEC
        assert estimate_gop_duration([2.0, 2.0]) == DEFAULT_GOP_DURATION_SEC

    def test_explicit_default_is_honoured(self):
        assert estimate_gop_duration([], default=5.0) == 5.0


class TestProbeKeyframes:
    def test_parses_packet_flags_and_caches_per_file(self, tmp_path):
        master = tmp_path / "master.mp4"
        master.write_bytes(b"not really a video")
        stdout = b"0.000000,K__\n1.001000,___\n2.002000,K__\nN/A,K__\n"

        calls = []

        def _fake_run(cmd, *a, **kw):
            calls.append(list(cmd))
            return MagicMock(returncode=0, stdout=stdout, stderr=b"")

        with patch("keycut.keyframes.subprocess.run", side_effect=_fake_run):
            first = probe_keyframes(master)
            second = probe_keyframes(master)

        assert first == [0.0, 2.002], "only K-flagged packets with real timestamps count"
        assert second == first
        assert len(calls) == 1, "a second probe of the same file must hit the cache"
        assert calls[0][0] == "ffprobe"

    def test_missing_file_returns_none(self, tmp_path):
        assert probe_keyframes(tmp_path / "nope.mp4") is None

    def test_nonzero_exit_returns_none(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with patch(
            "keycut.keyframes.subprocess.run",
            return_value=MagicMock(returncode=1, stdout=b"", stderr=b"boom"),
        ):
            assert probe_keyframes(master) is None

    def test_no_keyframe_lines_returns_none_not_empty_list(self, tmp_path):
        """``None`` means "unknown" and must stay distinguishable from "no
        keyframes", because callers re-encode on unknown."""
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with patch(
            "keycut.keyframes.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
        ):
            assert probe_keyframes(master) is None

    def test_missing_ffprobe_binary_returns_none(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with patch("keycut.keyframes.subprocess.run", side_effect=FileNotFoundError):
            assert probe_keyframes(master) is None

    def test_cache_can_be_bypassed(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        calls = []

        def _fake_run(cmd, *a, **kw):
            calls.append(1)
            return MagicMock(returncode=0, stdout=b"0.0,K__\n", stderr=b"")

        with patch("keycut.keyframes.subprocess.run", side_effect=_fake_run):
            probe_keyframes(master, use_cache=False)
            probe_keyframes(master, use_cache=False)
        assert len(calls) == 2
