"""SQLite 连接与建表。

编号按来源区分：灯杆号、单号都用 (来源台账, 台账内编号) 作为自然主键，
避免不同台账之间编号撞车。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(
    os.environ.get("POLE_RECON_DB")
    or Path(__file__).resolve().parent.parent / "data" / "recon.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('asset','ticket','confirm')),
    filename    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 资产号清单：资产号全局唯一
CREATE TABLE IF NOT EXISTS asset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_no    TEXT NOT NULL UNIQUE,
    pole_source TEXT NOT NULL,
    pole_no     TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 故障单：单号按来源台账区分 (source + ticket_no 唯一)
CREATE TABLE IF NOT EXISTS ticket (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    ticket_no    TEXT NOT NULL,
    pole_source  TEXT NOT NULL,
    pole_no      TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(source, ticket_no)
);

-- 同杆确认：可撤销。同一对灯杆只保留一条活动确认，撤销后可再次确认。
CREATE TABLE IF NOT EXISTS confirmation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_a     TEXT NOT NULL,
    pole_no_a    TEXT NOT NULL,
    source_b     TEXT NOT NULL,
    pole_no_b    TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_confirmation_active ON confirmation(active);

-- 已核对（已入仓）单子的归属，锁定不动
CREATE TABLE IF NOT EXISTS verified (
    ticket_id   INTEGER PRIMARY KEY REFERENCES ticket(id),
    asset_id    INTEGER NOT NULL REFERENCES asset(id),
    group_key   TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 选定的撤销方案（撤销一张同杆确认）
CREATE TABLE IF NOT EXISTS undo_plan (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key       TEXT NOT NULL,
    confirmation_id INTEGER NOT NULL REFERENCES confirmation(id),
    edge_label      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 整组入仓清单
CREATE TABLE IF NOT EXISTS warehouse (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key    TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS warehouse_item (
    warehouse_id INTEGER NOT NULL REFERENCES warehouse(id),
    ticket_id    INTEGER NOT NULL REFERENCES ticket(id),
    asset_id     INTEGER NOT NULL REFERENCES asset(id),
    PRIMARY KEY (warehouse_id, ticket_id)
);
"""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
