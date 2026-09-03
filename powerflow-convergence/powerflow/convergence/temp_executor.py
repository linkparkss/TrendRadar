"""Automatic sample recovery against the active PSASP Temp input files."""

from __future__ import annotations

import importlib
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .assessment import VerificationAssessment, assess_ordinary_result
from .automatic_impl import FieldAction, RecoveryCandidate, WmlfadjOutcome, candidate_from_catalog
from .config import VerificationConfig
from .models import CaseIdentity, CaseStatus
from .training import TrainingCatalog
from .verifier import capture_result_baseline


_CASE_LINE_RE = re.compile(r"^\s*(-?\d+)\s*,\s*['\"]?(.*?)['\"]?\s*,?\s*$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")
_RESULT_FILES = (
    "LF.CAL",
    "LFCAL.LIS",
    "LF.LP1",
    "lfreport.lis",
    "LFERR.LIS",
    "LF.adj",
)


@dataclass(frozen=True)
class ActiveTempCase:
    identity: CaseIdentity
    tolerance: float


@dataclass(frozen=True)
class TempRecoveryResult:
    candidate: RecoveryCandidate
    outcome: WmlfadjOutcome | None
    assessment: VerificationAssessment | None
    rolled_back: bool
    active_case: ActiveTempCase | None = None
    run_dir: Path | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "active_case": None
            if self.active_case is None
            else {
                "case_no": self.active_case.identity.case_no,
                "case_name": self.active_case.identity.case_name,
                "tolerance": self.active_case.tolerance,
            },
            "outcome": None if self.outcome is None else self.outcome.__dict__,
            "assessment": None
            if self.assessment is None
            else {
                "definitive": self.assessment.definitive,
                "result": self.assessment.result.to_dict(),
            },
            "rolled_back": self.rolled_back,
            "run_dir": None if self.run_dir is None else str(self.run_dir),
            "error": self.error,
        }


def _read_lines(path: Path) -> list[str]:
    with path.open("r", encoding="gbk", errors="surrogateescape", newline="") as handle:
        return handle.readlines()


def _encode_lines(lines: Iterable[str]) -> bytes:
    return "".join(lines).encode("gbk", errors="surrogateescape")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.automatic.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, content)


def _name(token: str) -> str:
    return token.strip().strip("'").strip('"').strip()


def _float(token: str, label: str) -> float:
    raw = token.strip()
    if not _NUMBER_RE.fullmatch(raw):
        raise ValueError(f"{label} is not numeric: {token!r}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{label} is not finite: {token!r}")
    return value


def _matches(actual: str, expected: Any, field: str) -> bool:
    if field == "Valid":
        try:
            return int(actual.strip()) == int(expected)
        except (TypeError, ValueError):
            return False
    return math.isclose(
        _float(actual, field),
        float(expected),
        rel_tol=0.0,
        abs_tol=1e-6,
    )


def _format_like(original: str, replacement: Any, field: str) -> str:
    leading = original[: len(original) - len(original.lstrip())]
    trailing = original[len(original.rstrip()) :]
    core = original.strip()
    if field == "Valid":
        rendered = str(int(replacement))
    else:
        decimal_places = len(core.split(".", 1)[1]) if "." in core else 6
        rendered = f"{float(replacement):.{decimal_places}f}"
    # Preserve the card's original leading/trailing whitespace.  Padding a
    # shorter replacement here is not reversible: changing 10.0 -> 8.0 would
    # introduce a new leading space that survives the inverse action.
    return leading + rendered + trailing


