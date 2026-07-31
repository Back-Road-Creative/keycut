"""Optional companion: attributing derived clips back to the output they came from.

This module is **not** part of the cutting path. Nothing in
:mod:`keycut.extract` imports it, and you can ignore it entirely if all you
want is lossless extraction.

It solves an adjacent bookkeeping problem that shows up whenever one master
feeds two independent products. Say you cut three long outputs from a master,
and separately pull twenty short clips from the same master by some other rule.
Later you need to know which long output each short belongs under — to file it,
caption it, or publish it alongside its parent. The answer is not "divide twenty
by three": it is "whichever output was built from the footage this clip sits
inside".
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import TypeVar

from .segments import Range, Segment, segment_to_master_range

K = TypeVar("K", bound=Hashable)


def assign_clips_by_source_window(
    clips: Sequence[Range],
    groups: Mapping[K, Sequence[Segment]],
    boundaries: Sequence[Range],
) -> dict[K, list[int]]:
    """Assign each clip to the group whose source footage contains it.

    A clip belongs to group *G* when the clip's midpoint falls inside any of
    *G*'s master-time intervals. A clip whose midpoint lands in no group's
    footage — normal when the groups were built from a subset of the master —
    goes to the **nearest** group, measured from the midpoint to the closest
    edge of that group's intervals.

    No clip is ever dropped. It was produced; it has to land somewhere. Ties go
    to the first group in iteration order, so pass ``groups`` in the order you
    want ties broken (a plain ``dict`` preserves insertion order).

    Args:
        clips: ``[(start, end), ...]`` in master seconds. The index of a clip in
            this sequence is what appears in the result, so keep it aligned with
            whatever list of clip files you are attributing.
        groups: Maps a group key to the segments that group was assembled from.
            Segment indices are resolved through ``boundaries``; a segment
            naming a part outside ``boundaries`` is skipped rather than fatal,
            since a partially-resolvable group still attributes better than none.
        boundaries: ``[(start, end), ...]`` per source part, from
            :func:`keycut.boundaries_from_durations`.

    Returns:
        ``{group_key: [clip_index, ...]}``, with an entry for every group even
        when it received no clips. Indices within each list stay in the order
        they appeared in ``clips``. Returns ``{}`` when ``groups`` is empty.
    """
    if not groups:
        return {}

    resolved: list[tuple[K, list[Range]]] = []
    for key, segments in groups.items():
        intervals: list[Range] = []
        for seg in segments or []:
            try:
                s, e = segment_to_master_range(seg, boundaries)
            except IndexError:
                continue
            if e > s:
                intervals.append((s, e))
        resolved.append((key, intervals))

    out: dict[K, list[int]] = {key: [] for key, _ in resolved}

    for clip_index, (start, end) in enumerate(clips):
        mid = 0.5 * (float(start) + float(end))

        matched = False
        for key, intervals in resolved:
            if any(s <= mid < e for s, e in intervals):
                out[key].append(clip_index)
                matched = True
                break
        if matched:
            continue

        nearest_key = resolved[0][0]
        nearest_distance = float("inf")
        for key, intervals in resolved:
            d = _distance_to_intervals(intervals, mid)
            if d < nearest_distance:
                nearest_distance = d
                nearest_key = key
        out[nearest_key].append(clip_index)

    return out


def _distance_to_intervals(intervals: Sequence[Range], point: float) -> float:
    """Gap from ``point`` to the closest interval edge; 0.0 when inside, and
    ``inf`` for a group with no resolvable intervals."""
    if not intervals:
        return float("inf")
    best = float("inf")
    for s, e in intervals:
        if s <= point < e:
            return 0.0
        d = s - point if point < s else point - e
        if d < best:
            best = d
    return best
