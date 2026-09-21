"""SQLite 连接管理与建库。

容器启动时调用 init_db() 建库建表；应用通过 FastAPI 依赖拿到连接。
为支持事务回滚，连接使用显式 BEGIN/COMMIT/ROLLBACK（autocommit 隔离级别）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id      TEXT PRIMARY KEY,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 乐观并发版本：创建为 0，每被一批成功提交的转移触碰一次 +1
    version INTEGER NOT NULL DEFAULT 0
);

-- 容器中来自每个祖先初始样品的“绝对体积份额”。
-- 初始样品登记后占 1 行（source_id = 自身）；空容器为 0 行。
-- Decimal 以字符串存储，杜绝浮点误差。
CREATE TABLE IF NOT EXISTS components (
    container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    amount       TEXT NOT NULL,
    PRIMARY KEY (container_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_components_source ON components(source_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # 显式事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """创建数据目录与全部表（幂等，容器启动时执行）。"""
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        # executescript 自带事务语义；各语句均为 IF NOT EXISTS，幂等
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容已有数据库：老库的 containers 表没有 version 列，补列（已有行默认 0）。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(containers)")}
    if "version" not in columns:
        conn.execute(
            "ALTER TABLE containers ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
        )


@contextmanager
def get_conn_context() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求依赖。"""
    with get_conn_context() as conn:
        yield conn
