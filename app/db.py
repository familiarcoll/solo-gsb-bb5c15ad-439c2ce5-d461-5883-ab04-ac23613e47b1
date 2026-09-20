"""SQLite 连接与建表。

数据库路径通过环境变量 ``DB_PATH`` 配置，默认当前目录下的
``teleprompter.db``。``init_db()`` 在服务启动时调用，幂等建表。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "teleprompter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    advance_threshold      INTEGER NOT NULL CHECK (advance_threshold >= 1),
    offline_timeout_seconds INTEGER NOT NULL CHECK (offline_timeout_seconds >= 1),
    public_position        INTEGER NOT NULL DEFAULT -1,
    revision               INTEGER NOT NULL DEFAULT 0,
    paused                 INTEGER NOT NULL DEFAULT 0,  -- 排练是否暂停
    clock_version          INTEGER NOT NULL DEFAULT 0,  -- 暂停/恢复每切换一次递增
    paused_at              REAL,                        -- 本次暂停开始的 epoch 秒
    progress_epoch         INTEGER NOT NULL DEFAULT 0,  -- 进度代际：导演跳转/重导台本/回环递增
    hold_version           INTEGER NOT NULL DEFAULT 0,  -- 止点版本：设置/放行/越点/重导递增
    last_block             TEXT,                        -- JSON：最近一次被止点阻挡的信息
    loop_start             INTEGER,                     -- 循环排练区间起点（NULL=未启用）
    loop_end               INTEGER,                     -- 循环排练区间终点（NULL=未启用）
    loop_version           INTEGER NOT NULL DEFAULT 0,  -- 循环配置版本：设置/清除/重导/修订递增
    loop_count             INTEGER NOT NULL DEFAULT 0,  -- 已回环次数（整体导入清零）
    created_at             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lines (
    room_id  TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    text     TEXT NOT NULL,
    PRIMARY KEY (room_id, position)
);

CREATE TABLE IF NOT EXISTS participants (
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    participant_id TEXT NOT NULL,
    last_seq       INTEGER NOT NULL,
    position       INTEGER NOT NULL,
    last_seen_at   REAL NOT NULL,
    PRIMARY KEY (room_id, participant_id)
);

CREATE TABLE IF NOT EXISTS advancements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    contributors TEXT NOT NULL,  -- JSON 数组：促成本次推进的参与者
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_advancements_room ON advancements(room_id, id);

CREATE TABLE IF NOT EXISTS holds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,  -- 止点所在台词位置（随增量修订按台词身份重排）
    released    INTEGER NOT NULL DEFAULT 0,  -- 0=未释放（生效中），1=已放行（仅留档）
    created_at  REAL NOT NULL,
    released_at REAL
);

-- 同一房间同一位置至多存在一个未释放止点；已放行止点留档不参与唯一约束
CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_pending
    ON holds(room_id, position) WHERE released = 0;
CREATE INDEX IF NOT EXISTS idx_holds_room ON holds(room_id, released, position);

CREATE TABLE IF NOT EXISTS handoffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    progress_epoch  INTEGER NOT NULL,  -- 交接发生的进度代际（退场限制仅在该代际内生效）
    from_participant_id TEXT NOT NULL, -- 原演员：本代际被标记退场
    to_participant_id   TEXT NOT NULL, -- 替补：继承位置与在线时间，事件序号从头计算
    position        INTEGER NOT NULL,  -- 交接时转给替补的原演员当前位置
    last_seen_at    REAL NOT NULL,     -- 交接时转给替补的原演员在线时间
    created_at      REAL NOT NULL
);

-- 同一代际内同一原演员至多交接一次（幂等重试的判定依据；不同目标 → 409）
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_room_epoch_from
    ON handoffs(room_id, progress_epoch, from_participant_id);
-- 同一代际内同一替补至多接手一次（替补“在本代际尚未参与”判定）
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoffs_room_epoch_to
    ON handoffs(room_id, progress_epoch, to_participant_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_room ON handoffs(room_id, id);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的旧库幂等补齐后加的列。

    - rooms.revision：台本版本号
    - rooms.paused / clock_version / paused_at：排练暂停/恢复时钟
    - rooms.progress_epoch：进度代际（导演校准公共位置 / 重导台本 / 回环时递增）
    - rooms.hold_version / last_block：台词止点版本与最近一次阻挡信息
    - rooms.loop_start / loop_end / loop_version / loop_count：循环排练区间
      （起止行、配置版本与已回环次数）
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(rooms)")}
    if "revision" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
    if "paused" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
        )
    if "clock_version" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN clock_version INTEGER NOT NULL DEFAULT 0"
        )
    if "paused_at" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN paused_at REAL"
        )
    if "progress_epoch" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN progress_epoch INTEGER NOT NULL DEFAULT 0"
        )
    if "hold_version" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN hold_version INTEGER NOT NULL DEFAULT 0"
        )
    if "last_block" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN last_block TEXT"
        )
    if "loop_start" not in columns:
        conn.execute("ALTER TABLE rooms ADD COLUMN loop_start INTEGER")
    if "loop_end" not in columns:
        conn.execute("ALTER TABLE rooms ADD COLUMN loop_end INTEGER")
    if "loop_version" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN loop_version INTEGER NOT NULL DEFAULT 0"
        )
    if "loop_count" not in columns:
        conn.execute(
            "ALTER TABLE rooms ADD COLUMN loop_count INTEGER NOT NULL DEFAULT 0"
        )
