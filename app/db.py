"""SQLite 存取层：台账（入库 / 转仓 / 交货与余箱）持久化。

并发约定
--------
* 连接使用 autocommit（``isolation_level=None``），事务一律通过
  :func:`write_transaction` / :func:`read_transaction` 显式开启。
* 写事务用 ``BEGIN IMMEDIATE``：入账与建组/撤组互斥，后拿到写锁的一方
  会看到先提交一方的结果，从而在事务内完成重名检查，不会出现检查与写入
  之间的空档。
* 数据库启用 WAL：下载核对包时持有读事务的同一快照，同时仍允许入账 /
  建组 / 撤组提交，包内箱数与分组永远来自同一个已提交状态。
"""
import sqlite3
import time
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_hash    TEXT NOT NULL UNIQUE,        -- 三个 CSV 内容的整体哈希，防止同一批重复入账
    uploaded_at   TEXT NOT NULL,
    inbound_count INTEGER NOT NULL,
    transfer_count INTEGER NOT NULL,
    delivery_count INTEGER NOT NULL
);

-- 每个仓次唯一归属一个果园（校验阶段保证）
CREATE TABLE IF NOT EXISTS sessions (
    name    TEXT PRIMARY KEY,
    orchard TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    session  TEXT NOT NULL,
    orchard  TEXT NOT NULL,
    crates   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    from_session TEXT NOT NULL,
    to_session   TEXT NOT NULL,
    crates   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    session  TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('交货', '余箱')),
    destination TEXT NOT NULL,
    crates   INTEGER NOT NULL
);

-- 分组按果园隔离；组名在同一果园内唯一
CREATE TABLE IF NOT EXISTS dest_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    orchard    TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (orchard, name)
);

-- 一个去向在同一果园最多属于一个组；不同果园互不影响
CREATE TABLE IF NOT EXISTS dest_group_members (
    group_id    INTEGER NOT NULL REFERENCES dest_groups(id) ON DELETE CASCADE,
    orchard     TEXT NOT NULL,
    destination TEXT NOT NULL,
    PRIMARY KEY (orchard, destination),
    UNIQUE (group_id, destination)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_session ON deliveries(session);
CREATE INDEX IF NOT EXISTS idx_sessions_orchard ON sessions(orchard);
CREATE INDEX IF NOT EXISTS idx_groups_orchard ON dest_groups(orchard);
"""


def connect(path):
    """建立一个显式事务模式的连接（调用方负责 BEGIN/COMMIT）。"""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["LEDGER_DB"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def write_transaction(conn):
    """写事务：立即取写锁，校验与写入在同一事务内完成。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _rollback_quietly(conn)
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            _rollback_quietly(conn)
            raise


@contextmanager
def read_transaction(conn):
    """读事务：首次读取后固定快照，期间其他连接仍可提交（WAL）。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        _rollback_quietly(conn)


def _rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        # 事务可能已被 SQLite 自动回滚，忽略
        pass


def init_db():
    conn = get_db()
    # 多个 gunicorn worker 可能同时启动：DDL / WAL 切换在写锁下串行执行，
    # 遇到短暂的 SQLITE_BUSY 就重试，避免启动期竞态。
    for _ in range(20):
        try:
            conn.executescript(SCHEMA)
            # WAL 让读事务（下载核对包）与写事务（入账 / 建组 / 撤组）不互相阻塞
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            return
        except sqlite3.OperationalError as exc:
            conn.close()
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            g.pop("db", None)
            time.sleep(0.1)
            conn = get_db()
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


def init_app(app):
    import os

    db_path = app.config["LEDGER_DB"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
