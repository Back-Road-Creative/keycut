"""Translating per-part segment references into master-local time ranges.

Use this when the thing you want to cut was described against the *original
parts* — "12s to 45s of clip 3" — but the file you actually hold is a single
master that those parts were concatenated into. That is the normal shape for
any workflow where the intermediate files are cleaned up after assembly.

If you already have master-local ranges, skip this module entirely and call
:func:`keycut.extract_and_concat_ranges`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import NamedTuple

Range = tuple[float, float]


class Segment(NamedTuple):
    """A reference to ``[start, end)`` seconds *within one source part*.

    ``source_index`` indexes into the boundary list from
    :func:`boundaries_from_durations` — that is, it is local to the master you
    are cutting, not global to some larger catalogue. If your indices are
    global, run :func:`remap_segment_indices` first.
    """

    source_index: int
    start: float
    end: float


def boundaries_from_durations(durations: Iterable[float]) -> list[Range]:
    """Turn per-part durations into master-local ``[(start, end), ...]`` ranges.

    Valid only when the master is a *lossless* concatenation of exactly those
    parts, in that order: each part's offset in the master is then the sum of
    the durations before it. A master produced by re-encoding, or one with
    anything inserted between parts, breaks this assumption — measure the real
    offsets instead.

    >>> boundaries_from_durations([1200.0, 900.0, 600.0])
    [(0.0, 1200.0), (1200.0, 2100.0), (2100.0, 2700.0)]
    """
    boundaries: list[Range] = []
    cursor = 0.0
    for d in durations:
        duration = float(d or 0.0)
        boundaries.append((cursor, cursor + duration))
        cursor += duration
    return boundaries


def segment_to_master_range(segment: Segment, boundaries: Sequence[Range]) -> Range:
    """Translate one :class:`Segment` into ``(master_start, master_end)``.

    Raises ``IndexError`` when ``source_index`` is outside ``boundaries``. That
    is deliberately loud: a silently clamped index produces a plausible-looking
    extraction of completely the wrong footage.
    """
    idx = int(segment.source_index)
    if idx < 0 or idx >= len(boundaries):
        raise IndexError(
            f"source_index {idx} out of range for boundaries with "
            f"{len(boundaries)} entries"
        )
    part_start, _part_end = boundaries[idx]
    return part_start + float(segment.start), part_start + float(segment.end)


def segments_to_master_ranges(
    segments: Iterable[Segment], boundaries: Sequence[Range]
) -> list[Range]:
    """Translate every segment, preserving the order given."""
    return [segment_to_master_range(seg, boundaries) for seg in segments]


def remap_segment_indices(
    segments: Iterable[Segment], source_indices: Sequence[int]
) -> list[Segment]:
    """Rewrite global ``source_index`` values into indices local to one master.

    ``source_indices`` lists the global indices of the parts that went into
    *this* master, in master order. A segment naming global part 5 becomes
    local part 0 when ``source_indices == [5]``.

    This exists because index spaces drift apart in real pipelines: whatever
    scores or selects segments usually works across the whole catalogue, while
    the boundary list is per-master. Mixing the two produces the worst kind of
    failure — either an ``IndexError`` that gets swallowed and drops the output
    entirely, or, when the ranges happen to overlap, a silently wrong cut.

    Raises ``IndexError`` for a segment naming a part this master does not
    contain, since that means the segment came from somewhere else.

    >>> remap_segment_indices([Segment(5, 10.0, 20.0)], [5])
    [Segment(source_index=0, start=10.0, end=20.0)]
    """
    global_to_local = {g: local for local, g in enumerate(source_indices)}
    out: list[Segment] = []
    for seg in segments:
        g = int(seg.source_index)
        if g not in global_to_local:
            raise IndexError(
                f"segment source_index {g} is not among this master's parts "
                f"{list(source_indices)}"
            )
        out.append(Segment(global_to_local[g], seg.start, seg.end))
    return out
