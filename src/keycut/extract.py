"""Running ffmpeg: per-range extraction and concat-demuxer assembly."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from .keyframes import (
    DEFAULT_GOP_DURATION_SEC,
    estimate_gop_duration,
    io_timeout,
    probe_keyframes,
    snap_start_to_keyframe,
)
from .ranges import (
    MaskLike,
    Range,
    as_mask_lookup,
    copy_would_snap_back_over_excluded,
    merge_ranges,
)
from .receipt import AssemblyReceipt, ReceiptSpan
from .segments import Segment, segments_to_master_ranges

logger = logging.getLogger(__name__)

# Source codec -> a software encoder that produces a concat-compatible stream.
_CODEC_TO_ENCODER = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp8": "libvpx",
    "vp9": "libvpx-vp9",
    "av1": "libsvtav1",
    "mpeg4": "mpeg4",
    "mpeg2video": "mpeg2video",
}

# Encoders that take -crf. Others fall back to a bitrate-free default.
_CRF_ENCODERS = {"libx264", "libx265", "libvpx-vp9", "libsvtav1"}

DEFAULT_CRF = "18"
"""Visually-transparent-ish default for the re-encode fallback.

The fallback runs on a minority of ranges, so quality matters more than speed
here. Override via ``reencode_args`` if you disagree.
"""


def probe_video_params(
    path: Path | str,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout: float = 60.0,
) -> dict:
    """Return the first video stream's codec and colour parameters.

    Empty dict when probing fails. Keys mirror ffprobe's own names
    (``codec_name``, ``pix_fmt``, ``color_range``, ``color_space``,
    ``color_primaries``, ``color_transfer``, ``width``, ``height``).
    """
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,color_range,color_space,"
        "color_primaries,color_transfer,width,height",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            return {}
        payload = json.loads((result.stdout or b"{}").decode("utf-8", errors="replace"))
    except FileNotFoundError:
        logger.error("keycut: %r not found on PATH", ffprobe_bin)
        return {}
    except Exception as exc:
        logger.error("keycut: stream probe failed for %s: %s", path, exc)
        return {}
    streams = payload.get("streams") or []
    return dict(streams[0]) if streams else {}


def default_reencode_args(
    master_path: Path | str,
    *,
    ffprobe_bin: str = "ffprobe",
    crf: str = DEFAULT_CRF,
    timeout: float = 60.0,
) -> list[str]:
    """Build ffmpeg output arguments for the re-encode fallback, matched to the
    master's own codec, pixel format and colour metadata.

    This matters more than it looks. The concat demuxer joins streams without
    re-encoding, so a re-encoded range has to be *parameter-compatible* with
    the stream-copied ranges around it — same codec, same pixel format, same
    colour signalling. An H.264 range spliced into an HEVC timeline will either
    fail the concat outright or produce a file that only some players decode.

    Audio is stream-copied (``-c:a copy``), which keeps it bit-identical and
    guarantees the audio codec still matches its neighbours.

    Falls back to ``libx264`` / ``yuv420p`` when the probe fails.
    """
    params = probe_video_params(master_path, ffprobe_bin=ffprobe_bin, timeout=timeout)
    codec = str(params.get("codec_name") or "").lower()
    encoder = _CODEC_TO_ENCODER.get(codec, "libx264")

    args: list[str] = ["-c:v", encoder]
    if encoder in _CRF_ENCODERS:
        args += ["-crf", str(crf)]

    pix_fmt = params.get("pix_fmt")
    args += ["-pix_fmt", str(pix_fmt) if pix_fmt else "yuv420p"]

    # Only forward colour metadata ffprobe actually resolved. "unknown"/"" would
    # make ffmpeg reject the flag or, worse, tag the output wrongly.
    for probe_key, flag in (
        ("color_range", "-color_range"),
        ("color_space", "-colorspace"),
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
    ):
        value = params.get(probe_key)
        if value and str(value).lower() not in {"unknown", "reserved", "unspecified"}:
            args += [flag, str(value)]

    args += ["-c:a", "copy"]
    return args


REENCODE_SEC_PER_SEC = 20.0
"""Seconds of wall-clock allowed per second of footage re-encoded by
:func:`reencode_range` — 1.5 fps at 30 fps, slower than x265 ``-preset slow``
manages on 4K with a modern CPU."""


def stream_copy_range(
    master_path: Path | str,
    start_sec: float,
    end_sec: float,
    output_path: Path | str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 1800.0,
) -> bool:
    """Copy ``[start_sec, end_sec)`` out of the master without re-encoding.

    Both ``-ss`` and ``-to`` are placed *before* ``-i``, making them input
    options: ffmpeg seeks the demuxer and stops reading, and both timestamps
    are interpreted in the input file's own timeline. This is the fast path —
    it moves packets, it does not decode them.

    The catch, and the reason this whole library exists: an input-side ``-ss``
    with ``-c copy`` starts at the keyframe **at or before** ``start_sec``.
    Pass a keyframe-aligned start (see
    :func:`keycut.snap_start_to_keyframe`) or the output will contain footage
    from before the cut.

    ``timeout`` is a floor, raised to the time to read the whole master (see
    :func:`keycut.io_timeout`), since no range can be larger than that.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{float(start_sec)}",
        "-to",
        f"{float(end_sec)}",
        "-i",
        str(master_path),
        "-c",
        "copy",
        str(output_path),
    ]
    return _run_ffmpeg(cmd, "stream copy", io_timeout(timeout, _size(master_path)))


