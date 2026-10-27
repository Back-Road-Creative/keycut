"""Extraction orchestration: what ffmpeg is actually asked to do, and why."""

from __future__ import annotations

import json

import pytest

from conftest import (
    DEFAULT_STREAM_JSON,
    fake_ffmpeg,
    is_reencode,
    is_stream_copy,
    keyframe_csv,
    seek_is_input_side,
)
from keycut import (
    Segment,
    concat_demuxer,
    default_reencode_args,
    extract_and_concat_ranges,
    extract_and_concat_segments,
    probe_video_params,
)


@pytest.fixture
def master(tmp_path):
    path = tmp_path / "master.mp4"
    path.write_bytes(b"not really a video")
    return path


def _ss(cmd):
    return float(cmd[cmd.index("-ss") + 1])


def _to(cmd):
    return float(cmd[cmd.index("-to") + 1])


def _t(cmd):
    return float(cmd[cmd.index("-t") + 1])


def _sparse(path, size):
    with path.open("wb") as fh:
        fh.truncate(size)  # no disk is used
    return path


class TestHappyPath:
    def test_one_extraction_per_range_then_a_concat(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            result = extract_and_concat_ranges(
                master,
                [(100.0, 160.0), (1250.0, 1290.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert result == tmp_path / "out.mp4"
        assert len(fake.extract_commands) == 2
        assert len(fake.concat_commands) == 1
        for cmd in fake.extract_commands:
            assert is_stream_copy(cmd)

    def test_requested_order_is_preserved_in_the_concat_manifest(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master,
                [(100.0, 160.0), (1250.0, 1290.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert fake.list_files, "the concat demuxer was never invoked"
        lines = [ln for ln in fake.list_files[0].splitlines() if ln.startswith("file ")]
        assert "keycut_000" in lines[0]
        assert "keycut_001" in lines[1]

    def test_stream_copy_seeks_on_the_input_side(self, master, tmp_path):
        # -ss before -i is a demuxer seek: no decoding, and the reason the
        # keyframe alignment is needed at all.
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", tmp_path / "work"
            )
        assert seek_is_input_side(fake.extract_commands[0])

    def test_scratch_pieces_are_removed_by_default(self, master, tmp_path):
        work = tmp_path / "work"
        with fake_ffmpeg():
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", work
            )
        assert list(work.iterdir()) == []

    def test_keep_scratch_leaves_the_pieces_and_the_manifest(self, master, tmp_path):
        work = tmp_path / "work"
        with fake_ffmpeg():
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", work, keep_scratch=True
            )
        names = sorted(p.name for p in work.iterdir())
        assert "keycut_000.mp4" in names
        assert "out_concat.txt" in names


class TestKeyframeAlignment:
    def test_a_mid_gop_start_is_advanced_to_the_next_keyframe(self, master, tmp_path):
        # Keyframes every 2.002s; 99.5 is mid-GOP, so the copy must start at
        # 50 * 2.002 = 100.1 rather than snapping back to 98.098.
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(99.5, 160.0)], tmp_path / "out.mp4", tmp_path / "work"
            )
        cmd = fake.extract_commands[0]
        assert _ss(cmd) == pytest.approx(100.1, abs=1e-4)
        assert _to(cmd) == 160.0

    def test_contiguous_ranges_merge_into_one_aligned_extraction(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master,
                [(99.5, 160.0), (160.0, 220.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert len(fake.extract_commands) == 1, "the internal join must be removed"
        cmd = fake.extract_commands[0]
        assert _ss(cmd) == pytest.approx(100.1, abs=1e-4)
        assert _to(cmd) == 220.0

    def test_the_merge_gap_is_measured_from_the_masters_own_keyframes(
        self, master, tmp_path
    ):
        # A 10-second GOP: a 6-second gap is sub-GOP here and must be absorbed,
        # even though it would be a real cut under the 2.1s fallback.
        with fake_ffmpeg(keyframe_stdout=keyframe_csv(interval=10.0, count=60)) as fake:
            extract_and_concat_ranges(
                master,
                [(0.0, 100.0), (106.0, 200.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert len(fake.extract_commands) == 1

    def test_an_explicit_max_gap_overrides_the_measurement(self, master, tmp_path):
        with fake_ffmpeg(keyframe_stdout=keyframe_csv(interval=10.0, count=60)) as fake:
            extract_and_concat_ranges(
                master,
                [(0.0, 100.0), (106.0, 200.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                max_gap_sec=1.0,
            )
        assert len(fake.extract_commands) == 2

    def test_injected_keyframes_skip_the_probe(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master,
                [(99.5, 160.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                keyframes=[0.0, 50.0, 150.0, 250.0],
                reencode_args=["-c:v", "libx264"],
            )
        assert not any("packet=" in " ".join(c) for c in fake.commands)
        assert _ss(fake.extract_commands[0]) == 150.0


class TestReencodeFallback:
    def test_unknown_keyframes_force_an_accurate_seek_re_encode(self, master, tmp_path):
        # ffprobe succeeded but returned no keyframe lines: positions unknown,
        # so a copy would snap back an unknown distance.
        with fake_ffmpeg(keyframe_stdout=b"") as fake:
            result = extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", tmp_path / "work"
            )
        assert result is not None
        cmd = fake.extract_commands[0]
        assert is_reencode(cmd)
        assert seek_is_input_side(cmd), (
            "an output-side seek decodes everything before the cut; a re-encode "
            "with an input-side seek is just as exact (ffmpeg's -accurate_seek)"
        )
        assert _ss(cmd) == 100.0, "the re-encode lands on the exact requested frame"
        assert _t(cmd) == 60.0, "the input seek resets timestamps, so length is -t"
        after_input = cmd[cmd.index("-i"):]
        assert "-ss" in after_input and float(after_input[after_input.index("-ss") + 1]) == 0.0, (
            "stream-copied audio keeps the packets back to the keyframe at negative "
            "timestamps; an output-side -ss 0 drops them"
        )

    def test_a_clean_cut_still_takes_the_lossless_path(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", tmp_path / "work"
            )
        assert is_stream_copy(fake.extract_commands[0])
        assert not is_reencode(fake.extract_commands[0])

    def test_a_copy_that_would_snap_back_over_excluded_footage_re_encodes(
        self, master, tmp_path
    ):
        # Sparse keyframes: none inside [100, 160), so a copy would snap back
        # to 0.0 and replay [0, 100) — which contains an excluded second.
        mask = [False] * 1200
        mask[50] = True
        with fake_ffmpeg(keyframe_stdout=b"0.000000,K__\n200.000000,K__\n") as fake:
            extract_and_concat_ranges(
                master,
                [(100.0, 160.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                exclude_mask=mask,
            )
        assert is_reencode(fake.extract_commands[0])

    def test_the_same_snap_back_over_clean_footage_still_copies(self, master, tmp_path):
        mask = [False] * 1200
        with fake_ffmpeg(keyframe_stdout=b"0.000000,K__\n200.000000,K__\n") as fake:
            extract_and_concat_ranges(
                master,
                [(100.0, 160.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                exclude_mask=mask,
            )
        assert is_stream_copy(fake.extract_commands[0])

    def test_allow_reencode_false_copies_even_when_keyframes_are_unknown(
        self, master, tmp_path
    ):
        with fake_ffmpeg(keyframe_stdout=b"") as fake:
            extract_and_concat_ranges(
                master,
                [(100.0, 160.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                allow_reencode=False,
            )
        cmd = fake.extract_commands[0]
        assert is_stream_copy(cmd)
        assert not any("stream=" in " ".join(c) for c in fake.commands), (
            "no re-encode means no need to probe encoder parameters"
        )

    def test_custom_reencode_args_are_used_verbatim(self, master, tmp_path):
        with fake_ffmpeg(keyframe_stdout=b"") as fake:
            extract_and_concat_ranges(
                master,
                [(100.0, 160.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
                reencode_args=["-c:v", "libx265", "-crf", "30"],
            )
        cmd = fake.extract_commands[0]
        assert "libx265" in cmd and "30" in cmd


class TestFailureHandling:
    def test_missing_master_returns_none(self, tmp_path):
        assert (
            extract_and_concat_ranges(
                tmp_path / "gone.mp4", [(0.0, 1.0)], tmp_path / "o.mp4", tmp_path / "w"
            )
            is None
        )

    def test_no_ranges_returns_none(self, master, tmp_path):
        assert (
            extract_and_concat_ranges(master, [], tmp_path / "o.mp4", tmp_path / "w")
            is None
        )

    def test_a_failing_ffmpeg_returns_none_and_stops(self, master, tmp_path):
        with fake_ffmpeg(returncode=1) as fake:
            result = extract_and_concat_ranges(
                master,
                [(10.0, 20.0), (100.0, 120.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert result is None
        assert len(fake.extract_commands) == 1, "it must not keep cutting after a failure"

    def test_a_missing_ffmpeg_binary_returns_none(self, master, tmp_path):
        result = extract_and_concat_ranges(
            master,
            [(10.0, 20.0)],
            tmp_path / "out.mp4",
            tmp_path / "work",
            ffmpeg_bin="definitely-not-a-real-binary",
            ffprobe_bin="definitely-not-a-real-binary",
        )
        assert result is None


class TestExtractAndConcatSegments:
    def test_segments_are_translated_through_the_boundaries(self, master, tmp_path):
        boundaries = [(0.0, 1200.0), (1200.0, 2100.0)]
        with fake_ffmpeg() as fake:
            result = extract_and_concat_segments(
                master,
                [Segment(1, 50.0, 90.0)],
                boundaries,
                tmp_path / "out.mp4",
                tmp_path / "work",
                keyframes=[1250.0, 1300.0],
                reencode_args=["-c:v", "libx264"],
            )
        assert result is not None
        assert _ss(fake.extract_commands[0]) == 1250.0
        assert _to(fake.extract_commands[0]) == 1290.0

    def test_a_bad_source_index_fails_visibly_rather_than_cutting_short(
        self, master, tmp_path
    ):
        with fake_ffmpeg() as fake:
            result = extract_and_concat_segments(
                master,
                [Segment(0, 0.0, 10.0), Segment(9, 0.0, 10.0)],
                [(0.0, 1200.0)],
                tmp_path / "out.mp4",
                tmp_path / "work",
            )
        assert result is None
        assert fake.extract_commands == [], "nothing is cut when a reference is wrong"


class TestProbeVideoParams:
    def test_parses_the_first_video_stream(self, master):
        with fake_ffmpeg() as _:
            params = probe_video_params(master)
        assert params["codec_name"] == "h264"
        assert params["pix_fmt"] == "yuv420p"

    def test_probe_failure_gives_an_empty_dict(self, master):
        with fake_ffmpeg(stream_stdout=b"{}"):
            assert probe_video_params(master) == {}

    def test_unparseable_output_gives_an_empty_dict(self, master):
        with fake_ffmpeg(stream_stdout=b"not json"):
            assert probe_video_params(master) == {}


class TestDefaultReencodeArgs:
    """The re-encoded piece has to be concat-compatible with the copies around
    it — same codec, same pixel format, same colour signalling — or the join
    fails or plays back wrong."""

    def test_matches_the_masters_codec_and_colour_metadata(self, master):
        with fake_ffmpeg():
            args = default_reencode_args(master)
        assert args[:2] == ["-c:v", "libx264"]
        assert "-crf" in args
        assert args[args.index("-pix_fmt") + 1] == "yuv420p"
        assert args[args.index("-colorspace") + 1] == "bt709"
        assert args[-2:] == ["-c:a", "copy"], "audio stays bit-identical"

    def test_hevc_master_gets_an_hevc_encoder(self, master):
        payload = json.dumps(
            {"streams": [{"codec_name": "hevc", "pix_fmt": "yuv420p10le"}]}
        ).encode()
        with fake_ffmpeg(stream_stdout=payload):
            args = default_reencode_args(master)
        assert args[:2] == ["-c:v", "libx265"]
        assert args[args.index("-pix_fmt") + 1] == "yuv420p10le"

    def test_unresolved_colour_metadata_is_not_forwarded(self, master):
        payload = json.dumps(
            {
                "streams": [
                    {"codec_name": "h264", "pix_fmt": "yuv420p", "color_space": "unknown"}
                ]
            }
        ).encode()
        with fake_ffmpeg(stream_stdout=payload):
            args = default_reencode_args(master)
        assert "-colorspace" not in args

    def test_an_unknown_codec_falls_back_to_h264(self, master):
        payload = json.dumps({"streams": [{"codec_name": "wackyvid"}]}).encode()
        with fake_ffmpeg(stream_stdout=payload):
            args = default_reencode_args(master)
        assert args[:2] == ["-c:v", "libx264"]
        assert args[args.index("-pix_fmt") + 1] == "yuv420p"

    def test_a_failed_probe_still_produces_usable_args(self, master):
        with fake_ffmpeg(stream_stdout=b"{}"):
            args = default_reencode_args(master)
        assert args[:2] == ["-c:v", "libx264"]

    def test_the_default_stream_json_fixture_is_what_the_probe_reads(self, master):
        # Guards the fixture itself: if DEFAULT_STREAM_JSON drifts, every
        # assertion above silently starts testing something else.
        assert b"h264" in DEFAULT_STREAM_JSON


class TestLargeMasterTimeouts:
    """A day master can be 100+ GB on a slow mount. Every ffmpeg step gets a
    window sized to the work, so a healthy run is never killed as a hang."""

    def test_a_stream_copy_window_scales_with_the_master(self, tmp_path):
        master = _sparse(tmp_path / "day_master.mp4", 118_000_000_000)
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", tmp_path / "work",
                keyframes=[100.0, 200.0],
            )
        copy_idx = fake.commands.index(fake.extract_commands[0])
        assert fake.timeouts[copy_idx] >= 118_000_000_000 / 20_000_000

    def test_a_small_stream_copy_keeps_the_1800s_floor(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(100.0, 160.0)], tmp_path / "out.mp4", tmp_path / "work",
                keyframes=[100.0, 200.0],
            )
        copy_idx = fake.commands.index(fake.extract_commands[0])
        assert fake.timeouts[copy_idx] == 1800.0

    def test_a_re_encode_window_scales_with_the_range_length(self, master, tmp_path):
        """A 1147 s range of 4K re-encoded with x265 on a CPU runs for hours,
        far past the flat 3600 s."""
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(3000.0, 4147.0)], tmp_path / "out.mp4", tmp_path / "work",
                keyframes=[], reencode_args=["-c:v", "libx265"],
            )
        idx = fake.commands.index(fake.extract_commands[0])
        assert fake.timeouts[idx] >= 1147.0 * 20

    def test_a_short_re_encode_keeps_the_3600s_floor(self, master, tmp_path):
        with fake_ffmpeg() as fake:
            extract_and_concat_ranges(
                master, [(100.0, 102.0)], tmp_path / "out.mp4", tmp_path / "work",
                keyframes=[], reencode_args=["-c:v", "libx265"],
            )
        idx = fake.commands.index(fake.extract_commands[0])
        assert fake.timeouts[idx] == 3600.0

    def test_a_concat_window_scales_with_the_pieces(self, tmp_path):
        parts = [
            _sparse(tmp_path / "a.mp4", 30_000_000_000),
            _sparse(tmp_path / "b.mp4", 21_000_000_000),
        ]
        with fake_ffmpeg() as fake:
            concat_demuxer(parts, tmp_path / "out.mp4", tmp_path / "list.txt",
                           faststart=False)
            concat_demuxer(parts, tmp_path / "out2.mp4", tmp_path / "list2.txt")
        plain, faststart = fake.timeouts
        assert plain >= 51_000_000_000 / 20_000_000
        assert faststart >= 2 * 51_000_000_000 / 20_000_000, (
            "+faststart rewrites the whole output a second time"
        )
