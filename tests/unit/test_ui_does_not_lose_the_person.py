"""滚过去要看得见，按了回车不能把人扔回文档开头，空态不许和同屏的另一行打架。

2026-09-06 第二轮重审的三条，共同点是「操作确实发生了，但人被丢下了」：

- 滚动/锚点跳转的目标（#issue-wrap / #mandate-list / #sets-title / #mandates-title）
  都没带 scroll-margin-top。写着避让的那条 CSS 挂在 `.panel` 上，而 .panel 只是
  它们的祖先——scroll-margin 只对滚动目标本身生效，实测四个全是 0px。于是
  「签发成功 → 滚到新签的那一行」把那一行推到 116px 高的 sticky 顶栏底下。
- 排序表头与绩效分桶按钮都在 refreshWorkbench 里被整体重建，键盘按回车之后
  焦点掉回 <body>：只用键盘的人每排一次序，就要重新 Tab 穿过整个页面回到那一列。
  审计 #50 特意给表头补过回车/空格，补完却在同一动作里把焦点丢掉。
- 拒掉唯一一批候选之后，待批空态说「还没有人发起过运行」，而同屏的授权书行
  写着「上次运行 产出候选」。两句话直接矛盾，人只能怀疑刚才那次拒绝没生效。

三条都在 8791（纯 Mock）上实测过：scrollMarginTop 0px→129px、
焦点 BODY→TH[spend]、空态改口。下面钉的是结构，不是措辞。
"""

import re
from pathlib import Path

_UI = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static"
_APP_JS = _UI / "app.js"
_INDEX = _UI / "index.html"
_CSS = _UI / "style.css"

#: app.js 里用 scrollIntoView({block:"start"}) 主动滚过去的目标。block:"nearest"
#  与 block:"center" 的不算——它们不会把目标顶到视口最上沿，顶栏遮不住它。
#  这一个是逐一核对写下的，下面那条计数守卫负责在有人新增第二个时把这份清单叫醒。
_JS_SCROLL_TARGETS = ("issue-wrap",)


def _strip_comments(text: str, block_only: bool = False) -> str:
    out = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return out if block_only else re.sub(r"(?<!:)//[^\n]*", " ", out)


def _anchor_targets() -> set[str]:
    """index.html 里 href="#..." 跳过去的目标——原生锚点跳转同样吃 scroll-margin。"""
    return set(re.findall(r'href="#([\w-]+)"', _INDEX.read_text(encoding="utf-8")))


def test_every_place_the_page_scrolls_you_to_clears_the_sticky_topbar() -> None:
    css = _strip_comments(_CSS.read_text(encoding="utf-8"), block_only=True)
    rule = re.search(r"([^{}]*?)\{\s*scroll-margin-top:([^;]+);\s*\}", css)
    assert rule is not None, "避让 sticky 顶栏的那条规则不见了"
    selectors = {s.strip() for s in rule.group(1).split(",")}
    targets = _anchor_targets() | set(_JS_SCROLL_TARGETS)
    assert targets, "一个滚动目标都没解析出来，这条守卫等于没写"
    for target in sorted(targets):
        assert "#" + target in selectors, (
            "页面会把 #" + target + " 顶到视口最上沿，但避让顶栏的规则没有选中它——"
            "它会被 sticky 顶栏整个盖住"
        )
    # 顶栏高度实测写入，不写死：身份行在窄屏会换行，任何固定数字都有一档是错的。
    assert "var(--topbar-h" in rule.group(2)
    assert '.setProperty("--topbar-h"' in _APP_JS.read_text(encoding="utf-8")


def test_a_new_scroll_target_has_to_be_added_to_that_list() -> None:
    #: 上面那份 JS 目标清单是手抄的。新增一个 block:"start" 的滚动点而忘了加进去，
    #  症状是"滚过去了但看不见"——不会报错。这条计数守卫让它变成一次红灯。
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    starts = re.findall(r"\.scrollIntoView\(\{[^}]*block: \"start\"", js)
    assert len(starts) == 2, (
        'block:"start" 的滚动点变成了 ' + str(len(starts)) + " 处（原本 2 处，"
        "都指向 " + "/".join(_JS_SCROLL_TARGETS) + "）——把新的那个也加进 "
        "_JS_SCROLL_TARGETS 和 style.css 的避让规则里"
    )


