from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from .backtest.engine import run_backtest
from .backtest.parity import check_data_parity, check_event_parity, check_performance_parity, load_reference_fixture
from .backtest.sweep import run_grid
from .config import load_strategy_config, load_strategy_mapping
from .data.normalize import normalize_bars
from .data.providers.csv_provider import CsvMarketDataProvider
from .data.quality import compute_data_hash, summarize_import
from .data.sync import sync_soxl_history
from .domain.models import BacktestJob, ManualLedger, StrategyConfig, new_run_id
from .domain.money import ZERO
from .manual.ledger import create_ledger, export_ledger, import_ledger, load_ledger, record_fill, reverse_fill, save_ledger, summarize_ledger
from .manual.recommendation import build_recommendations
from .manual.reconciliation import reconcile_ledger
from .persistence.repositories import InMemoryJobRepository
from .persistence.worker import run_once
from .reporting.mentor_matrix import build_mentor_matrix, load_reference_fixture as load_mentor_matrix_reference
from .reporting.parameter_sweep import build_parameter_sweep
from .reporting.risk_report import build_risk_report
from .reporting.strategy_explorer import build_strategy_explorer
from .backtest.parity import ParityResult


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_market_data_csv() -> str:
    return str(_repo_root() / "data" / "raw" / "soxl_daily_2011_present.csv")


def default_manual_ledger_path() -> str:
    return str(_repo_root() / "data" / "runtime" / "dashboard" / "manual_ledger.json")


def bootstrap_check() -> int:
    required_paths = [
        "AGENTS.md",
        "docs/_index.md",
        "docs/70-policy/strategy.md",
        "docker-compose.yml",
        "scripts/verify_no_autotrading.py",
        "dashboard/src/server.ts",
    ]
    root = _repo_root()
    missing = [path for path in required_paths if not (root / path).exists()]
    if missing:
        for path in missing:
            print(f"MISSING: {path}")
        return 1
    print("Bootstrap check passed")
    return 0


def _load_bars(csv_path: str, symbol: str) -> tuple[list[object], str]:
    bars = normalize_bars(CsvMarketDataProvider(csv_path).load_bars(symbol))
    return bars, compute_data_hash(bars)


def _print_json(payload: object) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _parity_exit_code(*results: ParityResult) -> int:
    return 0 if all(result.status == "PASS" for result in results) else 1


def _add_csv_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--csv", required=required, default=default_market_data_csv())


def _add_ledger_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger-path", default=default_manual_ledger_path())


def _add_strategy_override_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_thread_count: bool = True,
    include_stop_sessions: bool = True,
) -> None:
    if include_thread_count:
        parser.add_argument("--thread-count", type=int)
    if include_stop_sessions:
        parser.add_argument("--stop-sessions", type=int)
    parser.add_argument("--take-profit-pct")
    parser.add_argument("--take-profit-operator", choices=["gt", "gte"])
    parser.add_argument("--entry-drop-pct")
    parser.add_argument("--stop-loss-pct")
    parser.add_argument("--max-entries-per-session", type=int)
    parser.add_argument("--sizing-mode")
    parser.add_argument("--price-basis")


def _strategy_overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    mapping = {
        "thread_count": getattr(args, "thread_count", None),
        "stop_sessions": getattr(args, "stop_sessions", None),
        "take_profit_pct": getattr(args, "take_profit_pct", None),
        "take_profit_operator": getattr(args, "take_profit_operator", None),
        "entry_drop_pct": getattr(args, "entry_drop_pct", None),
        "stop_loss_pct": getattr(args, "stop_loss_pct", None),
        "max_entries_per_session": getattr(args, "max_entries_per_session", None),
        "sizing_mode": getattr(args, "sizing_mode", None),
        "price_basis": getattr(args, "price_basis", None),
    }
    return {key: value for key, value in mapping.items() if value is not None}


def _load_strategy_config_with_overrides(args: argparse.Namespace) -> StrategyConfig:
    payload = load_strategy_mapping(args.profile, initial_capital=args.initial_capital)
    payload.update(_strategy_overrides_from_args(args))
    return StrategyConfig.from_mapping(payload)


