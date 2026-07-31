"""Attributing derived clips back to the output they were cut from."""

from __future__ import annotations

from keycut import Segment, assign_clips_by_source_window, boundaries_from_durations

# Two source parts, 100s each, concatenated into a 200s master.
BOUNDARIES = boundaries_from_durations([100.0, 100.0])

GROUPS = {
    "a": [Segment(0, 0.0, 40.0)],  # master 0-40
    "b": [Segment(0, 60.0, 100.0), Segment(1, 0.0, 20.0)],  # master 60-100, 100-120
}


class TestContainment:
    def test_a_clip_inside_a_groups_footage_is_attributed_to_it(self):
        clips = [(10.0, 20.0), (70.0, 80.0), (105.0, 110.0)]
        assert assign_clips_by_source_window(clips, GROUPS, BOUNDARIES) == {
            "a": [0],
            "b": [1, 2],
        }

    def test_every_group_gets_an_entry_even_with_no_clips(self):
        result = assign_clips_by_source_window([(10.0, 20.0)], GROUPS, BOUNDARIES)
        assert set(result) == {"a", "b"}
        assert result["b"] == []

    def test_clip_indices_keep_their_original_order(self):
        clips = [(70.0, 80.0), (10.0, 20.0), (75.0, 85.0)]
        assert assign_clips_by_source_window(clips, GROUPS, BOUNDARIES)["b"] == [0, 2]

    def test_attribution_uses_the_midpoint_not_the_edges(self):
        # Starts inside "a" but its middle is past the end of a's footage.
        clips = [(35.0, 75.0)]
        assert assign_clips_by_source_window(clips, GROUPS, BOUNDARIES)["b"] == [0]


class TestNearestFallback:
    def test_a_clip_no_group_covers_goes_to_the_nearest_one(self):
        # Midpoint 50 sits in the gap: 10s past a's end, 10s before b's start.
        # Equidistant, so the first group in iteration order wins.
        clips = [(45.0, 55.0)]
        assert assign_clips_by_source_window(clips, GROUPS, BOUNDARIES)["a"] == [0]

    def test_nearest_is_measured_to_the_closest_edge(self):
        clips = [(56.0, 58.0)]  # midpoint 57, 17s past a, 3s before b
        assert assign_clips_by_source_window(clips, GROUPS, BOUNDARIES)["b"] == [0]

    def test_a_clip_beyond_every_group_still_lands_somewhere(self):
        clips = [(190.0, 200.0)]
        result = assign_clips_by_source_window(clips, GROUPS, BOUNDARIES)
        assert sum(len(v) for v in result.values()) == 1, "a clip is never dropped"
        assert result["b"] == [0]


class TestDegenerateInputs:
    def test_no_groups_gives_an_empty_result(self):
        assert assign_clips_by_source_window([(0.0, 1.0)], {}, BOUNDARIES) == {}

    def test_no_clips_gives_empty_buckets(self):
        assert assign_clips_by_source_window([], GROUPS, BOUNDARIES) == {"a": [], "b": []}

    def test_a_group_with_no_resolvable_footage_never_wins_containment(self):
        groups = {"empty": [], "real": [Segment(0, 0.0, 50.0)]}
        assert assign_clips_by_source_window([(10.0, 20.0)], groups, BOUNDARIES) == {
            "empty": [],
            "real": [0],
        }

    def test_a_group_with_no_footage_still_absorbs_clips_when_it_is_the_only_one(self):
        groups = {"empty": []}
        assert assign_clips_by_source_window([(10.0, 20.0)], groups, BOUNDARIES) == {
            "empty": [0]
        }

    def test_an_out_of_range_segment_is_skipped_not_fatal(self):
        # A partially-resolvable group still attributes better than none.
        groups = {"a": [Segment(9, 0.0, 10.0), Segment(0, 0.0, 50.0)]}
        assert assign_clips_by_source_window([(10.0, 20.0)], groups, BOUNDARIES) == {"a": [0]}

    def test_zero_length_segments_are_ignored(self):
        groups = {"a": [Segment(0, 10.0, 10.0)], "b": [Segment(0, 0.0, 50.0)]}
        assert assign_clips_by_source_window([(10.0, 20.0)], groups, BOUNDARIES)["b"] == [0]

    def test_integer_keys_work_as_well_as_strings(self):
        groups = {0: [Segment(0, 0.0, 50.0)], 1: [Segment(0, 50.0, 100.0)]}
        assert assign_clips_by_source_window([(60.0, 70.0)], groups, BOUNDARIES) == {
            0: [],
            1: [0],
        }