def test_signing_scrolls_to_the_row_you_just_signed_not_to_the_table_head() -> None:
    """「滚过去让你看到新签的那一行」得真的滚到那一行（2026-09-07 排查）。

    上一版滚的是 #mandate-list，落点是表头；而新行按签发时间排在表尾，行一多
    它就在视野之外——绿条那句「点该行的『复制指令』」仍然落空，而写在旁边的
    注释却已经宣布人看见了它。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    block = js[js.index("if (issuedId) {") :][:500]
    assert 'data-mandate-id="' in block, "必须按新授权书的 ID 找到那一行"
    assert 'closest("tr")' in block


def test_pressing_enter_on_a_sort_header_does_not_dump_you_at_the_top_of_the_page() -> None:
    src = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    body = src[src.index("async function refreshWorkbench()") :]
    body = body[: body.index("\n  }\n")]
    # 重建前记住站位、重建后站回去，缺一不可。
    assert "const focusKey = wbFocusKey();" in body
    assert "wbRestoreFocus(focusKey);" in body
    #: 承重的是「在**重建之后**」，不是「在取键之后」（2026-09-07 排查：上一版
    #  断的是后者，而把复位调用挪到任何一个 renderWb*() 之前都会让缺陷原样复活，
    #  测试照样全绿——重建会把复位刚放好的焦点再删一次）。
    renders = [m.start() for m in re.finditer(r"\n    renderWb\w+\(\);", body)]
    assert renders, "refreshWorkbench 里已经没有重渲染调用了，这条守卫该重写"
    assert body.index("wbRestoreFocus(focusKey)") > renders[-1], (
        "焦点复位必须排在最后一个重渲染之后，否则复位好的焦点会被下一次重建删掉"
    )


def test_an_empty_queue_does_not_contradict_the_row_right_above_it() -> None:
    #: 比对前先剥注释——解释这条修复的那段注释里正好写着这句被禁的话。
    src = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    body = src[src.index("function frozenEmptyState()") :]
    body = body[: body.index("\n  }\n")]
    # 「从没跑过」这句话必须被 run_count_known 挡在后面；跑过的分支要先走。
    assert "run_count_known" in body
    assert body.index("run_count_known") < body.index("还没有人发起过运行")
    #: 光有先后顺序不够（2026-09-07 排查：上一版就到此为止，而被它放行的那句话
    #  当时正在说假数）。跑过的那句里报的必须是**跑过的份数**，不是全部生效授权数——
    #  3 份生效、1 份跑过时，说「3 份都跑过了」，而同屏另外两行写着「还没跑过」。
    ran_sentence = body[body.index("if (ran.length > 0)") : body.index("还没有人发起过运行")]
    phrase = ran_sentence[ran_sentence.index("const ranPhrase") :]
    phrase = phrase[: phrase.index(";")]
    # 只有两个数相等时才允许说「N 份都跑过了」；不等时那句必须报跑过的份数。
    assert "ran.length === live.length" in phrase, "两个数不等时必须分开说"
    differs = phrase[phrase.index(":") :]
    assert "ran.length" in differs, "不等的那一支里必须出现跑过的份数"
    #: 下一步要跟着结局说。没跑通的那几种（币种签错、没接数据源、作用域全挡掉）
    #  照原样再跑一次必然是同一个失败，而每跑一次都真扣一次日配额。
    assert "spec.next" in ran_sentence, "没跑通的结局必须给出它自己的下一步，不能一律说「再跑一次」"


def test_the_number_on_a_tab_equals_the_rows_behind_it() -> None:
    """页签上的数必须等于点进去看到的行数。

    两条都会让人去找不存在的卡片：「已拒」此前数的是 state.sets 全集，不跟店铺
    筛选走（筛到 B 店时写着全店的数）；「待批」此前用的是 KPI 那个数（扣掉过期与
    来源授权书已撤销的），而列表照样把它们画出来——页签写 0、屏幕上 1 张卡。
    2026-09-06 在 8791 实测：撤销授权书后页签「待批（1）」对着 1 张卡片，
    KPI 仍是 0 并在副文案里说明，页签 title 说清差在哪。
    """
    src = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    body = src[src.index("function renderKpis()") :]
    body = body[: body.index("\n  }\n")]
    rows = re.search(r"const tabRows = \{(.*?)\};", body, re.DOTALL)
    assert rows is not None, "页签计数表不见了"
    table = rows.group(1)
    # 三个页签都从 scoped（跟着店铺筛选）里数，一个都不许直接读 state.sets。
    assert "state.sets" not in table, "页签计数不许绕开店铺筛选去读全集"
    # 待批数的是这一层全部 FROZEN 行，不扣不能批的那些——扣掉的是 KPI，不是页签。
    assert "FROZEN: frozen.length" in table
    #: 三个值里有两个是外面绑定的标识符，只扫这段字面量等于只查了三分之一
    #  （2026-09-07 排查）。把它们的定义一起钉上，否则 approved 改成读全集仍然全绿。
    assert "const scoped = state.sets.filter(matchesProfileFilter);" in body
    assert 'const frozen = scoped.filter((s) => s.state === "FROZEN");' in body
    assert 'const approved = scoped.filter((s) => s.state === "APPROVED").length;' in body


def test_the_page_fits_a_narrow_screen_instead_of_scrolling_sideways() -> None:
    """窄屏下整页横向撑开——不是「表格宽」，是整页宽。

    2026-09-06 在 375px 实测 document.scrollWidth = 934：grid 子项的默认
    min-width:auto 不肯缩到自身 min-content 以下，而 min-content 由最宽的数据表和
    那段 `codex mcp add …` 命令决定。两者各自都带着 overflow-x:auto，本该在自己
    框里横滚；子项不肯缩，那道内部滚动就永远用不上，改由整个 <body> 横滚——
    每一屏内容都要左右推着看。加上三条 min-width:0 后实测 934 → 375，
    表格与命令块各自恢复内部横滚。
    """
    css = _strip_comments(_CSS.read_text(encoding="utf-8"), block_only=True)
    rule = re.search(r"([^{}]*?)\{\s*min-width:\s*0;\s*\}", css)
    assert rule is not None, "让 grid 子项能缩的那条规则不见了——窄屏会整页横滚"
    selectors = {s.strip() for s in rule.group(1).split(",")}
    for host in (".container > *", ".kpi-strip > *", ".list-region > *"):
        assert host in selectors, host + " 不在能缩的名单里"


def test_the_selection_drawer_measures_the_action_bar_instead_of_guessing() -> None:
    """抽屉的位置不许再写死一个会过期的数字。

    2026-09-06 在 375px 实测动作栏高 405px，而抽屉的 bottom 写着 132px——
    那是照着宽屏量的。抽屉于是压在栏上，盖掉「理由」和「数值」这两个必填框，
    而它们不填就提交不了。栏高早已由 syncActionbarHeight 实测写入变量。
    """
    css = _strip_comments(_CSS.read_text(encoding="utf-8"), block_only=True)
    block = css[css.index(".wb-drawer {") :]
    block = block[: block.index("}")]
    assert "var(--wb-actionbar-h" in block, "抽屉的 bottom 必须跟着实测的动作栏高度走"


def test_the_drawer_can_be_left_with_the_keyboard() -> None:
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    body = js[js.index("function wbToggleDrawer(") :]
    body = body[: body.index("\n  }\n")]
    #: 要查的是「哪一句在哪个分支里」，不是「这两句在不在函数里」（2026-09-07 排查：
    #  上一版只查存在性，把两句对调即可复现原缺陷且仍然全绿）。
    open_branch = body[body.index("if (open)") : body.index("if (!mayTakeFocus)")]
    assert '$("wb-drawer-close").focus()' in open_branch, "打开时焦点必须送进抽屉"
    assert "return;" in open_branch, "打开分支要在这里收住，别继续走关闭那一段"
    #: 「焦点在 <body>」必须算进可接管的那一格：全局委派点击在分发前会把被点的按钮
    #  disabled 掉，而禁用一个正被聚焦的元素会让浏览器当场把焦点丢回 <body>——
    #  只看 contains 的话，抽屉里的「×」走到这里时会被误判成「人在别处」（2026-09-07 实测）。
    guard = body[body.index("const mayTakeFocus") : body.index("drawer.hidden = !open")]
    assert "a === document.body" in guard
    assert "drawer.contains(a)" in guard
    #: 关闭时的落点要逐个验过：#wb-selected-count 会随动作栏一起 hidden，
    #  #wb-check-all 在表格空态/加载中/失败态里根本不渲染、全托管时是 disabled。
    #  focus() 打在 null 或 disabled 上都是空操作，焦点照样掉回 <body>。
    close_branch = body[body.index("if (!mayTakeFocus)") :]
    assert "offsetParent !== null" in close_branch, "落点必须先确认它真的在屏上"
    assert "!n.disabled" in close_branch, "落点必须先确认它没被禁用"
    assert '$("wb-f-name")' in close_branch, (
        "最后的落点必须是一个静态节点——#wb-check-all 有 5 种渲染态里不存在"
    )
    # Escape 必须能关，但只在焦点属于抽屉时——它是非模态浮层，页面其余部分照常可用。
    assert 'ev.key !== "Escape"' in js
    #: 切片必须收在这个 keydown 处理器自己的花括号里（2026-09-07 排查：上一版取的是
    #  固定 700 字符，顺序断言绑到了隔壁 wb-drawer-close 处理器的调用上，
    #  本提交把 wbToggleDrawer 提到模块作用域之后它就成了空壳）。
    esc = js[js.index('ev.key !== "Escape"') :]
    esc = esc[: esc.index("\n    });")]
    assert '$("wb-drawer").contains(a)' in esc, (
        "Escape 必须先问焦点归属，否则会把焦点从动作栏的必填输入框里抢走"
    )
    assert "if (!mine) return;" in esc
    assert esc.index("if (!mine) return;") < esc.index("wbToggleDrawer(false)")


def test_any_path_that_empties_the_selection_closes_the_drawer() -> None:
    """清零 → 动作栏收起 → 抽屉的开关按钮跟着消失，留一个人回不去的空抽屉。

    清零的路不止一条：抽屉里的「清空全部」、动作栏的「清空勾选」、抽屉里逐个「×」
    或「移除本组」。上一版只在「清空全部」的处理器里关抽屉，另外三条原样留着空壳
    （2026-09-07 排查）。规矩要钉在勾选数变化的必经之路上。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    bar = js[js.index("function renderWbActionbar()") :]
    bar = bar[: bar.index("\n  }\n")]
    stmt = 'if (n === 0 && !$("wb-drawer").hidden) wbToggleDrawer(false);'
    assert stmt in bar
    #: 写在函数里不等于跑得到（2026-09-07 变异实测）。上一版只钉了这条语句的字面，
    #  在它前面插一句 `if (true) return;` 缺陷就原样复活，而测试照样绿。这个函数是
    #  直线的、一个 return 都没有——把这件事本身钉住，将来谁加早退谁就红。
    assert "return" not in bar[: bar.index(stmt)], "这条语句前面出现了 return，它可能跑不到"


