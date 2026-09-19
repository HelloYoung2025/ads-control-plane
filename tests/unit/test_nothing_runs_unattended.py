"""仓库里没有任何东西会到点自己跑。

2026-09-06 审核的头号发现：签发之后每一屏的主语都是「AI 按授权频次分析搜索词」
「系统将在 02:00–18:00 之间检查一次」，而唯一的运行入口是人在 AI 客户端里发起一次
调用。把闸门读成排程的人会一直等，待批永远空着，他会把这读成「这个店很干净」
或者「这系统坏了」。那套界面与授权书 2026-09-19 已整个删掉，这道守卫留下事实这一侧：

1. `src/` 里 generate_negation_candidates 只有定义它的那一处，没有第二个调用点；
2. 没有调度器依赖，也没有定时器/后台任务。

事实若变了（比如长出一个「每天自己跑」的入口），README 与模型面文字里
「不到点自己跑」那句就成了假话，这条测试会先红。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_nothing_in_the_repo_runs_the_strategy_on_a_schedule() -> None:
    """没有调度器，也没有第二个调用点。

    只认**调用形**（带左括号），不认注释里提到函数名的那几处——把「谁调用了它」
    和「谁在讨论它」分开，才不至于因为有人写了一句注释就变红。def 行同样带左括号，
    所以定义它的那个文件恒在集合里。
    """
    callers = sorted(
        p.relative_to(_ROOT).as_posix()
        for p in (_ROOT / "src").rglob("*.py")
        if "generate_negation_candidates(" in p.read_text(encoding="utf-8")
    )
    assert callers == [
        "src/ads_control_plane/strategies/negation.py",
    ], f"多出了调用点：{callers}——若是新增了排程，「不会自己运行」就成了假话"
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for dep in ("apscheduler", "celery", "croniter", "schedule"):
        assert dep not in pyproject, f"引入了 {dep}：先改文案再谈排程"
    # 定时器/后台线程同理：它们能让「到点自己跑」成真，而文案正说着相反的话。
    timers = sorted(
        p.relative_to(_ROOT).as_posix()
        for p in (_ROOT / "src").rglob("*.py")
        for marker in ("threading.Timer", "asyncio.create_task", "call_later")
        if marker in p.read_text(encoding="utf-8")
    )
    assert not timers, f"出现了定时器：{timers}"
