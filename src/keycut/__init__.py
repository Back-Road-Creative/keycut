"""keycut — lossless multi-range video extraction with ffmpeg stream copy.

Cut several ranges out of one video and join them into one file, without
re-encoding, and without the GOP-replay artefact that ``ffmpeg -ss … -c copy``
produces on its own.

Quick start::

    from keycut import extract_and_concat_ranges

    extract_and_concat_ranges(
        "master.mp4",
        [(120.0, 180.0), (300.0, 360.0)],
        output_path="highlights.mp4",
        work_dir="scratch/",
    )

Requires ``ffmpeg`` and ``ffprobe`` on PATH. No Python dependencies.
"""

from __future__ import annotations

from .attribution import assign_clips_by_source_window
from .extract import (
    DEFAULT_CRF,
    concat_demuxer,
    default_reencode_args,
    extract_and_concat_ranges,
    extract_and_concat_segments,
    probe_video_params,
    reencode_range,
    stream_copy_range,
)
from .keyframes import (
    DEFAULT_GOP_DURATION_SEC,
    clear_keyframe_cache,
    estimate_gop_duration,
    keyframe_at_or_before,
    probe_keyframes,
    snap_start_to_keyframe,
)
from .ranges import (
    MaskLike,
    Range,
    as_mask_lookup,
    copy_would_snap_back_over_excluded,
    merge_ranges,
    span_has_excluded_second,
)
from .segments import (
    Segment,
    boundaries_from_durations,
    remap_segment_indices,
    segment_to_master_range,
    segments_to_master_ranges,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CRF",
    "DEFAULT_GOP_DURATION_SEC",
    "MaskLike",
    "Range",
    "Segment",
    "__version__",
    "as_mask_lookup",
    "assign_clips_by_source_window",
    "boundaries_from_durations",
    "clear_keyframe_cache",
    "concat_demuxer",
    "copy_would_snap_back_over_excluded",
    "default_reencode_args",
    "estimate_gop_duration",
    "extract_and_concat_ranges",
    "extract_and_concat_segments",
    "keyframe_at_or_before",
    "merge_ranges",
    "probe_keyframes",
    "probe_video_params",
    "reencode_range",
    "remap_segment_indices",
    "segment_to_master_range",
    "segments_to_master_ranges",
    "snap_start_to_keyframe",
    "span_has_excluded_second",
    "stream_copy_range",
]
