# keycut

Cut several ranges out of one video and join them into a single file — without
re-encoding, and without the extra footage that `ffmpeg -ss … -c copy` quietly
hands back.

Pure Python, no dependencies. It shells out to `ffmpeg` and `ffprobe`.

```python
from keycut import extract_and_concat_ranges

extract_and_concat_ranges(
    "master.mp4",
    [(120.0, 180.0), (300.0, 360.0)],   # keep these, in this order
    output_path="highlights.mp4",
    work_dir="scratch/",
)
```

---

## The problem

This is the most confidently mis-answered question about ffmpeg, and the wrong
answer is on every forum:

```bash
ffmpeg -ss 10.5 -to 12.5 -i source.mp4 -c copy clip.mp4
```

A stream copy moves compressed packets; it never decodes them. A P- or B-frame
is stored as a difference from earlier frames, so it is meaningless without the
keyframe that opens its group of pictures (GOP). ffmpeg therefore cannot start
the copy at 10.5 — it starts at the keyframe **at or before** 10.5, which is
10.0. On a file with a keyframe every 2 seconds, you asked for 60 frames and
the file now holds **75**.

### Why this ships to production

Check that file and it looks perfect:

```
$ ffprobe -show_entries format=duration clip.mp4
duration=2.015011                       # 2.0 seconds, exactly as ordered
```

It is not fine. The mp4 muxer gave the 15 pre-cut frames negative timestamps
and a discard flag, and wrote an **edit list** telling players to skip them.
The frames are still in the file:

```
$ ffprobe -select_streams v:0 -show_entries packet=pts_time,flags clip.mp4
-0.500000,KD_        ← 15 frames the edit list hides
-0.466667,_D_
...
$ ffprobe -select_streams v:0 -count_packets clip.mp4
nb_read_packets=75   ← not 60
```

So the naive command passes every casual test. Then you concatenate — which is
the entire point of multi-range extraction — and the concat demuxer, which
reads packets and does not read edit lists, plays all 75:

```
$ ffmpeg -f concat -safe 0 -i list.txt -c copy joined.mp4
$ ffprobe -show_entries format=duration joined.mp4
duration=2.530975                       ← the hidden half-second is back
```

Every number above is measured, not asserted: it is
`tests/test_integration_ffmpeg.py`, run on a video the suite generates itself.

The result is silent, never an error, and it costs you in two ways that get
worse the more ranges you cut:

- **Replay at every join.** Cut `[0:00–0:30]` and `[0:30–1:00]` as two pieces
  and concatenate them, and the second piece begins by replaying the tail of
  the first. A visible stutter, in the middle of a file that has no obvious
  reason to stutter.
- **Resurrection.** Drop `[1:00–1:04]` — an advert, a stationary shot, a face
  you are obliged to remove — by cutting `[0:00–1:00]` plus `[1:04–end]`. The
  second copy backs up to the keyframe before 1:04, and part of the span you
  deliberately removed is back in the file.

The usual advice is to move `-ss` after `-i`. That works — it decodes to the
exact frame — but it re-encodes everything you keep, which is the entire cost
you were avoiding by using `-c copy` in the first place.

## What keycut does instead

Ask the file where its keyframes actually are, then only ever cut on one.

```
source, keyframes every 2.0 s (▲)

   ▲       ▲       ▲       ▲       ▲       ▲
  8.0     10.0    12.0    14.0    16.0    18.0
   |-------|-------|-------|-------|-------|

you asked for                [10.5 ──────────── 16.0]
-ss 10.5 -c copy         [10.0 ────────────────── 16.0]
                          ^^^^^^ 0.5 s you did not ask for
keycut                             [12.0 ─────── 16.0]
                                    ^ snapped FORWARD to the next keyframe
```

Snapping **forward** is the whole trick. Every naive implementation snaps
backward, because that is what ffmpeg does for free. Forward costs you up to
one GOP off the head of the range; backward costs you correctness.

Five steps, in order:

**1. Merge ranges that should not be separate cuts.** Ranges that overlap or
touch become one extraction, so the join between them cannot replay anything —
it no longer exists.

**2. Absorb sub-GOP gaps.** If two ranges are separated by less than one GOP,
there is no keyframe inside the gap to cut on. A stream copy physically cannot
excise it. keycut merges across it and keeps the fraction of a second, rather
than emitting a join that pretends to have removed something:

```
you asked for  [4.0 ── 8.0]   [9.0 ── 13.0]
                           ^^^ 1.0 s gap, narrower than the 2.0 s GOP
keycut cuts    [4.0 ─────────────────── 13.0]   one extraction; gap kept, honestly
```

**3. Measure the real GOP, do not assume it.** keycut reads every video
packet's flags with `ffprobe` and keeps the keyframe timestamps, then takes a
high percentile of the gaps between them. This is exact, and it beats trusting
the declared GOP size: encoders insert extra keyframes at scene cuts, and a
file that was itself produced by concatenation has a keyframe at every join.