def read_active_temp_case(temp_path: str | Path) -> ActiveTempCase:
    """Read Case_No, Case_Name and tolerance from LF.L0."""

    path = Path(temp_path) / "LF.L0"
    if not path.is_file():
        raise FileNotFoundError(f"active PSASP case file is missing: {path}")
    lines = _read_lines(path)
    match: re.Match[str] | None = None
    for line in reversed(lines):
        candidate = _CASE_LINE_RE.match(line.rstrip("\r\n"))
        if candidate:
            match = candidate
            break
    if match is None:
        raise ValueError(f"cannot read Case_No/Case_Name from {path}")

    tolerance = 0.0001
    if len(lines) > 1:
        fields = lines[1].split(",")
        if len(fields) > 3:
            try:
                parsed = float(fields[3].strip())
            except ValueError:
                parsed = tolerance
            if math.isfinite(parsed) and parsed > 0:
                tolerance = parsed
    return ActiveTempCase(
        CaseIdentity(int(match.group(1)), _name(match.group(2))),
        tolerance,
    )


def _action_file(root: Path, action: FieldAction) -> Path:
    if action.table.startswith("ls2_"):
        return root / "LF.L2"
    if action.table.startswith("ls3_"):
        return root / "LF.L3"
    if action.table.startswith("ls5_"):
        return root / "LF.L5"
    if action.table.startswith("ls6_"):
        return root / "LF.L6"
    if action.table.startswith("ls_lcc_"):
        return root / "LF.ML4"
    raise ValueError(f"unsupported automatic Temp table: {action.table}")


def _replace_l5(
    lines: list[str],
    actions: Sequence[FieldAction],
    allow_already_applied: bool = False,
) -> list[str]:
    by_name: dict[str, list[FieldAction]] = {}
    for action in actions:
        by_name.setdefault(action.device_name, []).append(action)
    if len({(action.device_name, action.field) for action in actions}) != len(actions):
        raise ValueError("duplicate LF.L5 device actions")
    counts = {(action.device_name, action.field): 0 for action in actions}
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        fields = body.split(",")
        if len(fields) >= 20:
            device_name = _name(fields[19])
            for action in by_name.get(device_name, []):
                index = {
                    "Valid": 0,
                    "Pg": 4,
                    "Qmax": 8,
                    "Qmin": 9,
                }.get(action.field)
                if index is None:
                    raise ValueError(f"unsupported LF.L5 field: {action.field}")
                if _matches(fields[index], action.before, action.field):
                    fields[index] = _format_like(
                        fields[index],
                        action.after,
                        action.field,
                    )
                elif allow_already_applied and _matches(
                    fields[index],
                    action.after,
                    action.field,
                ):
                    pass
                else:
                    raise RuntimeError(
                        f"refusing {device_name}: LF.L5 {action.field}="
                        f"{fields[index].strip()!r}, expected "
                        f"{action.before!r} or {action.after!r}"
                    )
                counts[(device_name, action.field)] += 1
            if by_name.get(device_name):
                line = ",".join(fields) + newline
        updated.append(line)
    _validate_counts("LF.L5", counts)
    return updated


def _lcc_record_starts(lines: Sequence[str]) -> list[int]:
    starts: list[int] = []
    in_section = False
    index = 0
    while index < len(lines):
        if lines[index].startswith("#4,"):
            in_section = True
            index += 1
            continue
        if in_section and lines[index].startswith("#"):
            break
        if in_section and lines[index].strip() and index + 2 < len(lines):
            if all(not lines[position].startswith("#") for position in range(index, index + 3)):
                starts.append(index)
                index += 3
                continue
        index += 1
    return starts


def _replace_lcc(
    lines: list[str],
    actions: Sequence[FieldAction],
    allow_already_applied: bool = False,
) -> list[str]:
    by_name: dict[str, list[FieldAction]] = {}
    for action in actions:
        by_name.setdefault(action.device_name, []).append(action)
    if len({(action.device_name, action.field) for action in actions}) != len(actions):
        raise ValueError("duplicate LF.ML4 device actions")
    counts = {(action.device_name, action.field): 0 for action in actions}
    for start in _lcc_record_starts(lines):
        first = lines[start].rstrip("\r\n").split(",")
        device_name = _name(first[8]) if len(first) > 8 else ""
        actions_for_device = by_name.get(device_name, [])
        if not actions_for_device:
            continue
        control_index = start + 2
        body = lines[control_index].rstrip("\r\n")
        newline = lines[control_index][len(body) :]
        fields = body.split(",")
        if len(fields) <= 4:
            raise ValueError(f"malformed LF.ML4 record for {device_name}")
        for action in actions_for_device:
            if action.field not in {"GivenDCPower_High", "GivenDCPower_Low"}:
                raise ValueError(f"unsupported LF.ML4 field: {action.field}")
            if _matches(fields[4], action.before, action.field):
                fields[4] = _format_like(fields[4], action.after, action.field)
            elif not (
                allow_already_applied
                and _matches(fields[4], action.after, action.field)
            ):
                raise RuntimeError(
                    f"refusing {device_name}: LF.ML4 {action.field}="
                    f"{fields[4].strip()!r}, expected "
                    f"{action.before!r} or {action.after!r}"
                )
            counts[(device_name, action.field)] += 1
        lines[control_index] = ",".join(fields) + newline
    _validate_counts("LF.ML4", counts)
    return lines