def test_the_table_is_rebuilt_before_the_bar_that_hands_focus_back() -> None:
    """反过来的话焦点会被交给一个下一行就要删掉的节点（2026-09-07 实测）。

    renderWbActionbar 里那条「清零就关抽屉」会把焦点放到 #wb-check-all 上，
    而 renderWbTable 紧接着重建表格、连同那个节点一起删掉——焦点照样掉回 <body>。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    body = js[js.index("function wbClearSelection()") :]
    body = body[: body.index("\n  }\n")]
    assert body.index("renderWbTable();") < body.index("renderWbActionbar();")
    # 抽屉里的两个「移除」入口同此。
    for action in ('d.action === "wb-drop-one"', 'd.action === "wb-drop-level"'):
        blk = js[js.index(action) :]
        blk = blk[: blk.index("} else if")]
        assert blk.index("renderWbTable();") < blk.index("renderWbActionbar();"), action


def _css_rule(css: str, selector: str) -> str:
    """取出某条规则自己的 {...}，不靠固定字数窗口。

    此前这里用的是「从选择器往后数 400 个字符」——那段窗口会越过规则边界，
    断言被**后面无关的规则**满足，于是删掉整条媒体查询它照样绿。
    """
    at = css.index(selector)
    start = css.index("{", at)
    return css[start : css.index("}", start) + 1]


def test_the_drawer_stays_inside_the_viewport_on_every_screen() -> None:
    """抽屉的上下两边都不许出视口，而判据必须是**竖向余量**，不是屏幕宽度。

    2026-09-07 第三次排查，浏览器实测（不是读源码）：
    · 844×390 横屏手机：宽度 844 过了 768 那道线，走不到浮层分支，抽屉
      top = -54px，唯一那颗「关闭」整个在视口外（top -44 / bottom -17）。
      此前为救高度加的 max(180px, …) 下界救不了上边缘——它只保证高度，
      而高度撑住了顶边就被顶出去。
    · 768×1024 iPad 竖屏：宽度压线走进浮层分支，被整页盖住 86% 视高，
      动作栏里「数值」「理由」两个必填框被抽屉压住点不到——elementFromPoint
      在它们中心返回 wb-drawer——可这块屏竖向还剩六百多像素。

    宽度两个方向都判错，因为它只是「竖向够不够」的替身。而余量本来就是量出来的
    （--wb-actionbar-h / --topbar-h），判据就该用它。
    """
    css = _strip_comments(_CSS.read_text(encoding="utf-8"), block_only=True)
    base = _css_rule(css, ".wb-drawer {")
    mh = base[base.index("max-height:") :]
    assert "var(--wb-actionbar-h" in mh and "var(--topbar-h" in mh, (
        "抽屉高度必须扣掉动作栏与顶栏实测占用的那两段，不能只写 52vh"
    )
    assert not mh.lstrip().startswith("max-height: max("), (
        "不许给高度加下界：撑住高度就会把顶边连同「关闭」按钮顶出视口"
        "（844×390 实测 top=-54px）。余量不够时该换浮层，不是硬撑。"
    )

    # 浮层那一档必须存在，且必须由实测的类触发，不能再由宽度媒体查询触发。
    assert "@media (max-width: 768px)" not in css, (
        "宽度不是竖向余量的替身：768×1024 的 iPad 竖屏会被它误判成没地方"
    )
    overlay = _css_rule(css, "body.wb-drawer-overlay .wb-drawer")
    for prop in ("top:", "bottom:", "left:"):
        assert prop in overlay, f"浮层那一档没有钉 {prop}，抽屉不会真的铺开"
    assert "max-height: none" in overlay, "浮层还留着高度上限，等于没铺开"

    # 那个类必须由 JS 按实测余量开合，否则 CSS 里这条规则永远不会生效。
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    fit = js[js.index("function syncDrawerFit()") :]
    fit = fit[: fit.index("\n  }\n")]
    assert "clientHeight" in fit, "余量没从视口高度算起"
    assert "--wb-actionbar-h" in fit and "--topbar-h" in fit, "余量没扣掉实测的那两段"
    assert "wb-drawer-overlay" in fit, "算了余量却没有开合那个类"
    # 量完就要重判——两个测量函数都必须把结果喂给它。
    for fn in ("function syncActionbarHeight()", "function syncTopbarHeight()"):
        blk = js[js.index(fn) :]
        blk = blk[: blk.index("\n  }\n")]
        assert "syncDrawerFit()" in blk, f"{fn} 量完没有重判浮层档，判据会停在上一次的数"


def _async_fn(name: str, js: str) -> str:
    """抽出 `async function <name>(` 的函数体，按缩进边界收口。

    与上面几条一样按 `\n  }\n` 收——app.js 里所有模块级函数都缩进两格。
    """
    body = js[js.index("async function " + name + "(") :]
    return body[: body.index("\n  }\n")]


def test_approving_a_batch_does_not_dump_the_keyboard_user_at_the_top() -> None:
    """批/拒/撤销之后，焦点必须落回一个说得出「它去哪了」的控件上。

    2026-09-07 实测：Tab 到「批准」、回车、确认 → 焦点 BODY。全局委派点击在分发前
    就把按钮 disabled（防双击），浏览器当场把焦点丢回 <body>；refreshAll 再把整张
    列表重建一遍，那颗按钮连同位置一起消失。于是只用键盘的人批完一份候选，下一次
    Tab 是从文档最顶上重来——而提示恰好在说「可在「已批」页签导出 CSV」。
    工作台侧早有同一套治法（wbToggleDrawer 的 mayTakeFocus / wbRestoreFocus），
    审批侧一直没有，而这边的动作不可逆。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    for fn, dest in (("approveSet", "APPROVED"), ("rejectSet", "REJECTED")):
        body = _async_fn(fn, js)
        call = 'focusSetTab("' + dest + '")'
        assert call in body, f"{fn} 之后没有人管焦点——键盘用户被扔回文档开头"
        assert body.index("await refreshAll()") < body.index(call), (
            f"{fn} 在列表重建之前就交出了焦点，那个节点下一行就会被删掉"
        )
    revoke = _async_fn("revokeMandate", js)
    assert "focusMandateRow(mandateId)" in revoke, "撤销之后同样把人丢在文档开头"
    assert revoke.index("await refreshAll()") < revoke.index("focusMandateRow(mandateId)")


