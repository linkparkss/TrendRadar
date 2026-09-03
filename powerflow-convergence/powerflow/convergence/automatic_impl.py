"""Automatic wmlfadj.exe recovery runner seeded by labeled failure samples.

The runner reuses the legacy program's subprocess/result-file conventions but
adds field-level, conditional and reversible database actions. It is kept
separate from the manual state machine so enabling automatic calculation is an
explicit choice.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .assessment import VerificationAssessment, assess_ordinary_result
from .config import DatabaseConfig, VerificationConfig
from .models import CaseIdentity
from .repository import PsaspMySqlRepository
from .repository_impl import _identifier
from .training import SampleChange, TrainingCatalog, TrainingSample
from .verifier import capture_result_baseline


SUPPORTED_FIELDS = {
    "Valid",
    "Pg",
    "Qmax",
    "Qmin",
    "Pl",
    "Ql",
    "GivenDCPower_High",
    "GivenDCPower_Low",
}


@dataclass(frozen=True)
class FieldAction:
    table: str
    record_id: int
    device_name: str
    field: str
    before: Any
    after: Any

    def __post_init__(self) -> None:
        if self.field not in SUPPORTED_FIELDS:
            raise ValueError(f"Unsupported automatic field: {self.field}")
        if self.before == self.after:
            raise ValueError("Automatic action must change a value")

    @property
    def key(self) -> str:
        return f"{self.table}:{self.record_id}:{self.field}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "record_id": self.record_id,
            "device_name": self.device_name,
            "field": self.field,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class RecoveryCandidate:
    candidate_id: str
    sample_id: str
    actions: tuple[FieldAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "sample_id": self.sample_id,
            "actions": [action.to_dict() for action in self.actions],
        }


def recovery_candidate(sample: TrainingSample) -> RecoveryCandidate:
    """Invert a labeled baseline→failure perturbation into a recovery action."""
    actions = tuple(
        FieldAction(
            table=change.table,
            record_id=change.record_id,
            device_name=change.device_name,
            field=change.field,
            before=change.after,
            after=change.before,
        )
        for change in sample.changes
    )
    return RecoveryCandidate(
        candidate_id=f"RECOVER_{sample.sample_id}",
        sample_id=sample.sample_id,
        actions=actions,
    )


def candidate_from_catalog(catalog: TrainingCatalog, sample_id: str) -> RecoveryCandidate:
    for sample in catalog.samples:
        if sample.sample_id == sample_id:
            return recovery_candidate(sample)
    raise KeyError(f"Sample is not included in catalog: {sample_id}")


class AutomaticFieldRepository(PsaspMySqlRepository):
    """Field-level adapter with conditional update and compensation rollback."""

    @staticmethod
    def _lock_field_tables(cursor: Any, actions: Sequence[FieldAction]) -> None:
        tables = sorted({action.table for action in actions})
        clause = ", ".join(f"{_identifier(table)} WRITE" for table in tables)
        cursor.execute(f"LOCK TABLES {clause}")

    @staticmethod
    def _current_field(cursor: Any, action: FieldAction) -> Any:
        cursor.execute(
            f"SELECT {_identifier(action.field)} FROM {_identifier(action.table)} "
            "WHERE `ID`=%s",
            (action.record_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"Device row is missing or not unique: {action.key}")
        return rows[0][0]

    @staticmethod
    def _update_field(
        cursor: Any, action: FieldAction, expected: Any, replacement: Any
    ) -> None:
        cursor.execute(
            f"UPDATE {_identifier(action.table)} SET {_identifier(action.field)}=%s "
            "WHERE `ID`=%s AND "
            f"{_identifier(action.field)}=%s",
            (replacement, action.record_id, expected),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Conditional update failed: {action.key}")

    def _apply_or_restore(self, actions: Sequence[FieldAction], restore: bool) -> None:
        if not actions:
            raise ValueError("At least one automatic field action is required")
        connection = self._connect()
        locked = False
        applied: list[tuple[FieldAction, Any, Any]] = []
        try:
            with connection.cursor() as cursor:
                self._lock_field_tables(cursor, actions)
                locked = True
                expected_values = [
                    action.after if restore else action.before for action in actions
                ]
                target_values = [
                    action.before if restore else action.after for action in actions
                ]
                current_values = [
                    self._current_field(cursor, action) for action in actions
                ]
                for action, current, expected in zip(actions, current_values, expected_values):
                    if current != expected:
                        raise RuntimeError(
                            f"Refusing {'rollback' if restore else 'apply'} for "
                            f"{action.device_name}: current={current!r}, expected={expected!r}"
                        )
                try:
                    for action, expected, target in zip(actions, expected_values, target_values):
                        self._update_field(cursor, action, expected, target)
                        applied.append((action, expected, target))
                    for action, target in zip(actions, target_values):
                        if self._current_field(cursor, action) != target:
                            raise RuntimeError(f"Post-update check failed: {action.key}")
                    connection.commit()
                except BaseException:
                    for action, _, target in reversed(applied):
                        original = action.before if restore else action.before
                        # For a failed apply, target is the applied after-value;
                        # for a failed restore, target is the applied before-value.
                        compensation = action.after if restore else action.before
                        cursor.execute(
                            f"UPDATE {_identifier(action.table)} SET {_identifier(action.field)}=%s "
                            "WHERE `ID`=%s AND {_identifier(action.field)}=%s",
                            (compensation, action.record_id, target),
                        )
                    connection.commit()
                    raise
        finally:
            if locked:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("UNLOCK TABLES")
                except Exception:
                    pass
            connection.close()

    def apply_field_actions(self, actions: Sequence[FieldAction]) -> None:
        self._apply_or_restore(actions, restore=False)

    def restore_field_actions(self, actions: Sequence[FieldAction]) -> None:
        self._apply_or_restore(actions, restore=True)


@dataclass(frozen=True)
class WmlfadjOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    error: str | None


def run_wmlfadj(
    executable: str | Path,
    workdir: str | Path,
    timeout_seconds: float,
    invalidate: Callable[[Path], None] | None = None,
) -> WmlfadjOutcome:
    """Run the same ordinary executable used by 潮流收敛.py."""
    root = Path(workdir)
    if invalidate is not None:
        for name in ("LF.CAL", "LFCAL.LIS", "LF.LP1", "lfreport.lis", "LFERR.LIS"):
            invalidate(root / name)
    try:
        completed = subprocess.run(
            [str(executable)],
            cwd=str(root),
            capture_output=True,
            encoding="gbk",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return WmlfadjOutcome(
            returncode=None,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            timed_out=True,
            error=f"wmlfadj.exe timed out after {timeout_seconds} seconds",
        )
    error = None if completed.returncode in (0, 1) else f"wmlfadj.exe returned {completed.returncode}"
    return WmlfadjOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        timed_out=False,
        error=error,
    )


@dataclass(frozen=True)
class AutomaticRecoveryResult:
    candidate: RecoveryCandidate
    outcome: WmlfadjOutcome | None
    assessment: VerificationAssessment | None
    rolled_back: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "outcome": None if self.outcome is None else self.outcome.__dict__,
            "assessment": None
            if self.assessment is None
            else {
                "definitive": self.assessment.definitive,
                "result": self.assessment.result.to_dict(),
            },
            "rolled_back": self.rolled_back,
            "error": self.error,
        }


class AutomaticRecoveryService:
    def __init__(
        self,
        catalog: TrainingCatalog,
        repository: AutomaticFieldRepository,
        temp_path: str | Path,
        executable: str | Path,
        timeout_seconds: float = 120.0,
        verification: VerificationConfig | None = None,
    ) -> None:
        self.catalog = catalog
        self.repository = repository
        self.temp_path = Path(temp_path)
        self.executable = Path(executable)
        self.timeout_seconds = float(timeout_seconds)
        self.verification = verification or VerificationConfig()

    def run_sample(self, sample_id: str, execute: bool = True) -> AutomaticRecoveryResult:
        candidate = candidate_from_catalog(self.catalog, sample_id)
        if not execute:
            return AutomaticRecoveryResult(candidate, None, None, False, "dry-run")
        sample = next(sample for sample in self.catalog.samples if sample.sample_id == sample_id)
        baseline = capture_result_baseline(self.temp_path)
        self.repository.apply_field_actions(candidate.actions)
        outcome = None
        try:
            outcome = run_wmlfadj(
                self.executable,
                self.temp_path,
                self.timeout_seconds,
                invalidate=lambda path: path.unlink(missing_ok=True),
            )
            case = self.repository.get_case_status(
                CaseIdentity(sample.case_no, sample.case_name)
            )
            assessment = assess_ordinary_result(
                self.temp_path,
                baseline,
                case.identity,
                case,
                self.verification,
            )
            if outcome.error or outcome.timed_out or not assessment.definitive or not assessment.result.converged:
                self.repository.restore_field_actions(candidate.actions)
                return AutomaticRecoveryResult(candidate, outcome, assessment, True, outcome.error if outcome.error else "recovery candidate did not converge definitively")
            return AutomaticRecoveryResult(candidate, outcome, assessment, False, None)
        except BaseException as exc:
            try:
                self.repository.restore_field_actions(candidate.actions)
            except BaseException as rollback_exc:
                raise RuntimeError(f"Automatic recovery failed and rollback failed: {rollback_exc}") from exc
            raise
