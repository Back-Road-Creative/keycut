"""The assembly receipt: a record of what an extraction actually cut.

Schema ``keycut-assembly-receipt/1``. All times are seconds, half-open
``[start, end)``. The receipt is plain data: ``to_dict()`` yields JSON-safe
values and ``from_dict()`` reads them back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "keycut-assembly-receipt/1"
PROVENANCE = "keycut"


@dataclass(frozen=True)
class ReceiptSpan:
    """One merged extraction and where it landed in the output."""

    index: int
    requested_indices: tuple[int, ...]
    merged_start: float
    merged_end: float
    source_start: float
    source_end: float
    method: str  # "copy" | "reencode"
    reason: str  # "stream_copy" | "keyframes_unknown" | "snap_back_over_excluded"
    output_start: float
    output_end: float
    duration_source: str  # "probed" | "planned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "requested_indices": list(self.requested_indices),
            "merged_start": self.merged_start,
            "merged_end": self.merged_end,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "method": self.method,
            "reason": self.reason,
            "output_start": self.output_start,
            "output_end": self.output_end,
            "duration_source": self.duration_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceiptSpan:
        return cls(
            index=int(data["index"]),
            requested_indices=tuple(int(i) for i in data["requested_indices"]),
            merged_start=float(data["merged_start"]),
            merged_end=float(data["merged_end"]),
            source_start=float(data["source_start"]),
            source_end=float(data["source_end"]),
            method=str(data["method"]),
            reason=str(data["reason"]),
            output_start=float(data["output_start"]),
            output_end=float(data["output_end"]),
            duration_source=str(data["duration_source"]),
        )


@dataclass(frozen=True)
class AssemblyReceipt:
    """What :func:`keycut.extract_and_concat_ranges_with_receipt` actually did."""

    keycut_version: str
    master_path: str
    master_size_bytes: int | None
    max_gap_sec: float
    requested: tuple[tuple[float, float], ...]
    spans: tuple[ReceiptSpan, ...]
    output_path: str
    output_duration: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "provenance": PROVENANCE,
            "keycut_version": self.keycut_version,
            "master": {"path": self.master_path, "size_bytes": self.master_size_bytes},
            "time_base": "seconds",
            "max_gap_sec": self.max_gap_sec,
            "requested": [[s, e] for s, e in self.requested],
            "spans": [span.to_dict() for span in self.spans],
            "output": {"path": self.output_path, "duration": self.output_duration},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AssemblyReceipt:
        if data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported receipt schema: {data.get('schema')!r}")
        size = data["master"]["size_bytes"]
        duration = data["output"]["duration"]
        return cls(
            keycut_version=str(data["keycut_version"]),
            master_path=str(data["master"]["path"]),
            master_size_bytes=None if size is None else int(size),
            max_gap_sec=float(data["max_gap_sec"]),
            requested=tuple((float(s), float(e)) for s, e in data["requested"]),
            spans=tuple(ReceiptSpan.from_dict(s) for s in data["spans"]),
            output_path=str(data["output"]["path"]),
            output_duration=None if duration is None else float(duration),
        )

    def write_json(self, path: Path | str) -> Path:
        """Write the receipt as indented JSON; returns the path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
