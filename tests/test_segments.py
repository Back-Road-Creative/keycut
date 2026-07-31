"""Translating per-part segment references into master time."""

from __future__ import annotations

import pytest

from keycut import (
    Segment,
    boundaries_from_durations,
    remap_segment_indices,
    segment_to_master_range,
    segments_to_master_ranges,
)


class TestBoundariesFromDurations:
    def test_boundaries_start_at_zero_and_accumulate(self):
        assert boundaries_from_durations([1200.0, 900.0, 600.0]) == [
            (0.0, 1200.0),
            (1200.0, 2100.0),
            (2100.0, 2700.0),
        ]

    def test_no_parts_gives_no_boundaries(self):
        assert boundaries_from_durations([]) == []

    def test_zero_length_part_keeps_its_slot(self):
        # The index space has to stay aligned with the part list even when a
        # part contributes nothing, or every later index shifts by one.
        assert boundaries_from_durations([10.0, 0.0, 5.0]) == [
            (0.0, 10.0),
            (10.0, 10.0),
            (10.0, 15.0),
        ]

    def test_none_duration_is_treated_as_zero(self):
        assert boundaries_from_durations([10.0, None]) == [(0.0, 10.0), (10.0, 10.0)]


class TestSegmentToMasterRange:
    BOUNDARIES = [(0.0, 1200.0), (1200.0, 2100.0)]

    def test_first_part_is_an_identity_translation(self):
        assert segment_to_master_range(Segment(0, 100.0, 200.0), self.BOUNDARIES) == (
            100.0,
            200.0,
        )

    def test_later_part_is_offset_by_everything_before_it(self):
        assert segment_to_master_range(Segment(1, 50.0, 150.0), self.BOUNDARIES) == (
            1250.0,
            1350.0,
        )

    def test_uneven_part_lengths(self):
        boundaries = boundaries_from_durations([3240.0, 3115.0])
        s, e = segment_to_master_range(Segment(1, 1572.0, 1658.0), boundaries)
        assert (s, e) == (3240.0 + 1572.0, 3240.0 + 1658.0)

    def test_out_of_range_index_raises_rather_than_clamping(self):
        # A clamped index produces a plausible extraction of the wrong footage.
        with pytest.raises(IndexError, match="out of range"):
            segment_to_master_range(Segment(5, 0.0, 10.0), self.BOUNDARIES)
        with pytest.raises(IndexError):
            segment_to_master_range(Segment(-1, 0.0, 10.0), self.BOUNDARIES)

    def test_batch_translation_preserves_order(self):
        segments = [Segment(1, 0.0, 10.0), Segment(0, 5.0, 6.0)]
        assert segments_to_master_ranges(segments, self.BOUNDARIES) == [
            (1200.0, 1210.0),
            (5.0, 6.0),
        ]


class TestRemapSegmentIndices:
    """Whatever selects segments usually works across a whole catalogue, while
    the boundary list is per-master. Mixing the index spaces either raises and
    drops the output, or — when the ranges happen to overlap — cuts the wrong
    footage without complaint."""

    def test_single_part_master_at_a_non_zero_global_index(self):
        out = remap_segment_indices([Segment(5, 10.0, 20.0)], [5])
        assert out == [Segment(0, 10.0, 20.0)]

    def test_non_contiguous_global_indices(self):
        segments = [Segment(2, 0.0, 5.0), Segment(3, 10.0, 20.0), Segment(4, 30.0, 40.0)]
        out = remap_segment_indices(segments, [2, 3, 4])
        assert [s.source_index for s in out] == [0, 1, 2]

    def test_already_local_indices_are_unchanged(self):
        segments = [Segment(0, 0.0, 10.0), Segment(1, 20.0, 30.0)]
        assert remap_segment_indices(segments, [0, 1]) == segments

    def test_reordered_parts_are_remapped_by_position(self):
        out = remap_segment_indices([Segment(9, 1.0, 2.0)], [4, 9, 7])
        assert out[0].source_index == 1

    def test_segment_from_another_master_raises(self):
        with pytest.raises(IndexError, match="not among this master"):
            remap_segment_indices([Segment(7, 0.0, 10.0)], [5])

    def test_empty_input_is_a_no_op(self):
        assert remap_segment_indices([], [0, 1, 2]) == []

    def test_input_segments_are_not_mutated(self):
        original = Segment(5, 10.0, 20.0)
        remap_segment_indices([original], [5])
        assert original.source_index == 5