**4. Align, then copy.** Each remaining cut start moves forward to the first
keyframe at or after it, so `-ss` lands exactly where the copy will actually
begin. Both `-ss` and `-to` go **before** `-i`, making them input options —
ffmpeg seeks the demuxer and both timestamps stay in the source's own timeline.

**5. Concatenate with the concat demuxer**, also a stream copy. Nothing in the
happy path is ever decoded.

### When a copy is not good enough

Sometimes there is no acceptable place to start a copy. Pass `exclude_mask` to
name seconds that must **never** appear in the output — not as content, not as
GOP slop:

```python
exclude = [False] * 3600
exclude[58:64] = [True] * 6          # seconds 58–63 must not survive

extract_and_concat_ranges(
    "master.mp4",
    [(0.0, 58.0), (64.0, 300.0)],
    output_path="clean.mp4",
    work_dir="scratch/",
    exclude_mask=exclude,
)
```

Two things change. The merge refuses to close a gap containing an excluded
second, however narrow — merging would re-admit exactly what you banned. And
for any range where a copy would still snap back across an excluded second,
that one range is re-encoded with a frame-accurate seek (`-ss` after `-i`),
while every other range is still copied losslessly.

The re-encode arguments are derived from the master's own codec, pixel format
and colour metadata, because the concat demuxer joins streams without
converting them — an H.264 piece spliced into an HEVC timeline either fails the
join or produces a file only some players decode. Audio is stream-copied, so it
stays bit-identical. Override with `reencode_args=[...]`, or set
`allow_reencode=False` to stay strictly lossless and accept the slop.

## Install

```bash
pip install keycut
```

Or from a checkout:

```bash
pip install -e .
```

Requires Python 3.11+, and `ffmpeg` and `ffprobe` on your PATH. There are no
Python dependencies.

**Verified against** the ffmpeg/ffprobe git build `2026-04-10-0e983a0`
(libavutil 60.30 / libavcodec 62.29 / libavformat 62.13 — the 8.x line) on
Python 3.12.3, plus whatever `apt-get install ffmpeg` ships on the CI image for
Python 3.11, 3.12 and 3.13. The oldest requirement is a build that accepts
`-to` as an input option; check yours with:

```bash
ffmpeg -ss 1 -to 2 -i any.mp4 -c copy -f null - && echo ok
```

### Windows installer

