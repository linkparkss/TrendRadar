"""Crash-safe JSON journal for a manual PSASP convergence run."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Candidate, CaseStatus, Device, DiagnosticReport, ResultBaseline, RunStatus


SCHEMA_VERSION = 1
JOURNAL_NAME = "run.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Value is not JSON serializable: {type(value).__name__}")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_default)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except BaseException:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def journal_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / JOURNAL_NAME


def save_journal(run_dir: str | Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = _now()
    atomic_write_json(journal_path(run_dir), payload)


def load_journal(run_dir: str | Path) -> dict[str, Any]:
    path = journal_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"Run journal does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported journal schema: {payload.get('schema_version')!r}"
        )
    return payload


def create_journal(
    run_dir: str | Path,
    *,
    run_id: str,
    config: dict[str, Any],
    case_status: CaseStatus,
    diagnosis: DiagnosticReport,
    devices: list[Device],
    candidates: list[Candidate],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": RunStatus.READY.value,
        "config": config,
        "case_status": case_status.to_dict(),
        "diagnosis": diagnosis.to_dict(),
        "devices": [device.to_dict() for device in devices],
        "candidates": [candidate.to_dict() for candidate in candidates],
        "candidate_cursor": 0,
        "current_attempt": None,
        "attempts": [],
        "result": None,
    }
    save_journal(run_dir, payload)
    return payload


def candidate_from_journal(payload: dict[str, Any], index: int | None = None) -> Candidate:
    candidates = payload.get("candidates", [])
    selected = payload.get("candidate_cursor", 0) if index is None else index
    try:
        item = candidates[int(selected)]
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Candidate index is out of range: {selected!r}") from exc
    return Candidate.from_dict(item)


def baseline_from_journal(payload: dict[str, Any]) -> ResultBaseline:
    current = payload.get("current_attempt") or {}
    baseline = current.get("baseline")
    if not baseline:
        raise RuntimeError("Current attempt has no result baseline")
    return ResultBaseline.from_dict(baseline)
