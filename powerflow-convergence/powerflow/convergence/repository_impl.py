"""PSASP state repositories with conditional, reversible device updates."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Protocol

from .config import ActionPolicy, CaseConfig, DatabaseConfig
from .models import Action, Candidate, CaseIdentity, CaseStatus, Device, DeviceType


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def decode_psasp_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return value.encode("latin1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def encode_psasp_text(value: str) -> str:
    return value.encode("gbk").decode("latin1")


def _json_value(value: Any) -> Any:
    value = decode_psasp_text(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _pick(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    lowered = {key.lower(): key for key in row}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return row[key]
    return default


class StateRepository(Protocol):
    def resolve_case(self, selection: CaseConfig) -> CaseStatus: ...

    def get_case_status(self, identity: CaseIdentity) -> CaseStatus: ...

    def discover_devices(
        self, identity: CaseIdentity, policy: ActionPolicy
    ) -> list[Device]: ...

    def candidate_state(self, candidate: Candidate) -> str: ...

    def apply_candidate(self, candidate: Candidate) -> None: ...

    def restore_candidate(self, candidate: Candidate) -> None: ...


class PsaspMySqlRepository:
    """Direct adapter for PSASP's per-job MySQL tables.

    PSASP installations commonly use MyISAM tables. Updates are therefore
    protected by table locks and explicitly compensated on partial failure.
    """

    def __init__(
        self,
        config: DatabaseConfig,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._connect_factory = connect_factory

    def _connect(self):
        factory = self._connect_factory
        if factory is None:
            try:
                import pymysql
            except ImportError as exc:
                raise RuntimeError("pymysql is required for the PSASP database") from exc
            factory = pymysql.connect
        return factory(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password(),
            database=self.config.database,
            charset=self.config.charset,
            autocommit=False,
        )

    @staticmethod
    def _columns(cursor: Any, table: str) -> list[str]:
        cursor.execute(f"SHOW COLUMNS FROM {_identifier(table)}")
        return [str(row[0]) for row in cursor.fetchall()]

    def _rows(self, cursor: Any, table: str) -> list[dict[str, Any]]:
        columns = self._columns(cursor, table)
        cursor.execute(f"SELECT * FROM {_identifier(table)}")
        return [
            {key: decode_psasp_text(value) for key, value in zip(columns, raw)}
            for raw in cursor.fetchall()
        ]

    @staticmethod
    def _case_status(row: dict[str, Any]) -> CaseStatus:
        return CaseStatus(
            identity=CaseIdentity(
                case_no=int(_pick(row, "Case_No")),
                case_name=str(_pick(row, "Case_Name", default="")),
            ),
            calculate=int(_pick(row, "CALCULATE", default=0) or 0),
            tolerance=float(_pick(row, "Tolerance", default=0.0001) or 0.0001),
            iteration_limit=int(_pick(row, "Iteration", default=0) or 0),
            calculation_date=str(_pick(row, "CAL_Date", default="") or ""),
            calculation_time=str(_pick(row, "CAL_Time", default="") or ""),
        )

    def resolve_case(self, selection: CaseConfig) -> CaseStatus:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                rows = self._rows(cursor, "lf_case")
            matches = []
            for row in rows:
                number_matches = selection.case_no is None or int(
                    _pick(row, "Case_No")
                ) == selection.case_no
                name_matches = not selection.name or str(
                    _pick(row, "Case_Name", default="")
                ) == selection.name
                if number_matches and name_matches:
                    matches.append(row)
            if len(matches) != 1:
                raise RuntimeError(
                    "Expected exactly one configured load-flow case, "
                    f"found {len(matches)}"
                )
            return self._case_status(matches[0])
        finally:
            connection.rollback()
            connection.close()

    def get_case_status(self, identity: CaseIdentity) -> CaseStatus:
        return self.resolve_case(
            CaseConfig(name=identity.case_name, case_no=identity.case_no)
        )

    @staticmethod
    def _device_from_row(
        table: str,
        row: dict[str, Any],
        device_type: DeviceType,
    ) -> Device:
        record_id = _pick(row, "ID")
        name = str(_pick(row, "ID_Name", default="") or "")
        valid = int(_pick(row, "Valid", default=-1))
        if record_id is None or not name or valid not in (0, 1):
            raise ValueError(f"Invalid adjustable-device row in {table}: {name!r}")
        bus_name = str(
            _pick(
                row,
                "Node_Name",
                "I_Name",
                "Bus_Name",
                "Set_Bus",
                "Ctrl_Bus",
                default=name,
            )
            or name
        )
        capacity = float(
            _pick(row, "Rate_MW", "Qmax", "Pg", "X", default=0.0) or 0.0
        )
        keep = (
            "ID_Name",
            "I_Name",
            "J_Name",
            "Node_Name",
            "Valid",
            "Pg",
            "Qg",
            "Qmax",
            "Qmin",
            "Rate_MW",
            "X",
        )
        metadata = {
            key: _json_value(_pick(row, key))
            for key in keep
            if _pick(row, key) is not None
        }
        return Device(
            table=table,
            record_id=int(record_id),
            name=name,
            device_type=device_type,
            bus_name=bus_name,
            valid=valid,
            capacity=capacity,
            metadata=metadata,
        )

    @staticmethod
    def _shunt_type(row: dict[str, Any]) -> DeviceType | None:
        name = str(_pick(row, "ID_Name", default="") or "")
        if "电容" in name:
            return DeviceType.CAPACITOR
        if "电抗" in name:
            return DeviceType.REACTOR
        return None

    @staticmethod
    def _is_self_branch(row: dict[str, Any]) -> bool:
        i_no = _pick(row, "I_No")
        j_no = _pick(row, "J_No")
        if i_no is not None and j_no is not None:
            return i_no == j_no
        i_name = _pick(row, "I_Name")
        j_name = _pick(row, "J_Name")
        return i_name is not None and i_name == j_name

    def discover_devices(
        self, identity: CaseIdentity, policy: ActionPolicy
    ) -> list[Device]:
        connection = self._connect()
        devices: list[Device] = []
        try:
            with connection.cursor() as cursor:
                if policy.generator_status:
                    table = f"ls5_{identity.case_no}"
                    for row in self._rows(cursor, table):
                        devices.append(
                            self._device_from_row(table, row, DeviceType.GENERATOR)
                        )
                if policy.shunt_status:
                    table = f"ls2_{identity.case_no}"
                    for row in self._rows(cursor, table):
                        kind = self._shunt_type(row)
                        if kind is None or not self._is_self_branch(row):
                            continue
                        devices.append(self._device_from_row(table, row, kind))
            devices.sort(key=lambda item: (item.device_type.value, item.name, item.record_id))
            return devices
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _current_value(cursor: Any, action: Action, lock: bool = False) -> int:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(
            f"SELECT `Valid` FROM {_identifier(action.device.table)} "
            f"WHERE `ID`=%s{suffix}",
            (action.device.record_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                f"Device {action.device.key} is missing or no longer unique"
            )
        return int(rows[0][0])

    def candidate_state(self, candidate: Candidate) -> str:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                values = [self._current_value(cursor, action) for action in candidate.actions]
            if all(value == action.before for value, action in zip(values, candidate.actions)):
                return "before"
            if all(value == action.after for value, action in zip(values, candidate.actions)):
                return "after"
            return "mixed"
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _lock_tables(cursor: Any, actions: Sequence[Action]) -> None:
        tables = sorted({action.device.table for action in actions})
        clause = ", ".join(f"{_identifier(table)} WRITE" for table in tables)
        cursor.execute(f"LOCK TABLES {clause}")

    @staticmethod
    def _update(cursor: Any, action: Action, before: int, after: int) -> None:
        cursor.execute(
            f"UPDATE {_identifier(action.device.table)} SET `Valid`=%s "
            "WHERE `ID`=%s AND `Valid`=%s",
            (after, action.device.record_id, before),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Conditional update failed for {action.device.name}: "
                f"expected Valid={before}"
            )

    def apply_candidate(self, candidate: Candidate) -> None:
        connection = self._connect()
        applied: list[Action] = []
        locked = False
        try:
            with connection.cursor() as cursor:
                self._lock_tables(cursor, candidate.actions)
                locked = True
                for action in candidate.actions:
                    current = self._current_value(cursor, action)
                    if current != action.before:
                        raise RuntimeError(
                            f"Refusing to apply {action.device.name}: "
                            f"Valid={current}, journal expected {action.before}"
                        )
                try:
                    for action in candidate.actions:
                        self._update(cursor, action, action.before, action.after)
                        applied.append(action)
                    for action in candidate.actions:
                        if self._current_value(cursor, action) != action.after:
                            raise RuntimeError(
                                f"Post-update check failed for {action.device.name}"
                            )
                    connection.commit()
                except BaseException:
                    for action in reversed(applied):
                        cursor.execute(
                            f"UPDATE {_identifier(action.device.table)} SET `Valid`=%s "
                            "WHERE `ID`=%s AND `Valid`=%s",
                            (action.before, action.device.record_id, action.after),
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

    def restore_candidate(self, candidate: Candidate) -> None:
        connection = self._connect()
        locked = False
        try:
            with connection.cursor() as cursor:
                self._lock_tables(cursor, candidate.actions)
                locked = True
                for action in candidate.actions:
                    current = self._current_value(cursor, action)
                    if current == action.before:
                        continue
                    if current != action.after:
                        raise RuntimeError(
                            f"Refusing rollback for {action.device.name}: Valid={current}"
                        )
                    self._update(cursor, action, action.after, action.before)
                for action in candidate.actions:
                    if self._current_value(cursor, action) != action.before:
                        raise RuntimeError(
                            f"Rollback check failed for {action.device.name}"
                        )
                connection.commit()
        finally:
            if locked:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("UNLOCK TABLES")
                except Exception:
                    pass
            connection.close()


class InMemoryRepository:
    """Deterministic repository used by offline tests."""

    def __init__(self, case_status: CaseStatus, devices: Sequence[Device]) -> None:
        self.case_status = case_status
        self.devices = {device.key: device for device in devices}

    def resolve_case(self, selection: CaseConfig) -> CaseStatus:
        identity = self.case_status.identity
        if selection.case_no is not None and selection.case_no != identity.case_no:
            raise RuntimeError("Configured case number was not found")
        if selection.name and selection.name != identity.case_name:
            raise RuntimeError("Configured case name was not found")
        return self.case_status

    def get_case_status(self, identity: CaseIdentity) -> CaseStatus:
        if identity != self.case_status.identity:
            raise RuntimeError("Expected case was not found")
        return self.case_status

    def set_case_status(self, **changes: Any) -> None:
        self.case_status = replace(self.case_status, **changes)

    def discover_devices(
        self, identity: CaseIdentity, policy: ActionPolicy
    ) -> list[Device]:
        self.get_case_status(identity)
        allowed = set()
        if policy.generator_status:
            allowed.add(DeviceType.GENERATOR)
        if policy.shunt_status:
            allowed.update((DeviceType.CAPACITOR, DeviceType.REACTOR))
        return [device for device in self.devices.values() if device.device_type in allowed]

    def candidate_state(self, candidate: Candidate) -> str:
        values = [self.devices[action.device.key].valid for action in candidate.actions]
        if all(value == action.before for value, action in zip(values, candidate.actions)):
            return "before"
        if all(value == action.after for value, action in zip(values, candidate.actions)):
            return "after"
        return "mixed"

    def apply_candidate(self, candidate: Candidate) -> None:
        if self.candidate_state(candidate) != "before":
            raise RuntimeError("Candidate is not in its recorded before-state")
        for action in candidate.actions:
            self.devices[action.device.key] = replace(
                self.devices[action.device.key], valid=action.after
            )

    def restore_candidate(self, candidate: Candidate) -> None:
        for action in candidate.actions:
            current = self.devices[action.device.key]
            if current.valid not in (action.before, action.after):
                raise RuntimeError("Device has drifted outside the journaled states")
            self.devices[action.device.key] = replace(current, valid=action.before)
