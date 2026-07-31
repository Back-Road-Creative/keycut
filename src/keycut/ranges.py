"""Time-range arithmetic: merging, and the per-second exclusion mask.

Nothing here touches ffmpeg. These are the pure decisions made *before* a
single byte is copied, which is why they are the easy part to test.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from .keyframes import DEFAULT_GOP_DURATION_SEC, keyframe_at_or_before

Range = tuple[float, float]

MaskLike = Callable[[int], bool] | Sequence[bool]
"""A per-second exclusion mask.

Either a callable ``second_index -> bool`` or any indexable sequence of bools
(a plain list, or a numpy array — this module never imports numpy). ``True``
means "this second of the source must never appear in the output".
"""


def as_mask_lookup(exclude_mask: MaskLike | None) -> Callable[[int], bool] | None:
    """Normalise an exclusion mask into a ``second_index -> bool`` callable.

    Sequences are bounds-checked, so a second past the end of the mask reads
    ``False`` rather than raising. Returns ``None`` for ``None`` (no mask) and
    for anything with neither ``__call__`` nor ``__len__``.
    """
    if exclude_mask is None:
        return None
    if callable(exclude_mask):
        return exclude_mask
    seq = exclude_mask
    try:
        n = len(seq)
    except TypeError:
        return None

    def _lookup(sec: int) -> bool:
        return 0 <= sec < n and bool(seq[sec])

    return _lookup


def span_has_excluded_second(
    lo: float, hi: float, mask_lookup: Callable[[int], bool] | None
) -> bool:
    """True when any excluded second overlaps the half-open span ``[lo, hi)``.

    Second ``i`` in the mask covers ``[i, i+1)``, so a span is dirty if it
    touches any part of an excluded second.
    """
    if mask_lookup is None or hi <= lo:
        return False
    for sec in range(max(0, int(math.floor(lo))), int(math.ceil(hi))):
        if mask_lookup(sec):
            return True
    return False


def copy_would_snap_back_over_excluded(
    aligned_start: float,
    keyframes: Sequence[float] | None,
    mask_lookup: Callable[[int], bool] | None,
) -> bool:
    """True when starting a stream copy at ``aligned_start`` would drag
    excluded footage into the output.

    ``-ss aligned_start -c copy`` actually begins at the keyframe at or before
    ``aligned_start``. If that snap-back region contains an excluded second,
    the copy would emit footage the caller explicitly banned — the range has to
    be re-encoded with an accurate seek instead.

    False when ``aligned_start`` already sits on a keyframe (nothing is
    replayed) or when the replayed span is clean.
    """
    if mask_lookup is None:
        return False
    kf = keyframe_at_or_before(aligned_start, keyframes)
    region_lo = 0.0 if kf is None else kf
    if region_lo >= aligned_start - 1e-3:
        return False
    return span_has_excluded_second(region_lo, aligned_start, mask_lookup)


def merge_ranges(
    ranges: Sequence[Range],
    max_gap_sec: float = DEFAULT_GOP_DURATION_SEC,
    is_excluded: MaskLike | None = None,
) -> list[Range]:
    """Merge ranges that overlap, touch, or are separated by less than one GOP.

    Two reasons, and they are different:

    1. **Adjacent ranges.** Two ranges that meet exactly should be one
       extraction, not two glued together. Every internal join is a place where
       a stream copy can replay a GOP; merging removes the join entirely.
    2. **Sub-GOP gaps.** A gap narrower than ``max_gap_sec`` cannot be excised
       by a stream copy — there is no keyframe inside it to cut on. Keeping a
       fraction of a second of unwanted footage is the honest outcome;
       pretending to drop it produces a visible stutter at the join.

    Only forward continuations merge. A range that starts before the previous
    one stays separate, so a deliberately out-of-order edit keeps its order.

    ``is_excluded`` makes the merge mask-aware: two ranges are never merged
    across a gap containing an excluded second, however narrow that gap is,
    because merging would re-admit exactly the footage the mask banned. Such a
    gap is left as a real cut, which the extractor then handles by re-encoding
    if a stream copy would snap back over it.
    """
    mask_lookup = as_mask_lookup(is_excluded)
    merged: list[Range] = []
    for s, e in ranges:
        if merged:
            prev_s, prev_e = merged[-1]
            if prev_s - 1e-3 <= s <= prev_e + max_gap_sec:
                if s > prev_e and span_has_excluded_second(prev_e, s, mask_lookup):
                    merged.append((s, e))
                    continue
                merged[-1] = (prev_s, max(prev_e, e))
                continue
        merged.append((s, e))
    return merged
