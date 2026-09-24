"""操盘手的记忆库（sfw/memory.py）：只有自己能读、两本账只追加、同一商品同时只有一轮、
进程死掉留下的半截运行会被认出来、库比代码新就不打开（也不改它一个字节）、
人刚说的话不会被一轮看了几十秒的旧快照盖掉。
"""

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ads_control_plane.sfw.memory import (
    SCHEMA_VERSION,
    DecisionRow,
    Memory,
    MemoryStoreError,
    Pending,
    Remembered,
)

PROFILE = "1000000000000001"
NOW = datetime(2026, 9, 24, 1, 0, tzinfo=UTC)


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[Memory]:
    opened = Memory.open(tmp_path / "state" / "operator.sqlite3")
    yield opened
    opened.close()


def adopt(memory: Memory, asin: str = "B0TEST0001", name: str = "猫抓板") -> int:
    goal = memory.adopt(
        store="美国店", profile_id=PROFILE, asin=asin, name=name, currency="USD", at=NOW
    )
    return goal.id


def item(object_id: str = "kw-1", **changes: object) -> Remembered:
    base = {
        "kind": "keyword",
        "object_id": object_id,
        "campaign_id": "cmp-1",
        "ad_group_id": "ag-1",
        "label": "cat scratcher [exact]",
        "start_bid": Decimal("1.00"),
        "last_bid": Decimal("0.85"),
        "first_seen": date(2026, 9, 1),
        "last_seen": date(2026, 9, 24),
        "gone_at": None,
        "last_change": date(2026, 9, 20),
        "hands_off_until": date(2026, 10, 8),
        "shared": False,
        "managed": False,
        "proposed_bid": Decimal("0.72"),
    }
    return Remembered(**{**base, **changes})  # type: ignore[arg-type]


# ------------------------------------------------------------------ 文件


