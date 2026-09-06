"""测试自己不许是定时炸弹：会话令牌的有效期必须锚在真实墙钟上。

2026-09-07 20:00 UTC 真的炸过一次。test_mandate_scope_api 里那条把会话有效期写成
`now + timedelta(hours=8)`，而 now 是硬编码的 2026-09-06 12:00 UTC——那一刻到了，
一条与代码毫无关系的测试在会话中途从绿变红，报 401 AUTHENTICATION_REQUIRED，
diff 里一个相关改动都没有。排查它花掉的时间远超一开始写对它的成本。

根因是两种时间被写成了一种：`ActorContext.is_expired()` 默认拿 `datetime.now(UTC)`
比（identity/actor.py），因为令牌是不是过期是**真实**世界的事；而域层判定的
「现在几点」是注入的 clock，故意可以定在任何一天。前者钉在固定日历日上，
就等于给测试装了一个到期自毁装置。

这条守卫只管前者。域层对象（ApprovalDecision、执行意图、授权书自己的有效期）
照旧跟着固定 NOW 走——那正是它们该有的样子，不在扫描范围内。
"""

import ast
import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parent.parent


def _actor_context_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ActorContext"
    ]


def test_no_session_token_in_the_suite_expires_on_a_calendar_date() -> None:
    """全仓扫描：每一处 ActorContext 的有效期都要从真实墙钟算。

    允许的写法只有两种：`datetime.now(UTC)` 直接算，或一个明摆着是墙钟的名字
    （real_now / _session_now）。任何从固定常量算出来的窗口都会在那一天之后
    让整套测试莫名其妙地红。

    唯一的例外由调用点自己声明：就地 model_copy 一个过期时刻去测**域层**过期
    规则（见 test_authorization 的 test_expired_actor_denied）——那不是构造
    ActorContext，扫不到，也不该扫到。
    """
    offenders: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for call in _actor_context_calls(ast.parse(source)):
            for kw in call.keywords:
                if kw.arg not in ("issued_at", "expires_at"):
                    continue
                expr = ast.unparse(kw.value)
                anchored = "datetime.now(" in expr or re.search(
                    r"\b(real_now|_session_now)\b", expr
                )
                if not anchored:
                    offenders.append(
                        f"{path.relative_to(_TESTS.parent)}:{kw.value.lineno}  {kw.arg}={expr}"
                    )
    assert not offenders, (
        "会话有效期钉在了固定日期上——这些测试会在那一刻之后突然 401：\n" + "\n".join(offenders)
    )