def _serialize_config(config: StrategyConfig) -> dict[str, object]:
    return {
        "profile_id": config.profile_id,
        "symbol": config.symbol,
        "thread_count": config.thread_count,
        "stop_sessions": config.stop_sessions,
        "max_entries_per_session": config.max_entries_per_session,
        "take_profit_pct": str(config.take_profit_pct),
        "take_profit_operator": config.take_profit_operator,
        "entry_drop_pct": str(config.entry_drop_pct),
        "stop_loss_pct": str(config.stop_loss_pct),
        "price_basis": config.price_basis.value,
        "execution_model": config.execution_model.value,
        "sizing_mode": config.sizing_mode.value,
        "year_boundary": config.year_boundary.value,
        "end_of_test": config.end_of_test.value,
        "config_hash": config.config_hash(),
        "initial_capital": str(config.initial_capital),
    }


def _serialize_run(run: object, config: StrategyConfig) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "profile_id": config.profile_id,
        "code_commit": run.code_commit,
        "data_hash": run.data_hash,
        "config_hash": config.config_hash(),
        "config": _serialize_config(config),
        "metrics": {key: str(value) for key, value in run.metrics.items()},
        "yearly": {
            str(year): {metric: str(value) for metric, value in payload.items()}
            for year, payload in run.yearly.items()
        },
        "daily": [
            {
                "session_date": snapshot.session_date.isoformat(),
                "session_index": snapshot.session_index,
                "total_equity": str(snapshot.total_equity),
                "realized_pnl": str(snapshot.realized_pnl),
                "drawdown": str(snapshot.drawdown),
                "open_threads": snapshot.open_threads,
                "entries": snapshot.entries,
                "take_profits": snapshot.take_profits,
                "time_stops": snapshot.time_stops,
                "skipped_entries": snapshot.skipped_entries,
            }
            for snapshot in run.daily
        ],
        "trades": [
            {
                "thread_id": trade.thread_id,
                "signal_date": trade.signal_date.isoformat(),
                "fill_entry_date": trade.fill_entry_date.isoformat(),
                "entry_price": str(trade.entry_price),
                "shares": str(trade.shares),
                "invested_amount": str(trade.invested_amount),
                "exit_signal_date": trade.exit_signal_date.isoformat() if trade.exit_signal_date else None,
                "fill_exit_date": trade.fill_exit_date.isoformat() if trade.fill_exit_date else None,
                "exit_price": str(trade.exit_price) if trade.exit_price is not None else None,
                "holding_sessions": trade.holding_sessions,
                "pnl": str(trade.pnl),
                "return_pct": str(trade.return_pct),
                "close_reason": trade.close_reason.value if trade.close_reason else None,
            }
            for trade in run.trades
        ],
    }


def _serialize_ledger(ledger: ManualLedger) -> dict[str, object]:
    return {
        "summary": summarize_ledger(ledger),
        "issues": reconcile_ledger(ledger),
        "threads": [
            {
                "thread_id": thread.thread_id,
                "cash": str(thread.cash),
                "quantity": str(thread.quantity),
                "entry_price": str(thread.entry_price),
                "entry_date": thread.entry_date.isoformat() if thread.entry_date else None,
            }
            for thread in ledger.threads.values()
        ],
        "fills": [
            {
                "fill_id": fill.fill_id,
                "thread_id": fill.thread_id,
                "side": fill.side,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "filled_at": fill.filled_at.isoformat(),
                "reversed_by_fill_id": fill.reversed_by_fill_id,
            }
            for fill in ledger.fills
        ],
    }


def _open_positions_from_ledger(ledger: ManualLedger, bars: list[object]) -> dict[int, tuple[object, int]]:
    positions: dict[int, tuple[object, int]] = {}
    if not bars:
        return positions
    latest_index = len(bars) - 1
    session_index_by_date = {bar.session_date: index for index, bar in enumerate(bars)}
    for thread_id, thread in ledger.threads.items():
        if thread.quantity <= ZERO or thread.entry_date is None:
            continue
        entry_index = session_index_by_date.get(thread.entry_date)
        if entry_index is None:
            entry_index = next(
                (index for index, bar in enumerate(bars) if bar.session_date >= thread.entry_date),
                None,
            )
        if entry_index is None:
            continue
        positions[thread_id] = (thread.entry_price, max(0, latest_index - entry_index))
    return positions


def _data_validate(args: argparse.Namespace) -> int:
    bars, data_hash = _load_bars(args.csv, args.symbol)
    report = summarize_import(args.symbol, "csv", bars)
    return _print_json(
        {
            "symbol": args.symbol,
            "rows": report.rows,
            "data_hash": data_hash,
            "warnings": report.warnings,
        }
    )