def test_only_i_can_read_it(tmp_path: Path) -> None:
    path = tmp_path / "state" / "operator.sqlite3"
    Memory.open(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_file_others_could_read_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "operator.sqlite3"
    path.touch(mode=0o644)
    path.chmod(0o644)
    Memory.open(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_newer_memory_is_not_opened_and_not_touched(tmp_path: Path) -> None:
    path = tmp_path / "operator.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE later (x)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(MemoryStoreError) as caught:
        Memory.open(path)
    assert caught.value.code == "MEMORY_TOO_NEW"
    assert "升回新版本" in str(caught.value)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (tmp_path / "operator.sqlite3-wal").exists()


def test_opening_again_keeps_everything(tmp_path: Path) -> None:
    path = tmp_path / "operator.sqlite3"
    first = Memory.open(path)
    adopt(first)
    first.set_paused(True)
    first.close()
    again = Memory.open(path)
    assert [g.name for g in again.goals()] == ["猫抓板"]
    assert again.paused()
    again.close()


def _open_after(start: float, path: str) -> str:
    import time as clock

    clock.sleep(max(0.0, start - clock.time()))
    try:
        Memory.open(Path(path)).close()
    except BaseException as exc:  # pragma: no cover - 只在失败时走到
        return f"{type(exc).__name__}: {exc}"
    return "ok"


def test_several_processes_opening_a_new_memory_at_once_all_succeed(tmp_path: Path) -> None:
    """两个 SFW 对话各拉一个插件进程，第一次同时打开：切 WAL 那一下不走 busy timeout，
    2026-09-24 评审用真进程测到一半以上失败。这里用真进程、同一时刻起跑，跑几遍。"""
    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        for attempt in range(5):
            path = tmp_path / f"operator-{attempt}.sqlite3"
            start = datetime.now(UTC).timestamp() + 1.0
            results = pool.starmap(_open_after, [(start, str(path))] * 4)
            assert results == ["ok"] * 4
            check = sqlite3.connect(path)
            assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert check.execute("SELECT COUNT(*) FROM control").fetchone()[0] == 1
            check.close()


# ------------------------------------------------------------------ 只追加


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE goal_events SET detail = 'rewritten'",
        "DELETE FROM goal_events",
        "UPDATE decisions SET why = 'rewritten'",
        "DELETE FROM decisions",
    ],
)
def test_the_two_ledgers_are_append_only(memory: Memory, statement: str) -> None:
    goal_id = adopt(memory)
    run_id = memory.start_run(goal_id, "manual", NOW)
    assert run_id is not None
    memory.finish_run(
        run_id,
        goal_id=goal_id,
        ok=True,
        light="green",
        headline="猫抓板：这轮不用改",
        facts={},
        error_code=None,
        at=NOW,
        decisions=[
            DecisionRow(
                kind="keyword",
                object_id="kw-1",
                by="operator",
                mode="shadow",
                action="down",
                old_bid=Decimal("1.00"),
                new_bid=Decimal("0.85"),
                why="ACOS_HIGH",
                evidence={"days": 14},
            )
        ],
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        memory._conn.execute(statement)


# ------------------------------------------------------------------ 商品


def test_adopting_records_who_and_when(memory: Memory) -> None:
    goal_id = adopt(memory)
    goal = memory.goal_named("猫抓板")
    assert goal is not None and goal.id == goal_id
    assert goal.target_acos is None
    assert goal.status == "active"
    assert [e.what for e in memory.events(goal_id)] == ["adopted"]


def test_one_product_is_one_goal(memory: Memory) -> None:
    adopt(memory)
    with pytest.raises(sqlite3.IntegrityError):
        memory._conn.execute(
            "INSERT INTO goals (store, profile_id, asin, name, currency, created_at) "
            "VALUES ('美国店', ?, 'B0TEST0001', '别名', 'USD', 'x')",
            (PROFILE,),
        )


def test_a_name_is_taken_only_while_the_goal_is_not_retired(memory: Memory) -> None:
    goal_id = adopt(memory)
    with pytest.raises(sqlite3.IntegrityError):
        adopt(memory, asin="B0TEST0002")
    memory.set_status(goal_id, "retired", "不管了", NOW)
    assert memory.goal_named("猫抓板") is None
    other = adopt(memory, asin="B0TEST0002")
    assert other != goal_id


def test_taking_a_retired_product_back_keeps_its_memory(memory: Memory) -> None:
    goal_id = adopt(memory)
    run_id = memory.start_run(goal_id, "manual", NOW)
    assert run_id is not None
    memory.finish_run(
        run_id,
        goal_id=goal_id,
        ok=True,
        light="green",
        headline="h",
        facts={},
        error_code=None,
        at=NOW,
        remembered=[item()],
    )
    memory.set_status(goal_id, "retired", "不管了", NOW)
    back = memory.adopt(
        store="美国店", profile_id=PROFILE, asin="B0TEST0001", name="小猫", currency="USD", at=NOW
    )
    assert back.id == goal_id
    assert back.name == "小猫"
    assert back.status == "active"
    assert memory.objects(goal_id)[("keyword", "kw-1")].start_bid == Decimal("1.00")
    assert [e.what for e in memory.events(goal_id)][:2] == ["adopted", "retired"]


def test_the_target_is_kept_as_a_fraction(memory: Memory) -> None:
    goal_id = adopt(memory)
    memory.set_target(goal_id, 25, "ACOS 上限改成 25%", NOW)
    goal = memory.goal_named("猫抓板")
    assert goal is not None and goal.target_acos == Decimal("0.25")


def test_events_come_newest_first_and_limited(memory: Memory) -> None:
    goal_id = adopt(memory)
    for percent in (20, 25, 30):
        memory.set_target(goal_id, percent, f"{percent}%", NOW)
    assert [e.detail for e in memory.events(goal_id, 2)] == ["30%", "25%"]


# ------------------------------------------------------------------ 每一轮


def test_at_most_one_running_row_per_product(memory: Memory) -> None:
    goal_id = adopt(memory)
    memory.start_run(goal_id, "manual", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        memory._conn.execute(
            "INSERT INTO runs (goal_id, trigger, started_at, status) VALUES (?, 'tick', 'x', "
            "'RUNNING')",
            (goal_id,),
        )


def test_a_run_left_behind_by_a_dead_process_is_abandoned_at_once(memory: Memory) -> None:
    """拿着运行锁开新一轮时，还挂着的 RUNNING 就是死掉的进程留下的：不用等一小时。"""
    goal_id = adopt(memory)
    other = adopt(memory, asin="B0TEST0002", name="小猫")
    dead = memory.start_run(goal_id, "manual", NOW)
    elsewhere = memory.start_run(other, "manual", NOW)
    fresh = memory.start_run(goal_id, "manual", NOW + timedelta(minutes=1))
    assert fresh != dead
    status = dict(memory._conn.execute("SELECT id, status FROM runs").fetchall())
    assert status == {dead: "ABANDONED", elsewhere: "RUNNING", fresh: "RUNNING"}


def test_failures_count_up_and_reset_on_success(memory: Memory) -> None:
    goal_id = adopt(memory)
    counts = []
    for ok in (False, False, True, False):
        run_id = memory.start_run(goal_id, "manual", NOW)
        assert run_id is not None
        counts.append(
            memory.finish_run(
                run_id,
                goal_id=goal_id,
                ok=ok,
                light="yellow",
                headline="h",
                facts={},
                error_code=None if ok else "LX_TRANSPORT_ERROR",
                at=NOW,
            )
        )
    assert counts == [1, 2, 0, 1]


def test_recent_runs_are_the_finished_ones_newest_first(memory: Memory) -> None:
    goal_id = adopt(memory)
    for n in range(3):
        run_id = memory.start_run(goal_id, "manual", NOW + timedelta(hours=n))
        assert run_id is not None
        memory.finish_run(
            run_id,
            goal_id=goal_id,
            ok=True,
            light="green",
            headline=f"第 {n} 轮",
            facts={"n": n},
            error_code=None,
            at=NOW + timedelta(hours=n),
        )
    memory.start_run(goal_id, "manual", NOW + timedelta(hours=5))
    runs = memory.recent_runs(goal_id, 2)
    assert [r.headline for r in runs] == ["第 2 轮", "第 1 轮"]
    assert runs[0].facts == {"n": 2}


def test_objects_and_decisions_round_trip(memory: Memory) -> None:
    goal_id = adopt(memory)
    run_id = memory.start_run(goal_id, "manual", NOW)
    assert run_id is not None
    remembered = [
        item(),
        item("kw-2", start_bid=None, last_bid=None, shared=True, managed=True, proposed_bid=None),
    ]
    decision = DecisionRow(
        kind="keyword",
        object_id="kw-1",
        by="human",
        mode="shadow",
        action="human_change",
        old_bid=Decimal("0.85"),
        new_bid=Decimal("0.90"),
        why="BID_CHANGED_ELSEWHERE",
        evidence=None,
    )
    memory.finish_run(
        run_id,
        goal_id=goal_id,
        ok=True,
        light="green",
        headline="h",
        facts={},
        error_code=None,
        at=NOW,
        remembered=remembered,
        decisions=[decision],
    )
    back = memory.objects(goal_id)
    assert back[("keyword", "kw-1")] == remembered[0]
    assert back[("keyword", "kw-2")] == remembered[1]
    assert memory.decisions(run_id) == [decision]


# ------------------------------------------------------------------ 全局开关


def test_stop_and_resume(memory: Memory) -> None:
    assert not memory.paused()
    memory.set_paused(True)
    assert memory.paused()
    memory.set_paused(False)
    assert not memory.paused()


def test_resume_also_wakes_products_that_stopped_after_failing(memory: Memory) -> None:
    tired = adopt(memory)
    adopt(memory, asin="B0TEST0002", name="小猫")
    gone = adopt(memory, asin="B0TEST0003", name="旧货")
    memory.set_status(tired, "paused", "连着 3 轮没看成，先歇着", NOW)
    memory.set_status(gone, "retired", "不管了", NOW)
    memory.set_paused(True)
    assert memory.resume(NOW) == ["猫抓板"]
    assert not memory.paused()
    assert {g.name: g.status for g in memory.goals()} == {"猫抓板": "active", "小猫": "active"}
    assert memory.events(tired, 1)[0].detail == "说了继续干活，接着看"


# ------------------------------------------------------------------ 看的途中人说了话


def test_a_default_target_does_not_overwrite_one_a_person_just_set(memory: Memory) -> None:
    goal_id = adopt(memory)
    memory.set_target(goal_id, 25, "ACOS 上限改成 25%", NOW)
    assert not memory.set_target(goal_id, 27, "按近 14 天定了", NOW, only_if_unset=True)
    goal = memory.goal(goal_id)
    assert goal is not None and goal.target_acos == Decimal("0.25")
    assert [e.detail for e in memory.events(goal_id)][0] == "ACOS 上限改成 25%"


def test_a_status_change_based_on_an_old_snapshot_does_not_undo_a_drop(memory: Memory) -> None:
    goal_id = adopt(memory)
    memory.set_status(goal_id, "retired", "不管了", NOW)
    assert not memory.set_status(goal_id, "active", "看成了，接着看", NOW, only_from="paused")
    assert not memory.set_status(goal_id, "paused", "连着 3 轮", NOW, only_from="active")
    goal = memory.goal(goal_id)
    assert goal is not None and goal.status == "retired"


def test_adopting_a_product_already_handed_over_keeps_its_name(memory: Memory) -> None:
    """两个对话同时交同一个商品：先到的那个人被告知的小名不能被悄悄换掉。"""
    goal_id = adopt(memory)
    again = memory.adopt(
        store="美国店", profile_id=PROFILE, asin="B0TEST0001", name="小猫", currency="USD", at=NOW
    )
    assert again.id == goal_id
    assert again.name == "猫抓板"
    assert [e.what for e in memory.events(goal_id)] == ["adopted"]


def test_a_pending_answer_expires(memory: Memory) -> None:
    goal_id = adopt(memory)
    memory.set_pending(
        Pending(goal_id=goal_id, target_percent=10, expires_at=NOW + timedelta(minutes=10))
    )
    pending = memory.pending(NOW + timedelta(minutes=9))
    assert pending is not None and pending.target_percent == 10
    assert memory.pending(NOW + timedelta(minutes=10)) is None
    memory.set_pending(None)
    assert memory.pending(NOW) is None
