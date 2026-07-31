"""Range merging and the per-second exclusion mask."""

from __future__ import annotations

from keycut import (
    as_mask_lookup,
    copy_would_snap_back_over_excluded,
    merge_ranges,
    span_has_excluded_second,
)


class TestMergeRanges:
    """Every internal join is a place a stream copy can replay a GOP. Merging
    removes joins that do not need to exist."""

    def test_touching_ranges_become_one(self):
        assert merge_ranges([(10.0, 40.0), (40.0, 70.0)]) == [(10.0, 70.0)]

    def test_overlapping_ranges_become_one(self):
        assert merge_ranges([(10.0, 40.0), (30.0, 70.0)]) == [(10.0, 70.0)]

    def test_sub_gop_gap_is_absorbed(self):
        # A 1.5s gap with a 2.1s GOP: there is no keyframe inside it to cut on,
        # so keeping it is honest and cutting it is a stutter.
        assert merge_ranges([(10.0, 40.0), (41.5, 70.0)]) == [(10.0, 70.0)]

    def test_gap_wider_than_a_gop_stays_a_real_cut(self):
        apart = [(10.0, 40.0), (100.0, 130.0)]
        assert merge_ranges(apart) == apart

    def test_backwards_jump_never_merges(self):
        # Out-of-order is a deliberate edit, not a mistake to tidy up.
        backwards = [(100.0, 130.0), (10.0, 40.0)]
        assert merge_ranges(backwards) == backwards

    def test_nested_range_does_not_shorten_the_merge(self):
        assert merge_ranges([(10.0, 70.0), (20.0, 30.0)]) == [(10.0, 70.0)]

    def test_chain_of_three_collapses(self):
        assert merge_ranges([(0.0, 10.0), (10.0, 20.0), (21.0, 30.0)]) == [(0.0, 30.0)]

    def test_custom_gap_threshold(self):
        pair = [(10.0, 40.0), (45.0, 70.0)]
        assert merge_ranges(pair, max_gap_sec=10.0) == [(10.0, 70.0)]
        assert merge_ranges(pair, max_gap_sec=1.0) == pair

    def test_empty_input(self):
        assert merge_ranges([]) == []


class TestMergeRangesWithExclusionMask:
    def test_excluded_second_in_a_sub_gop_gap_forces_a_split(self):
        # Merging would re-admit the very second the caller banned.
        mask = [False] * 100
        mask[40] = True
        assert merge_ranges([(10.0, 40.0), (41.5, 70.0)], is_excluded=mask) == [
            (10.0, 40.0),
            (41.5, 70.0),
        ]

    def test_the_same_gap_still_merges_when_nothing_is_excluded(self):
        mask = [False] * 100
        assert merge_ranges([(10.0, 40.0), (41.5, 70.0)], is_excluded=mask) == [(10.0, 70.0)]

    def test_mask_may_be_a_callable(self):
        assert merge_ranges(
            [(10.0, 40.0), (41.5, 70.0)], is_excluded=lambda sec: sec == 40
        ) == [(10.0, 40.0), (41.5, 70.0)]

    def test_overlapping_ranges_merge_even_with_an_excluded_second_nearby(self):
        # There is no gap to re-admit when the ranges overlap.
        mask = [False] * 100
        mask[35] = True
        assert merge_ranges([(10.0, 40.0), (30.0, 70.0)], is_excluded=mask) == [(10.0, 70.0)]


class TestAsMaskLookup:
    def test_none_stays_none(self):
        assert as_mask_lookup(None) is None

    def test_callable_passes_through(self):
        def f(sec):
            return sec == 3

        assert as_mask_lookup(f) is f

    def test_sequence_is_bounds_checked(self):
        lookup = as_mask_lookup([False, True, False])
        assert lookup(1) is True
        assert lookup(0) is False
        assert lookup(99) is False, "a second past the mask reads False, never raises"
        assert lookup(-1) is False

    def test_unusable_object_becomes_none(self):
        assert as_mask_lookup(object()) is None


class TestSpanHasExcludedSecond:
    def test_a_second_covers_its_whole_interval(self):
        lookup = as_mask_lookup({40}.__contains__)
        assert span_has_excluded_second(40.0, 41.5, lookup) is True
        assert span_has_excluded_second(40.9, 41.0, lookup) is True
        assert span_has_excluded_second(41.0, 42.0, lookup) is False

    def test_empty_or_inverted_span_is_clean(self):
        lookup = as_mask_lookup({40}.__contains__)
        assert span_has_excluded_second(40.0, 40.0, lookup) is False
        assert span_has_excluded_second(41.0, 40.0, lookup) is False

    def test_no_mask_is_always_clean(self):
        assert span_has_excluded_second(0.0, 1e6, None) is False


class TestCopyWouldSnapBackOverExcluded:
    KFS = [0.0, 200.0]

    def test_snap_back_across_an_excluded_second_is_detected(self):
        # A copy starting at 100 snaps back to the keyframe at 0, replaying
        # [0, 100) — which contains excluded second 50.
        lookup = as_mask_lookup({50}.__contains__)
        assert copy_would_snap_back_over_excluded(100.0, self.KFS, lookup) is True

    def test_snap_back_across_clean_footage_is_tolerated(self):
        lookup = as_mask_lookup({500}.__contains__)
        assert copy_would_snap_back_over_excluded(100.0, self.KFS, lookup) is False

    def test_a_start_on_a_keyframe_replays_nothing(self):
        lookup = as_mask_lookup({50}.__contains__)
        assert copy_would_snap_back_over_excluded(200.0, self.KFS, lookup) is False

    def test_no_mask_means_nothing_to_protect(self):
        assert copy_would_snap_back_over_excluded(100.0, self.KFS, None) is False

    def test_start_before_the_first_keyframe_measures_from_zero(self):
        lookup = as_mask_lookup({2}.__contains__)
        assert copy_would_snap_back_over_excluded(5.0, [100.0], lookup) is True
