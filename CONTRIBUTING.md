# Contributing to keycut

Thanks for wanting to improve keycut. It is a small library with one job, so
the bar for a change is mostly "does it make the cut more correct, or the
explanation clearer".

## Reporting

- **Bugs and features:** open an issue. For a bad cut, the useful report is the
  output of `keycut … --dry-run` plus `ffprobe -select_streams v:0
  -show_entries packet=pts_time,flags <file>` on the source around the cut
  point. Duration alone is not enough — see the edit-list section of the README
  for why.
- **Security:** do not open a public issue. Follow [`SECURITY.md`](SECURITY.md).

## Development setup

Python 3.11 or newer, plus `ffmpeg` and `ffprobe` on your PATH.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## The gates

CI runs these on Python 3.11, 3.12 and 3.13. Run them locally first:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
```

`tests/test_integration_ffmpeg.py` **skips silently** when ffmpeg is not on
PATH, and it is the half of the suite that proves anything about real files.
Install ffmpeg before you trust a green run.

## What a change should look like

- **A bug fix starts with a failing test.** Write it, watch it fail for the
  right reason, then fix it. Never weaken or delete a test to get a suite green.
- **Claims about ffmpeg behaviour get an integration test, not a comment.**
  Two of the assertions in this repository exist because the behaviour everyone
  "knows" turned out to be wrong when measured. If you are asserting what
  ffmpeg does with some flag combination, prove it against a generated fixture.
- **No sample media.** The integration suite builds its own video from
  ffmpeg's `testsrc2` pattern. Keep it that way — nothing binary belongs in the
  repository.
- **Docs land in the same change.** If you alter behaviour, update the README
  in the same commit. A separate docs pass never happens.
- **Keep it dependency-free.** The standard library and ffmpeg are the whole
  toolkit. A change that needs numpy or a media library belongs in a different
  project.

## Commits and pull requests

- [Conventional commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- One logical change per pull request, with the gate output in the description.
- Pin any GitHub Action you add to a version tag, never a moving branch.
