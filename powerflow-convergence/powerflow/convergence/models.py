"""Domain models shared by the convergence-search components."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DeviceType(str, Enum):
    GENERATOR = "generator"
    CAPACITOR = "capacitor"
    REACTOR = "reactor"
    OTHER = "other"


class RunStatus(str, Enum):
    READY = "ready"
    APPLYING = "applying"
    WAITING_FOR_MANUAL_RUN = "waiting_for_manual_run"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    ROLLED_BACK = "rolled_back"
    ERROR = "error"


@dataclass(frozen=True)
class CaseIdentity:
    case_no: int
    case_name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CaseStatus:
    identity: CaseIdentity
    calculate: int
    tolerance: float
    iteration_limit: int
    calculation_date: str = ""
    calculation_time: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["identity"] = self.identity.to_dict()
        return result


@dataclass(frozen=True)
class Device:
    table: str
    record_id: int
    name: str
    device_type: DeviceType
    bus_name: str
    valid: int
    capacity: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.table}:{self.record_id}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["device_type"] = self.device_type.value
        result["key"] = self.key
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Device":
        data = dict(payload)
        data.pop("key", None)
        data["device_type"] = DeviceType(data["device_type"])
        return cls(**data)


@dataclass(frozen=True)
class Action:
    device: Device
    before: int
    after: int
    field: str = "Valid"
    reason: str = ""
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.field != "Valid":
            raise ValueError("The initial implementation only permits Valid changes")
        if self.before not in (0, 1) or self.after not in (0, 1):
            raise ValueError("Action states must be binary")
        if self.before == self.after:
            raise ValueError("Action must change device state")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device.to_dict(),
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Action":
        return cls(
            device=Device.from_dict(payload["device"]),
            field=payload.get("field", "Valid"),
            before=int(payload["before"]),
            after=int(payload["after"]),
            reason=str(payload.get("reason", "")),
            score=float(payload.get("score", 0.0)),
        )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    actions: tuple[Action, ...]
    score: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "actions": [action.to_dict() for action in self.actions],
            "score": self.score,
            "explanation": self.explanation,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Candidate":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            actions=tuple(Action.from_dict(item) for item in payload["actions"]),
            score=float(payload["score"]),
            explanation=str(payload.get("explanation", "")),
        )


@dataclass(frozen=True)
class FileFingerprint:
    exists: bool
    mtime_ns: int = 0
    size: int = 0
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FileFingerprint":
        return cls(**payload)


@dataclass(frozen=True)
class ResultBaseline:
    captured_at: str
    files: dict[str, FileFingerprint]

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "files": {name: value.to_dict() for name, value in self.files.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResultBaseline":
        return cls(
            captured_at=str(payload["captured_at"]),
            files={
                name: FileFingerprint.from_dict(value)
                for name, value in payload["files"].items()
            },
        )


@dataclass(frozen=True)
class VerificationResult:
    converged: bool
    reasons: tuple[str, ...]
    lf_cal_first_line: str = ""
    result_date: str = ""
    result_time: str = ""
    final_bus: str = ""
    final_mismatch: float | None = None
    iteration_records: int = 0
    files_fresh: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


@dataclass(frozen=True)
class DiagnosticReport:
    nonconverged: bool
    implicated_buses: tuple[str, ...]
    final_bus: str
    final_mismatch: float | None
    max_mismatch: float | None
    phase_count: int
    reactive_limit_restarts: int

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["implicated_buses"] = list(self.implicated_buses)
        return result

