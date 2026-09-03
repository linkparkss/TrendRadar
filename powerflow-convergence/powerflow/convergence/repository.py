"""Public repository API with full preflight and rollback compensation."""

from __future__ import annotations

from .repository_impl import (
    InMemoryRepository,
    StateRepository,
    decode_psasp_text,
    encode_psasp_text,
    PsaspMySqlRepository as _PsaspMySqlRepository,
    _identifier,
)


class PsaspMySqlRepository(_PsaspMySqlRepository):
    """MySQL adapter whose rollback validates every row before writing any row."""

    def restore_candidate(self, candidate) -> None:
        connection = self._connect()
        locked = False
        try:
            with connection.cursor() as cursor:
                self._lock_tables(cursor, candidate.actions)
                locked = True
                current_values = [
                    self._current_value(cursor, action) for action in candidate.actions
                ]
                for action, current in zip(candidate.actions, current_values):
                    if current not in (action.before, action.after):
                        raise RuntimeError(
                            f"Refusing rollback for {action.device.name}: Valid={current}"
                        )
                restored = []
                try:
                    for action, current in zip(candidate.actions, current_values):
                        if current == action.after:
                            self._update(cursor, action, action.after, action.before)
                            restored.append(action)
                    for action in candidate.actions:
                        if self._current_value(cursor, action) != action.before:
                            raise RuntimeError(
                                f"Rollback check failed for {action.device.name}"
                            )
                    connection.commit()
                except BaseException:
                    for action in reversed(restored):
                        cursor.execute(
                            f"UPDATE {_identifier(action.device.table)} SET `Valid`=%s "
                            "WHERE `ID`=%s AND `Valid`=%s",
                            (action.after, action.device.record_id, action.before),
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
    "InMemoryRepository",
    "PsaspMySqlRepository",
    "StateRepository",
    "decode_psasp_text",
    "encode_psasp_text",
]
