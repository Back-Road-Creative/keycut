"""Keyframe discovery and cut-point alignment.

A stream copy (``-c copy``) cannot re-encode, so it cannot start a video on a
frame that is not a keyframe. Everything in this module exists to answer one
question exactly rather than approximately: *where are this file's keyframes?*
"""

from __future__ import annotations

import bisect
import logging
import subprocess
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_GOP_DURATION_SEC = 2.1
"""Fallback keyframe interval, in seconds, used when a file's real GOP length
cannot be measured.

A little over 2.0 s, which is the most common keyframe interval for
web-delivery H.264/HEVC. Prefer :func:`estimate_gop_duration` on a real
keyframe list — this constant is only a floor for the case where probing
failed entirely.
"""

# Keyframe timestamps keyed by (resolved path, mtime_ns, size) so a file is
# probed at most once per process even when several extractions share it.
# A failed probe caches ``None`` so it is not retried in a loop.
_keyframe_cache: dict[tuple[str, int, int], list[float] | None] = {}


def clear_keyframe_cache() -> None:
    """Drop every cached probe result.

    Only needed if a file is rewritten in place *and* keeps its size and
    mtime; the cache key already covers ordinary edits.
    """
    _keyframe_cache.clear()


def probe_keyframes(
    path: Path | str,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout: float = 600.0,
    use_cache: bool = True,
) -> list[float] | None:
    """Return the sorted keyframe timestamps (seconds) of a file's first video
    stream, or ``None`` when probing fails.

    Reads every video packet's ``pts_time`` and flags and keeps the ones
    flagged ``K`` (keyframe). This is an exact answer, not an estimate from the
    declared GOP size — encoders insert extra keyframes at scene cuts, and a
    file that was itself assembled by concatenation has a keyframe at every
    join.

    Because it walks the whole packet index, expect roughly a second per hour
    of footage on a local disk. Results are cached per (path, mtime, size).

    ``None`` means "unknown", not "no keyframes". Callers must treat it as a
    reason to re-encode rather than a reason to cut blind.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        logger.warning("keycut: cannot stat %s", path)
        return None

    key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    if use_cache and key in _keyframe_cache:
        return _keyframe_cache[key]

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,flags",
        "-of",
        "csv=p=0",
        str(path),
    ]

    keyframes: list[float] | None = None
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode == 0:
            parsed: list[float] = []
            for line in (result.stdout or b"").decode("utf-8", errors="replace").splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 2 and "K" in parts[1]:
                    try:
                        parsed.append(float(parts[0]))
                    except ValueError:
                        # ffprobe emits "N/A" for packets with no timestamp.
                        continue
            if parsed:
                keyframes = sorted(parsed)
        else:
            logger.error(
                "keycut: ffprobe rc=%d for %s: %s",
                result.returncode,
                path,
                (result.stderr or b"").decode("utf-8", errors="replace")[-400:],
            )
    except FileNotFoundError:
        logger.error("keycut: %r not found on PATH", ffprobe_bin)
    except Exception as exc:  # subprocess timeout, OS errors
        logger.error("keycut: keyframe probe failed for %s: %s", path, exc)

    if keyframes is None:
        logger.warning(
            "keycut: keyframe positions unknown for %s — a stream copy cut "
            "here would snap back to the previous keyframe and replay up to "
            "one GOP of footage",
            path,
        )

    if use_cache:
        _keyframe_cache[key] = keyframes
    return keyframes


def keyframe_at_or_before(t: float, keyframes: Sequence[float] | None) -> float | None:
    """The largest keyframe timestamp ``<= t`` — where ``-ss t … -c copy``
    actually lands — or ``None`` when no keyframe sits at or before ``t``.
    """
    if not keyframes:
        return None
    i = bisect.bisect_right(keyframes, t + 1e-3) - 1
    return keyframes[i] if i >= 0 else None


def snap_start_to_keyframe(
    start_sec: float,
    end_limit_sec: float,
    keyframes: Sequence[float] | None,
) -> float:
    """Move ``start_sec`` FORWARD to the first keyframe at or after it.

    Forward, never backward. Snapping backward is exactly what ``-ss … -c
    copy`` does on its own, and it is the bug: it replays footage from before
    the requested cut. Snapping forward costs at most one GOP off the head of
    the range and guarantees the copy starts on the frame that was asked for.

    Returns ``start_sec`` unchanged when ``keyframes`` is unavailable or no
    keyframe falls inside ``[start_sec, end_limit_sec)`` — a slightly loose cut
    beats silently emitting an empty range.
    """
    if not keyframes:
        return start_sec
    i = bisect.bisect_left(keyframes, start_sec - 1e-3)
    if i < len(keyframes) and keyframes[i] < end_limit_sec:
        return max(start_sec, keyframes[i])
    return start_sec


def estimate_gop_duration(
    keyframes: Sequence[float] | None,
    *,
    default: float = DEFAULT_GOP_DURATION_SEC,
    percentile: float = 0.95,
    margin: float = 1.05,
) -> float:
    """Measure a file's effective GOP length from its keyframe timestamps.

    This is the width of the smallest gap a stream copy can excise cleanly.
    Anything narrower has to be either kept or re-encoded.

    Uses a high percentile of the gaps between consecutive keyframes rather
    than the mean or the max: scene-cut keyframes create gaps *shorter* than
    the nominal GOP and would drag a mean down, while one pathological gap
    would drag a max up. A small ``margin`` covers timestamp rounding.

    Returns ``default`` when fewer than two keyframes are known.
    """
    if not keyframes or len(keyframes) < 2:
        return default
    deltas = sorted(b - a for a, b in pairwise(keyframes) if b > a)
    if not deltas:
        return default
    idx = min(len(deltas) - 1, int(round(percentile * (len(deltas) - 1))))
    return deltas[idx] * margin
