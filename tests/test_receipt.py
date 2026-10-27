"""The assembly receipt: what was actually cut, from mocked ffmpeg."""

from __future__ import annotations

import json

import pytest

from conftest import fake_ffmpeg
from keycut import (
    AssemblyReceipt,
    Segment,
    extract_and_concat_ranges,
    extract_and_concat_ranges_with_receipt,
    extract_and_concat_segments_with_receipt,
)


@pytest.fixture
def master(tmp_path):
    path = tmp_path / "master.mp4"
    path.write_bytes(b"not really a video")
    return path


def _run(master, tmp_path, ranges, durations=None, **kwargs):
    with fake_ffmpeg(durations=durations) as fake:
        path, receipt = extract_and_concat_ranges_with_receipt(
            master, ranges, tmp_path / "out.mp4", tmp_path / "work", **kwargs
        )
    return path, receipt, fake


class TestReceiptContent:
    def test_header_and_master(self, master, tmp_path):
        path, r, _ = _run(master, tmp_path, [(100.0, 160.0)], durations=[60.0, 60.0])
        d = r.to_dict()
        assert path == tmp_path / "out.mp4"
        assert d["schema"] == "keycut-assembly-receipt/1"
        assert d["provenance"] == "keycut"
        assert d["time_base"] == "seconds"
        assert d["master"] == {"path": str(master), "size_bytes": master.stat().st_size}
        assert d["requested"] == [[100.0, 160.0]]
        assert d["output"] == {"path": str(tmp_path / "out.mp4"), "duration": 60.0}

    def test_merge_reports_requested_indices(self, master, tmp_path):
        _, r, _ = _run(
            master,
            tmp_path,
            [(100.0, 160.0), (160.0, 200.0), (1250.0, 1290.0)],
            durations=[100.0, 40.0, 140.0],
        )
        assert [s.requested_indices for s in r.spans] == [(0, 1), (2,)]
        assert (r.spans[0].merged_start, r.spans[0].merged_end) == (100.0, 200.0)

    def test_out_of_order_ranges_keep_their_own_spans(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(500.0, 520.0), (100.0, 120.0)])
        assert [s.requested_indices for s in r.spans] == [(0,), (1,)]

    def test_copy_source_start_is_the_snapped_keyframe(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(99.5, 160.0)])
        span = r.spans[0]
        assert span.method == "copy" and span.reason == "stream_copy"
        assert span.merged_start == 99.5
        assert span.source_start == pytest.approx(100.1, abs=1e-4)
        assert span.source_end == 160.0

    def test_reencode_keyframes_unknown(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(99.5, 160.0)], keyframes=[])
        span = r.spans[0]
        assert (span.method, span.reason) == ("reencode", "keyframes_unknown")
        assert span.source_start == 99.5

    def test_reencode_snap_back_over_excluded(self, master, tmp_path):
        # No keyframe inside 100-160, so a copy would snap back to 0.0; second 50
        # is banned and lies in that region.
        mask = [False] * 200
        mask[50] = True
        _, r, _ = _run(
            master, tmp_path, [(100.0, 160.0)], exclude_mask=mask, keyframes=[0.0, 200.0]
        )
        span = r.spans[0]
        assert (span.method, span.reason) == ("reencode", "snap_back_over_excluded")
        assert span.source_start == 100.0

    def test_output_spans_are_contiguous_from_zero_using_probed_durations(
        self, master, tmp_path
    ):
        _, r, _ = _run(
            master,
            tmp_path,
            [(100.0, 160.0), (1250.0, 1290.0)],
            durations=[59.9, 40.1, 100.0],
        )
        a, b = r.spans
        assert (a.output_start, a.output_end) == (0.0, 59.9)
        assert b.output_start == a.output_end
        assert b.output_end == pytest.approx(100.0)
        assert a.duration_source == b.duration_source == "probed"
        assert r.output_duration == 100.0

    def test_probe_failure_falls_back_to_planned(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(99.5, 160.0), (1250.0, 1290.0)])
        a, b = r.spans
        assert a.duration_source == b.duration_source == "planned"
        assert a.output_end - a.output_start == pytest.approx(160.0 - a.source_start)
        assert (a.output_start, b.output_start) == (0.0, a.output_end)
        assert r.output_duration is None

    def test_max_gap_is_recorded(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(0.0, 10.0)], max_gap_sec=1.5)
        assert r.max_gap_sec == 1.5

    def test_round_trip_and_write_json(self, master, tmp_path):
        _, r, _ = _run(master, tmp_path, [(100.0, 160.0)], durations=[60.0, 60.0])
        assert AssemblyReceipt.from_dict(r.to_dict()) == r
        out = r.write_json(tmp_path / "sub" / "receipt.json")
        assert AssemblyReceipt.from_dict(json.loads(out.read_text())) == r


class TestFailureAndCompat:
    def test_failure_gives_none_pair(self, master, tmp_path):
        with fake_ffmpeg(returncode=1):
            assert extract_and_concat_ranges_with_receipt(
                master, [(1.0, 2.0)], tmp_path / "o.mp4", tmp_path / "w"
            ) == (None, None)

    def test_missing_master_gives_none_pair(self, tmp_path):
        assert extract_and_concat_ranges_with_receipt(
            tmp_path / "gone.mp4", [(1.0, 2.0)], tmp_path / "o.mp4", tmp_path / "w"
        ) == (None, None)

    def test_plain_function_still_returns_only_the_path(self, master, tmp_path):
        with fake_ffmpeg():
            result = extract_and_concat_ranges(
                master, [(1.0, 2.0)], tmp_path / "o.mp4", tmp_path / "w"
            )
        assert result == tmp_path / "o.mp4"


class TestSegments:
    def test_segments_with_receipt(self, master, tmp_path):
        with fake_ffmpeg():
            path, r = extract_and_concat_segments_with_receipt(
                master,
                [Segment(0, 5.0, 10.0)],
                [(0.0, 100.0), (100.0, 200.0)],
                tmp_path / "o.mp4",
                tmp_path / "w",
            )
        assert path == tmp_path / "o.mp4"
        assert r.requested == ((5.0, 10.0),)

    def test_bad_segment_gives_none_pair(self, master, tmp_path):
        assert extract_and_concat_segments_with_receipt(
            master, [Segment(9, 0.0, 1.0)], [(0.0, 100.0)], tmp_path / "o.mp4", tmp_path / "w"
        ) == (None, None)
