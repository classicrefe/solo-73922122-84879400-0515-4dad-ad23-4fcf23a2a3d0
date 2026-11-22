"""SQLite 连接与建表。每次操作新开连接，配合 WAL + busy_timeout 支撑并发。"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS basemap (
    id         INTEGER PRIMARY KEY CHECK (id = 1),  -- 单幅底图，恒为 1
    rows       INTEGER NOT NULL,
    cols       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiles (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL,   -- 现行版本号，每次换版 +1
    data    TEXT NOT NULL,      -- 17×17 整数高程，JSON
    PRIMARY KEY (row, col)
);
CREATE TABLE IF NOT EXISTS staged (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时也是"最新"的判定依据
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL          -- change_seq：全局变更序号，每次成功发版 +1
);
CREATE TABLE IF NOT EXISTS qc_marks (
    mark_id    TEXT PRIMARY KEY,    -- 取数标记（随机 id，持久化，重启后仍可用）
    row0       INTEGER NOT NULL,    -- 取数窗口（整图坐标，含端点）
    col0       INTEGER NOT NULL,
    row1       INTEGER NOT NULL,
    col1       INTEGER NOT NULL,
    fetch_seq  INTEGER NOT NULL,    -- 取数时刻的全局变更序号
    created_at TEXT NOT NULL
    -- 注意：只存窗口范围与序号，不另存窗口高程副本
);
CREATE TABLE IF NOT EXISTS qc_isolations (
    row        INTEGER NOT NULL,    -- 坏点全图坐标；共边上的同一格点只占一行
    col        INTEGER NOT NULL,
    mark_id    TEXT NOT NULL,       -- 由哪次提交隔离（审计用）
    created_at TEXT NOT NULL,
    PRIMARY KEY (row, col)          -- 重复标记同一读数只隔离一次
);
CREATE TABLE IF NOT EXISTS point_changes (
    row        INTEGER NOT NULL,    -- 格点全图坐标
    col        INTEGER NOT NULL,
    change_seq INTEGER NOT NULL,    -- 最近一次"实际改变该点高程"的发版序号
    PRIMARY KEY (row, col)
);
"""

BUSY_TIMEOUT_MS = 30000


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 进入自动提交模式，事务由 SQL 显式控制。"""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def init_db(db_path: str) -> None:
    """建目录、开 WAL、建表。WAL 让读写不互斥，写事务之间仍严格串行。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
