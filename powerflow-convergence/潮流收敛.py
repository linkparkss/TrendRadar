"""Use a minimum-switch search to make the active PSASP load flow converge."""

from __future__ import annotations

import csv
import itertools
import os
import json
import math
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


# Path configuration
PSASP_PATH = Path(os.environ.get("PSASP_PATH", "D:/PSASP/PSASP7"))
TEMP_PATH = Path(os.environ.get("PSASP_TEMP_PATH", "D:/PSASP/Temp"))
WMLFRT_EXE = PSASP_PATH / "wmlfadj.exe"
LF_L2 = TEMP_PATH / "LF.L2"
LF_LP1 = TEMP_PATH / "LF.LP1"
LFERR_LIS = TEMP_PATH / "LFERR.LIS"
LFCAL_LIS = TEMP_PATH / "LFCAL.LIS"

# Discover only shunt devices at the three stations implicated by the cases.
TARGET_STATION_PREFIXES = ("赣赣江", "赣进贤", "赣云峰")
MAX_SWITCHES = 2
PROCESS_TIMEOUT_SECONDS = 120
ACCEPTED_RETURN_CODES = (0, 1)


@dataclass(frozen=True)
class CandidateState:
    state: dict[str, int]
    changed_devices: tuple[str, ...]
    switch_count: int


@dataclass(frozen=True)
class AdjustableDevice:
    name: str
    bus_no: int
    kind: str
    x_value: float
    initial_mark: int


@dataclass
class SolverOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None
    convergence_status: bool | None = None


@dataclass
class AttemptRecord:
    attempt_number: int
    state: dict[str, int]
    changed_devices: tuple[str, ...]
    switch_count: int
    returncode: int | None
    converged: bool
    duration_seconds: float
    bus_count: int
    stdout: str
    stderr: str
    error: str | None


@dataclass
class SearchResult:
    success: bool
    original_state: dict[str, int]
    selected_state: dict[str, int] | None
    changed_devices: tuple[str, ...]
    attempts: list[AttemptRecord]
    failure_reason: str | None


SolverRunner = Callable[[], SolverOutcome]


def _normalized_device_names(device_names: Iterable[str]) -> tuple[str, ...]:
    names = tuple(str(name) for name in device_names)
    if not names:
        raise ValueError("设备列表不能为空")
    if len(set(names)) != len(names):
        raise ValueError("配置的设备名称重复")
    return names