def _validate_counts(filename: str, counts: Mapping[str, int]) -> None:
    missing = [name for name, count in counts.items() if count == 0]
    duplicated = [name for name, count in counts.items() if count > 1]
    if missing:
        raise ValueError(f"devices not found in {filename}: {missing}")
    if duplicated:
        raise ValueError(f"device records are duplicated in {filename}: {duplicated}")


def _replace_simple_card(
    lines: list[str],
    actions: Sequence[FieldAction],
    *,
    filename: str,
    name_index: int,
    field_indices: Mapping[str, int],
    allow_already_applied: bool = False,
) -> list[str]:
    by_name: dict[str, list[FieldAction]] = {}
    for action in actions:
        by_name.setdefault(action.device_name, []).append(action)
    if len({(action.device_name, action.field) for action in actions}) != len(actions):
        raise ValueError(f"duplicate {filename} device actions")
    counts = {(action.device_name, action.field): 0 for action in actions}
    updated: list[str] = []
    for line in lines:
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        fields = body.split(",")
        if len(fields) <= name_index:
            updated.append(line)
            continue
        device_name = _name(fields[name_index])
        actions_for_device = by_name.get(device_name, [])
        if not actions_for_device:
            updated.append(line)
            continue
        for action in actions_for_device:
            index = field_indices.get(action.field)
            if index is None or len(fields) <= index:
                raise ValueError(f"unsupported {filename} field: {action.field}")
            if _matches(fields[index], action.before, action.field):
                fields[index] = _format_like(fields[index], action.after, action.field)
            elif not (
                allow_already_applied
                and _matches(fields[index], action.after, action.field)
            ):
                raise RuntimeError(
                    f"refusing {device_name}: {filename} {action.field}="
                    f"{fields[index].strip()!r}, expected "
                    f"{action.before!r} or {action.after!r}"
                )
            counts[(device_name, action.field)] += 1
        updated.append(",".join(fields) + newline)
    _validate_counts(filename, counts)
    return updated


def _grouped_actions(root: Path, actions: Sequence[FieldAction]) -> dict[Path, list[FieldAction]]:
    if not actions:
        raise ValueError("at least one Temp action is required")
    grouped: dict[Path, list[FieldAction]] = {}
    for action in actions:
        grouped.setdefault(_action_file(root, action), []).append(action)
    return grouped


def apply_temp_actions(
    temp_path: str | Path,
    actions: Sequence[FieldAction],
    *,
    allow_already_applied: bool = False,
) -> None:
    """Conditionally apply all sample actions to LF.L5 and/or LF.ML4."""

    grouped = _grouped_actions(Path(temp_path), actions)
    replacements: dict[Path, bytes] = {}
    for path, file_actions in grouped.items():
        if not path.is_file():
            raise FileNotFoundError(f"PSASP input file is missing: {path}")
        lines = _read_lines(path)
        card = path.name.upper()
        if card == "LF.L2":
            updated = _replace_simple_card(
                lines,
                file_actions,
                filename="LF.L2",
                name_index=17,
                field_indices={"Valid": 0},
                allow_already_applied=allow_already_applied,
            )
        elif card == "LF.L3":
            updated = _replace_simple_card(
                lines,
                file_actions,
                filename="LF.L3",
                name_index=24,
                field_indices={"Valid": 0},
                allow_already_applied=allow_already_applied,
            )
        elif card == "LF.L5":
            updated = _replace_l5(lines, file_actions, allow_already_applied)
        elif card == "LF.L6":
            updated = _replace_simple_card(
                lines,
                file_actions,
                filename="LF.L6",
                name_index=18,
                field_indices={"Valid": 0, "Pl": 4, "Ql": 5},
                allow_already_applied=allow_already_applied,
            )
        else:
            updated = _replace_lcc(lines, file_actions, allow_already_applied)
        replacements[path] = _encode_lines(updated)
    for path, content in replacements.items():
        _atomic_write_bytes(path, content)


