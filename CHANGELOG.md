# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.2.0 - 2026-10-27

### Added

- Assembly receipt, schema `keycut-assembly-receipt/1`: a record of what an
  extraction actually cut. For each merged span it gives the requested ranges
  absorbed, the keyframe-snapped source start, copy versus re-encode and the
  reason, and where the span landed in the output (parts are probed with
  ffprobe; a failed probe falls back to the planned length and says so).
  `AssemblyReceipt` and `ReceiptSpan` offer `to_dict()`, `from_dict()` and
  `write_json()`.
- `extract_and_concat_ranges_with_receipt` and
  `extract_and_concat_segments_with_receipt`, returning `(path, receipt)`, or
  `(None, None)` on failure. `extract_and_concat_ranges` and
  `extract_and_concat_segments` are unchanged in signature and behaviour.

## 0.1.2 - 2026-10-27

### Fixed

- Every ffmpeg and ffprobe step now sizes its timeout to the file it reads,
  at a 20 MB/s floor (`MIN_IO_BYTES_PER_SEC`, `io_timeout`), instead of a
  flat window. The keyframe probe (600 s floor), a stream copy (1800 s floor)
  and the concat (1800 s floor, twice the bytes with `faststart`) all read
  the whole file. A 110 GB master on a slow mount took longer than 600 s to
  probe, so keyframes came back unknown and every range fell back to a
  re-encode.
- The re-encode fallback seeks on the input side (`-ss S -i master -ss 0
  -t D`) instead of the output side, so it decodes at most one GOP before
  the cut rather than everything from the start of the file. It is still
  frame-exact. The output-side `-ss 0` drops the stream-copied audio's
  pre-cut packets, which the concat demuxer would otherwise play. Its
  timeout is at least 20 s per second of footage
  (`REENCODE_SEC_PER_SEC`), with a 3600 s floor.
- `probe_keyframes` no longer caches a failed probe forever: `None` is now
  remembered for `FAILED_PROBE_RETRY_SEC` (30 s), so a transient ffprobe
  failure on unchanged media recovers, and repeated failures are re-probed at
  most once per window. Successful probes are still cached, and a changed file
  is still re-probed immediately.

## 0.1.1 - 2026-08-03

### Added

- Windows installer, built and attached to a `v*` tag's GitHub release by
  `.github/workflows/release.yml`: a PyInstaller one-file executable wrapped in
  an Inno Setup installer (`installer/`) that installs into Program Files, adds
  itself to `PATH`, and registers an uninstaller. It does not bundle ffmpeg and
  it is not code-signed; both are stated in the installer and the README.

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