def reencode_range(
    master_path: Path | str,
    start_sec: float,
    end_sec: float,
    output_path: Path | str,
    *,
    encode_args: Sequence[str],
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 3600.0,
) -> bool:
    """Decode and re-encode ``[start_sec, end_sec)`` with a frame-accurate seek.

    ``-ss`` goes *before* ``-i``: the demuxer seeks to the keyframe at or before
    ``start_sec``, and because this transcodes, ffmpeg's ``-accurate_seek`` (on
    by default) decodes and drops the frames between that keyframe and the
    exact start. The output lands on the requested frame, like an output-side
    seek, but decodes at most one GOP before the cut instead of everything
    from the start of the file. Timestamps restart at zero after an input
    seek, so the length is given with ``-t``.

    Stream-copied audio is not trimmed by the input seek: its packets from the
    keyframe on survive at negative timestamps behind an edit list, which the
    concat demuxer ignores. An output-side ``-ss 0`` drops them.

    ``timeout`` is a floor, raised to :data:`REENCODE_SEC_PER_SEC` seconds per
    second of footage: a long 4K range through a CPU encoder runs for hours.

    This is the escape hatch for the two cases a stream copy cannot serve:
    keyframe positions are unknown, or the snap-back region contains footage
    the caller banned.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = float(end_sec) - float(start_sec)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss",
        f"{float(start_sec)}",
        "-i",
        str(master_path),
        "-ss",
        "0",
        "-t",
        f"{duration}",
        *list(encode_args),
        str(output_path),
    ]
    return _run_ffmpeg(
        cmd, "re-encode", max(float(timeout), duration * REENCODE_SEC_PER_SEC)
    )


def concat_demuxer(
    parts: Sequence[Path | str],
    output_path: Path | str,
    list_file: Path | str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    timeout: float = 1800.0,
    faststart: bool = True,
) -> bool:
    """Join ``parts`` end to end with the concat demuxer, stream-copying.

    Writes the ``file '<path>'`` manifest to ``list_file`` first, single-quote-
    escaped, and passes ``-safe 0`` so absolute paths are accepted.

    ``timeout`` is a floor, raised to the time to move the pieces' bytes (see
    :func:`keycut.io_timeout`) — twice over with ``faststart``, which rewrites
    the whole output a second time to move the moov atom.
    """
    list_file = Path(list_file)
    output_path = Path(output_path)
    list_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for p in parts:
        escaped = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(output_path))
    nbytes = sum(_size(p) for p in parts) * (2 if faststart else 1)
    return _run_ffmpeg(cmd, "concat", io_timeout(timeout, nbytes))


def _size(path: Path | str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _run_ffmpeg(cmd: list[str], what: str, timeout: float) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        logger.error("keycut: %r not found on PATH", cmd[0])
        return False
    except Exception as exc:
        logger.error("keycut: %s subprocess error: %s", what, exc)
        return False
    if result.returncode != 0:
        logger.error(
            "keycut: %s failed rc=%d: %s",
            what,
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[-600:],
        )
        return False
    return True


def _probe_duration(
    path: Path | str, ffprobe_bin: str = "ffprobe", timeout: float = 60.0
) -> float | None:
    """The container duration of ``path`` in seconds, or ``None`` if it cannot
    be probed. The timeout is a floor, raised to fit the file's size."""
    cmd = [
        ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=io_timeout(timeout, _size(path)))
        if result.returncode != 0:
            return None
        value = float((result.stdout or b"").decode("utf-8", errors="replace").strip())
    except Exception as exc:
        logger.debug("keycut: duration probe failed for %s: %s", path, exc)
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _requested_membership(
    ranges: Sequence[Range],
    n_merged: int,
    max_gap_sec: float,
    mask_lookup: MaskLike | None,
) -> list[tuple[int, ...]]:
    """For each merged span, the indices of the requested ranges it absorbed.

    Replays :func:`merge_ranges` over growing prefixes: it only ever extends or
    appends to the last span, so range ``k`` belongs to span
    ``len(merge(ranges[:k+1])) - 1``. Exact by construction, no second copy of
    the merge rule.
    """
    members: list[list[int]] = [[] for _ in range(n_merged)]
    for k in range(len(ranges)):
        count = len(merge_ranges(ranges[: k + 1], max_gap_sec=max_gap_sec, is_excluded=mask_lookup))
        members[count - 1].append(k)
    return [tuple(m) for m in members]