def synchronize_temp_candidate(
    temp_path: str | Path,
    base_actions: Sequence[FieldAction],
    candidate_actions: Sequence[FieldAction],
) -> None:
    """Set every labeled Temp field to the exact candidate state."""

    base_by_key = {action.key: action for action in base_actions}
    selected_by_key = {action.key: action for action in candidate_actions}
    if len(base_by_key) != len(base_actions):
        raise ValueError("duplicate base Temp action keys")
    if len(selected_by_key) != len(candidate_actions):
        raise ValueError("duplicate candidate Temp action keys")
    unknown = sorted(set(selected_by_key) - set(base_by_key))
    if unknown:
        raise ValueError(f"candidate actions are not in the base sample: {unknown}")

    synchronization = []
    for action in base_actions:
        selected = selected_by_key.get(action.key)
        if selected is not None:
            if selected != action:
                raise ValueError(f"candidate action differs from base sample: {action.key}")
            synchronization.append(action)
        else:
            synchronization.append(
                FieldAction(
                    table=action.table,
                    record_id=action.record_id,
                    device_name=action.device_name,
                    field=action.field,
                    before=action.after,
                    after=action.before,
                )
            )
    apply_temp_actions(
        temp_path,
        tuple(synchronization),
        allow_already_applied=True,
    )