def test_the_approval_focus_guard_counts_body_as_focus_we_may_take() -> None:
    """守卫必须把 <body> 算进「可接管」，否则它一次都不会生效。

    委派处理器的 `btn.disabled = true` 会把焦点丢回 <body>，所以真到这一步时
    activeElement 恒为 body——只看 contains 的话守卫恒为假，整条修复是死的
    （与抽屉那条同一个坑，2026-09-07 实测）。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    for fn, container in (("focusSetTab", "set-list"), ("focusMandateRow", "mandate-list")):
        body = js[js.index("function " + fn + "(") :]
        body = body[: body.index("\n  }\n")]
        guard = body[: body.index("if (!mine) return;")]
        assert "a === document.body" in guard, f"{fn} 的守卫漏了 <body>，它恒为假"
        assert '$("' + container + '").contains(a)' in guard, (
            f"{fn} 必须只在焦点原本属于这块区域时才接管"
        )
        assert "offsetParent !== null" in body, f"{fn} 的落点没验它真的在屏上"


def test_the_tab_the_approval_focus_lands_on_actually_exists() -> None:
    """focusSetTab 找的 data-state 必须是 index.html 真有的那几个。

    拼错一个字母不会报错、不会变红：querySelector 返回 null，focus() 落空，
    焦点照样掉回 <body>——修复看起来在，实际是死的。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    html = _INDEX.read_text(encoding="utf-8")
    real = set(re.findall(r'data-state="(\w+)"', html))
    assert real, "index.html 里已经没有按状态筛选的页签了"
    used = set(re.findall(r'focusSetTab\("(\w+)"\)', js))
    assert used, "一处都没有调用 focusSetTab——那这条守卫该删"
    assert used <= real, f"focusSetTab 指向不存在的页签：{sorted(used - real)}"


