"""Public automatic recovery API with compensated field rollback."""

from __future__ import annotations

from typing import Any, Sequence

from .automatic_impl import (
    AutomaticRecoveryResult,
    AutomaticRecoveryService,
    FieldAction,
    RecoveryCandidate,
    WmlfadjOutcome,
    candidate_from_catalog,
    recovery_candidate,
    run_wmlfadj,
)
from .automatic_impl import AutomaticFieldRepository as _AutomaticFieldRepository
from .repository_impl import _identifier


class AutomaticFieldRepository(_AutomaticFieldRepository):
    """Field adapter with a preflight and a fully reversible write block."""

    def _apply_or_restore(self, actions: Sequence[FieldAction], restore: bool) -> None:
        if not actions:
            raise ValueError("At least one automatic field action is required")
        connection = self._connect()
        locked = False
        applied: list[tuple[FieldAction, Any]] = []
        try:
            with connection.cursor() as cursor:
                self._lock_field_tables(cursor, actions)
                locked = True
                expected_values = [action.after if restore else action.before for action in actions]
                target_values = [action.before if restore else action.after for action in actions]
                current_values = [self._current_field(cursor, action) for action in actions]
                for action, current, expected in zip(actions, current_values, expected_values):
                    if current != expected:
                        raise RuntimeError(
                            f"Refusing {'rollback' if restore else 'apply'} for "
                            f"{action.device_name}: current={current!r}, expected={expected!r}"
                        )
                try:
                    for action, expected, target in zip(actions, expected_values, target_values):
                        self._update_field(cursor, action, expected, target)
                        applied.append((action, target))
                    for action, target in zip(actions, target_values):
                        if self._current_field(cursor, action) != target:
                            raise RuntimeError(f"Post-update check failed: {action.key}")
                    connection.commit()
                except BaseException:
                    for action, target in reversed(applied):
                        compensation = action.after if restore else action.before
                        cursor.execute(
                            f"UPDATE {_identifier(action.table)} SET {_identifier(action.field)}=%s "
                            f"WHERE `ID`=%s AND {_identifier(action.field)}=%s",
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


__all__ = [
    "AutomaticFieldRepository",
    "AutomaticRecoveryResult",
    "AutomaticRecoveryService",
    "FieldAction",
    "RecoveryCandidate",
    "WmlfadjOutcome",
    "candidate_from_catalog",
    "recovery_candidate",
    "run_wmlfadj",
]
