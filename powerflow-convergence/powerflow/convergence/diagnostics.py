"""Parse PSASP iterative reports into search-oriented diagnostics."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .models import DiagnosticReport


ITERATION_RE = re.compile(r"^\s*(\d+)\s*,\s*([^,]+),\s*([-+0-9.Ee]+)\s*$")


def read_gbk(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="gbk", errors="replace")


def parse_iteration_records(text: str) -> list[tuple[int, str, float]]:
    records: list[tuple[int, str, float]] = []
    for line in text.splitlines():
        match = ITERATION_RE.match(line)
        if not match:
            continue
        records.append((int(match.group(1)), match.group(2).strip(), float(match.group(3))))
    return records


def diagnose_load_flow(
    lfcal_path: str | Path,
    lfreport_path: str | Path | None = None,
    max_buses: int = 8,
) -> DiagnosticReport:
    lfcal_text = read_gbk(Path(lfcal_path))
    report_text = read_gbk(Path(lfreport_path)) if lfreport_path else ""
    records = parse_iteration_records(lfcal_text)
    counts = Counter(bus for _, bus, _ in records)

    phase_count = 0
    previous_iteration: int | None = None
    for iteration, _, _ in records:
        if previous_iteration is None or iteration <= previous_iteration:
            phase_count += 1
        previous_iteration = iteration

    ranked_buses = [name for name, _ in counts.most_common(max_buses)]
    if records and records[-1][1] not in ranked_buses:
        ranked_buses.insert(0, records[-1][1])
        ranked_buses = ranked_buses[:max_buses]

    nonconverged = "\u6f6e\u6d41\u8ba1\u7b97\u4e0d\u6536\u655b" in lfcal_text
    final_bus = records[-1][1] if records else ""
    final_mismatch = records[-1][2] if records else None
    max_mismatch = max((value for _, _, value in records), default=None)
    return DiagnosticReport(
        nonconverged=nonconverged,
        implicated_buses=tuple(ranked_buses),
        final_bus=final_bus,
        final_mismatch=final_mismatch,
        max_mismatch=max_mismatch,
        phase_count=phase_count,
        reactive_limit_restarts=report_text.count("\u5904\u7406\u65e0\u529f\u8d8a\u9650"),
    )