def _validate_binary_state(
    state: Mapping[str, int],
    device_names: Sequence[str],
) -> dict[str, int]:
    missing = [name for name in device_names if name not in state]
    if missing:
        raise ValueError(f"设备状态缺少: {missing}")

    normalized = {}
    for name in device_names:
        try:
            mark = int(state[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"设备 {name} 的 Mark 不是整数") from exc
        if mark not in (0, 1):
            raise ValueError(f"设备 {name} 的 Mark 必须为 0 或 1，实际为 {mark}")
        normalized[name] = mark
    return normalized


def _state_respects_bus_compatibility(
    state: Mapping[str, int],
    device_specs: Sequence[AdjustableDevice],
) -> bool:
    active_kinds_by_bus: dict[int, set[str]] = {}
    for device in device_specs:
        if int(state[device.name]) != 1:
            continue
        kinds = active_kinds_by_bus.setdefault(device.bus_no, set())
        kinds.add(device.kind)
        if "capacitor" in kinds and "reactor" in kinds:
            return False
    return True


def generate_candidate_states(
    original_state: Mapping[str, int],
    ordered_devices: Iterable[str],
    max_switches: int | None = None,
    device_specs: Sequence[AdjustableDevice] | None = None,
) -> list[CandidateState]:
    devices = _normalized_device_names(ordered_devices)
    original = _validate_binary_state(original_state, devices)
    if max_switches is None:
        switch_limit = len(devices)
    else:
        switch_limit = int(max_switches)
        if switch_limit < 0:
            raise ValueError("max_switches 不能小于 0")
        switch_limit = min(switch_limit, len(devices))

    specs = tuple(device_specs or ())
    if specs:
        spec_names = tuple(device.name for device in specs)
        if len(set(spec_names)) != len(spec_names):
            raise ValueError("候选设备元数据名称重复")
        if set(spec_names) != set(devices):
            raise ValueError("候选设备元数据与设备状态名称不一致")

    candidates = []
    for switch_count in range(switch_limit + 1):
        for changed in itertools.combinations(devices, switch_count):
            state = dict(original)
            for name in changed:
                state[name] = 1 - state[name]
            if specs and not _state_respects_bus_compatibility(state, specs):
                continue
            candidates.append(
                CandidateState(
                    state=state,
                    changed_devices=tuple(changed),
                    switch_count=switch_count,
                )
            )

    return sorted(
        candidates,
        key=lambda item: (
            item.switch_count,
            tuple(item.state[name] for name in devices),
        ),
    )


def discover_adjustable_devices(
    l2_file: str | Path,
    station_prefixes: Iterable[str],
) -> list[AdjustableDevice]:
    path = Path(l2_file)
    prefixes = tuple(str(prefix) for prefix in station_prefixes)
    if not prefixes:
        raise ValueError("目标站名前缀不能为空")

    devices = []
    seen_names: set[str] = set()
    with path.open("r", encoding="gbk", errors="surrogateescape", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            parts = line.rstrip("\r\n").split(",")
            if len(parts) < 18:
                continue
            name = parts[17].strip().strip("'").strip('"')
            if not name.startswith(prefixes):
                continue
            if "#电容器" in name:
                kind = "capacitor"
            elif "#电抗器" in name:
                kind = "reactor"
            else:
                continue
            try:
                mark = int(parts[0].strip())
                i_no = int(parts[1].strip())
                j_no = int(parts[2].strip())
                x_value = float(parts[5].strip())
            except ValueError as exc:
                raise ValueError(f"LF.L2 第 {line_number} 行候选设备字段无效") from exc
            if i_no != j_no:
                continue
            if mark not in (0, 1):
                raise ValueError(f"LF.L2 第 {line_number} 行设备 {name} 的 Mark 非二进制")
            if not math.isfinite(x_value) or abs(x_value) < 1e-12:
                continue
            if name in seen_names:
                raise ValueError(f"LF.L2 中候选设备名称重复: {name}")
            seen_names.add(name)
            devices.append(AdjustableDevice(name, i_no, kind, x_value, mark))

    if not devices:
        raise ValueError(f"LF.L2 未发现目标站并联补偿设备: {prefixes}")
    return devices


def _record_name(parts: Sequence[str]) -> str:
    return parts[17].strip().strip("'").strip('"')


def read_device_states_from_l2(
    l2_file: str | Path,
    device_names: Iterable[str],
) -> dict[str, int]:
    path = Path(l2_file)
    names = _normalized_device_names(device_names)
    counts = {name: 0 for name in names}
    states: dict[str, int] = {}

    with path.open("r", encoding="gbk", errors="surrogateescape", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            parts = line.rstrip("\r\n").split(",")
            if len(parts) < 18:
                continue
            name = _record_name(parts)
            if name not in counts:
                continue

            counts[name] += 1
            try:
                mark = int(parts[0].strip())
            except ValueError as exc:
                raise ValueError(
                    f"LF.L2 第 {line_number} 行设备 {name} 的 Mark 不是整数"
                ) from exc
            if mark not in (0, 1):
                raise ValueError(
                    f"LF.L2 第 {line_number} 行设备 {name} 的 Mark 必须为 0 或 1"
                )
            states[name] = mark

    duplicates = [name for name, count in counts.items() if count > 1]
    if duplicates:
        raise ValueError(f"LF.L2 中设备记录重复: {duplicates}")
    missing = [name for name, count in counts.items() if count == 0]
    if missing:
        raise ValueError(f"设备未在 LF.L2 中找到: {missing}")
    return {name: states[name] for name in names}


def write_device_states_to_l2(
    l2_file: str | Path,
    device_state: Mapping[str, int],
) -> None:
    path = Path(l2_file)
    names = _normalized_device_names(device_state.keys())
    normalized = _validate_binary_state(device_state, names)

    # Validate occurrence counts before opening the file for writing.
    read_device_states_from_l2(path, names)
    with path.open("r", encoding="gbk", errors="surrogateescape", newline="") as handle:
        lines = handle.readlines()

    updated_lines = []
    for line in lines:
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        parts = body.split(",")
        if len(parts) >= 18:
            name = _record_name(parts)
            if name in normalized:
                parts[0] = str(normalized[name])
                line = ",".join(parts) + newline
        updated_lines.append(line)

    with path.open("w", encoding="gbk", errors="surrogateescape", newline="") as handle:
        handle.writelines(updated_lines)


def invalidate_result_file(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def read_valid_bus_voltages(lp1_file: str | Path) -> dict[int, float]:
    path = Path(lp1_file)
    if not path.is_file():
        raise ValueError("本次潮流未生成 LF.LP1")

    with path.open("r", encoding="gbk", errors="surrogateescape") as handle:
        lines = handle.readlines()
    if len(lines) < 3:
        raise ValueError("LF.LP1 文件内容不足")

    bus_voltages: dict[int, float] = {}
    for line in lines[2:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            bus_number = int(parts[0].strip())
            voltage = float(parts[1].strip())
        except ValueError:
            continue
        bus_voltages[bus_number] = voltage

    if not bus_voltages:
        raise ValueError("LF.LP1 未包含可解析母线电压")
    if not all(math.isfinite(value) for value in bus_voltages.values()):
        raise ValueError("LF.LP1 包含非有限电压值")
    return bus_voltages


def _decode_timeout_stream(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("gbk", errors="replace")
    return str(value)


def run_load_flow(
    exe_path: str | Path,
    workdir: str | Path,
    timeout_seconds: float,
) -> SolverOutcome:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(exe_path)],
            cwd=str(workdir),
            capture_output=True,
            encoding="gbk",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SolverOutcome(
            returncode=None,
            stdout=_decode_timeout_stream(exc.stdout),
            stderr=_decode_timeout_stream(exc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            error=f"潮流计算超时（{timeout_seconds} 秒）",
        )

    error = None
    if completed.returncode not in ACCEPTED_RETURN_CODES:
        error = (
            f"wmlfadj.exe 返回码 {completed.returncode}，"
            f"允许值为 {ACCEPTED_RETURN_CODES}"
        )
    return SolverOutcome(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=time.monotonic() - started,
        error=error,
    )


def read_lferr_excerpt(path: str | Path, max_lines: int = 20) -> str:
    error_path = Path(path)
    if not error_path.is_file():
        return ""
    try:
        with error_path.open("r", encoding="gbk", errors="replace") as handle:
            nonempty = [line.strip() for line in handle if line.strip()]
    except OSError as exc:
        return f"读取 LFERR.LIS 失败: {exc}"
    return "\n".join(nonempty[-max(1, int(max_lines)):])


def read_lfcal_status(path: str | Path) -> tuple[bool | None, str | None]:
    report_path = Path(path)
    if not report_path.is_file():
        return None, "LFCAL.LIS was not generated"

    text = report_path.read_text(encoding="gbk", errors="replace")
    nonconverged_marker = "\u6f6e\u6d41\u8ba1\u7b97\u4e0d\u6536\u655b"
    converged_marker = "\u6f6e\u6d41\u8ba1\u7b97\u6536\u655b"
    if re.search(nonconverged_marker, text):
        return False, "LFCAL.LIS reports non-converged load flow"
    if re.search(converged_marker, text):
        return True, None
    return None, None


def _combine_errors(primary: str | None, diagnostic: str | None) -> str | None:
    parts = [str(item).strip() for item in (primary, diagnostic) if item and str(item).strip()]
    return "\n".join(parts) if parts else None


def restore_and_verify(backup_path: str | Path, target_path: str | Path) -> None:
    backup = Path(backup_path)
    target = Path(target_path)
    shutil.copyfile(backup, target)
    if backup.read_bytes() != target.read_bytes():
        raise RuntimeError(f"恢复校验失败: {target}")


def search_convergent_state(
    l2_path: str | Path,
    lp1_path: str | Path,
    backup_path: str | Path,
    ordered_devices: Iterable[str],
    runner: SolverRunner,
    max_switches: int | None = None,
    device_specs: Sequence[AdjustableDevice] | None = None,
) -> SearchResult:
    l2 = Path(l2_path)
    lp1 = Path(lp1_path)
    backup = Path(backup_path)
    devices = _normalized_device_names(ordered_devices)
    original_state = read_device_states_from_l2(l2, devices)
    original_bytes = l2.read_bytes()
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original_bytes)
    attempts: list[AttemptRecord] = []

    try:
        candidates = generate_candidate_states(
            original_state,
            devices,
            max_switches=max_switches,
            device_specs=device_specs,
        )
        for attempt_number, candidate in enumerate(candidates, start=1):
            shutil.copyfile(backup, l2)
            if candidate.switch_count:
                write_device_states_to_l2(l2, candidate.state)
            invalidate_result_file(lp1)

            outcome = runner()
            converged = False
            bus_count = 0
            error = outcome.error
            if outcome.convergence_status is False:
                error = _combine_errors(
                    "LFCAL.LIS reports non-converged load flow",
                    outcome.error,
                )
            elif outcome.convergence_status is not True:
                error = _combine_errors(
                    "Ordinary load-flow convergence was not explicitly confirmed",
                    outcome.error,
                )
            elif outcome.returncode in ACCEPTED_RETURN_CODES and not outcome.timed_out:
                try:
                    bus_count = len(read_valid_bus_voltages(lp1))
                except ValueError as exc:
                    error = _combine_errors(str(exc), outcome.error)
                else:
                    converged = True
                    error = None
            elif error is None:
                error = f"wmlfadj.exe 返回码 {outcome.returncode}"

            attempts.append(
                AttemptRecord(
                    attempt_number=attempt_number,
                    state=dict(candidate.state),
                    changed_devices=candidate.changed_devices,
                    switch_count=candidate.switch_count,
                    returncode=outcome.returncode,
                    converged=converged,
                    duration_seconds=outcome.duration_seconds,
                    bus_count=bus_count,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    error=error,
                )
            )

            if converged:
                return SearchResult(
                    success=True,
                    original_state=dict(original_state),
                    selected_state=dict(candidate.state),
                    changed_devices=candidate.changed_devices,
                    attempts=attempts,
                    failure_reason=None,
                )

        restore_and_verify(backup, l2)
        return SearchResult(
            success=False,
            original_state=dict(original_state),
            selected_state=None,
            changed_devices=(),
            attempts=attempts,
            failure_reason=(
                f"{len(devices)} 个允许设备在最多 "
                f"{len(devices) if max_switches is None else min(int(max_switches), len(devices))} "
                f"次投切范围内的 {len(candidates)} 种候选均未使潮流收敛"
            ),
        )
    except BaseException:
        restore_and_verify(backup, l2)
        raise


def validate_configuration(
    executable: str | Path,
    workdir: str | Path,
    l2_path: str | Path,
    ordered_devices: Iterable[str],
) -> dict[str, int]:
    exe = Path(executable)
    working_directory = Path(workdir)
    l2 = Path(l2_path)
    devices = _normalized_device_names(ordered_devices)

    if not working_directory.is_dir():
        raise FileNotFoundError(f"PSASP 工作目录不存在: {working_directory}")
    if not exe.is_file():
        raise FileNotFoundError(f"wmlfadj.exe 不存在: {exe}")
    if not l2.is_file():
        raise FileNotFoundError(f"LF.L2 不存在: {l2}")
    return read_device_states_from_l2(l2, devices)


def create_run_output_dir(
    base_dir: str | Path,
    prefix: str = "AUTO_CONV",
) -> Path:
    runs_root = Path(base_dir) / "Convergence_runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    suffix = 1
    while True:
        name = stem if suffix == 1 else f"{stem}_{suffix}"
        candidate = runs_root / name
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        return candidate


def action_names_for_result(result: SearchResult) -> list[str]:
    if not result.success or result.selected_state is None:
        return []
    return [
        f"{name}_{'ON' if result.selected_state[name] else 'OFF'}"
        for name in result.changed_devices
    ]


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def write_reports(
    output_dir: str | Path,
    result: SearchResult,
    config: Mapping[str, object],
) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    attempts_csv = root / "attempts.csv"
    summary_json = root / "summary.json"
    run_config_txt = root / "run_config.txt"

    fieldnames = [
        "attempt_number",
        "switch_count",
        "changed_devices",
        "actions",
        "state",
        "returncode",
        "converged",
        "duration_seconds",
        "bus_count",
        "error",
        "stdout",
        "stderr",
    ]
    with attempts_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for attempt in result.attempts:
            actions = [
                f"{name}_{'ON' if attempt.state[name] else 'OFF'}"
                for name in attempt.changed_devices
            ]
            writer.writerow(
                {
                    "attempt_number": attempt.attempt_number,
                    "switch_count": attempt.switch_count,
                    "changed_devices": ";".join(attempt.changed_devices),
                    "actions": ";".join(actions),
                    "state": json.dumps(attempt.state, ensure_ascii=False),
                    "returncode": "" if attempt.returncode is None else attempt.returncode,
                    "converged": int(attempt.converged),
                    "duration_seconds": f"{attempt.duration_seconds:.6f}",
                    "bus_count": attempt.bus_count,
                    "error": attempt.error or "",
                    "stdout": attempt.stdout,
                    "stderr": attempt.stderr,
                }
            )

    summary = {
        "status": "converged" if result.success else "not_converged",
        "original_state": result.original_state,
        "selected_state": result.selected_state,
        "changed_devices": list(result.changed_devices),
        "actions": action_names_for_result(result),
        "attempt_count": len(result.attempts),
        "failure_reason": result.failure_reason,
        "outputs": {
            "attempts_csv": str(attempts_csv),
            "summary_json": str(summary_json),
            "run_config_txt": str(run_config_txt),
        },
    }
    with summary_json.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    config_lines = [
        f"{key} = {json.dumps(_json_safe(value), ensure_ascii=False)}"
        for key, value in config.items()
    ]
    run_config_txt.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    return {
        "attempts_csv": attempts_csv,
        "summary_json": summary_json,
        "run_config_txt": run_config_txt,
    }


def _write_unexpected_error(output_dir: Path, exc: Exception) -> None:
    payload = {
        "status": "error",
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    try:
        with (output_dir / "error.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _print_result(result: SearchResult, output_dir: Path) -> None:
    if result.success:
        actions = action_names_for_result(result)
        print("\n潮流已收敛。")
        if actions:
            print(f"推荐投切序列: {' -> '.join(actions)}")
        else:
            print("当前设备状态已经收敛，无需投切。")
        print(f"最终设备状态: {result.selected_state}")
    else:
        print(f"\n潮流未能恢复收敛: {result.failure_reason}")
        print("原始 LF.L2 已恢复。")
    print(f"搜索记录目录: {output_dir}")


def _legacy_main() -> int:
    output_dir: Path | None = None
    backup_path: Path | None = None

    try:
        discovered_devices = discover_adjustable_devices(LF_L2, TARGET_STATION_PREFIXES)
        ordered_devices = tuple(device.name for device in discovered_devices)
        original_state = validate_configuration(
            WMLFRT_EXE,
            TEMP_PATH,
            LF_L2,
            ordered_devices,
        )
        candidates = generate_candidate_states(
            original_state,
            ordered_devices,
            max_switches=MAX_SWITCHES,
            device_specs=discovered_devices,
        )
        config = {
            "executable": WMLFRT_EXE,
            "workdir": TEMP_PATH,
            "lf_l2": LF_L2,
            "lf_lp1": LF_LP1,
            "lfcal_lis": LFCAL_LIS,
            "timeout_seconds": PROCESS_TIMEOUT_SECONDS,
            "accepted_return_codes": ACCEPTED_RETURN_CODES,
            "target_station_prefixes": TARGET_STATION_PREFIXES,
            "max_switches": MAX_SWITCHES,
            "candidate_count": len(candidates),
            "ordered_devices": ordered_devices,
            "adjustable_devices": [
                {
                    "name": device.name,
                    "bus_no": device.bus_no,
                    "kind": device.kind,
                    "x_value": device.x_value,
                    "initial_mark": device.initial_mark,
                }
                for device in discovered_devices
            ],
        }

        output_dir = create_run_output_dir(TEMP_PATH)
        backup_path = output_dir / "LF.L2.original"
        print(f"已发现目标区域可投切并联补偿设备: {len(discovered_devices)} 个")
        print(f"当前投运: {sum(original_state.values())} 个，停运: {len(original_state) - sum(original_state.values())} 个")
        print(f"开始按最少投切顺序搜索 {len(candidates)} 种兼容候选（最多 {MAX_SWITCHES} 次投切）。")

        def solver_runner() -> SolverOutcome:
            invalidate_result_file(LFERR_LIS)
            invalidate_result_file(LFCAL_LIS)
            outcome = run_load_flow(
                WMLFRT_EXE,
                TEMP_PATH,
                PROCESS_TIMEOUT_SECONDS,
            )
            outcome.convergence_status, lfcal_error = read_lfcal_status(LFCAL_LIS)
            outcome.error = _combine_errors(outcome.error, lfcal_error)
            diagnostic = read_lferr_excerpt(LFERR_LIS)
            outcome.error = _combine_errors(outcome.error, diagnostic)
            return outcome

        result = search_convergent_state(
            LF_L2,
            LF_LP1,
            backup_path,
            ordered_devices,
            solver_runner,
            max_switches=MAX_SWITCHES,
            device_specs=discovered_devices,
        )
        write_reports(output_dir, result, config)
        _print_result(result, output_dir)
        return 0 if result.success else 2
    except Exception as exc:
        if backup_path is not None and backup_path.is_file() and LF_L2.parent.is_dir():
            try:
                restore_and_verify(backup_path, LF_L2)
            except Exception as restore_exc:
                print(f"恢复原始 LF.L2 失败: {restore_exc}", file=sys.stderr)
        if output_dir is not None:
            _write_unexpected_error(output_dir, exc)
        print(f"程序运行失败: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the legacy search or the configured automatic recovery workflow."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return _legacy_main()

    import importlib

    if arguments[0] == "baseline-bundle":
        bundle_entry = importlib.import_module(
            "powerflow.convergence.baseline_bundle"
        )
        return int(bundle_entry.main(arguments[1:]))
    if "--generated-plan" in arguments or "--generate-plan" in arguments:
        generated_entry = importlib.import_module(
            "powerflow.convergence.generated_training_v2"
        )
        return int(generated_entry.main(arguments))
    automatic_entry = importlib.import_module("潮流收敛_数据库同步版")
    return int(automatic_entry.main(arguments))


if __name__ == "__main__":
    sys.modules.setdefault(Path(__file__).stem, sys.modules[__name__])
    raise SystemExit(main())
