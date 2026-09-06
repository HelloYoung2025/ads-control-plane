"""界面不许承诺这套系统做不到的事。

2026-09-06 一轮重审里，同一个模式被抓到三次：某句文案描述的是一条不存在的下一步，
而人正是照着这句话决定接下来干什么。这些话不会报错、不会变红，只会让人白跑一趟：

- 工作台抬头说「批准与执行走既有审批链」——工作台预览既没有提交审批的入口，
  本系统也从不执行任何修改；
- 预览的必填「理由」说「这条会进审计记录」——audit/ledger.py 在 src/ 里没有任何
  外部 import，理由只随响应回显，服务重启即无；
- 避让固定动作栏的那条 CSS 写的类名（`.panel`）和 app.js 实际生成的（`wb-result-panel`）
  对不上，规则一次都没命中，面板连同它底部的下载按钮被动作栏盖住。

下面钉的是「不许再说」和「选择器必须指向真存在的东西」，不钉具体措辞——
措辞可以改好，承诺不能凭空发明。注释里出现这些字样不算数（注释是写给读代码的人的），
所以比对前先把注释剥掉。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_UI = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static"
_APP_JS = _UI / "app.js"
_INDEX = _UI / "index.html"
_CSS = _UI / "style.css"


def _strip_block_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def _visible_js() -> str:
    """app.js 去掉注释后剩下的部分——即真会被人看见的字符串所在之处。

    `//` 前若紧跟冒号则不当注释（http:// 之类）。宁可少剥一点，也不要把
    真的文案剥掉而让守卫失效。
    """
    return re.sub(
        r"(?<!:)//[^\n]*", " ", _strip_block_comments(_APP_JS.read_text(encoding="utf-8"))
    )


def _visible_html() -> str:
    return re.sub(r"<!--.*?-->", " ", _INDEX.read_text(encoding="utf-8"), flags=re.DOTALL)


def test_the_scroll_guard_targets_a_class_the_page_actually_emits() -> None:
    """避让规则里的类名必须是 app.js 真会生成的。

    选择器打偏是静默失效：没有报错、没有测试变红，只有人看到面板被动作栏盖住。
    """
    css = _strip_block_comments(_CSS.read_text(encoding="utf-8"))
    js = _APP_JS.read_text(encoding="utf-8")
    blocks = re.findall(r"([^{}]*)\{[^{}]*scroll-margin-bottom[^{}]*\}", css)
    assert blocks, "style.css 里已经没有避让规则了——动作栏会盖住结果面板"
    for selector in blocks:
        for cls in re.findall(r"\.([a-z][a-z0-9-]+)", selector):
            assert f'"{cls}"' in js, f"CSS 里避让 .{cls}，而 app.js 从不生成这个类名"


def test_no_screen_claims_the_adjustment_reason_is_audited() -> None:
    # workbench_api 只把 reason 随预览响应回显；audit/ledger.py 无外部 import。
    assert "会进审计记录" not in _visible_js()


def test_the_workbench_does_not_promise_an_approval_chain() -> None:
    # 工作台预览没有任何提交审批的端点，系统也从不执行修改。
    assert "走既有审批链" not in _visible_js()
    assert "走既有审批链" not in _visible_html()


def test_the_page_does_not_claim_it_never_updates_itself_while_it_does() -> None:
    """「本页不会自动更新」在同一个文件里是假的（2026-09-06 排查）。

    app.js 挂着一个 visibilitychange 监听器：切走再回来、且距上次读取超过一分钟，
    就自动重读一次。那个行为本身是对的（开着昨天的标签页回来的人会把「待批 0」
    读成「AI 昨晚没产出」），说谎的是这三处文案。留着它，人会拿一次自己不知道
    发生过的重读当成"我上次看到的那一屏"，而两屏之间可能已经差了一整晚。

    这条钉的是「说过就得做到」：只要那个自动重读还在，就不许再说「不会自动更新」。
    """
    js = _visible_js()
    auto_refreshes = "visibilitychange" in js and "refreshAll()" in js
    assert auto_refreshes, (
        "自动重读没有了——那这条守卫该删（治理规则：文书只准变短），不是留着一条描述不存在行为的规则"
    )
    for text in (js, _visible_html()):
        assert "不会自动更新" not in text


# --- 「间隔卡住的日上限」不许被印成还能花的额度（2026-09-07 排查） ---
#
# 一份 run_interval_minutes=1440 / max_runs_per_day=2 的授权，第二次运行**永远**发起
# 不了：闸门要到 24 小时后才开，那时配额日已经翻过。而卡片照印「2 次/日」，配额行
# 照印「今天还能跑 1 次 · 最早 [明天] 可再发起」——两句话摆在一起，前一句是邀请，
# 人照着它去叫 AI，换回来的是 RUN_TOO_SOON。
#
# 说清这件事的那句话此前**只在签发表单里**（runsVsIntervalNote），签完就再没人说过；
# 而人每天看的是卡片，不是那张早就关掉的表单。


def _block_after(body: str, marker: str) -> str:
    """marker 之后那一对花括号里的内容（含括号），按深度配对。

    不用「前后 N 个字符」的窗口：窗口会滑到隔壁那一格上去，断言随之锚错位置。
    """
    start = body.index(marker)
    brace = body.index("{", start)
    depth = 0
    for j in range(brace, len(body)):
        if body[j] == "{":
            depth += 1
        elif body[j] == "}":
            depth -= 1
            if depth == 0:
                return body[brace : j + 1]
    raise AssertionError(f"{marker} 之后的块没有闭合")


def _nearest_property_key(body: str, at: int) -> str:
    """at 这个位置属于哪个属性——往前找最近的 `xxx:` 属性名。

    用来区分「写进了常驻可见的 text」和「藏进了悬停的 title」。
    """
    keys = re.findall(r"\b(text|title|class|dataset)\s*:", body[:at])
    assert keys, "这段之前一个属性名都没有，抽错位置了"
    return keys[-1]


def _fn_source(name: str, js: str) -> str:
    """抽出具名函数的完整源码（含 `function` 与参数表），可直接丢给 node 执行。"""
    head = js.index("function " + name)
    return js[head : head + len(_fn_body(name, js)) + (js.index("{", head) - head)]


def _fn_body(name: str, js: str) -> str:
    """抽出具名函数的函数体。

    先按括号深度走完参数表再找 `{`——否则解构默认参数（`{ a = 1 } = {}`）里的
    花括号会被当成函数体开头，抽出一段空壳，断言随之永远为真。
    """
    head = js.index("function " + name)
    i = js.index("(", head)
    depth = 0
    while True:
        if js[i] == "(":
            depth += 1
        elif js[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    start = js.index("{", i)
    depth = 0
    for j in range(start, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError(f"{name} 的函数体没有闭合")


def test_the_interval_cap_arithmetic_is_what_the_server_actually_enforces() -> None:
    """把 intervalCapsRunsAt 与 derivedRunsPerDay 真的跑一遍，不是扫源码。

    钉的是算术本身：每天一次的闸门下写 2 次/日，实际只有 1 次；每 12 小时下写
    2 次/日则货真价实。判错方向会让界面在该说话时闭嘴、或在不该说时乱说。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available on this machine")
    js = _visible_js()
    derived = _fn_source("derivedRunsPerDay", js)
    caps = _fn_source("intervalCapsRunsAt", js)
    cases = [
        # (interval, runs, 期望被卡到的次数 或 None)
        (1440, 2, 1),  # 活生生的那一份：每天一次的闸门 + 写了 2 次/日
        (1440, 1, None),  # 合同与闸门一致，没话可说
        (720, 2, None),  # 每 12 小时确实跑得满 2 次
        (720, 3, 2),  # 写 3 次，闸门只给 2 次
        (60, 24, None),  # 每小时 24 次，正好
        (2880, 2, 1),  # 比一天还长的间隔，一天仍只能 1 次
    ]
    script = (
        derived
        + "\n"
        + caps
        + "\n"
        + "const cases = "
        + json.dumps([[a, b, c] for a, b, c in cases])
        + ";\n"
        + "console.log(JSON.stringify(cases.map(([i, r]) => intervalCapsRunsAt(i, r))));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    got = json.loads(out.stdout)
    want = [c for _, _, c in cases]
    assert got == want, f"间隔卡日上限的算术错了：期望 {want}，实得 {got}"
    assert "intervalCapsRunsAt" in js


def test_the_card_says_out_loud_that_the_interval_caps_the_daily_quota() -> None:
    """常驻可见的那一句必须自己说清楚，不许把解释藏进悬停。

    这正是 runsVsIntervalNote 的注释里已经点名过的错：会引起误解的常驻可见，
    能解释它的要手动展开/悬停才看得到。卡片上重犯一次就白修了。
    """
    js = _visible_js()
    body = _fn_body("mandateRow", js)
    marker = "（间隔下实际 "
    assert marker in body, (
        "授权书卡片的「N 次/日」没有过间隔上限这一关——人会照着一个发起不了的数字去叫 AI"
    )
    at = body.index(marker)
    # 解释必须落在常驻可见的 text 上，不能藏进悬停的 title
    assert _nearest_property_key(body, at) == "text", (
        "间隔上限的说明多半挂进了 title——会引起误解的那句常驻，能解释它的要悬停才看得到，正好放反"
    )
    assert "intervalCapsRunsAt" in body[:at], "这句说明不是由间隔上限判定出来的，是无条件印的"


def test_an_unspendable_remaining_run_is_not_advertised_as_available() -> None:
    """闸门关着、且间隔比配额紧时，不许再印「今天还能跑 N 次」。

    这句话在那个组合下是一笔花不掉的钱：闸门要到下一个配额日才开，
    今天剩的次数那时早已作废。而「今天的次数已用完」必须留着——它是真的。
    """
    js = _visible_js()
    body = _fn_body("quotaNowLine", js)
    assert "intervalCapsRunsAt" in body, "quotaNowLine 没有考虑间隔把配额卡死的情况"
    guarded = _block_after(body, "if (!unspendable)")
    assert '"今天还能跑 "' in guarded, "邀请那句没被守卫罩住——闸门关着时照样会印出来"
    assert '"今天的次数已用完"' in guarded, (
        "「今天的次数已用完」被挪到守卫外面了——那句是真的，不该跟着一起消失"
    )
    # 全篇只许有这一处印「今天还能跑」，否则守卫外面还留着一条旁路
    assert body.count('"今天还能跑 "') == 1, "还有另一处会印「今天还能跑」，守卫管不到它"


def test_every_control_the_copy_tells_you_to_click_actually_exists() -> None:
    """文案里「点「X」」引到的每个控件名，页面必须真的渲染得出来。

    与上面那条 CSS 选择器守卫同族：指错了不会报错、不会变红，只会让人在屏幕上
    找一个不存在的按钮。2026-09-07 加这条时全站 9 处指路里有 1 处是刚写坏的——
    引的是「已选 N 个对象」，而按钮上写的是「已选 3 / 200 个对象」。
    """
    js = _visible_js()
    html = _visible_html()
    named = set(re.findall(r"点[^「」\"]{0,6}「([^「」]{1,20})」", js))
    assert named, "一条指路文案都没有了——那这条守卫该删（治理规则：文书只准变短）"
    for label in sorted(named):
        assert f'text: "{label}"' in js or f">{label}<" in html, (
            f"文案让人去点「{label}」，而页面从不渲染这个名字的控件"
        )


def test_revoking_says_the_pending_sets_go_down_with_it() -> None:
    """撤销的确认框要说清连坐：名下还在「待批」的候选，撤销后一并批不了。

    服务端对已撤销授权名下的集合是 409 MANDATE_REVOKED（approval_api 里那条
    「撤销时你要停的就是它」）。此前确认框只说「AI 立即停止按它运行」——人读到的
    是「以后不再自动跑了」，读不出「我现在正要审的这几份，一并作废」。卡片上那个
    已撤销标记是**事后**才看得到的，而撤销不可逆，话要在按下之前说。

    2026-09-07 实测确认框实际文案：
      撤销这份授权书？…「待批」里还有 1 份来自这份授权的候选，撤销后它们一并
      批不了（只能拒绝掉）。
    """
    js = _visible_js()
    body = _fn_body("revokeMandate", js)
    assert "pendingSetsOfMandate" in body, "确认框没去数名下还有几份待批"
    # 数出来的那句必须真的拼进 confirm 的实参，而不是算完就扔。
    also = body[body.index("const also =") : body.index("if (!window.confirm(")]
    assert "pending > 0" in also and "pending +" in also, "数出来了却没写进那句话"
    at = body.index("window.confirm")
    prompt = body[at : body.index(")) {", at)]
    assert "+ also" in prompt, "算了这句话却没接到确认框上"

    counter = _fn_body("pendingSetsOfMandate", js)
    assert 'state === "FROZEN"' in counter, "只有还停在待批的才会被连坐"
    assert "expired !== true" in counter, "已过期的本来就批不了，不该算进这句话吓人"
    assert "mandate_id === mandateId" in counter, "数的必须是这份授权名下的，不是全部"


def test_rejecting_does_not_offer_a_regeneration_today_that_the_quota_refuses() -> None:
    """「需要重新生成」这句，得先确认这条路今天走得通。

    默认打法是 1 次/日，而产出这批候选的那一次运行已经把它用掉了。人照着这句话
    拒绝，之后才发现今天再也生成不出东西来——而拒绝是不可逆的（集合进终态）。
    配额与「最早几点可再发起」服务端早就算好挂在授权书上，按下之前照实说出来。

    2026-09-07 实测确认框实际文案：
      拒绝这批候选？…（需要重新生成）。注意：这份授权今天的次数已用完，今天重新
      生成不出来，最早 9/7 04:01 才能再发起。
    """
    js = _visible_js()
    body = _fn_body("rejectSet", js)
    assert "regenerateOutlook" in body, "确认框没问过重新生成这条路今天走不走得通"

    outlook = _fn_body("regenerateOutlook", js)
    assert "runs_remaining_today" in outlook, "没看配额"
    assert "next_run_allowed_at" in outlook, "只说不行，不说什么时候才行"
    # 即席生成不受授权书配额约束，不许对它说一句吓人的假话。
    assert 'if (!mid) return ""' in outlook, "即席生成没有配额可言，不该也挂这句"
    # 还有次数时也不该说。
    assert "runs_remaining_today > 0" in outlook, "还有次数时也照说，就成了另一句假话"


def test_a_stale_preview_cannot_still_hand_out_its_csv() -> None:
    """「已过期」必须连下载一起过期。

    这份 CSV 是整条链上唯一的产出，人拿着它去领星后台**手工执行**，而下载回执写着
    「本系统不留存这批调整，请以这份文件为准」。此前改完勾选或数值之后，面板挂上
    「已过期」横幅，按钮却照常可点、wb.lastPreview 仍是旧 payload——下出来的是改动
    之前的旧数值，配一句宣称权威的回执。一份会被真的执行的错文件，比没有产出更坏。

    与「过期集合批准即拒」同一条 fail-closed 纪律：当场停用并说清怎么恢复。
    2026-09-07 于 8791 实测：改数值后按钮 disabled=true、title 说明原因，
    点它不产生任何下载；重新点「预览」后按钮恢复、横幅消失。
    """
    js = _visible_js()
    stale = _fn_body("markPreviewStale", js)
    assert 'data-action="wb-preview-csv"' in stale, "标了过期却没去管那颗下载按钮"
    assert "disabled = true" in stale, "过期后按钮仍可点，会下出改动之前的旧数值"
    assert "title" in stale, "停用了却不说为什么、也不说怎么恢复"

    # 停用之外还要有第二道闸：按钮走的是委派点击，将来多一条入口就会绕过 disabled。
    dl = _fn_body("wbDownloadPreviewCsv", js)
    guard = dl[: dl.index("const esc")]
    assert "previewStale" in guard, "下载函数自己不看过期标志，多一条入口就绕过去了"
    assert "return" in guard, "看了却没拦住"


def test_the_card_names_the_asins_it_tells_you_to_go_negate() -> None:
    """「去领星否定这些 ASIN」——两条会说这句话的路径都得说得出是哪几个。

    · ALL_ASIN：走静态的 spec.next，说得出要做什么、说不出对哪几个下手；
    · 其余结局带部分 ASIN 弃权：走 asinLine，此前只有一个数字。

    这两条恰恰都不创建候选集合（候选集合那张卡早就列了词），所以词表在界面上没有
    第二个出处。服务端已随运行流水记下（MandateRunRecord.asin_abstain_terms），
    照列即可——人拿着「3 个」去领星后台什么也做不了。
    """
    js = _visible_js()
    block = js[js.index("const asinTerms = ") : js.index("const nextLine = ")]
    assert "latestRun.asin_abstain_terms" in block, "词表没从运行流水里取"
    # 两条路径都要接上：asinLine 那句，和 ALL_ASIN 的 spec.next 那句。
    assert "asinWhich" in block, "部分 ASIN 弃权那句仍然只有数字"
    assert 'm.last_outcome === "ALL_ASIN" && asinUnique.length' in block, (
        "ALL_ASIN 那条路仍然只说「这些 ASIN」，说不出是哪几个"
    )
    #: 列出来之前必须去重（2026-09-07 实测）。asin_abstain_terms 是**逐行**记的，
    #  同一个 ASIN 投在两个广告组里就在里面出现两次，原样 join 出来是
    #  「B08XYZ1234、B08XYZ1234」——人读到的是两个 ASIN，实际只有一个。
    assert "new Set(asinTerms)" in block, "词表没去重，同一个 ASIN 会被念两遍"
    assert block.count("asinUnique.join(") >= 2, "两条路径里有一条没把词真的列出来"
    assert "asinTerms.join(" not in block, "还有一处在列未去重的原始词表"


def test_issuing_a_mandate_asks_first_and_reads_out_the_terms_it_hides() -> None:
    """签发前必须问一句，而且要念出「参数细则」里那些此刻还折叠着的数字。

    签发是这条流程里最先发生、也最要紧的终局动作：签完 AI 就能按它跑。批准、
    拒绝、撤销都问一句，唯独它不问（2026-09-07 第五轮排查）。它同时最容易被误触：
    表单里的单行输入按回车就是原生隐式提交，而 details#adv-wrap 默认不展开——
    人在「为什么签这份授权」里敲完一句顺手回车，就签下了一份自己一眼都没看过
    数字的授权。

    该念哪几个字段不写死在这里，从 index.html 的折叠区反推：往那里再挪进一个
    参数，这条测试就要求确认框把它也念出来。只写「确定签发吗」等于什么都没问。
    """
    html = _visible_html()
    at = html.index('id="adv-wrap"')
    collapsed = html[at : html.index("</details>", at)]
    assert "open" not in collapsed[: collapsed.index(">")], (
        "adv-wrap 已经默认展开，这条测试的前提变了"
    )
    hidden_fields = set(re.findall(r'name="([a-z_]+)"', collapsed))
    assert hidden_fields, "折叠区里一个带 name 的字段都没有——切法失效了"

    js = _visible_js()
    body = js[js.index("async function submitIssueForm(") :]
    body = body[: body.index("\n  }\n")]
    ask = body.index("window.confirm(")
    post = body.index('api("/mandates"')
    assert ask < post, "先发了请求才问——那句确认拦不住任何东西"

    #: 断言的是「被拼进了那句话」，不是「在附近出现过」——读一下变量不算念出来。
    #  确认框的实参是 terms / gates / dupLine 三个变量，它们就在这一段里构造。
    prompt = body[body.index("const windowText") : post]
    for field in sorted(hidden_fields):
        pinned = f"+ body.{field}" in prompt or f"body.{field} +" in prompt
        assert pinned, f"确认框没把折叠区里的 {field} 拼进文案"
    call = prompt[prompt.index("window.confirm(") :]
    for part in ("terms", "gates", "dupLine"):
        assert part in call, f"{part} 算了却没接到确认框上"

    #: 重复签发那句要并进同一个框。连弹两个确认，人只会连点两次「确定」。
    assert body.count("window.confirm(") == 1, "签发路径上不止一个确认框"


def test_the_count_on_a_candidate_set_is_not_called_a_number_of_words() -> None:
    """候选数数的是 (广告组 × 搜索词) 行，界面就不许叫它「N 个候选词」。

    去重键是 (ad_group_id, term)——同一个词投在三个广告组里就是三条候选
    （negation.py 的 evaluated_count 注释把这个区分写死了，域层还另存了
    distinct_search_terms）。2026-09-07 实测：4 条候选、只有 2 个不同的词。

    人拿「4 个候选词」去领星后台数词必然对不上；而他真要录的正是 4 条——每个
    广告组各录一条。数字没错，名词错了。两数不等时把词数也说出来，那正是他的
    预期会落空的那一刻。

    CSV 的行、表尾的合计、卡片上的计数、批准确认框里的那个数，是同一个数的四个
    出口，四处必须同一个口径——只改一处，人在另外三处照样对不上账。
    """
    js = _visible_js()
    assert "个候选词" not in js and "个否定词" not in js, "还有地方把 (广告组×词) 行叫成「个词」"

    helper = js[js.index("function candidateCountText(") :]
    helper = helper[: helper.index("\n  }\n")]
    assert "(广告组×词)" in helper, "卡片上的计数没说清单位"
    assert "new Set(" in helper and "search_term" in helper, "没数出到底有几个不同的词"
    assert "words < n" in helper, "两数不等时没把词数说出来——那正是人对不上账的时刻"

    foot = js[js.index('"合计 "') :]
    assert "(广告组×词)" in foot[: foot.index("\n")], "表尾合计还写着「个词」"

    approve = js[js.index("async function approveSet(") :]
    approve = approve[: approve.index("window.confirm")]
    assert "(广告组×词)" in approve, "批准确认框里的那个数还没说清单位"


def test_every_column_the_copy_sends_you_to_actually_exists() -> None:
    """文案里「见「X」列」引到的每个列名，表头必须真的渲染这几个字。

    与上面那条「控件必须存在」同族，同一种静默失效：指错了不报错、不变红，只让
    人在表头上找一个不存在的列，然后认定是自己没找到。2026-09-07 浏览器实测，
    授权书表的列是「目标 / 作用域·状态 / 上次运行·店铺·节奏与时段·配额·有效期·
    签发人·操作」，而配额格的悬停写着「见「运行时段」列」——没有这个列。
    """
    js = _visible_js()
    html = _visible_html()
    named = set(re.findall(r"见「([^「」]{1,20})」列", js))
    assert named, "一条指列文案都没有了——那这条守卫该删（治理规则：文书只准变短）"
    for label in sorted(named):
        assert f'el("th", {{ text: "{label}" }})' in js or f"<th>{label}</th>" in html, (
            f"文案让人去看「{label}」列，而表头从不渲染这个名字"
        )


def test_the_run_history_hover_does_not_count_abstains_as_judged() -> None:
    """整批弃权的那一轮，悬停不许同时写着「已判断 40 组」和「弃权 40」。

    evaluated_ad_group_terms 是**送进策略的行数**，弃权就在这个数里面——而弃权的
    定义正是「没能给出可执行的结论」（negation.py 的 AbstainRecord docstring）。
    两句话贴在同一段悬停里直接互相否定，人只能怀疑自己看错了。

    单位仍要是「组(广告组×搜索词)」：它与下一行的未判断数必须可比，这是上一轮
    修好的事，不能因为改动词又退回去。
    """
    js = _visible_js()
    body = _fn_body("mandateRunsRow", js)
    line = body[
        body.index("r.evaluated_ad_group_terms") - 40 : body.index("r.evaluated_ad_group_terms")
    ]
    assert "已判断" not in line, "把送进来的行数称作「已判断」，弃权也被算了进去"
    after = body[body.index("r.evaluated_ad_group_terms") :]
    assert "组(广告组×搜索词)" in after[: after.index("\n")], "单位丢了，与下一行的未判断数不再可比"


def test_the_run_history_hover_translates_its_error_code() -> None:
    """历史悬停里的错误码不许只有裸英文。

    同一个码，同一屏上方那条琥珀行已经用 ERROR_TEXT 译过一遍。这里留英文等于同
    一件事在同一屏上说两种语言——而人翻历史时看到的只有这一份。码要留着（它是人
    转给别人时唯一能对上的东西），后面得跟上人话。
    """
    js = _visible_js()
    body = _fn_body("mandateRunsRow", js)
    at = body.index('"错误码 "')
    tail = body[at : body.index("].filter(Boolean)", at)]
    assert "ERROR_TEXT[r.error_code]" in tail, "历史悬停里的错误码没翻译"
    assert "r.error_code" in tail, "翻译之后把码本身丢了——人转给别人时对不上"
