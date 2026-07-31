# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0

First release.

### Added

- `extract_and_concat_ranges` — cut several ranges out of one video and join
  them into a single file with `ffmpeg` stream copy, without the frames from
  before each cut that `-ss … -c copy` silently carries along.
- Exact keyframe discovery (`probe_keyframes`) from `ffprobe` packet flags,
  cached per (path, mtime, size), with forward-only cut alignment
  (`snap_start_to_keyframe`) so a copy starts where it was asked to.
- GOP measurement from a file's own keyframes (`estimate_gop_duration`) rather
  than a hardcoded interval, used as the default range-merge threshold.
- Range merging (`merge_ranges`) that removes internal joins and absorbs gaps
  narrower than one GOP instead of pretending a stream copy can excise them.
- `exclude_mask` — per-second windows that must never reach the output. Blocks
  a merge from re-admitting an excluded gap, and promotes any range whose copy
  would snap back over one to a frame-accurate re-encode.
- Re-encode fallback with encoder arguments derived from the master's own
  codec, pixel format and colour metadata (`default_reencode_args`), so a
  re-encoded range still splices into a stream-copied timeline.
- Segment translation for ranges measured against a master's source clips:
  `Segment`, `boundaries_from_durations`, `segment_to_master_range`,
  `segments_to_master_ranges`, `remap_segment_indices`, and
  `extract_and_concat_segments`.
- Optional companion `assign_clips_by_source_window` for attributing derived
  clips back to the output whose source footage contains them.
- `keycut` command line (also `python -m keycut`) with timecode ranges,
  exclusions, and a `--dry-run` plan showing merges, alignments, and which
  ranges will be copied versus re-encoded.
- Test suite covering the decision logic against mocked ffmpeg argv, plus an
  integration suite that generates its own fixture video, demonstrates the
  artefact on it at packet level, and proves keycut does not reproduce it.