class PersistentSnapshot:
    """Byte-exact Temp backup with a recoverable and verifiable manifest."""

    def __init__(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.manifest = manifest

    @classmethod
    def create(
        cls,
        temp_path: Path,
        run_root: Path,
        candidate: RecoveryCandidate,
        backup_actions: Sequence[FieldAction] | None = None,
        *,
        include_results: bool = False,
        extra_input_names: Sequence[str] = (),
    ) -> "PersistentSnapshot":
        run_root.mkdir(parents=True, exist_ok=True)
        stem = f"SAMPLE_{candidate.sample_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir = run_root / stem
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = run_root / f"{stem}_{suffix}"
        run_dir.mkdir()

        files = []
        actions_to_backup = tuple(backup_actions or candidate.actions)
        if not actions_to_backup and not extra_input_names:
            raise ValueError("snapshot requires Temp actions or extra input files")
        sources = (
            list(_grouped_actions(temp_path, actions_to_backup)) if actions_to_backup else []
        )
        known_source_names = {source.name.upper() for source in sources}
        for raw_name in extra_input_names:
            name = Path(raw_name).name
            if name != raw_name or name.upper() in known_source_names:
                continue
            sources.append(temp_path / name)
            known_source_names.add(name.upper())
        for source in sources:
            if not source.is_file():
                raise FileNotFoundError(f"PSASP input file is missing: {source}")
            backup = run_dir / f"{source.name}.original"
            stat = source.stat()
            content = source.read_bytes()
            backup.write_bytes(content)
            files.append(
                {
                    "target": str(source),
                    "backup": backup.name,
                    "kind": "input",
                    "existed": True,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "atime_ns": stat.st_atime_ns,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        if include_results:
            known_targets = {Path(str(item["target"])).name.upper() for item in files}
            for name in _RESULT_FILES:
                if name.upper() in known_targets:
                    continue
                source = temp_path / name
                item: dict[str, Any] = {
                    "target": str(source),
                    "backup": None,
                    "kind": "result",
                    "existed": source.is_file(),
                    "size": 0,
                    "sha256": None,
                }
                if source.is_file():
                    stat = source.stat()
                    content = source.read_bytes()
                    backup = run_dir / f"{source.name}.original"
                    backup.write_bytes(content)
                    item.update(
                        backup=backup.name,
                        size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        atime_ns=stat.st_atime_ns,
                        mtime_ns=stat.st_mtime_ns,
                    )
                files.append(item)
        manifest = {
            "schema_version": 2,
            "status": "prepared",
            "sample_id": candidate.sample_id,
            "candidate": candidate.to_dict(),
            "files": files,
            "error": None,
        }
        snapshot = cls(run_dir, manifest)
        snapshot.mark("prepared")
        return snapshot

    @classmethod
    def open(cls, run_dir: str | Path) -> "PersistentSnapshot":
        root = Path(run_dir)
        manifest_path = root / "run.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"run manifest is missing: {manifest_path}")
        return cls(root, json.loads(manifest_path.read_text(encoding="utf-8")))

    def mark(self, status: str, error: str | None = None) -> None:
        self.manifest["status"] = status
        self.manifest["error"] = error
        self.manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(self.run_dir / "run.json", self.manifest)

    def restore(self, temp_path: str | Path) -> None:
        root = Path(temp_path).resolve()
        try:
            for item in self.manifest.get("files", []):
                target = Path(str(item["target"])).resolve()
                if not target.is_relative_to(root):
                    raise RuntimeError(f"refusing restore outside Temp: {target}")
                existed = bool(item.get("existed", True))
                if not existed:
                    target.unlink(missing_ok=True)
                    continue
                backup_name = item.get("backup")
                if not backup_name:
                    raise RuntimeError(f"backup is missing for {target}")
                backup = (self.run_dir / str(backup_name)).resolve()
                if not backup.is_relative_to(self.run_dir.resolve()) or not backup.is_file():
                    raise RuntimeError(f"invalid backup path: {backup}")
                content = backup.read_bytes()
                expected = item.get("sha256")
                if expected and hashlib.sha256(content).hexdigest() != expected:
                    raise RuntimeError(f"backup checksum mismatch: {backup}")
                _atomic_write_bytes(target, content)
                if "mtime_ns" in item:
                    os.utime(
                        target,
                        ns=(
                            int(item.get("atime_ns", item["mtime_ns"])),
                            int(item["mtime_ns"]),
                        ),
                    )

            for item in self.manifest.get("files", []):
                target = Path(str(item["target"])).resolve()
                existed = bool(item.get("existed", True))
                if target.is_file() != existed:
                    raise RuntimeError(f"restore existence mismatch: {target}")
                if existed:
                    content = target.read_bytes()
                    expected = item.get("sha256")
                    if expected and hashlib.sha256(content).hexdigest() != expected:
                        raise RuntimeError(f"restore checksum mismatch: {target}")
                    if "size" in item and len(content) != int(item["size"]):
                        raise RuntimeError(f"restore size mismatch: {target}")
        except BaseException as exc:
            self.mark("rollback_failed", f"{type(exc).__name__}: {exc}")
            raise
        self.mark("rolled_back")


def run_legacy_wmlfadj(
    executable: str | Path,
    workdir: str | Path,
    timeout_seconds: float,
) -> WmlfadjOutcome:
    """Use the process wrapper defined by the original ``潮流收敛.py``."""

    root = Path(workdir)
    for name in _RESULT_FILES:
        (root / name).unlink(missing_ok=True)
    legacy = importlib.import_module("潮流收敛")
    result = legacy.run_load_flow(executable, root, timeout_seconds)
    return WmlfadjOutcome(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        timed_out=result.timed_out,
        error=result.error,
    )


def _lf_cal_timestamp(temp_path: Path) -> tuple[str, str]:
    path = temp_path / "LF.CAL"
    if not path.is_file():
        return "", ""
    lines = _read_lines(path)
    if len(lines) < 2:
        return "", ""
    values = lines[1].strip().split(",")
    if len(values) < 2:
        return "", ""
    date, clock = values[0].strip(), values[1].strip()
    if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"\d{6}", clock):
        return "", ""
    return f"{date[:4]}/{date[4:6]}/{date[6:]}", f"{clock[:2]}:{clock[2:4]}:{clock[4:]}"


class TempRecoveryService:
    """Apply one labeled inverse action, run wmlfadj, verify, and roll back."""

    def __init__(
        self,
        catalog: TrainingCatalog,
        temp_path: str | Path,
        executable: str | Path,
        run_root: str | Path,
        timeout_seconds: float = 120.0,
        verification: VerificationConfig | None = None,
    ) -> None:
        self.catalog = catalog
        self.temp_path = Path(temp_path)
        self.executable = Path(executable)
        self.run_root = Path(run_root)
        self.timeout_seconds = float(timeout_seconds)
        self.verification = verification or VerificationConfig()

    def run_sample(self, sample_id: str, execute: bool = True) -> TempRecoveryResult:
        candidate = candidate_from_catalog(self.catalog, sample_id)
        sample = next(sample for sample in self.catalog.samples if sample.sample_id == sample_id)
        active = read_active_temp_case(self.temp_path)
        expected = CaseIdentity(sample.case_no, sample.case_name)
        if active.identity != expected:
            return TempRecoveryResult(
                candidate,
                None,
                None,
                False,
                active,
                error=(
                    "active Temp case does not match sample: "
                    f"{active.identity.case_no}/{active.identity.case_name!r} != "
                    f"{expected.case_no}/{expected.case_name!r}"
                ),
            )
        if not execute:
            return TempRecoveryResult(candidate, None, None, False, active, error="dry-run")
        if not self.executable.is_file():
            raise FileNotFoundError(f"wmlfadj.exe does not exist: {self.executable}")

        baseline = capture_result_baseline(self.temp_path)
        snapshot = PersistentSnapshot.create(self.temp_path, self.run_root, candidate)
        try:
            apply_temp_actions(self.temp_path, candidate.actions)
            snapshot.mark("applied")
            outcome = run_legacy_wmlfadj(self.executable, self.temp_path, self.timeout_seconds)
            date, clock = _lf_cal_timestamp(self.temp_path)
            status = CaseStatus(
                identity=active.identity,
                calculate=1,
                tolerance=active.tolerance,
                iteration_limit=0,
                calculation_date=date,
                calculation_time=clock,
            )
            assessment = assess_ordinary_result(
                self.temp_path,
                baseline,
                expected,
                status,
                self.verification,
            )
            if outcome.error or outcome.timed_out or not assessment.definitive or not assessment.result.converged:
                reason = outcome.error or "recovery candidate did not converge definitively"
                snapshot.restore(self.temp_path)
                snapshot.mark("rolled_back", reason)
                return TempRecoveryResult(candidate, outcome, assessment, True, active, snapshot.run_dir, reason)
            snapshot.mark("converged")
            return TempRecoveryResult(candidate, outcome, assessment, False, active, snapshot.run_dir)
        except BaseException as exc:
            try:
                snapshot.restore(self.temp_path)
                snapshot.mark("rolled_back_error", str(exc))
            except BaseException as rollback_error:
                snapshot.mark("rollback_failed", str(rollback_error))
                raise RuntimeError(
                    f"automatic recovery failed and file rollback failed; run_dir={snapshot.run_dir}"
                ) from exc
            raise

    def restore_run(self, run_dir: str | Path) -> Path:
        snapshot = PersistentSnapshot.open(run_dir)
        snapshot.restore(self.temp_path)
        return snapshot.run_dir


__all__ = [
    "ActiveTempCase",
    "PersistentSnapshot",
    "TempRecoveryResult",
    "TempRecoveryService",
    "apply_temp_actions",
    "read_active_temp_case",
    "run_legacy_wmlfadj",
    "synchronize_temp_candidate",
]
