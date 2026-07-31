"""Command-line front end: ``keycut`` / ``python -m keycut``."""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from .extract import extract_and_concat_ranges
from .keyframes import (
    DEFAULT_GOP_DURATION_SEC,
    estimate_gop_duration,
    probe_keyframes,
    snap_start_to_keyframe,
)
from .ranges import Range, as_mask_lookup, copy_would_snap_back_over_excluded, merge_ranges


def parse_timecode(text: str) -> float:
    """Parse ``SS``, ``MM:SS`` or ``HH:MM:SS`` (fractional seconds allowed).

    >>> parse_timecode("90.5")
    90.5
    >>> parse_timecode("1:30")
    90.0
    >>> parse_timecode("01:02:03")
    3723.0
    """
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"not a timecode: {text!r}")
    total = 0.0
    for part in parts:
        if part == "":
            raise ValueError(f"not a timecode: {text!r}")
        total = total * 60.0 + float(part)
    return total


def _pairs(values: Sequence[Sequence[str]] | None, label: str) -> list[Range]:
    out: list[Range] = []
    for raw_start, raw_end in values or []:
        try:
            start, end = parse_timecode(raw_start), parse_timecode(raw_end)
        except ValueError as exc:
            raise SystemExit(f"keycut: bad --{label} value: {exc}") from exc
        if end <= start:
            raise SystemExit(f"keycut: --{label} {raw_start} {raw_end} ends at or before it starts")
        out.append((start, end))
    return out


def _build_mask(excluded: Sequence[Range], horizon: float) -> list[bool] | None:
    """Turn excluded ranges into a per-second bool mask.

    A second is excluded when *any* part of it is inside an excluded range —
    the conservative reading, since half a banned second is still banned.
    """
    if not excluded:
        return None
    size = int(math.ceil(max(horizon, max(e for _, e in excluded)))) + 1
    mask = [False] * size
    for start, end in excluded:
        for sec in range(max(0, int(math.floor(start))), min(size, int(math.ceil(end)))):
            mask[sec] = True
    return mask


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keycut",
        description=(
            "Cut several ranges out of a video and join them into one file "
            "without re-encoding."
        ),
        epilog=(
            "Example:\n"
            "  keycut master.mp4 highlights.mp4 -r 0:30 1:15 -r 4:00 4:45\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("master", type=Path, help="source video to cut from")
    parser.add_argument("output", type=Path, help="joined result to write")
    parser.add_argument(
        "-r", "--range", dest="ranges", nargs=2, action="append", metavar=("START", "END"),
        help="a range to keep, as SS / MM:SS / HH:MM:SS. Repeatable; order is kept.",
    )
    parser.add_argument(
        "-x", "--exclude", dest="excludes", nargs=2, action="append", metavar=("START", "END"),
        help="footage that must never appear in the output, even as GOP slop. Repeatable.",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=None,
        help="scratch directory for per-range pieces (default: alongside the output)",
    )
    parser.add_argument(
        "--max-gap", type=float, default=None, metavar="SECONDS",
        help="merge ranges closer together than this (default: measure the master's GOP)",
    )
    parser.add_argument(
        "--no-reencode", action="store_true",
        help="never re-encode; accept up to one GOP of slop when a start cannot be aligned",
    )
    parser.add_argument("--keep-scratch", action="store_true", help="leave the per-range pieces")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="print the plan (merges, alignments, copy vs re-encode) and stop",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: ffmpeg)")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe binary (default: ffprobe)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="errors only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    ranges = _pairs(args.ranges, "range")
    if not ranges:
        raise SystemExit("keycut: at least one --range is required")
    excludes = _pairs(args.excludes, "exclude")
    mask = _build_mask(excludes, horizon=max(e for _, e in ranges))

    work_dir = args.work_dir or args.output.parent / f".{args.output.stem}-keycut"

    if args.dry_run:
        return _print_plan(args, ranges, mask)

    result = extract_and_concat_ranges(
        args.master,
        ranges,
        args.output,
        work_dir,
        exclude_mask=mask,
        max_gap_sec=args.max_gap,
        allow_reencode=not args.no_reencode,
        ffmpeg_bin=args.ffmpeg,
        ffprobe_bin=args.ffprobe,
        keep_scratch=args.keep_scratch,
    )
    if result is None:
        return 1
    print(result)
    return 0


def _print_plan(args, ranges: list[Range], mask) -> int:
    keyframes = probe_keyframes(args.master, ffprobe_bin=args.ffprobe)
    gap = args.max_gap
    if gap is None:
        gap = estimate_gop_duration(keyframes) if keyframes else DEFAULT_GOP_DURATION_SEC
    mask_lookup = as_mask_lookup(mask)
    merged = merge_ranges(ranges, max_gap_sec=gap, is_excluded=mask_lookup)

    print(f"master:    {args.master}")
    print(f"keyframes: {len(keyframes) if keyframes else 'UNKNOWN'}")
    print(f"merge gap: {gap:.3f}s")
    print(f"requested: {len(ranges)} range(s) -> {len(merged)} extraction(s)")
    for i, (start, end) in enumerate(merged):
        aligned = snap_start_to_keyframe(start, end, keyframes)
        if not keyframes and not args.no_reencode:
            how = "re-encode (keyframes unknown)"
        elif not args.no_reencode and copy_would_snap_back_over_excluded(
            aligned, keyframes, mask_lookup
        ):
            how = "re-encode (copy would snap back over excluded footage)"
        else:
            how = "stream copy"
        drift = f" (start aligned +{aligned - start:.3f}s)" if aligned != start else ""
        print(f"  [{i}] {aligned:.3f} -> {end:.3f}  {how}{drift}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