def extract_and_concat_ranges(
    master_path: Path | str,
    ranges: Sequence[Range],
    output_path: Path | str,
    work_dir: Path | str,
    *,
    exclude_mask: MaskLike | None = None,
    max_gap_sec: float | None = None,
    reencode_args: Sequence[str] | None = None,
    allow_reencode: bool = True,
    keyframes: Sequence[float] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    keep_scratch: bool = False,
    faststart: bool = True,
) -> Path | None:
    """Cut every range out of ``master_path`` and join them into one file.

    Returns ``output_path`` on success, ``None`` on any failure. See
    :func:`extract_and_concat_ranges_with_receipt` for the full description of
    the algorithm and every argument; this is the same call without the receipt.
    """
    path, _ = extract_and_concat_ranges_with_receipt(
        master_path,
        ranges,
        output_path,
        work_dir,
        exclude_mask=exclude_mask,
        max_gap_sec=max_gap_sec,
        reencode_args=reencode_args,
        allow_reencode=allow_reencode,
        keyframes=keyframes,
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        keep_scratch=keep_scratch,
        faststart=faststart,
    )
    return path


def extract_and_concat_ranges_with_receipt(
    master_path: Path | str,
    ranges: Sequence[Range],
    output_path: Path | str,
    work_dir: Path | str,
    *,
    exclude_mask: MaskLike | None = None,
    max_gap_sec: float | None = None,
    reencode_args: Sequence[str] | None = None,
    allow_reencode: bool = True,
    keyframes: Sequence[float] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    keep_scratch: bool = False,
    faststart: bool = True,
) -> tuple[Path | None, AssemblyReceipt | None]:
    """Cut every range out of ``master_path`` and join them into one file.

    The whole algorithm, in order:

    1. **Merge** ranges that overlap, touch, or sit closer together than one
       GOP. Removes internal joins, and refuses to pretend a stream copy can
       excise a sub-GOP gap.
    2. **Probe** the master's real keyframe timestamps (once, cached).
    3. **Align** each remaining cut start forward onto a keyframe, so the copy
       starts where it was asked to instead of replaying the previous GOP.
    4. **Copy** each range with ``-c copy``, or **re-encode** it with an
       accurate seek when a copy would drag banned footage in.
    5. **Concat** the pieces with the concat demuxer — also a stream copy.

    Args:
        master_path: The file to cut from.
        ranges: ``[(start, end), ...]`` in master seconds, in the order they
            should appear in the output. Out-of-order ranges are honoured, not
            sorted.
        output_path: Where the joined result goes.
        work_dir: Scratch directory for the per-range pieces. Created if
            missing; cleaned up unless ``keep_scratch``.
        exclude_mask: Per-second bool sequence or ``second -> bool`` callable,
            ``True`` meaning "this second must never appear in the output".
            Makes both the merge and the copy/re-encode choice mask-aware.
        max_gap_sec: Merge threshold. Default measures the master's real GOP
            from its keyframes, falling back to
            :data:`keycut.DEFAULT_GOP_DURATION_SEC`.
        reencode_args: ffmpeg output arguments for the re-encode fallback.
            Default derives concat-compatible ones from the master.
        allow_reencode: When ``False``, always stream-copy — faster and
            strictly lossless, but a range whose start cannot be aligned will
            include up to one GOP of preceding footage. Only set this if that
            slop is acceptable.
        keyframes: Pre-probed keyframe timestamps, to skip the probe. ``None``
            means "probe it"; an empty list means "known to be unusable", which
            sends every range down the re-encode path.
        keep_scratch: Leave the per-range pieces and the concat manifest in
            ``work_dir`` for inspection.

    Returns:
        ``(output_path, receipt)`` on success: an :class:`AssemblyReceipt`
        recording each merged span, its keyframe-snapped source start, whether
        it was copied or re-encoded and why, and where it landed in the output.
        ``(None, None)`` on any failure — missing master, empty range list, or
        an ffmpeg command that exited non-zero. Nothing raises; a failed
        extraction is a normal outcome to branch on.
    """
    master_path = Path(master_path)
    output_path = Path(output_path)
    work_dir = Path(work_dir)

    if not master_path.exists():
        logger.error("keycut: master missing: %s", master_path)
        return None, None
    if not ranges:
        logger.error("keycut: no ranges to extract from %s", master_path)
        return None, None

    master_size = _size(master_path)
    work_dir.mkdir(parents=True, exist_ok=True)
    mask_lookup = as_mask_lookup(exclude_mask)

    if keyframes is None:
        keyframes = probe_keyframes(master_path, ffprobe_bin=ffprobe_bin)

    if max_gap_sec is None:
        max_gap_sec = (
            estimate_gop_duration(keyframes) if keyframes else DEFAULT_GOP_DURATION_SEC
        )
        logger.debug("keycut: merge gap %.3fs", max_gap_sec)

    merged = merge_ranges(ranges, max_gap_sec=max_gap_sec, is_excluded=mask_lookup)
    if len(merged) < len(ranges):
        logger.info(
            "keycut: merged %d requested ranges into %d extraction(s) — %d "
            "internal join(s) removed",
            len(ranges),
            len(merged),
            len(ranges) - len(merged),
        )

    if reencode_args is None and allow_reencode:
        reencode_args = default_reencode_args(master_path, ffprobe_bin=ffprobe_bin)

    parts: list[Path] = []
    plans: list[tuple[float, float, str, str]] = []  # source_start, source_end, method, reason
    for i, (start, end) in enumerate(merged):
        part = work_dir / f"keycut_{i:03d}{output_path.suffix or '.mp4'}"
        aligned = snap_start_to_keyframe(start, end, keyframes)

        if not keyframes and allow_reencode:
            # Positions unknown: a copy would snap back an unknown distance.
            ok = reencode_range(
                master_path, start, end, part,
                encode_args=reencode_args or [],
                ffmpeg_bin=ffmpeg_bin,
            )
            how = "re-encode (keyframes unknown)"
            plan = (start, end, "reencode", "keyframes_unknown")
        elif allow_reencode and copy_would_snap_back_over_excluded(
            aligned, keyframes, mask_lookup
        ):
            ok = reencode_range(
                master_path, start, end, part,
                encode_args=reencode_args or [],
                ffmpeg_bin=ffmpeg_bin,
            )
            how = "re-encode (copy would snap back over excluded footage)"
            plan = (start, end, "reencode", "snap_back_over_excluded")
        else:
            if aligned != start:
                logger.debug("keycut: range %d start aligned %.3f -> %.3f", i, start, aligned)
            ok = stream_copy_range(
                master_path, aligned, end, part, ffmpeg_bin=ffmpeg_bin
            )
            how = "stream copy"
            plan = (aligned, end, "copy", "stream_copy")

        if not ok:
            logger.error(
                "keycut: %s failed on range %d/%d (%.3f -> %.3f) of %s",
                how, i + 1, len(merged), start, end, master_path,
            )
            return None, None
        parts.append(part)
        plans.append(plan)

    part_durations = [_probe_duration(p, ffprobe_bin) for p in parts]

    list_file = work_dir / f"{output_path.stem}_concat.txt"
    if not concat_demuxer(
        parts, output_path, list_file, ffmpeg_bin=ffmpeg_bin, faststart=faststart
    ):
        return None, None

    spans: list[ReceiptSpan] = []
    cursor = 0.0
    membership = _requested_membership(ranges, len(merged), max_gap_sec, mask_lookup)
    for i, ((m_start, m_end), (src_start, src_end, method, reason)) in enumerate(
        zip(merged, plans, strict=True)
    ):
        probed = part_durations[i]
        duration = probed if probed is not None else src_end - src_start
        spans.append(
            ReceiptSpan(
                index=i,
                requested_indices=membership[i],
                merged_start=float(m_start),
                merged_end=float(m_end),
                source_start=float(src_start),
                source_end=float(src_end),
                method=method,
                reason=reason,
                output_start=cursor,
                output_end=cursor + duration,
                duration_source="probed" if probed is not None else "planned",
            )
        )
        cursor += duration
    from . import __version__

    receipt = AssemblyReceipt(
        keycut_version=__version__,
        master_path=str(master_path),
        master_size_bytes=master_size,
        max_gap_sec=float(max_gap_sec),
        requested=tuple((float(a), float(b)) for a, b in ranges),
        spans=tuple(spans),
        output_path=str(output_path),
        output_duration=_probe_duration(output_path, ffprobe_bin),
    )

    if not keep_scratch:
        for p in [*parts, list_file]:
            try:
                p.unlink()
            except OSError:
                pass

    return output_path, receipt


