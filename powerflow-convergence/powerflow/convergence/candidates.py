"""Deterministic minimum-change candidate generation and ranking."""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence

from .config import ActionPolicy, SearchConfig
from .models import Action, Candidate, Device, DeviceType, DiagnosticReport


def station_key(value: str) -> str:
    value = value.strip()
    value = re.sub(r"(?:[_#-]?\d+)+.*$", "", value)
    return value or value.strip()


def common_prefix_length(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def device_relevance(device: Device, diagnosis: DiagnosticReport) -> float:
    target = f"{device.bus_name} {device.name}"
    score = 0.0
    for rank, bus in enumerate(diagnosis.implicated_buses):
        prefix = max(
            common_prefix_length(device.bus_name, bus),
            common_prefix_length(device.name, bus),
        )
        score = max(score, prefix * 10.0 + max(0.0, 20.0 - rank * 2.0))
        if bus and bus in target:
            score += 50.0
    if device.device_type is DeviceType.GENERATOR:
        score += 5.0
    score += min(abs(device.capacity), 1000.0) / 1000.0
    return score


def _action_for(device: Device, policy: ActionPolicy, diagnosis: DiagnosticReport) -> Action | None:
    if device.valid == 0 and policy.allow_start:
        after = 1
    elif device.valid == 1 and policy.allow_stop:
        after = 0
    else:
        return None
    score = device_relevance(device, diagnosis)
    reason = "ranked by proximity to mismatch buses and minimum device changes"
    return Action(device=device, before=device.valid, after=after, reason=reason, score=score)


def _compatible(actions: Sequence[Action]) -> bool:
    active_shunts: dict[str, set[DeviceType]] = {}
    for action in actions:
        if action.after != 1 or action.device.device_type not in {
            DeviceType.CAPACITOR,
            DeviceType.REACTOR,
        }:
            continue
        kinds = active_shunts.setdefault(action.device.bus_name, set())
        kinds.add(action.device.device_type)
        if len(kinds) > 1:
            return False
    return True


def generate_candidates(
    devices: Sequence[Device],
    diagnosis: DiagnosticReport,
    policy: ActionPolicy,
    search: SearchConfig,
) -> list[Candidate]:
    actions = [
        action
        for device in devices
        if (action := _action_for(device, policy, diagnosis)) is not None
    ]
    actions.sort(key=lambda item: (-item.score, item.device.key))
    pool = actions[: search.combination_pool_size]
    candidates: list[Candidate] = []

    for count in range(1, min(search.max_changed_devices, len(pool)) + 1):
        for combination in itertools.combinations(pool, count):
            if not _compatible(combination):
                continue
            same_station = len({station_key(item.device.name) for item in combination}) == 1
            pairing_bonus = 15.0 if count > 1 and same_station and search.same_station_pairing else 0.0
            score = sum(item.score for item in combination) + pairing_bonus - count * 100.0
            candidate_id = "C" + "_".join(item.device.key.replace(":", "-") for item in combination)
            explanation = (
                f"{count} status change(s); relevance={sum(item.score for item in combination):.2f}; "
                f"same_station={same_station}"
            )
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    actions=tuple(combination),
                    score=score,
                    explanation=explanation,
                )
            )

    candidates.sort(key=lambda item: (len(item.actions), -item.score, item.candidate_id))
    return candidates[: search.max_attempts]

