"""服务端能给出的每一种运行结局，界面都得有一句人话。

界面对结局码的处理是 `RUN_OUTCOME_TEXT[m.last_outcome] || { text: m.last_outcome }`
——查不到就把英文枚举名原样印在中文界面上，而那一行恰恰是催人去处理问题的琥珀条。
枚举加了一项、界面忘了跟，没有任何断言会红：新结局在演示数据里不出现，
只有真实店铺才走得到，人第一次看到它是在生产环境。

2026-09-06 加这道守卫的直接由头：ALL_ASIN 就是这样一项新结局，而它还顺带证明了
「有条目」并不够——它落进 `spec.ok` 的二分支里，两句现成的话
（「上次没跑通」「跑通了但没判断完」）对它都是假的。所以下面还钉了一条：
结局要么落进那两句里说得通，要么自带一句 `alert`。
"""

import re
from pathlib import Path

from ads_control_plane.strategies.mandate_run import NEEDS_ATTENTION, MandateRunOutcome

_APP_JS = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static/app.js"


def _outcome_dict() -> str:
    src = _APP_JS.read_text(encoding="utf-8")
    start = src.index("const RUN_OUTCOME_TEXT = {")
    end = src.index("\n  };", start)
    return src[start:end]


def test_every_outcome_the_server_can_emit_has_a_sentence_in_the_ui() -> None:
    block = _outcome_dict()
    keys = set(re.findall(r"^\s{4}([A-Z_]+):", block, flags=re.MULTILINE))
    missing = {o.value for o in MandateRunOutcome} - keys
    assert not missing, f"这些结局码在界面上会印成英文枚举名：{sorted(missing)}"


def test_the_ui_dictionary_does_not_invent_outcomes_the_server_never_sends() -> None:
    # 反方向同样要钉：界面里躺着一个服务端永不发送的码，读代码的人会以为它还会出现，
    # 照着它去改文案、去排查，而那段代码一次都跑不到。
    block = _outcome_dict()
    keys = set(re.findall(r"^\s{4}([A-Z_]+):", block, flags=re.MULTILINE))
    assert keys <= {o.value for o in MandateRunOutcome}


def test_an_outcome_that_needs_attention_says_what_to_do_about_it() -> None:
    """催人干活的结局必须给下一步。只说原因等于把排查原样丢回给人。"""
    block = _outcome_dict()
    for outcome in NEEDS_ATTENTION:
        entry = re.search(
            rf"^\s{{4}}{outcome.value}:\s*(\{{.*?\}},$)", block, flags=re.MULTILINE | re.DOTALL
        )
        assert entry is not None, f"{outcome.value} 在词典里找不到"
        assert "next:" in entry.group(1), f"{outcome.value} 没有下一步动作"


def test_an_outcome_that_ran_fine_but_still_needs_a_human_brings_its_own_headline() -> None:
    """琥珀行的标题是二分的：ok 就说「跑通了但没判断完」，否则说「上次没跑通」。

    一个「跑通了、也判断完了、但人还有事要做」的结局落进这个二分里，两句都是假话。
    这种结局必须自带 alert；不带就说明它正在用一句假话催人。
    """
    block = _outcome_dict()
    for outcome in NEEDS_ATTENTION:
        entry = re.search(
            rf"^\s{{4}}{outcome.value}:\s*(\{{.*?\}},$)", block, flags=re.MULTILINE | re.DOTALL
        )
        assert entry is not None
        body = entry.group(1)
        if "ok: true" in body:
            assert "alert:" in body, (
                f"{outcome.value} 既标了 ok 又要人动手，"
                "却没自带标题——琥珀行会说「跑通了但没判断完」"
            )


# ---------- 2026-09-07 第四轮：上一轮修弃权文案时新造出来的四处不一致 ----------

_UI2 = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static"


def _js_no_comments() -> str:
    src = (_UI2 / "app.js").read_text(encoding="utf-8")
    out = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", " ", out)