def _data_status(args: argparse.Namespace) -> int:
    bars, data_hash = _load_bars(args.csv, args.symbol)
    report = summarize_import(args.symbol, "csv", bars)
    return _print_json(
        {
            "symbol": args.symbol,
            "rows": len(bars),
            "start": bars[0].session_date.isoformat(),
            "end": bars[-1].session_date.isoformat(),
            "data_hash": data_hash,
            "source": bars[-1].source,
            "warnings": report.warnings,
            "snapshot_path": str(Path(args.csv).resolve()),
        }
    )


def _data_sync(args: argparse.Namespace) -> int:
    result = sync_soxl_history(
        args.output_csv,
        symbol=args.symbol,
        start_date=args.start_date,
    )
    return _print_json(result)


def _backtest_run(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    run = run_backtest(bars, config, data_hash=data_hash)
    payload = _serialize_run(run, config)
    payload.pop("daily")
    payload.pop("trades")
    return _print_json(payload)


def _backtest_detail(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    run = run_backtest(bars, config, data_hash=data_hash)
    return _print_json(_serialize_run(run, config))


def _backtest_grid(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    thread_counts = [int(item) for item in args.threads.split(",")]
    stop_sessions = [int(item) for item in args.stops.split(",")]
    runs = run_grid(bars, config, thread_counts, stop_sessions, data_hash=data_hash)
    return _print_json(
        [
            {
                "profile_id": run.config.profile_id,
                "thread_count": run.config.thread_count,
                "stop_sessions": run.config.stop_sessions,
                "config_hash": run.config.config_hash(),
                "data_hash": run.data_hash,
                "total_return_pct": str(run.metrics["total_return_pct"]),
                "max_drawdown_pct": str(run.metrics["max_drawdown_pct"]),
                "volatility_pct": str(run.metrics["volatility_pct"]),
                "trade_count": run.metrics["trade_count"],
            }
            for run in runs
        ]
    )


def _backtest_risk_report(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    return _print_json(build_risk_report(bars, config, data_hash=data_hash))


def _parse_windows(raw: str | None) -> dict[str, tuple[int, int]]:
    if not raw:
        return {
            "total": (2011, 2024),
            "y5": (2020, 2024),
            "y3": (2022, 2024),
            "y1": (2024, 2024),
        }
    windows: dict[str, tuple[int, int]] = {}
    for item in raw.split(","):
        label, span = item.split("=", 1)
        start_year, end_year = span.split(":", 1)
        windows[label] = (int(start_year), int(end_year))
    return windows


def _parse_combo_csv(raw: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in raw.split(",") if item.strip())


def _backtest_strategy_explorer(args: argparse.Namespace) -> int:
    config = load_strategy_config(args.profile, initial_capital=args.initial_capital)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    return _print_json(
        build_strategy_explorer(
            bars,
            config,
            data_hash=data_hash,
            catalog_id=args.catalog_id,
            execution_model=args.execution_model,
            price_basis=args.price_basis,
        )
    )


def _backtest_parameter_sweep(args: argparse.Namespace) -> int:
    config = load_strategy_config(args.profile, initial_capital=args.initial_capital)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    return _print_json(
        build_parameter_sweep(
            bars,
            config,
            data_hash=data_hash,
            sweep_id=args.sweep_id,
            execution_model=args.execution_model,
            price_basis=args.price_basis,
        )
    )


def _backtest_mentor_matrix(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    bars, data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    combos = tuple(
        (thread_count, stop_sessions)
        for thread_count in _parse_combo_csv(args.threads)
        for stop_sessions in _parse_combo_csv(args.stops)
    )
    payload = build_mentor_matrix(
        bars,
        config,
        data_hash=data_hash,
        reference=load_mentor_matrix_reference(args.reference) if args.reference else None,
        combos=combos,
        windows=_parse_windows(args.windows),
    )
    return _print_json(payload)


def _parity_report(args: argparse.Namespace) -> int:
    reference = load_reference_fixture(args.reference)
    bars, data_hash = _load_bars(args.csv, args.symbol)
    config = load_strategy_config(args.profile, initial_capital=args.initial_capital)
    run = run_backtest(bars, config, data_hash=data_hash)
    data_result = check_data_parity(bars, reference)
    profile_key = args.profile_key or f"{config.thread_count}x{config.stop_sessions}"
    event_result = check_event_parity(run, reference, profile_key)
    performance_result = check_performance_parity(run, reference, profile_key)
    payload = {
        "run_id": run.run_id,
        "data_hash": data_hash,
        "profile_key": profile_key,
        "data_parity": {
            "status": data_result.status,
            "details": data_result.details,
            "first_mismatch": data_result.first_mismatch,
        },
        "event_parity": {
            "status": event_result.status,
            "details": event_result.details,
            "first_mismatch": event_result.first_mismatch,
        },
        "performance_parity": {
            "status": performance_result.status,
            "details": performance_result.details,
            "first_mismatch": performance_result.first_mismatch,
        },
    }
    _print_json(payload)
    return _parity_exit_code(data_result, event_result, performance_result)


def _manual_today(args: argparse.Namespace) -> int:
    config = load_strategy_config(args.profile, initial_capital=args.initial_capital)
    bars, _data_hash = _load_bars(args.csv, args.symbol or config.symbol)
    open_positions = {}
    ledger_path = Path(args.ledger_path)
    if ledger_path.exists():
        open_positions = _open_positions_from_ledger(load_ledger(ledger_path), bars)
    recommendations = build_recommendations(bars, config, open_positions)
    return _print_json(
        [
            {
                "thread_id": recommendation.thread_id,
                "action": recommendation.action.value,
                "reason": recommendation.reason,
                "basis_price": str(recommendation.basis_price),
                "session_date": recommendation.session_date.isoformat(),
            }
            for recommendation in recommendations
        ]
    )


def _profile_show(args: argparse.Namespace) -> int:
    config = _load_strategy_config_with_overrides(args)
    return _print_json(_serialize_config(config))


def _manual_ledger_init(args: argparse.Namespace) -> int:
    ledger = create_ledger(args.account_id, args.thread_count, args.initial_capital)
    save_ledger(args.ledger_path, ledger)
    return _print_json(_serialize_ledger(ledger))


def _manual_ledger_show(args: argparse.Namespace) -> int:
    ledger = load_ledger(args.ledger_path)
    return _print_json(_serialize_ledger(ledger))


def _manual_ledger_fill(args: argparse.Namespace) -> int:
    ledger = load_ledger(args.ledger_path)
    fill = record_fill(
        ledger,
        thread_id=args.thread_id,
        side=args.side,
        quantity=args.quantity,
        price=args.price,
        fee=args.fee,
        filled_at=datetime.fromisoformat(args.filled_at) if args.filled_at else None,
    )
    save_ledger(args.ledger_path, ledger)
    return _print_json(
        {
            "fill": {
                "fill_id": fill.fill_id,
                "thread_id": fill.thread_id,
                "side": fill.side,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "filled_at": fill.filled_at.isoformat(),
                "reversed_by_fill_id": fill.reversed_by_fill_id,
            },
            "ledger": _serialize_ledger(ledger),
        }
    )


def _manual_ledger_reverse(args: argparse.Namespace) -> int:
    ledger = load_ledger(args.ledger_path)
    reversal = reverse_fill(ledger, args.fill_id)
    save_ledger(args.ledger_path, ledger)
    return _print_json(
        {
            "fill": {
                "fill_id": reversal.fill_id,
                "thread_id": reversal.thread_id,
                "side": reversal.side,
                "quantity": str(reversal.quantity),
                "price": str(reversal.price),
                "fee": str(reversal.fee),
                "filled_at": reversal.filled_at.isoformat(),
                "reversed_by_fill_id": reversal.reversed_by_fill_id,
            },
            "ledger": _serialize_ledger(ledger),
        }
    )


def _manual_ledger_restore(args: argparse.Namespace) -> int:
    ledger = import_ledger(Path(args.source_path).read_text(encoding="utf-8"))
    save_ledger(args.ledger_path, ledger)
    return _print_json(
        {
            "ledger": _serialize_ledger(ledger),
            "backup": json.loads(export_ledger(ledger)),
        }
    )


def _worker_smoke(_args: argparse.Namespace) -> int:
    repo = InMemoryJobRepository()
    job = BacktestJob(job_id="smoke-job", config_hash="cfg", data_hash="data")
    repo.add(job)
    run_id = run_once(repo, "smoke-worker", lambda _job_id: new_run_id())
    return _print_json({"job_id": job.job_id, "status": repo.jobs[job.job_id].status, "run_id": run_id})


def main() -> int:
    parser = argparse.ArgumentParser(prog="soxl-mania")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap-check")
    bootstrap_parser.set_defaults(handler=lambda _args: bootstrap_check())

    data_parser = subparsers.add_parser("data")
    data_subparsers = data_parser.add_subparsers(dest="data_command", required=True)
    data_validate = data_subparsers.add_parser("validate")
    _add_csv_argument(data_validate)
    data_validate.add_argument("--symbol", default="SOXL")
    data_validate.set_defaults(handler=_data_validate)
    data_sync = data_subparsers.add_parser("sync")
    data_sync.add_argument("--output-csv", default=default_market_data_csv())
    data_sync.add_argument("--symbol", default="SOXL")
    data_sync.add_argument("--start-date", default="2011-01-01")
    data_sync.set_defaults(handler=_data_sync)
    data_status = data_subparsers.add_parser("status")
    _add_csv_argument(data_status)
    data_status.add_argument("--symbol", default="SOXL")
    data_status.set_defaults(handler=_data_status)

    backtest_parser = subparsers.add_parser("backtest")
    backtest_subparsers = backtest_parser.add_subparsers(dest="backtest_command", required=True)
    backtest_run_parser = backtest_subparsers.add_parser("run")
    backtest_run_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_run_parser)
    backtest_run_parser.add_argument("--symbol")
    backtest_run_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(backtest_run_parser)
    backtest_run_parser.set_defaults(handler=_backtest_run)
    backtest_detail_parser = backtest_subparsers.add_parser("detail")
    backtest_detail_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_detail_parser)
    backtest_detail_parser.add_argument("--symbol")
    backtest_detail_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(backtest_detail_parser)
    backtest_detail_parser.set_defaults(handler=_backtest_detail)
    backtest_grid_parser = backtest_subparsers.add_parser("grid")
    backtest_grid_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_grid_parser)
    backtest_grid_parser.add_argument("--symbol")
    backtest_grid_parser.add_argument("--threads", required=True)
    backtest_grid_parser.add_argument("--stops", required=True)
    backtest_grid_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(backtest_grid_parser, include_thread_count=False, include_stop_sessions=False)
    backtest_grid_parser.set_defaults(handler=_backtest_grid)
    backtest_risk_parser = backtest_subparsers.add_parser("risk-report")
    backtest_risk_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_risk_parser)
    backtest_risk_parser.add_argument("--symbol")
    backtest_risk_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(backtest_risk_parser)
    backtest_risk_parser.set_defaults(handler=_backtest_risk_report)
    backtest_strategy_explorer_parser = backtest_subparsers.add_parser("strategy-explorer")
    backtest_strategy_explorer_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_strategy_explorer_parser)
    backtest_strategy_explorer_parser.add_argument("--symbol")
    backtest_strategy_explorer_parser.add_argument("--initial-capital", type=float, default=10000.0)
    backtest_strategy_explorer_parser.add_argument("--catalog-id", default="core_profiles_v1")
    backtest_strategy_explorer_parser.add_argument(
        "--execution-model",
        default="next_open",
        choices=["ideal_same_close", "next_open", "next_close"],
    )
    backtest_strategy_explorer_parser.add_argument(
        "--price-basis",
        default="adjusted_close",
        choices=["adjusted_close", "raw_close_with_actions"],
    )
    backtest_strategy_explorer_parser.set_defaults(handler=_backtest_strategy_explorer)
    backtest_parameter_sweep_parser = backtest_subparsers.add_parser("parameter-sweep")
    backtest_parameter_sweep_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_parameter_sweep_parser)
    backtest_parameter_sweep_parser.add_argument("--symbol")
    backtest_parameter_sweep_parser.add_argument("--initial-capital", type=float, default=10000.0)
    backtest_parameter_sweep_parser.add_argument("--sweep-id", default="core6_v1")
    backtest_parameter_sweep_parser.add_argument(
        "--execution-model",
        default="next_open",
        choices=["ideal_same_close", "next_open", "next_close"],
    )
    backtest_parameter_sweep_parser.add_argument(
        "--price-basis",
        default="adjusted_close",
        choices=["adjusted_close", "raw_close_with_actions"],
    )
    backtest_parameter_sweep_parser.set_defaults(handler=_backtest_parameter_sweep)
    backtest_mentor_matrix_parser = backtest_subparsers.add_parser("mentor-matrix")
    backtest_mentor_matrix_parser.add_argument("--profile", required=True)
    _add_csv_argument(backtest_mentor_matrix_parser)
    backtest_mentor_matrix_parser.add_argument("--symbol")
    backtest_mentor_matrix_parser.add_argument("--threads", default="5,6,7")
    backtest_mentor_matrix_parser.add_argument("--stops", default="10,30,40")
    backtest_mentor_matrix_parser.add_argument("--windows")
    backtest_mentor_matrix_parser.add_argument("--reference")
    backtest_mentor_matrix_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(backtest_mentor_matrix_parser, include_thread_count=False, include_stop_sessions=False)
    backtest_mentor_matrix_parser.set_defaults(handler=_backtest_mentor_matrix)

    parity_parser = subparsers.add_parser("parity")
    parity_subparsers = parity_parser.add_subparsers(dest="parity_command", required=True)
    parity_report_parser = parity_subparsers.add_parser("report")
    parity_report_parser.add_argument("--reference", required=True)
    parity_report_parser.add_argument("--profile", required=True)
    parity_report_parser.add_argument("--csv", required=True)
    parity_report_parser.add_argument("--symbol", default="SOXL")
    parity_report_parser.add_argument("--profile-key")
    parity_report_parser.add_argument("--initial-capital", type=float, default=10000.0)
    parity_report_parser.set_defaults(handler=_parity_report)

    profile_parser = subparsers.add_parser("profile")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_show_parser = profile_subparsers.add_parser("show")
    profile_show_parser.add_argument("--profile", required=True)
    profile_show_parser.add_argument("--initial-capital", type=float, default=10000.0)
    _add_strategy_override_arguments(profile_show_parser)
    profile_show_parser.set_defaults(handler=_profile_show)

    manual_parser = subparsers.add_parser("manual")
    manual_subparsers = manual_parser.add_subparsers(dest="manual_command", required=True)
    manual_today = manual_subparsers.add_parser("today")
    manual_today.add_argument("--profile", required=True)
    _add_csv_argument(manual_today)
    _add_ledger_argument(manual_today)
    manual_today.add_argument("--symbol")
    manual_today.add_argument("--initial-capital", type=float, default=10000.0)
    manual_today.set_defaults(handler=_manual_today)
    manual_ledger = manual_subparsers.add_parser("ledger")
    manual_ledger_subparsers = manual_ledger.add_subparsers(dest="manual_ledger_command", required=True)
    manual_ledger_init = manual_ledger_subparsers.add_parser("init")
    _add_ledger_argument(manual_ledger_init)
    manual_ledger_init.add_argument("--account-id", default="soxl-mania")
    manual_ledger_init.add_argument("--thread-count", type=int, required=True)
    manual_ledger_init.add_argument("--initial-capital", type=float, default=10000.0)
    manual_ledger_init.set_defaults(handler=_manual_ledger_init)
    manual_ledger_show = manual_ledger_subparsers.add_parser("show")
    _add_ledger_argument(manual_ledger_show)
    manual_ledger_show.set_defaults(handler=_manual_ledger_show)
    manual_ledger_fill = manual_ledger_subparsers.add_parser("fill")
    _add_ledger_argument(manual_ledger_fill)
    manual_ledger_fill.add_argument("--thread-id", type=int, required=True)
    manual_ledger_fill.add_argument("--side", required=True)
    manual_ledger_fill.add_argument("--quantity", required=True)
    manual_ledger_fill.add_argument("--price", required=True)
    manual_ledger_fill.add_argument("--fee", default="0")
    manual_ledger_fill.add_argument("--filled-at")
    manual_ledger_fill.set_defaults(handler=_manual_ledger_fill)
    manual_ledger_reverse = manual_ledger_subparsers.add_parser("reverse")
    _add_ledger_argument(manual_ledger_reverse)
    manual_ledger_reverse.add_argument("--fill-id", required=True)
    manual_ledger_reverse.set_defaults(handler=_manual_ledger_reverse)
    manual_ledger_restore = manual_ledger_subparsers.add_parser("restore")
    _add_ledger_argument(manual_ledger_restore)
    manual_ledger_restore.add_argument("--source-path", required=True)
    manual_ledger_restore.set_defaults(handler=_manual_ledger_restore)

    worker_parser = subparsers.add_parser("worker")
    worker_subparsers = worker_parser.add_subparsers(dest="worker_command", required=True)
    worker_smoke_parser = worker_subparsers.add_parser("smoke")
    worker_smoke_parser.set_defaults(handler=_worker_smoke)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