def extract_and_concat_segments(
    master_path: Path | str,
    segments: Iterable[Segment],
    boundaries: Sequence[Range],
    output_path: Path | str,
    work_dir: Path | str,
    **kwargs,
) -> Path | None:
    """Same as :func:`extract_and_concat_ranges`, for segments expressed
    against the master's *source parts* rather than master time.

    Translates via :func:`keycut.segment_to_master_range` and forwards every
    keyword argument. Returns ``None`` (rather than raising) when a segment
    names a part outside ``boundaries``, so one bad reference fails the whole
    extraction visibly instead of producing a short output nobody notices.
    """
    path, _ = extract_and_concat_segments_with_receipt(
        master_path, segments, boundaries, output_path, work_dir, **kwargs
    )
    return path


def extract_and_concat_segments_with_receipt(
    master_path: Path | str,
    segments: Iterable[Segment],
    boundaries: Sequence[Range],
    output_path: Path | str,
    work_dir: Path | str,
    **kwargs,
) -> tuple[Path | None, AssemblyReceipt | None]:
    """:func:`extract_and_concat_segments` that also returns the
    :class:`AssemblyReceipt`. ``(None, None)`` on any failure."""
    try:
        ranges = segments_to_master_ranges(segments, boundaries)
    except IndexError as exc:
        logger.error("keycut: segment translation failed: %s", exc)
        return None, None
    return extract_and_concat_ranges_with_receipt(
        master_path, ranges, output_path, work_dir, **kwargs
    )