def _fn(name: str) -> str:
    js = _js_no_comments()
    start = js.index("function " + name + "(")
    open_brace = js.index("{", js.index(")", start))
    depth = 0
    for i in range(open_brace, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[open_brace : i + 1]
    raise AssertionError(name + " 花括号没配平")


def test_the_asin_next_step_is_not_tied_to_one_outcome() -> None:
    """ASIN 型弃权要做的事不随结局改变，所以它不许挂在某一个结局分支上。

    上一版写死 NO_CANDIDATES，漏掉同样可达的另一格：一轮里全部弃权、而弃权里只有
    一部分是 ASIN——那一格落 ALL_ABSTAINED，界面只说「等新数据」，而那几个 ASIN
    等多久都不会变；只跑过一次时连历史条都不画，它们在整个界面上一个字都不出现。
    """
    body = _fn("mandateAlertRow")
    asin = body[body.index("const asinLine") : body.index("const restToCompare")]
    assert "NO_CANDIDATES" not in asin, "ASIN 那句不许被某一个结局挡住"
    # ALL_ASIN 自己的 next 说的就是这句，不重复一遍。
    assert 'm.last_outcome !== "ALL_ASIN"' in asin


def test_it_does_not_send_you_comparing_an_empty_set() -> None:
    """弃权全是 ASIN 时「跟下一轮比」的对比对象是空集——派出去纯属白跑，
    还顺带暗示「可能是源侧数据的问题」这个本轮毫无证据的方向。
    """
    body = _fn("mandateAlertRow")
    assert "const otherAbstains" in body
    rest = body[body.index("const restToCompare") : body.index("const baseLine")]
    assert "otherAbstains > 0" in rest
    assert "unjudged_ad_group_terms" in rest and "unattributable_rows" in rest
    #: 定义对了不等于接上了（2026-09-07 变异实测）。上一版只钉了 restToCompare
    #  这个常量算得对，没钉它真的挡在判据上——把 baseLine 上的 `&& restToCompare`
    #  整个删掉，缺陷原样复活，这条测试照样绿。闸门要钉在它生效的那一处。
    cond = body[body.index("const baseLine =") :]
    cond = cond[: cond.index("?")]
    assert "restToCompare" in cond, "闸门定义了却没接到 baseLine 的判据上"


def test_a_fetch_failure_is_not_told_that_retrying_is_pointless() -> None:
    """取数失败是唯一一种**有条件**可重试的结局。

    它自己的 next 就写着「超时类可以再发起一次，参数类错误重试永远不会成功」，
    再拼上那句绝对的「照原样再跑一次会是同一个结果」，两句紧挨着直接互相否定，
    而超时时重试正是唯一正确的动作——界面却拿配额吓阻，人不重试，授权一直空转。
    """
    body = _fn("frozenEmptyState")
    tail = "照原样再跑一次会是同一个结果"
    at = body.index('latest.last_outcome === "SOURCE_ERROR"')
    #: 「判据出现在那句话之前」这个断言在三元表达式里恒真——`c ? A : B` 的 c
    #  永远排在 A 和 B 前面，所以把两个分支体对调、让 SOURCE_ERROR 恰好拿到这句
    #  绝对断言，它照样绿（2026-09-07 变异实测）。要钉的是**这一支印的是什么**，
    #  于是把真值支切出来单看：区间内 ASCII 冒号只有三元的那一个，文案里用的是
    #  全角「：」。
    hit = body.index("?", at)
    arm = body[hit : body.index(":", hit)]
    assert tail not in arm, "取数失败这一支印了「再跑也是同一个结果」——超时时重试恰恰是对的"
    assert tail in body, "这句对其余结局是真话，不该整个删掉"


def test_the_warning_mark_on_a_run_chip_explains_itself() -> None:
    """⚠ 由弃权触发时，悬停里不能印「未判断 0 组」——那句话在人眼里就是「没漏判」，
    正好否定了旁边那个 ⚠（2026-09-07 排查：上一版只改了判据，没改依赖它的悬停三元支）。
    """
    body = _fn("mandateRunsRow")
    assert "const unjudged =" in body and "const partlyAbstained =" in body
    # 「未判断 N 组」这句只许由 unjudged 触发，不许由弃权触发。
    line = body[body.index("未判断 ") - 200 : body.index("未判断 ")]
    assert "unjudged" in line and "incomplete" not in line.split("unjudged")[-1]
    #: 弃权触发的 ⚠ 要有自己的一句解释，而这句只许在**没有**未判断组时出现。
    #  上一版只断言这句话在函数里存在，没断言它由什么守卫——把 `!unjudged &&`
    #  去掉，两句会同时印出来，人得到「未判断 3 组」和「⚠ 指的是那 2 条弃权」，
    #  两个不同的原因认领同一个记号，而这条测试照样绿（2026-09-07 变异实测）。
    at = body.index("⚠ 指的是上面那 ")
    guard = body[body.rindex(': "",', 0, at) : at]
    assert "!unjudged && partlyAbstained" in guard, "弃权那句没被 !unjudged 守住"


def test_auto_pull_says_something_when_it_gives_up_at_the_round_cap() -> None:
    """自动拉取撞上硬上限时必须开口，且不许把人主动叫停也说成撞上限。

    do{...}while(... < 上限) 的循环条件一假就静静退出。撞上限后界面只剩一条还写着
    「继续拉取一轮」的续拉条，与人自己点了「停止」之后的样子逐像素相同——人看到
    的是「自动拉取直到拉全」跑了很久然后停了，不知道该再点一次、还是该去查为什么。

    另一半是「别说错」：叫停标志在 finally 里会被复位，判定必须发生在复位之前，
    否则人点了停止反而收到一句「跑满上限」。
    """
    body = _fn("wbSyncAll")
    assert "AUTO_SYNC_MAX_ROUNDS" in body, "轮数上限要有名字，循环条件和播报才不会各写一个 300"
    assert re.search(r"wb\.autoSyncRound\s*<\s*AUTO_SYNC_MAX_ROUNDS", body), "循环条件没用这个常量"

    # 判定必须写在 do-while 之后、finally 之前——finally 会把 autoSyncStop 复位。
    after_loop = body[body.index("AUTO_SYNC_MAX_ROUNDS);") :]
    decide = after_loop[: after_loop.index("} finally {")]
    assert "autoSyncRound >= AUTO_SYNC_MAX_ROUNDS" in decide, "撞上限这件事没有在 finally 之前判"
    assert "!wb.autoSyncStop" in decide, "没排除人主动叫停，点了停止会收到「跑满上限」"

    # 撞上限之后要有一句，且这句要给得出下一步。
    tail = body[body.index("} finally {") :]
    assert "if (hitCap)" in tail, "撞上限之后没有任何分支——这条路径静静退出，什么都不说"
    cap_branch = tail[tail.index("if (hitCap)") :]
    cap_branch = cap_branch[: cap_branch.index("\n    }")]
    assert "showAlert" in cap_branch, "撞上限后一句话都不说"
    assert "AUTO_SYNC_MAX_ROUNDS" in cap_branch, "播报里的轮数是另写的字面量，会和循环条件漂开"
    assert "自动拉取直到拉全" in cap_branch, "只说停了、没说下一步该点哪儿"


def _ternary_arm(body: str, needle: str) -> str:
    """取出「印出 needle 的那一支」自己的判据。

    切法不靠固定字数窗口：从 needle 往回找最近的 `?`，那是这一支的问号；再从
    问号往回找最近的 `:`（或表达式起点），中间那段就是判据本身。文案里的冒号
    与问号都是全角，不会误伤。
    """
    assert body.count(needle) == 1, f"{needle!r} 在这个函数里不唯一，切出来的判据靠不住"
    at = body.index(needle)
    # +1 让 needle 自带的问号也算数：needle 以 `?` 开头时钩点就是它自己，
    # 否则往前找到紧邻的那个。差这一位会切到**上一支**的判据上。
    hook = body.rindex("?", 0, at + 1)
    start = body.rindex(":", 0, hook)
    return body[start:hook]


def test_a_run_that_produced_candidates_still_says_where_to_approve_them() -> None:
    """一轮里既产出候选、又有 ASIN 弃权时，「去『待批』页签审批」不许被吞掉。

    压掉 spec.next 的那一支原本判的是 spec.ok。ok:true 的结局有三个：ALL_ASIN 的
    asinLine 恒空走不到那里，NO_CANDIDATES 的 next 是「不用做什么，这段窗口确实
    干净」——正是 ASIN 那句在否定的话，压掉它是对的；连坐的是 CANDIDATES，而它的
    next 是这一行唯一一句把人送到待批的话（2026-09-07 第四次排查）。三件事可以
    同时发生：产出候选、有 ASIN 弃权、有读不出归属的行（最后一条让琥珀行现身）。
    人读完只知道去领星否 ASIN，那批候选 72 小时后自己过期。
    """
    body = _fn("mandateAlertRow")
    guard = _ternary_arm(body, '? ""')
    assert "NO_CANDIDATES" in guard, "压掉下一步的那一支没有点名结局，会连坐 CANDIDATES"
    assert "spec.ok" not in guard, "spec.ok 把 CANDIDATES 也圈进去了——它的下一步不能压"


def test_it_does_not_say_a_batch_was_handled_when_the_list_never_loaded() -> None:
    """取集合失败时不许断言「这一批已经处理掉了，不用再过去」。

    loadSets 的 catch 把 state.sets 清成空数组，stillPending 随之为 false——而这
    恰恰是待批页签自己也打不开、只剩这一行在说话的时刻。人照做不去，那批候选
    72 小时后自己作废。不知道就别断言：白跑一趟可以回头，不去不行。
    """
    body = _fn("mandateAlertRow")
    guard = _ternary_arm(body, "这一批已经处理掉了")
    assert "setsKnown" in guard, "没确认待批列表真的加载成功过，就断言了它是空的"
    known = body[body.index("const setsKnown") :]
    assert 'state.setsStatus === "ok"' in known[: known.index("\n")], (
        "setsKnown 必须来自加载状态本身；从 state.sets 是空数组反推不出「加载成功且为空」"
    )
