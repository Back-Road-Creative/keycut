"""Shared helpers: a fake ffmpeg/ffprobe that never touches a real binary."""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keycut import clear_keyframe_cache


@pytest.fixture(autouse=True)
def _clear_keyframe_cache():
    clear_keyframe_cache()
    yield
    clear_keyframe_cache()


def keyframe_csv(interval: float = 2.002, count: int = 400) -> bytes:
    """ffprobe packet output for a file with a keyframe every ``interval``."""
    return "\n".join(f"{i * interval:.6f},K__" for i in range(count)).encode()


DEFAULT_STREAM_JSON = json.dumps(
    {
        "streams": [
            {
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "color_range": "tv",
                "color_space": "bt709",
                "color_primaries": "bt709",
                "color_transfer": "bt709",
                "width": 1920,
                "height": 1080,
            }
        ]
    }
).encode()


class FakeFfmpeg:
    """Records every command and fabricates plausible output.

    ``commands`` holds each argv list. ``list_files`` holds the text of every
    concat manifest at the moment it was used, since the real code deletes it
    afterwards.
    """

    def __init__(self, *, keyframe_stdout: bytes, stream_stdout: bytes, returncode: int = 0):
        self.keyframe_stdout = keyframe_stdout
        self.stream_stdout = stream_stdout
        self.returncode = returncode
        self.commands: list[list[str]] = []
        self.list_files: list[str] = []

    def __call__(self, cmd, *args, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        joined = " ".join(cmd)
        if cmd[0].endswith("ffprobe"):
            if "packet=" in joined:
                return MagicMock(returncode=0, stdout=self.keyframe_stdout, stderr=b"")
            return MagicMock(returncode=0, stdout=self.stream_stdout, stderr=b"")
        if "concat" in cmd and "-f" in cmd:
            manifest = Path(cmd[cmd.index("-i") + 1])
            if manifest.exists():
                self.list_files.append(manifest.read_text())
        if self.returncode == 0:
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fake-output")
        return MagicMock(returncode=self.returncode, stdout=b"", stderr=b"boom")

    # -- convenience views over the recorded commands -------------------

    @property
    def extract_commands(self) -> list[list[str]]:
        return [c for c in self.commands if c[0].endswith("ffmpeg") and "-ss" in c]

    @property
    def concat_commands(self) -> list[list[str]]:
        return [c for c in self.commands if "concat" in c and "-f" in c]


def is_stream_copy(cmd: list[str]) -> bool:
    return any(cmd[i] == "-c" and cmd[i + 1] == "copy" for i in range(len(cmd) - 1))


def is_reencode(cmd: list[str]) -> bool:
    return "-c:v" in cmd and not is_stream_copy(cmd)


def seek_is_input_side(cmd: list[str]) -> bool:
    """True when ``-ss`` comes before ``-i`` — a demuxer seek (fast, snaps to a
    keyframe). False means an output-side seek: a full decode to the exact frame.
    """
    return cmd.index("-ss") < cmd.index("-i")


@contextlib.contextmanager
def fake_ffmpeg(
    *,
    keyframe_stdout: bytes | None = None,
    stream_stdout: bytes | None = None,
    returncode: int = 0,
):
    """Patch both modules' ``subprocess.run`` with one :class:`FakeFfmpeg`.

    Yields the recorder, so a test can assert on the exact argv ffmpeg would
    have been given.
    """
    fake = FakeFfmpeg(
        keyframe_stdout=keyframe_csv() if keyframe_stdout is None else keyframe_stdout,
        stream_stdout=DEFAULT_STREAM_JSON if stream_stdout is None else stream_stdout,
        returncode=returncode,
    )
    with patch("keycut.extract.subprocess.run", side_effect=fake), patch(
        "keycut.keyframes.subprocess.run", side_effect=fake
    ):
        yield fake


HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg and ffprobe must be on PATH"
)
