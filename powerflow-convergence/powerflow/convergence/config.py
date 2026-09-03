"""TOML configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .adaptive_load import AdaptiveLoadConfig



@dataclass(frozen=True)
class ProjectConfig:
    psasp_path: Path
    temp_path: Path
    run_root: Path


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 3309
    user: str = "root"
    database: str = ""
    password_env: str = "PSASP_DB_PASSWORD"
    charset: str = "latin1"

    def password(self) -> str:
        value = os.environ.get(self.password_env)
        if value is None:
            raise RuntimeError(
                f"Database password environment variable is missing: {self.password_env}"
            )
        return value


@dataclass(frozen=True)
class CaseConfig:
    name: str = ""
    case_no: int | None = None


@dataclass(frozen=True)
class ActionPolicy:
    generator_status: bool = True
    shunt_status: bool = True
    allow_start: bool = True
    allow_stop: bool = False
    generator_voltage: bool = False
    transformer_tap: bool = False
    load_shedding: bool = False


@dataclass(frozen=True)
class SearchConfig:
    max_changed_devices: int = 2
    max_attempts: int = 100
    combination_pool_size: int = 20
    prefer_local_devices: bool = True
    same_station_pairing: bool = True


@dataclass(frozen=True)
class VerificationConfig:
    require_fresh_lf_cal: bool = True
    require_fresh_lfcal: bool = True
    require_case_status: bool = True
    require_fresh_lp1: bool = False
    timestamp_slack_seconds: float = 3.0


@dataclass(frozen=True)
class ExecutorConfig:
    mode: str = "manual_psasp"
    reload_instruction: str = (
        "Close PSASP without saving stale in-memory data, reopen the project, "
        "select the target job, and run ordinary load flow."
    )


@dataclass(frozen=True)
class AppConfig:
    project: ProjectConfig
    database: DatabaseConfig
    case: CaseConfig
    actions: ActionPolicy = field(default_factory=ActionPolicy)
    search: SearchConfig = field(default_factory=SearchConfig)
    adaptive_load: AdaptiveLoadConfig = field(default_factory=AdaptiveLoadConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)

    def validate(self) -> None:
        if not self.database.database:
            raise ValueError("database.database must not be empty")
        if self.case.case_no is None and not self.case.name:
            raise ValueError("case.case_no or case.name must be configured")
        if self.search.max_changed_devices < 1:
            raise ValueError("search.max_changed_devices must be at least 1")
        if self.search.max_attempts < 1:
            raise ValueError("search.max_attempts must be at least 1")
        if self.search.combination_pool_size < 1:
            raise ValueError("search.combination_pool_size must be at least 1")
        if self.executor.mode != "manual_psasp":
            raise ValueError("Only executor.mode='manual_psasp' is currently supported")
        forbidden = {
            "generator_voltage": self.actions.generator_voltage,
            "transformer_tap": self.actions.transformer_tap,
            "load_shedding": self.actions.load_shedding,
        }
        enabled_forbidden = [name for name, enabled in forbidden.items() if enabled]
        if enabled_forbidden:
            raise ValueError(
                "Unsupported high-impact actions are enabled: " + ", ".join(enabled_forbidden)
            )
        if not (self.actions.generator_status or self.actions.shunt_status):
            raise ValueError("At least one status action type must be enabled")

    def safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("psasp_path", "temp_path", "run_root"):
            result["project"][key] = str(result["project"][key])
        return result


def _section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"TOML section [{name}] must be a table")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)

    project_data = _section(payload, "project")
    temp_path = Path(project_data["temp_path"])
    project = ProjectConfig(
        psasp_path=Path(project_data["psasp_path"]),
        temp_path=temp_path,
        run_root=Path(project_data.get("run_root", temp_path / "Convergence_runs")),
    )
    config = AppConfig(
        project=project,
        database=DatabaseConfig(**_section(payload, "database")),
        case=CaseConfig(**_section(payload, "case")),
        actions=ActionPolicy(**_section(payload, "actions")),
        search=SearchConfig(**_section(payload, "search")),
        adaptive_load=AdaptiveLoadConfig(**_section(payload, "adaptive_load")),
        verification=VerificationConfig(**_section(payload, "verification")),
        executor=ExecutorConfig(**_section(payload, "executor")),
    )
    config.validate()
    return config

