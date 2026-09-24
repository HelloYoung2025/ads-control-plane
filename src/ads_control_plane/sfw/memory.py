"""操盘手的记忆：~/.amazon-ads/operator.sqlite3。它记得交给它的商品、每一次设置变化、
每一轮看到了什么、判了什么，以及每个关键词和投放第一次见到时的出价。

用 SQLite（2026-09-24 定稿计划第二节第 3 条）：一台 Mac、一个人、单写者。旧登记簿里要
PostgreSQL 的两条（DEC-107/117，同日删）是给已经不存在的多租户平台定的。

连接纪律：
- WAL + synchronous=FULL + fullfsync：macOS 上普通 fsync 不保证落盘，断电会丢最后几笔。
- 写一律 `BEGIN IMMEDIATE`：两个 SFW 对话各拉一个插件进程，同时写时后到的排队，不会读到一半。
- 两本账只追加（goal_events、decisions），触发器挡住 UPDATE 和 DELETE：记忆被改写，
  「它之前为什么这么判」就再也问不出来。
- schema 版本放在 user_version。库比代码新（装回了旧版本），拒绝打开，不猜着往下写——
  版本要在任何会改文件的 PRAGMA 之前查。
- 文件只有自己能读（0600）：里面有店铺 Profile ID 和每个词的出价。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 1

#: 等另一个进程放开记忆库的最长时间。
BUSY_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    pending TEXT
);
INSERT OR IGNORE INTO control (id) VALUES (1);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY,
    store TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    asin TEXT NOT NULL,
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    target_acos TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'retired')),
    failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (profile_id, asin)
);
CREATE UNIQUE INDEX IF NOT EXISTS goals_one_name ON goals (name) WHERE status != 'retired';

CREATE TABLE IF NOT EXISTS goal_events (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals (id),
    at TEXT NOT NULL,
    what TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals (id),
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'tick')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'OK', 'FAILED', 'ABANDONED')),
    light TEXT CHECK (light IN ('green', 'yellow', 'red')),
    headline TEXT,
    facts TEXT,
    error_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_running ON runs (goal_id) WHERE status = 'RUNNING';

CREATE TABLE IF NOT EXISTS objects (
    goal_id INTEGER NOT NULL REFERENCES goals (id),
    kind TEXT NOT NULL CHECK (kind IN ('keyword', 'target')),
    object_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    ad_group_id TEXT NOT NULL,
    label TEXT NOT NULL,
    start_bid TEXT,
    last_bid TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    gone_at TEXT,
    last_change TEXT,
    hands_off_until TEXT,
    shared INTEGER NOT NULL CHECK (shared IN (0, 1)),
    managed INTEGER NOT NULL CHECK (managed IN (0, 1)),
    proposed_bid TEXT,
    PRIMARY KEY (goal_id, kind, object_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id),
    goal_id INTEGER NOT NULL REFERENCES goals (id),
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    by TEXT NOT NULL CHECK (by IN ('operator', 'human')),
    mode TEXT NOT NULL CHECK (mode IN ('shadow', 'live')),
    action TEXT NOT NULL CHECK (action IN ('down', 'human_change')),
    old_bid TEXT,
    new_bid TEXT,
    why TEXT NOT NULL,
    evidence TEXT
);

CREATE TRIGGER IF NOT EXISTS goal_events_no_update BEFORE UPDATE ON goal_events
BEGIN SELECT RAISE(ABORT, 'goal_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS goal_events_no_delete BEFORE DELETE ON goal_events
BEGIN SELECT RAISE(ABORT, 'goal_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;
"""