def test_a_result_panel_takes_the_focus_its_trigger_just_lost() -> None:
    """预览 / 历史面板挂上去之后，焦点必须落进面板里。

    2026-09-07 实测：焦点在 #wb-preview-btn 上按回车 → 面板渲染完 activeElement
    变成 BODY。触发按钮在请求在途时被 disabled（防双击），浏览器当场把焦点丢回
    <body>；面板挂在页面另一处，于是只用键盘的人要从文档最顶上 Tab 过来才够得到
    面板里的「下载变更清单 CSV」——而那是整条预览路唯一的产出。
    与 wbToggleDrawer 打开抽屉时是同一条规矩，两个面板此前都漏了。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    for fn in ("function renderWbPreview(", "async function wbShowHistory("):
        body = js[js.index(fn) :]
        body = body[: body.index("\n  }\n")]
        assert "wbFocusPanel(panel)" in body, f"{fn} 之后没有人管焦点"
        assert body.index("region.append(panel)") < body.index("wbFocusPanel(panel)"), (
            f"{fn} 在面板还没挂进文档时就 focus，focus() 打在离场节点上是空操作"
        )
    helper = js[js.index("function wbFocusPanel(") :]
    helper = helper[: helper.index("\n  }\n")]
    #: 与抽屉那条同一个坑：真到这一步时 activeElement 恒为 <body>，
    #  只看 contains 的话守卫恒为真/恒为假，整条修复是死的。
    assert "a !== document.body" in helper, "守卫没把 <body> 当成可接管的焦点"
    assert "panel.contains(a)" in helper
    assert "offsetParent !== null" in helper, "落点没验它真的在屏上"
    #: 落点必须是面板自己头部的那颗按钮。写成 panel.querySelector("button") 的话，
    #  哪天头部换个顺序或表格里先出现一颗按钮，焦点就落到半截表格里去了。
    assert ".wb-result-head button" in helper


def test_closing_a_result_panel_hands_focus_back_to_what_opened_it() -> None:
    """「打开时把焦点送进面板」只是一半，另一半是关掉时把它还回去。

    2026-09-07 实测：关掉面板 → activeElement 变成 BODY。那颗「关闭」就在面板里，
    面板一清它自己也没了，浏览器无处可放。于是只用键盘的人每关一次面板，就被扔回
    文档开头一次——而他多半是想接着看表格里的下一行。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    handler = js[js.index('d.action === "wb-close-history"') :]
    handler = handler[: handler.index('d.action === "wb-preview-csv"')]
    assert "wbFocusTrigger" in handler, "关掉面板之后没有人管焦点"
    assert '"#wb-preview-btn"' in handler, "预览面板关掉后要回到「生成预览」"
    assert 'data-action="wb-history"' in handler, "历史面板关掉后要回到那一行的「历史」"
    #: 历史的触发按钮长在表格行里，得知道是「为谁开的」才找得回去。
    assert "wb.historyFor" in handler, "不记住是为哪个对象开的，就找不回那一颗「历史」"
    show = js[js.index("async function wbShowHistory(") :]
    show = show[: show.index("\n  }\n")]
    assert "wb.historyFor = objectKey" in show

    helper = js[js.index("function wbFocusTrigger(") :]
    helper = helper[: helper.index("\n  }\n")]
    #: 必须现查选择器。存节点的话，表格重建之后那个节点早已离场，
    #  focus() 打在离场节点上是空操作，焦点照样掉回 <body>。
    assert "document.querySelector(selector)" in helper, (
        "wbFocusTrigger 得现查——存下来的节点在表格重建后已经离场"
    )
    assert "offsetParent !== null" in helper and "!node.disabled" in helper


