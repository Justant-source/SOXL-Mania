from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import json
from typing import Any

from ..domain.models import BacktestRun, MarketBar
from ..domain.money import D


@dataclass(frozen=True)
class ParityResult:
    status: str
    details: list[str]
    first_mismatch: dict[str, Any] | None = None


def load_reference_fixture(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _boundary_mismatch(
    *,
    year: int,
    boundary: str,
    expected: Decimal,
    actual: Decimal,
    session_date: str,
    tolerance: Decimal,
) -> tuple[str, dict[str, str]] | None:
    delta = actual - expected
    if abs(delta) <= tolerance:
        return None
    mismatch = {
        "year": str(year),
        "boundary": boundary,
        "session_date": session_date,
        "expected": str(expected),
        "actual": str(actual),
        "delta": str(delta),
        "tolerance": str(tolerance),
    }
    detail = (
        f"{year} {boundary} {session_date}: expected {expected} got {actual} "
        f"(delta {delta}, tolerance {tolerance})"
    )
    return detail, mismatch


def check_data_parity(bars: list[MarketBar], reference: dict) -> ParityResult:
    failures: list[str] = []
    first_mismatch: dict[str, Any] | None = None
    grouped: dict[int, list[MarketBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.session_date.year, []).append(bar)
    tolerance = D("0.01")
    for row in reference["annual_soxl_boundaries"]:
        year = int(row["year"])
        year_bars = grouped.get(year)
        if year_bars is None:
            detail = f"Missing bars for {year}"
            failures.append(detail)
            if first_mismatch is None:
                first_mismatch = {"year": str(year), "boundary": "missing", "detail": detail}
            continue
        checks = (
            ("start", D(str(row["start"])), year_bars[0].adj_close, year_bars[0].session_date.isoformat()),
            ("end", D(str(row["end"])), year_bars[-1].adj_close, year_bars[-1].session_date.isoformat()),
        )
        for boundary, expected_value, actual_value, session_date in checks:
            mismatch = _boundary_mismatch(
                year=year,
                boundary=boundary,
                expected=expected_value,
                actual=actual_value,
                session_date=session_date,
                tolerance=tolerance,
            )
            if mismatch is None:
                continue
            detail, payload = mismatch
            failures.append(detail)
            if first_mismatch is None:
                first_mismatch = payload
    if failures:
        return ParityResult("DATA_MISMATCH", failures, first_mismatch=first_mismatch)
    return ParityResult("PASS", [f"Annual adjusted-close boundaries match within tolerance {tolerance}"])


def check_event_parity(run: BacktestRun, reference: dict, profile_key: str) -> ParityResult:
    expected = reference["event_counts"].get(profile_key)
    if expected is None:
        return ParityResult("NOT_APPLICABLE", [f"No event fixture for {profile_key}"])
    failures: list[str] = []
    first_mismatch: dict[str, Any] | None = None
    for year, payload in expected.items():
        actual = run.yearly.get(int(year), {})
        expected_tp = payload["take_profit"]
        expected_ts = payload["time_stop"]
        if actual.get("take_profit_count") != expected_tp or actual.get("time_stop_count") != expected_ts:
            actual_tp = actual.get("take_profit_count")
            actual_ts = actual.get("time_stop_count")
            failures.append(f"{year}: expected {expected_tp}/{expected_ts} got {actual_tp}/{actual_ts}")
            if first_mismatch is None:
                first_mismatch = {
                    "year": str(year),
                    "expected_take_profit": str(expected_tp),
                    "expected_time_stop": str(expected_ts),
                    "actual_take_profit": str(actual_tp),
                    "actual_time_stop": str(actual_ts),
                }
    if failures:
        return ParityResult("FAIL", failures, first_mismatch=first_mismatch)
    return ParityResult("PASS", [f"Event counts match {profile_key}"])


def check_performance_parity(run: BacktestRun, reference: dict, profile_key: str) -> ParityResult:
    expected = reference["annual_returns"].get(profile_key)
    if expected is None:
        return ParityResult("NOT_APPLICABLE", [f"No performance fixture for {profile_key}"])
    failures: list[str] = []
    first_mismatch: dict[str, Any] | None = None
    for year, expected_return in expected.items():
        actual = run.yearly.get(int(year), {}).get("return_pct")
        if actual is None:
            failures.append(f"{year}: missing actual yearly return")
            if first_mismatch is None:
                first_mismatch = {
                    "year": str(year),
                    "expected_return_pct": str(expected_return),
                    "actual_return_pct": "missing",
                    "tolerance": "0.15",
                }
            continue
        if abs(float(actual) - float(expected_return)) > 0.15:
            failures.append(f"{year}: expected {expected_return} got {actual}")
            if first_mismatch is None:
                first_mismatch = {
                    "year": str(year),
                    "expected_return_pct": str(expected_return),
                    "actual_return_pct": str(actual),
                    "delta_pct": str(float(actual) - float(expected_return)),
                    "tolerance": "0.15",
                }
    if failures:
        return ParityResult("FAIL", failures, first_mismatch=first_mismatch)
    return ParityResult("PASS", [f"Yearly returns within tolerance for {profile_key}"])