class MemoryStoreError(Exception):
    """记忆库打不开或形状不对。`str(exc)` 是一句能直接给人看的中文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class Goal:
    id: int
    store: str
    profile_id: str
    asin: str
    name: str
    currency: str
    target_acos: Decimal | None
    status: str
    failures: int


@dataclass(frozen=True, kw_only=True)
class Event:
    at: datetime
    what: str
    detail: str


@dataclass(frozen=True, kw_only=True)
class Run:
    id: int
    goal_id: int
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    light: str | None
    headline: str | None
    facts: dict[str, object]
    error_code: str | None


@dataclass(frozen=True, kw_only=True)
class Remembered:
    """记忆里关于一个关键词或投放的全部事。"""

    kind: str
    object_id: str
    campaign_id: str
    ad_group_id: str
    label: str
    start_bid: Decimal | None
    last_bid: Decimal | None
    first_seen: date
    last_seen: date
    gone_at: date | None
    last_change: date | None
    hands_off_until: date | None
    #: 上一轮看到时：所在组和别的 ASIN 共用 / 归领星管。变了要当一次改动。
    shared: bool
    managed: bool
    #: 最近一次「本来会改」改成的价，大人还没照做。大人照做了、改了别的价，
    #: 或者它没法再碰了，就清掉。
    proposed_bid: Decimal | None


@dataclass(frozen=True, kw_only=True)
class Pending:
    """等人回「好」的那件事：给出安全值之后 10 分钟内有效。"""

    goal_id: int
    target_percent: int
    expires_at: datetime


@dataclass(frozen=True, kw_only=True)
class DecisionRow:
    kind: str
    object_id: str
    by: str
    mode: str
    action: str
    old_bid: Decimal | None
    new_bid: Decimal | None
    why: str
    evidence: dict[str, object] | None


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


def _text(value: Decimal | date | datetime | None) -> str | None:
    return (
        None if value is None else (str(value) if isinstance(value, Decimal) else value.isoformat())
    )


class Memory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ 打开

    @classmethod
    def open(cls, path: Path) -> Memory:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            # 先用 0600 建出空文件：让 sqlite3 去建，权限跟着 umask 走，可能是 0644。
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
        conn = sqlite3.connect(path, timeout=BUSY_SECONDS, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise MemoryStoreError(
                    "MEMORY_TOO_NEW",
                    f"记忆库是更新的版本（{version}）写的，现在装的版本只认到 {SCHEMA_VERSION}；"
                    "把插件升回新版本再用",
                )
            _use_wal(conn)
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA fullfsync = ON")
            conn.execute("PRAGMA foreign_keys = ON")
            memory = cls(conn)
            memory._migrate(version)
        except BaseException:
            conn.close()
            raise
        return memory

    def close(self) -> None:
        self._conn.close()

    def _migrate(self, version: int) -> None:
        """建表。整段脚本自带 BEGIN IMMEDIATE…COMMIT，而且每一句都可以重跑：

        executescript 遇到进行中的事务会先替你 COMMIT，所以不能套在 _write 里；两个插件
        进程同时第一次打开，后到的那个等锁、再把同一段脚本原样跑一遍，什么也不会多出来。
        """
        if version < SCHEMA_VERSION:
            self._conn.executescript(
                f"BEGIN IMMEDIATE;{_SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ 全局开关

    def paused(self) -> bool:
        return bool(self._conn.execute("SELECT paused FROM control").fetchone()[0])

    def set_paused(self, value: bool) -> None:
        with self._write() as db:
            db.execute("UPDATE control SET paused = ?", (int(value),))

    def resume(self, at: datetime) -> list[str]:
        """「继续干活」：放开全局开关，连着失败被自动停下的商品也一起接着看。返回后者的小名。"""
        with self._write() as db:
            db.execute("UPDATE control SET paused = 0")
            rows = db.execute(
                "SELECT id, name FROM goals WHERE status = 'paused' ORDER BY id"
            ).fetchall()
            for row in rows:
                self._event(db, int(row["id"]), "active", "说了继续干活，接着看", at)
            db.execute("UPDATE goals SET status = 'active', failures = 0 WHERE status = 'paused'")
        return [str(row["name"]) for row in rows]

    def pending(self, now: datetime) -> Pending | None:
        raw = self._conn.execute("SELECT pending FROM control").fetchone()[0]
        if raw is None:
            return None
        data = json.loads(raw)
        pending = Pending(
            goal_id=int(data["goal_id"]),
            target_percent=int(data["target_percent"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )
        return pending if now < pending.expires_at else None

    def set_pending(self, pending: Pending | None) -> None:
        raw = (
            None
            if pending is None
            else json.dumps(
                {
                    "goal_id": pending.goal_id,
                    "target_percent": pending.target_percent,
                    "expires_at": pending.expires_at.isoformat(),
                }
            )
        )
        with self._write() as db:
            db.execute("UPDATE control SET pending = ?", (raw,))

    # ------------------------------------------------------------------ 商品目标

    def _goal(self, row: sqlite3.Row) -> Goal:
        return Goal(
            id=row["id"],
            store=row["store"],
            profile_id=row["profile_id"],
            asin=row["asin"],
            name=row["name"],
            currency=row["currency"],
            target_acos=_dec(row["target_acos"]),
            status=row["status"],
            failures=row["failures"],
        )

    def goals(self) -> list[Goal]:
        """没退掉的商品，按交给它的先后。"""
        rows = self._conn.execute("SELECT * FROM goals WHERE status != 'retired' ORDER BY id")
        return [self._goal(row) for row in rows]

    def goal_named(self, name: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT * FROM goals WHERE name = ? AND status != 'retired'", (name,)
        ).fetchone()
        return self._goal(row) if row is not None else None

    def goal_for(self, profile_id: str, asin: str) -> Goal | None:
        row = self._conn.execute(
            "SELECT * FROM goals WHERE profile_id = ? AND asin = ?", (profile_id, asin)
        ).fetchone()
        return self._goal(row) if row is not None else None

    def adopt(
        self, *, store: str, profile_id: str, asin: str, name: str, currency: str, at: datetime
    ) -> Goal:
        """交给它一个商品。退掉过的同一个商品原样收回来：它的记忆（起点出价等）还在。

        已经在管（没退掉）的同一个商品，原样返回，不改名：两个对话同时交同一个商品时，
        先到的那个人被告知的小名不能被后到的悄悄换掉。
        """
        with self._write() as db:
            old = db.execute(
                "SELECT id, status FROM goals WHERE profile_id = ? AND asin = ?",
                (profile_id, asin),
            ).fetchone()
            if old is not None and old["status"] != "retired":
                return self._goal(
                    db.execute("SELECT * FROM goals WHERE id = ?", (old["id"],)).fetchone()
                )
            if old is None:
                cursor = db.execute(
                    "INSERT INTO goals (store, profile_id, asin, name, currency, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (store, profile_id, asin, name, currency, at.isoformat()),
                )
                goal_id = int(cursor.lastrowid or 0)
                detail = f"交给我：{store} {asin}，叫「{name}」"
            else:
                goal_id = int(old["id"])
                db.execute(
                    "UPDATE goals SET status = 'active', name = ?, store = ?, failures = 0 "
                    "WHERE id = ?",
                    (name, store, goal_id),
                )
                detail = f"重新交给我：{store} {asin}，叫「{name}」"
            self._event(db, goal_id, "adopted", detail, at)
        goal = self.goal_named(name)
        assert goal is not None
        return goal

    def set_target(
        self, goal_id: int, percent: int, detail: str, at: datetime, *, only_if_unset: bool = False
    ) -> bool:
        """定 ACOS 上限。only_if_unset：只在还没定时才定（自动定的默认值不能盖掉人刚说的）。"""
        with self._write() as db:
            cursor = db.execute(
                "UPDATE goals SET target_acos = ? WHERE id = ?"
                + (" AND target_acos IS NULL" if only_if_unset else ""),
                (str(Decimal(percent) / 100), goal_id),
            )
            if cursor.rowcount == 0:
                return False
            self._event(db, goal_id, "target", detail, at)
            return True

    def set_status(
        self,
        goal_id: int,
        status: str,
        detail: str,
        at: datetime,
        *,
        only_from: str | None = None,
    ) -> bool:
        """改商品的状态。only_from：只在它还是这个状态时才改——看一遍要几十秒，这期间人可能
        已经说了「不管它了」，按看之前拿到的旧样子去改，会把人的话悄悄撤掉。"""
        with self._write() as db:
            cursor = db.execute(
                "UPDATE goals SET status = ? WHERE id = ?"
                + (" AND status = ?" if only_from is not None else ""),
                (status, goal_id) + ((only_from,) if only_from is not None else ()),
            )
            if cursor.rowcount == 0:
                return False
            self._event(db, goal_id, status, detail, at)
            return True

    def goal(self, goal_id: int) -> Goal | None:
        row = self._conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return self._goal(row) if row is not None else None

    def events(self, goal_id: int, limit: int = 5) -> list[Event]:
        rows = self._conn.execute(
            "SELECT at, what, detail FROM goal_events WHERE goal_id = ? ORDER BY id DESC LIMIT ?",
            (goal_id, limit),
        )
        return [
            Event(at=datetime.fromisoformat(r["at"]), what=r["what"], detail=r["detail"])
            for r in rows
        ]

    @staticmethod
    def _event(db: sqlite3.Connection, goal_id: int, what: str, detail: str, at: datetime) -> None:
        db.execute(
            "INSERT INTO goal_events (goal_id, at, what, detail) VALUES (?, ?, ?, ?)",
            (goal_id, at.isoformat(), what, detail),
        )

    # ------------------------------------------------------------------ 每一轮

    def start_run(self, goal_id: int, trigger: str, at: datetime) -> int:
        """开一轮。调用方必须拿着跨进程的运行锁（sfw/lxlock.py）：同一时刻只有一个进程在看，
        所以这时还挂着 RUNNING 的，一定是上一个进程看到一半死了（⌘Q、关对话、崩溃）——
        先记成 ABANDONED，账才对得上，也不会让这个商品卡在「上一轮还在看」。
        部分唯一索引 runs_one_running 另外钉死「一个商品最多一行 RUNNING」。
        """
        with self._write() as db:
            db.execute(
                "UPDATE runs SET status = 'ABANDONED', finished_at = ? "
                "WHERE goal_id = ? AND status = 'RUNNING'",
                (at.isoformat(), goal_id),
            )
            cursor = db.execute(
                "INSERT INTO runs (goal_id, trigger, started_at, status) "
                "VALUES (?, ?, ?, 'RUNNING')",
                (goal_id, trigger, at.isoformat()),
            )
            return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        goal_id: int,
        ok: bool,
        light: str | None,
        headline: str,
        facts: dict[str, object],
        error_code: str | None,
        at: datetime,
        remembered: list[Remembered] | None = None,
        decisions: list[DecisionRow] | None = None,
    ) -> int:
        """一轮收尾：对象记忆、决定、运行记录在同一个事务里落下。返回收尾后的连续失败次数。"""
        with self._write() as db:
            for item in remembered or []:
                self._save_object(db, goal_id, item)
            for decision in decisions or []:
                db.execute(
                    "INSERT INTO decisions (run_id, goal_id, kind, object_id, by, mode, action, "
                    "old_bid, new_bid, why, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        goal_id,
                        decision.kind,
                        decision.object_id,
                        decision.by,
                        decision.mode,
                        decision.action,
                        _text(decision.old_bid),
                        _text(decision.new_bid),
                        decision.why,
                        None if decision.evidence is None else json.dumps(decision.evidence),
                    ),
                )
            db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, light = ?, headline = ?, facts = ?, "
                "error_code = ? WHERE id = ?",
                (
                    "OK" if ok else "FAILED",
                    at.isoformat(),
                    light,
                    headline,
                    json.dumps(facts, ensure_ascii=False),
                    error_code,
                    run_id,
                ),
            )
            db.execute(
                "UPDATE goals SET failures = CASE WHEN ? THEN 0 ELSE failures + 1 END WHERE id = ?",
                (int(ok), goal_id),
            )
            return int(
                db.execute("SELECT failures FROM goals WHERE id = ?", (goal_id,)).fetchone()[0]
            )

    def _run(self, row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            goal_id=row["goal_id"],
            trigger=row["trigger"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
            status=row["status"],
            light=row["light"],
            headline=row["headline"],
            facts=json.loads(row["facts"]) if row["facts"] else {},
            error_code=row["error_code"],
        )

    def recent_runs(self, goal_id: int, limit: int = 7) -> list[Run]:
        """最近几轮收了尾的（新的在前）。"""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE goal_id = ? AND status IN ('OK', 'FAILED') "
            "ORDER BY id DESC LIMIT ?",
            (goal_id, limit),
        )
        return [self._run(row) for row in rows]

    def decisions(self, run_id: int) -> list[DecisionRow]:
        rows = self._conn.execute("SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,))
        return [
            DecisionRow(
                kind=r["kind"],
                object_id=r["object_id"],
                by=r["by"],
                mode=r["mode"],
                action=r["action"],
                old_bid=_dec(r["old_bid"]),
                new_bid=_dec(r["new_bid"]),
                why=r["why"],
                evidence=json.loads(r["evidence"]) if r["evidence"] else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ 对象

    def objects(self, goal_id: int) -> dict[tuple[str, str], Remembered]:
        rows = self._conn.execute("SELECT * FROM objects WHERE goal_id = ?", (goal_id,))
        found: dict[tuple[str, str], Remembered] = {}
        for r in rows:
            first_seen = _day(r["first_seen"])
            last_seen = _day(r["last_seen"])
            assert first_seen is not None and last_seen is not None
            found[(r["kind"], r["object_id"])] = Remembered(
                kind=r["kind"],
                object_id=r["object_id"],
                campaign_id=r["campaign_id"],
                ad_group_id=r["ad_group_id"],
                label=r["label"],
                start_bid=_dec(r["start_bid"]),
                last_bid=_dec(r["last_bid"]),
                first_seen=first_seen,
                last_seen=last_seen,
                gone_at=_day(r["gone_at"]),
                last_change=_day(r["last_change"]),
                hands_off_until=_day(r["hands_off_until"]),
                shared=bool(r["shared"]),
                managed=bool(r["managed"]),
                proposed_bid=_dec(r["proposed_bid"]),
            )
        return found

    @staticmethod
    def _save_object(db: sqlite3.Connection, goal_id: int, item: Remembered) -> None:
        db.execute(
            "INSERT OR REPLACE INTO objects (goal_id, kind, object_id, campaign_id, ad_group_id, "
            "label, start_bid, last_bid, first_seen, last_seen, gone_at, last_change, "
            "hands_off_until, shared, managed, proposed_bid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                goal_id,
                item.kind,
                item.object_id,
                item.campaign_id,
                item.ad_group_id,
                item.label,
                _text(item.start_bid),
                _text(item.last_bid),
                _text(item.first_seen),
                _text(item.last_seen),
                _text(item.gone_at),
                _text(item.last_change),
                _text(item.hands_off_until),
                int(item.shared),
                int(item.managed),
                _text(item.proposed_bid),
            ),
        )


def _use_wal(conn: sqlite3.Connection) -> None:
    """切到 WAL。两个进程同时第一次打开一个新库时，切换要独占锁，而且不走 busy timeout，
    后到的那个会立刻收到 database is locked（2026-09-24 评审实测）：再看一眼已经是 WAL
    就算成了，否则等一会儿重试，直到 BUSY_SECONDS。"""
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError:
            mode = None
        if mode is None:
            with contextlib.suppress(sqlite3.OperationalError):
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode == "wal":
            return
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError(f"could not switch the memory to WAL (mode={mode})")
        time.sleep(0.05)
