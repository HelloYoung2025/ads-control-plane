"""界面不许承诺「到点会自己跑」——因为仓库里没有任何东西会到点自己跑。

2026-09-06 审核的头号发现：签发之后每一屏的主语都是「AI 按授权频次分析搜索词」
「系统将在 02:00–18:00 之间检查一次」「等下一个窗口即可」，而唯一的运行入口是
人在 Codex 等 AI 客户端里发起一次调用。授权书里的频次与时段是**闸门**——服务端
拿它们拒绝来得太早、来得不是时候的调用（RUN_TOO_SOON / OUTSIDE_RUN_WINDOW），
不是排程。两者在界面上逐字同形，而后果相反：把闸门读成排程的人会一直等，
待批永远空着，他会把这读成「这个店很干净」或者「这系统坏了」。

这道守卫钉两件事：
1. 事实这一侧——`src/` 与 `scripts/` 里没有第二个 generate_negation_candidate_set
   的调用点（只有 MCP 工具注册那一处），也没有调度器依赖。事实若变了，
   下面的文案就该跟着变，这条测试会先红。
2. 文案这一侧——人在「等」的那四个落点，每一处都写明了运行要由人发起。

这不是文案洁癖：这四个落点正是一个签完授权书的人接下来会看的全部地方。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_APP_JS = _ROOT / "src/ads_control_plane/api/ui_static/app.js"
_INDEX = _ROOT / "src/ads_control_plane/api/ui_static/index.html"

#: 说「不会自己运行 / 由人发起」的等价说法。只要命中一条即算说清楚了。
_SAYS_HUMAN_STARTS_IT = (
    "不会自己",
    "不会定时",
    "由人",
    "有人在",
    "人发起",
    "人在 AI 客户端",
    "要有人",
)


def _slice_around(text: str, anchor: str, span: int = 420) -> str:
    """取锚点附近的一段。文案会被改写，锚点取那一屏最稳定的短语。"""
    idx = text.find(anchor)
    assert idx >= 0, f"锚点消失了：{anchor!r}——文案改写时请同步更新这道守卫"
    return text[idx : idx + span]


def test_nothing_in_the_repo_runs_the_strategy_on_a_schedule() -> None:
    """没有调度器，也没有第二个调用点：这是上面那些文案为真的前提。

    只认**调用形**（带左括号），不认注释里提到工具名的那几处——把「谁调用了它」
    和「谁在讨论它」分开，才不至于因为有人写了一句注释就变红。
    """
    callers = sorted(
        p.relative_to(_ROOT).as_posix()
        for p in [*(_ROOT / "src").rglob("*.py"), *(_ROOT / "scripts").rglob("*.py")]
        if "generate_negation_candidate_set(" in p.read_text(encoding="utf-8")
    )
    # 工具注册（server.py）与它背后的服务实现（strategy_service.py）之外不该有调用点。
    assert callers == [
        "src/ads_control_plane/api/mcp_tools/server.py",
        "src/ads_control_plane/api/mcp_tools/strategy_service.py",
    ], f"多出了调用点：{callers}——若是新增了排程，界面上那几句「不会自己运行」就成了假话"
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for dep in ("apscheduler", "celery", "croniter", "schedule"):
        assert dep not in pyproject, f"引入了 {dep}：先改界面文案再谈排程"
    # 定时器/后台线程同理：它们能让「到点自己跑」成真，而界面正说着相反的话。
    timers = sorted(
        p.relative_to(_ROOT).as_posix()
        for p in (_ROOT / "src").rglob("*.py")
        for marker in ("threading.Timer", "asyncio.create_task", "call_later")
        if marker in p.read_text(encoding="utf-8")
    )
    assert not timers, f"出现了定时器：{timers}"


def test_the_four_places_a_waiting_person_looks_all_say_who_starts_a_run() -> None:
    """签完授权书的人接下来只会看这四处。每一处都要说清楚运行由谁发起。"""
    app_js = _APP_JS.read_text(encoding="utf-8")
    index = _INDEX.read_text(encoding="utf-8")
    surfaces = {
        "首屏流程线②": _slice_around(index, "让 AI 生成否定词候选"),
        "待批空态": _slice_around(app_js, "还没有待审的否定词"),
        "授权书行「还没跑过」": _slice_around(app_js, "签发后还没有过一次运行"),
        "签发表单的时段回显": _slice_around(app_js, "之间发起运行"),
    }
    for where, text in surfaces.items():
        assert any(k in text for k in _SAYS_HUMAN_STARTS_IT), (
            f"{where} 没有一句说明运行要由人发起——签发的人会一直等一个不会来的候选"
        )


def test_the_ui_does_not_describe_the_interval_as_a_schedule() -> None:
    """频次说明必须点明它是「最短间隔」这道闸，并说出被拒时人看到的那个码。"""
    app_js = _APP_JS.read_text(encoding="utf-8")
    note = _slice_around(app_js, "const INTERVAL_GATE_NOTE")
    assert "最短间隔" in note and "RUN_TOO_SOON" in note
    # 表单里那句「系统将在…之间检查一次」是这次修掉的原话，不许回潮。
    assert "之间检查一次；其余时间不触发" not in app_js
    assert "等下一个窗口即可" not in app_js
