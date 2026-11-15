"""SQL for paper accounts, positions, the balance ledger and equity snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from goldscalp.core.clock import iso, parse_iso_aware
from goldscalp.simulation.account import AccountConfig
from goldscalp.simulation.costs import CostModel
from goldscalp.simulation.decimal_utils import dstr
from goldscalp.simulation.instrument import InstrumentSpec
from goldscalp.simulation.positions import Position, Side


def upsert_spec(con: sqlite3.Connection, spec: InstrumentSpec, now: datetime) -> int:
    fp = spec.fingerprint
    row = con.execute("SELECT id FROM instrument_specs WHERE fingerprint = ?", (fp,)).fetchone()
    if row:
        return int(row["id"])
    cur = con.execute("INSERT INTO instrument_specs (fingerprint, spec_json, created_at) VALUES (?, ?, ?)",
                      (fp, json.dumps(spec.to_mapping(), sort_keys=True), iso(now)))
    return int(cur.lastrowid)


def insert_account(con: sqlite3.Connection, config: AccountConfig, spec_id: int, costs: CostModel,
                   now: datetime, *, purpose: str = "manual") -> int:
    """A ``manual`` account becomes THE active account (the previous one is kept, inactive). A ``validation``
    account (Phase 4: one per monitored strategy version) is addressed by id and never becomes active."""
    if purpose not in ("manual", "validation"):
        raise ValueError("purpose must be manual or validation")
    if purpose == "manual":
        con.execute("UPDATE paper_accounts SET active = 0 WHERE active = 1 AND purpose = 'manual'")
    cur = con.execute(
        "INSERT INTO paper_accounts (name, currency, starting_balance, leverage, instrument_spec_id, "
        "cost_model_json, max_quote_age_seconds, allow_delayed_fills, active, created_at, purpose) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (config.name, config.currency, dstr(config.starting_balance), config.leverage, spec_id,
         json.dumps(costs.to_mapping(), sort_keys=True), config.max_quote_age_seconds,
         int(config.allow_delayed_fills), 1 if purpose == "manual" else 0, iso(now), purpose))
    account_id = int(cur.lastrowid)
    append_ledger(con, account_id, now, "deposit", config.starting_balance, None,
                  "virtual starting balance (simulated)")
    return account_id


def active_account_row(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT a.*, s.spec_json FROM paper_accounts a JOIN instrument_specs s "
                       "ON s.id = a.instrument_spec_id WHERE a.active = 1 AND a.purpose = 'manual' "
                       "ORDER BY a.id DESC LIMIT 1").fetchone()


def account_row(con: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT a.*, s.spec_json FROM paper_accounts a JOIN instrument_specs s "
                       "ON s.id = a.instrument_spec_id WHERE a.id = ?", (account_id,)).fetchone()


def list_accounts(con: sqlite3.Connection, purpose: str | None = "manual") -> list[dict[str, Any]]:
    sql = "SELECT id, name, starting_balance, leverage, active, created_at, purpose FROM paper_accounts"
    args: list[Any] = []
    if purpose is not None:
        sql += " WHERE purpose = ?"
        args.append(purpose)
    rows = con.execute(sql + " ORDER BY id DESC", args).fetchall()
    return [dict(r) for r in rows]


def validation_accounts_with_open_positions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT a.*, s.spec_json FROM paper_accounts a JOIN instrument_specs s "
                       "ON s.id = a.instrument_spec_id WHERE a.purpose = 'validation' AND EXISTS (SELECT 1 FROM "
                       "paper_positions p WHERE p.account_id = a.id AND p.status = 'open') ORDER BY a.id").fetchall()


def balance(con: sqlite3.Connection, account_id: int) -> Decimal:
    row = con.execute("SELECT balance_after FROM account_ledger WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                      (account_id,)).fetchone()
    return Decimal(row["balance_after"]) if row else Decimal(0)


def append_ledger(con: sqlite3.Connection, account_id: int, ts: datetime, kind: str, amount: Decimal,
                  position_id: int | None, note: str) -> Decimal:
    new_balance = balance(con, account_id) + amount
    con.execute("INSERT INTO account_ledger (account_id, ts, kind, amount, balance_after, position_id, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (account_id, iso(ts), kind, dstr(amount), dstr(new_balance), position_id, note))
    return new_balance


def ledger(con: sqlite3.Connection, account_id: int, limit: int) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM account_ledger WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                       (account_id, limit)).fetchall()
    return [dict(r) for r in rows]


def insert_position(con: sqlite3.Connection, p: Position) -> int:
    cur = con.execute(
        "INSERT INTO paper_positions (account_id, side, lots, ounces, open_price, open_time, open_quote_time, "
        "open_price_state, open_spread, open_spread_source, open_slippage, open_commission, margin, stop_loss, "
        "take_profit, status, open_source, signal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        (p.account_id, p.side.value, dstr(p.lots), dstr(p.ounces), dstr(p.open_price), iso(p.open_time),
         iso(p.open_quote_time), p.open_price_state, dstr(p.open_spread), p.open_spread_source,
         dstr(p.open_slippage), dstr(p.open_commission), dstr(p.margin), dstr(p.stop_loss),
         dstr(p.take_profit), p.open_source, p.signal_id))
    return int(cur.lastrowid)


def close_position_row(con: sqlite3.Connection, p: Position) -> None:
    con.execute(
        "UPDATE paper_positions SET status = 'closed', close_price = ?, close_time = ?, close_quote_time = ?, "
        "close_price_state = ?, close_commission = ?, close_reason = ?, realized_gross = ?, realized_net = ?, close_source = ? "
        "WHERE id = ? AND status = 'open'",
        (dstr(p.close_price), iso(p.close_time), iso(p.close_quote_time), p.close_price_state,
         dstr(p.close_commission), p.close_reason, dstr(p.realized_gross), dstr(p.realized_net), p.close_source,
         p.id))


def update_protection(con: sqlite3.Connection, position_id: int, sl: Decimal | None, tp: Decimal | None) -> None:
    con.execute("UPDATE paper_positions SET stop_loss = ?, take_profit = ? WHERE id = ? AND status = 'open'",
                (dstr(sl), dstr(tp), position_id))


def positions(con: sqlite3.Connection, account_id: int, status: str | None = None,
              limit: int = 500) -> list[Position]:
    if status is None:
        rows = con.execute("SELECT * FROM paper_positions WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                           (account_id, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM paper_positions WHERE account_id = ? AND status = ? "
                           "ORDER BY id DESC LIMIT ?", (account_id, status, limit)).fetchall()
    return [_position(r) for r in rows]


def position(con: sqlite3.Connection, position_id: int) -> Position | None:
    row = con.execute("SELECT * FROM paper_positions WHERE id = ?", (position_id,)).fetchone()
    return _position(row) if row else None


def insert_snapshot(con: sqlite3.Connection, account_id: int, ts: datetime, values: dict[str, Any]) -> None:
    con.execute("INSERT OR REPLACE INTO equity_snapshots (account_id, ts, balance, equity, used_margin, "
                "free_margin, open_positions, price_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, iso(ts), values["balance"], values["equity"], values["used_margin"],
                 values["free_margin"], values["open_positions"], values["price_state"]))


def snapshots(con: sqlite3.Connection, account_id: int, limit: int) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM (SELECT * FROM equity_snapshots WHERE account_id = ? ORDER BY ts DESC "
                       "LIMIT ?) ORDER BY ts ASC", (account_id, limit)).fetchall()
    return [dict(r) for r in rows]


def last_snapshot_time(con: sqlite3.Connection, account_id: int) -> datetime | None:
    row = con.execute("SELECT MAX(ts) AS ts FROM equity_snapshots WHERE account_id = ?", (account_id,)).fetchone()
    return parse_iso_aware(row["ts"]) if row and row["ts"] else None


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _time(value: str | None) -> datetime | None:
    return None if value is None else parse_iso_aware(value)


def _position(r: sqlite3.Row) -> Position:
    return Position(
        id=r["id"], account_id=r["account_id"], side=Side(r["side"]), lots=Decimal(r["lots"]),
        ounces=Decimal(r["ounces"]), open_price=Decimal(r["open_price"]), open_time=parse_iso_aware(r["open_time"]),
        open_quote_time=parse_iso_aware(r["open_quote_time"]), open_price_state=r["open_price_state"],
        open_spread=Decimal(r["open_spread"]), open_spread_source=r["open_spread_source"],
        open_slippage=Decimal(r["open_slippage"]), open_commission=Decimal(r["open_commission"]),
        margin=Decimal(r["margin"]), stop_loss=_dec(r["stop_loss"]), take_profit=_dec(r["take_profit"]),
        status=r["status"], close_price=_dec(r["close_price"]), close_time=_time(r["close_time"]),
        close_quote_time=_time(r["close_quote_time"]), close_price_state=r["close_price_state"],
        close_commission=_dec(r["close_commission"]), close_reason=r["close_reason"],
        realized_gross=_dec(r["realized_gross"]), realized_net=_dec(r["realized_net"]),
        open_source=r["open_source"], close_source=r["close_source"], signal_id=r["signal_id"])
