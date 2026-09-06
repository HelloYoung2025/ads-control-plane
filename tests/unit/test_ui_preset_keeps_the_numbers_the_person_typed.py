"""「打法卡」不许悄悄换掉人自己定的数，也不许拿人没选的卡当基准。

2026-09-06 的第二轮重审在同一片区域抓到两条，症状都是**界面全程不出声**：

- 「一天最多跑几次」此前存不下打法。存/更新/下载/导入四个入口一起丢这一项，
  applyPreset 又拿频次推导值把输入框覆写掉。于是把日上限从 1 改成 2 再存成打法，
  下次照这张打法签出来的仍是 1，当天第二次运行被 RUN_BUDGET_EXCEEDED 拒绝；
  反方向（每小时频次下把 24 手工压到 4）是一条人为收窄的边界被悄悄放宽 6 倍。
- 「照这份再签一份」把 mandateDraft.preset 置为 null，而 activePreset() 会静静
  回落到 STEADY。徽章于是拿一张人根本没选的卡当基准报「已微调 N 项」，
  旁边的「恢复为打法推荐值」走 applyPreset(null)，在函数第一行就 return——
  一颗点了零反应的按钮。

这两条钉的是**结构事实**（字段在不在那张表里、守卫在不在比较之前），
不是行为——仓库里没有跑 JS 的测试床，源码扫描是现有唯一手段。
它挡得住「有人把这一项从表里删掉」，挡不住「表还在但赋值写错了」。
"""

import re
from pathlib import Path

_APP_JS = Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static/app.js"


def _visible(text: str) -> str:
    """剥掉注释——注释是写给读代码的人的，不是被测的行为。

    2026-09-07 排查：不剥的话，解释某条守卫的那段注释里往往逐字写着守卫本身，
    于是断言命中的是注释、守卫被删掉测试也照样绿。
    """
    out = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", " ", out)


def _body_of(name: str) -> str:
    """按花括号配对取出某个顶层函数的函数体源码（已剥注释）。

    先走完参数表再找函数体的左花括号：解构默认值（`{ a = 1 } = {}`）会让
    「函数名之后的第一个 {」落在参数表里，配对当场就闭合，取回来的是参数表不是函数体。
    """
    src = _visible(_APP_JS.read_text(encoding="utf-8"))
    start = src.index("function " + name + "(")
    paren = 0
    body_start = -1
    for i in range(src.index("(", start), len(src)):
        if src[i] == "(":
            paren += 1
        elif src[i] == ")":
            paren -= 1
            if paren == 0:
                body_start = src.index("{", i)
                break
    assert body_start > 0, "找不到 function " + name + " 的函数体"
    depth = 0
    for i in range(body_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[body_start : i + 1]
    raise AssertionError("function " + name + " 的花括号没有配平")


def test_a_playbook_card_remembers_the_daily_cap_the_person_typed() -> None:
    src = _visible(_APP_JS.read_text(encoding="utf-8"))
    fields = re.search(r"const PRESET_NUM_FIELDS = \[(.*?)\];", src, re.DOTALL)
    assert fields is not None, "PRESET_NUM_FIELDS 不见了"
    # 这张表同时驱动 sanitizePreset（存/导入的校验与落值）与下载出去的 JSON；
    # 少了它，日上限就走不进打法。
    assert '"max_runs_per_day"' in fields.group(1)
    # 存表单为打法时也必须把它交出去，否则表里有这一项也永远拿不到值。
    assert 'max_runs_per_day: $("f-runs-per-day").value' in _body_of("presetFromForm")


def test_an_old_playbook_file_without_the_daily_cap_is_not_called_broken() -> None:
    # 2026-09-06 之前存下的打法文件没有这一项。缺它必须是「跳过、回落到推导值」，
    # 不能是 return null——否则同事发来的旧文件会被判成坏文件。
    body = _body_of("sanitizePreset")
    skip = body.index('if (k === "max_runs_per_day" && allowMissingRunsPerDay) continue;')
    reject = body.index("return null;", body.index("for (const k of PRESET_NUM_FIELDS)"))
    assert skip < reject, "缺 max_runs_per_day 时必须先 continue，不能先 return null"


def test_leaving_the_daily_cap_blank_is_an_error_not_an_old_file() -> None:
    """这个后门只对导入开（2026-09-07 排查）。

    表单永远有这个字段，空值不是「旧格式」而是「人填错了」。开给表单，就成了
    四个数值框里唯独这一项的非法值不出声：清空「往回看多少天」再点「更新」会
    红条明说「表单参数有非法值，没有覆盖」，清空「每天最多跑几次」却绿条通过、
    卡里存的值被静默抹掉——而这段 diff 的立意恰恰是「这一项此前存不下、界面不出声」。
    """
    src = _visible(_APP_JS.read_text(encoding="utf-8"))
    #: 整条修复压在这个参数的默认值上（2026-09-07 排查：上一版两个测试都没钉它，
    #  把 `= false` 改成 `= true` 后门就对所有调用方重新打开，而全部测试照绿）。
    assert "function sanitizePreset(obj, { allowMissingRunsPerDay = false } = {}) {" in src
    # 表单出口不许带这个后门。
    assert "allowMissingRunsPerDay" not in _body_of("presetFromForm")
    # 导入出口必须明确带上它，否则同事发来的旧文件会被判成坏文件。
    upload = src[src.index("JSON.parse(String(reader.result))") - 200 :][:400]
    assert "allowMissingRunsPerDay: true" in upload


def test_applying_a_card_keeps_the_cap_flagged_as_a_decision_a_person_made() -> None:
    """卡里存着的日上限本身就是人当初手填、再按「存为新打法」钉下来的决定。

    2026-09-07 在 8791 实测这条漏修的后果：「每 12 小时 + 手填 1 次/日」存成卡 →
    点回这张卡（日上限正确填回 1，但 touched 被清掉）→ 把频次改成每小时 →
    日上限被无声改写成 24。人为收窄的边界放宽 6 倍，警告条因 v===k 沉默，
    摘要只报新数字，徽章反过来说「已微调 1 项」，把这次改动记在人头上。
    """
    body = _body_of("applyPreset")
    assert 'if (runsFromPreset) $("f-runs-per-day").dataset.touched = "1";' in body
    # 清标记只许发生在「回落到频次推导值」那条路上。
    assert 'else delete $("f-runs-per-day").dataset.touched;' in body


def test_with_no_card_selected_the_form_does_not_measure_itself_against_one() -> None:
    body = _body_of("renderAdvBadge")
    guard = body.index("mandateDraft.preset")
    compare = body.index("ADV_FIELDS")
    assert guard < compare, "没选打法时必须先短路，不能先去和一张人没选的卡逐项比较"
    assert "disabled = true" in body[:compare], "同一分支里要把「恢复为打法推荐值」置灰"
