"""拒绝时把「是哪个参数、允许范围是什么」一并说出来。

前端词典对 `PARAMETER_REJECTED` 的中文是「参数被服务端白名单拒绝（可能是幅度越界、
数值形状不合法、或理由为空）」——一句让人在三种可能里猜的话。而服务端此刻手里
就攥着确切原因：`max_data_staleness_hours must be within [1, 72]`。丢掉它不是少一句
提示，是把「你改哪个值才能成功」这个唯一可行动的信息扣下不给。

2026-08-30 实测同一个缺口在 MCP 面上更刺眼：AI 客户端传 `max_data_staleness_hours=73`
拿到的整条回复就是 `PARAMETER_REJECTED`，既不知道是哪个参数，也不知道上限是 72，
只能靠猜或者放弃。

`app.js` 的 `api()` 早就认识 `{code, message}` 形状并会把 message 拼在中文后面
（第 483~486 行），一直没有服务端用它。本模块就是那个缺掉的服务端一侧。

**只透出我们自己写的校验语句**（pydantic 的 `msg`），绝不透 `input_value`，
也绝不把任意异常文本原样外抛：AX-16 要求错误码稳定且不泄露资源是否存在。
参数形状的校验语句谈的是调用方自己的输入和我们公开的白名单边界，两者都不是资源。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

#: 拼接多条校验语句时的分隔符，也用于截断上限——错误条要能一眼读完。
_JOIN = "；"
_MAX_HINT_CHARS = 300


def _reason_of(exc: BaseException) -> str:
    """从校验异常里取出人能照着改的那句话。取不到就返回空串，绝不编。"""
    if isinstance(exc, ValidationError):
        parts: list[str] = []
        for err in exc.errors():
            # 只取 msg。input 是调用方原样回声，loc 是内部字段路径，都不外抛。
            msg = str(err.get("msg", "")).removeprefix("Value error, ").strip()
            field = ".".join(str(p) for p in err.get("loc", ()) if isinstance(p, str))
            if not msg:
                continue
            parts.append(f"{field}: {msg}" if field and field not in msg else msg)
        return _JOIN.join(dict.fromkeys(parts))[:_MAX_HINT_CHARS]
    reason = str(exc).strip()
    return reason[:_MAX_HINT_CHARS]


def coded_detail(code: str, exc: BaseException | None = None) -> dict[str, Any] | str:
    """FastAPI 的 `detail`：能说清原因就出 `{code, message}`，说不清就退回裸码。

    退回裸码而不是塞一句「未知错误」是有意的：前端遇到字符串 detail 走的是老路径，
    行为与从前逐字一致，不会因为我们没话说而多出一条噪音。
    """
    if exc is None:
        return code
    reason = _reason_of(exc)
    return {"code": code, "message": reason} if reason else code


def coded_detail_message(code: str, message: str) -> dict[str, Any]:
    """已经知道确切原因时直接给 {code, message}，不必从异常里刨。"""
    return {"code": code, "message": message}


def denial_hint(exc: BaseException) -> str | None:
    """MCP 面用：ToolDenied 的 detail。拿不到确切原因就返回 None。"""
    return _reason_of(exc) or None