Each release also carries `keycut-setup-<version>.exe` on its
[releases page](https://github.com/Back-Road-Creative/keycut/releases) — keycut
frozen into one executable, so it needs no Python. It installs into Program
Files, appends that directory to the system `PATH`, and registers an
uninstaller in Add/Remove Programs. Open a *new* terminal afterwards; one that
was already open still holds the old `PATH`.

Two things to know before downloading it.

**ffmpeg is not bundled, and keycut cannot do anything without it.** The
installer contains keycut and nothing else. An ffmpeg build dwarfs it, and
which licence a given build falls under depends on how it was configured, so
shipping one inside this installer would be both large and a claim this project
is not in a position to make. Install it yourself — this puts `ffmpeg` and
`ffprobe` on `PATH`:

```powershell
winget install -e --id Gyan.FFmpeg
```

`choco install ffmpeg` works too. Confirm with `ffmpeg -version`.

**The build is not code-signed.** There is no code-signing certificate for this
project, so Windows cannot show you a publisher. Expect the blue *"Windows
protected your PC"* box — "Microsoft Defender SmartScreen prevented an
unrecognized app from starting" — which runs the installer only after **More
info** → **Run anyway**, and expect your browser to warn during the download.
That is simply what an unsigned binary looks like; it is not evidence the file
is safe. If you would rather not make that call, `pip install keycut` needs no
installer.

## Command line

```bash
keycut master.mp4 highlights.mp4 -r 0:30 1:15 -r 4:00 4:45
```

Ranges are `SS`, `MM:SS` or `HH:MM:SS`, repeatable, and kept in the order you
give them. `-x/--exclude` adds an excluded window. `--dry-run` prints the plan —
which ranges merged, where each start was aligned to, and which ranges will be
copied versus re-encoded — without touching a byte:

```
$ keycut source.mp4 out.mp4 -r 10 20 -r 21 28 --dry-run
master:    source.mp4
keyframes: 15
merge gap: 2.100s
requested: 2 range(s) -> 1 extraction(s)
  [0] 10.000 -> 28.000  stream copy
```

The two ranges were 1 second apart — narrower than the measured 2.1 s GOP — so
they merged into one cut with no join to go wrong.

`python -m keycut …` works identically.

## Cutting against the original parts

Sometimes the ranges you have were measured against the *source clips* — "12 s
to 45 s of clip 3" — but the file you hold is a single master those clips were
concatenated into, and the clips themselves are long gone. Give keycut the
clip durations and it does the arithmetic:

```python
from keycut import Segment, boundaries_from_durations, extract_and_concat_segments

boundaries = boundaries_from_durations([1200.0, 900.0, 600.0])
# -> [(0.0, 1200.0), (1200.0, 2100.0), (2100.0, 2700.0)]

extract_and_concat_segments(
    "master.mp4",
    [Segment(source_index=1, start=50.0, end=150.0)],   # 50–150 s of clip 2
    boundaries,                                          # -> master 1250–1350
    output_path="out.mp4",
    work_dir="scratch/",
)
```

This only holds when the master is a *lossless* concatenation of exactly those
clips in that order. If anything was re-encoded or inserted between them,
measure the real offsets instead.

If your segment indices are global to a larger catalogue rather than local to
this one master, run them through `remap_segment_indices` first. That mismatch
is worth guarding: it either raises and drops the whole output, or — when the
index happens to be in range — cuts completely the wrong footage and says
nothing.

## Honest limits

- **You lose up to one GOP at the head of each range.** That is the price of
  refusing to include footage from before the cut. Ranges shorter than one GOP
  may end up with no keyframe to snap to at all; keycut leaves the start where
  it was rather than emitting nothing, so a copy of such a range still carries
  slop. Use `exclude_mask` if that slop is unacceptable, and keycut will
  re-encode instead.
- **Range ends are not aligned, and do not need to be.** `-to` stops the
  demuxer; the trailing partial GOP is simply not read.
- **Audio is cut at packet boundaries.** Expect up to a frame of audio (~21 ms
  for AAC) of drift at each join. Stream copy cannot do better; only a
  re-encode can.
- **Timestamps are rewritten by the concat demuxer.** Chapter marks, subtitle
  tracks and any other timeline-referenced data do not survive. keycut cuts
  video and audio.
- **Probing costs a full packet scan.** Roughly a second per hour of footage on
  a local disk. Results are cached per (path, mtime, size) for the life of the
  process; pass `keyframes=[…]` to skip it entirely.
- **Open-GOP sources are not handled specially.** With open GOPs a "keyframe"
  can still depend on the previous group, and the first frames after a cut may
  show artefacts. Encode with `open-gop=0` if you control the source.
- **Nothing raises.** A failed extraction returns `None` and logs the ffmpeg
  stderr. Check the return value — do not assume the file is there.
- **Variable frame rate sources** are cut correctly but may report a surprising
  duration afterwards, because the concat demuxer re-times the joins.

## API

Cutting:

| Function | Purpose |
| --- | --- |
| `extract_and_concat_ranges(master, ranges, output_path, work_dir, **opts)` | The main entry point. Returns the output path, or `None` on failure. |
| `extract_and_concat_segments(master, segments, boundaries, output_path, work_dir, **opts)` | Same, for ranges expressed against the master's source clips. |

Options: `exclude_mask`, `max_gap_sec`, `reencode_args`, `allow_reencode`,
`keyframes`, `ffmpeg_bin`, `ffprobe_bin`, `keep_scratch`, `faststart`.

The pieces, usable on their own:

| Function | Purpose |
| --- | --- |
| `probe_keyframes(path)` | Exact keyframe timestamps, or `None` when unknown. Cached. |
| `estimate_gop_duration(keyframes)` | The narrowest gap a stream copy can excise. |
| `snap_start_to_keyframe(start, end_limit, keyframes)` | Move a cut start forward onto a keyframe. |
| `keyframe_at_or_before(t, keyframes)` | Where `-ss t -c copy` would actually land. |
| `merge_ranges(ranges, max_gap_sec, is_excluded)` | Collapse overlapping, touching and sub-GOP-separated ranges. |
| `stream_copy_range` / `reencode_range` / `concat_demuxer` | The three ffmpeg invocations. |
| `default_reencode_args(master)` | Concat-compatible encoder arguments derived from the master. |
| `probe_video_params(master)` | Codec, pixel format and colour metadata of the first video stream. |

Segment translation: `Segment`, `boundaries_from_durations`,
`segment_to_master_range`, `segments_to_master_ranges`,
`remap_segment_indices`.

Optional companion, not part of the cutting path:
`assign_clips_by_source_window(clips, groups, boundaries)` — when one master
feeds two independent products, works out which output each derived clip came
from, by source footage rather than by dividing one count into another.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
```

The suite splits in two. Most of it mocks `subprocess` and asserts on the exact
argv ffmpeg would have been handed — that is where the decisions live. The rest
(`tests/test_integration_ffmpeg.py`) generates a 30-second test pattern with a
known 2-second GOP, demonstrates the artefact on it, and then proves keycut
does not reproduce it. Those tests skip when ffmpeg is not on PATH, so run them
before trusting a change.

Bug fixes want a failing test first.

## Licence

MIT — see [LICENSE](LICENSE).
