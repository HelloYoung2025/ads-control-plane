"""改了店铺，币种提示必须跟着改口。

签发表单里「结算币种」下面那句话是这个字段存在的全部理由：它替人确认了
数据以哪种币结算。填错的代价不在当场——签发会成功、卡片显示「生效中」，
而每一次运行都在 MCP 面被 CURRENCY_MISMATCH 拒，那个码在这个界面上一次都不出现。

2026-09-06 实测到的缺陷：店铺输入框的 input 监听只重算作用域警告，不碰币种。
把店铺从 profile-A 手改成 profile-DE-9，下面仍逐字写着
「已按该店铺站点带出：USD。搜索词数据就是以它结算的。」——一个从没查过的店铺，
一句斩钉截铁的假话。纯 Mock 与「接了同步但没开策略侧真实取数」两种部署下，
店铺就是手输的，这条路是常规路径而非边角。

这道守卫只钉两件事：改店铺要重算，重算不许按键即发请求。
"""

from pathlib import Path

_APP_JS = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static/app.js"
_ANCHOR = '$("f-profile").addEventListener("input"'


def _input_handler() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index(_ANCHOR)
    return src[start : start + 400]


def test_editing_the_store_by_hand_recomputes_the_currency_note() -> None:
    assert "syncCurrencyToProfile" in _input_handler(), (
        "店铺输入框一变必须重算币种提示，否则新店铺下面挂着旧店铺的结论"
    )


def test_typing_a_store_id_does_not_ask_the_server_once_per_keystroke() -> None:
    # profile-DE-9 是 12 个字符，逐字触发 input。若这条路直接问服务端，
    # 每个前缀都是一个没见过的 pid，去重集合拦不住——12 次请求打到线上。
    assert "ask: false" in _input_handler(), "input 路径不许向服务端发问"


def test_the_server_still_gets_asked_once_the_person_stops_typing() -> None:
    # 只关掉发问会让手输店铺永远停在「服务端不知道」，而服务端知道。
    src = _APP_JS.read_text(encoding="utf-8")
    assert '$("f-profile").addEventListener("change"' in src, "敲完（失焦/回车）必须补问一次服务端"