def test_removing_one_item_from_a_list_leaves_you_standing_in_that_list() -> None:
    """删掉清单里的一项之后，焦点要留在同一位置的下一项上。

    2026-09-07 实测：签发表单里逐条核对作用域，每点一次「移除」就被扔回文档开头
    ——那颗按钮随清单一起重建消失，浏览器无处可放。而人多半正要接着删第二项。
    抽屉里逐个「×」同病。

    删空是单独一档：清单连同它旁边的「清空」一起隐藏，只给一个回退目标等于没给。
    实测删到 0 项时焦点应落在屏上那颗恢复动作（「把勾选的 N 个带进来」）上。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))
    handler = js[js.index('d.action === "scope-remove"') :]
    handler = handler[: handler.index('d.action === "wb-chip-drop"')]

    scope = handler[: handler.index('d.action === "wb-drop-one"')]
    assert "focusAfterRemoval(" in scope, "删掉作用域里一项之后没有人管焦点"
    assert scope.index("const at =") < scope.index("mandateDraft.scopeItems ="), (
        "位置必须在改数据之前取——重建之后原节点已经不在清单里了"
    )
    assert scope.index("renderScope()") < scope.index("focusAfterRemoval("), (
        "清单还没重建就交出焦点，那个节点下一行就会被删掉"
    )
    assert '"#scope-error button"' in scope, "删空之后要落到屏上那颗恢复动作上"

    drop_one = handler[handler.index('d.action === "wb-drop-one"') :]
    drop_one = drop_one[: drop_one.index('d.action === "wb-drop-level"')]
    assert "focusAfterRemoval(" in drop_one, "抽屉里删掉一项之后没有人管焦点"
    assert drop_one.index("const at =") < drop_one.index("wb.selected.delete"), (
        "位置必须在改数据之前取"
    )

    drop_level = handler[handler.index('d.action === "wb-drop-level"') :]
    #: 整组移除**不**落到下一组的「移除本组」上——那是把焦点放在一个回车就能再毁掉
    #  一整组的按钮上。
    assert "focusAfterRemoval(" not in drop_level, (
        "整组移除不该落到下一组的「移除本组」上——一个回车就再毁一组"
    )
    assert 'wbFocusTrigger("#wb-drawer-close")' in drop_level

    helper = js[js.index("function focusAfterRemoval(") :]
    helper = helper[: helper.index("\n  }\n")]
    #: 钉签名本身。只查 "...fallbacks" 会被函数体里的 `...fallbacks.map(...)` 满足，
    #  把参数改成单个值仍然全绿（2026-09-07 逆向验证抓到）。
    assert "function focusAfterRemoval(selector, index, ...fallbacks)" in helper, (
        "回退目标要收一串——删空时清单连同它旁边的「清空」一起隐藏，只给一个等于没给"
    )
    assert "offsetParent !== null" in helper and "!n.disabled" in helper


def test_the_numbers_the_drawer_stands_on_are_never_stale_or_missing() -> None:
    """抽屉钉在两个实测值上，那两个值必须**总是**量得到、且量得准。

    2026-09-07 浏览器实测的两处失效：

    · 顶栏高度关在早退后面。renderIdentityMeta 拿不到身份就 `if (!id) return`，
      而 syncTopbarHeight() 写在那之后——身份端点失败或名录为空时 --topbar-h
      永不写入，抽屉与四个滚动目标全按 116px 的回退值站着。顶栏在不在那儿，
      和名录取没取到没有关系。

    · 动作栏高度只在 renderWbActionbar 末尾量。切换「动作」下拉走的是
      wbSyncValueInput → renderWbPrecheck 这条路，量不到：375px 实测
      PAUSE → SCALE_BID 时真实栏高 385 → 319，而 --wb-actionbar-h 停在 385，
      抽屉顶边纹丝不动，与栏之间凭空空出 66px（反向切则是压住内容）。
    """
    js = _strip_comments(_APP_JS.read_text(encoding="utf-8"))

    meta = js[js.index("function renderIdentityMeta()") :]
    meta = meta[: meta.index("\n  }\n")]
    assert meta.index("syncTopbarHeight()") < meta.index("if (!id) return"), (
        "量顶栏排在早退之后：身份名录取不回来时 --topbar-h 永不写入"
    )

    pre = js[js.index("function renderWbPrecheck()") :]
    pre = pre[: pre.index("\n  }\n")]
    assert "syncActionbarHeight()" in pre, "预检行会改变栏高，量不到就让抽屉钉在上一次的数上"
