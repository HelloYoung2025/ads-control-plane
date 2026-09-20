"""产物：否定词 CSV、自包含的 HTML 报表、运行记录。全部落在 export_dir（属服务用户）。

三个文件承担的是此前 Web 台承担的事，但没有任何状态：

- CSV 就是人拿去领星的那份东西，`render_bulk_csv` 原样（UTF-8 BOM、公式引导符中和），
  文件名里印着内容指纹的前 8 位；报表里印着同一个指纹与 set_hash。人对照报表把 CSV
  交给领星那一步就是「批准」，组件没有批准入口（AX-05 由结构成立）。
- 文件名日期是**回看窗口右端日**（UTC），不是本地 now：同一批数据晚上跑与第二天早上跑
  是同一个窗口、同一个指纹，就该是同一个文件；本地日期会把它们拆成两份（施工计划 §8 攻击 10）。
- 报表 HTML 自包含：内联 CSS、无脚本、无外链——SFW 右栏预览与浏览器直开都不出网。
  每一个来自数据的字符串都经 html.escape：搜索词是站外自由文本（AX-15）。
- 写文件先写临时文件再 os.replace：人点开链接时看到的要么是旧的完整文件，要么是新的
  完整文件，没有半个。0644 让孩子账号读得到、改不了（属主是服务用户）。
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ads_control_plane.sfw.config import StoreConfig
from ads_control_plane.strategies.negation import (
    CANONICALIZATION_VERSION,
    AbstainReason,
    NegationCandidate,
    render_bulk_csv,
    to_bulk_rows,
)

if TYPE_CHECKING:  # 只要类型：service 在运行期 import 本模块，反向只能是注解
    from ads_control_plane.sfw.service import StoreRun

#: 运行记录的表头，固定；追加时不再写。
RUN_LOG_HEADER = (
    "时间",
    "店铺",
    "结局",
    "评估组数",
    "去重词数",
    "候选数",
    "弃权数",
    "ASIN数",
    "指纹",
    "set_hash",
    "csv文件名",
)

#: 产物文件权限：属主可写，别人只读。
FILE_MODE = 0o644

#: 与 negation.render_bulk_csv 的 defuse 同一张表：以这些字符开头的词在 CSV 里会被前置单引号。
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

_e = html.escape


# ------------------------------------------------------------------ 文件名与指纹


def file_stem(store: StoreConfig, window_end: datetime, fingerprint: str) -> str:
    """<昵称>-<窗口右端日 YYYY-MM-DD>-<指纹8>。end 是排他的次日零点，右端日 = end − 1 天。"""
    last_day = (window_end.astimezone(UTC) - timedelta(days=1)).date().isoformat()
    return f"{store.nickname}-{last_day}-{fingerprint[:8]}"


def fingerprint_of(run: StoreRun) -> str:
    """报表印的指纹。有冻结集合时就是它的内容指纹；没有候选的报表照样要一个可复现的值，
    否则同一批数据再跑一次会多出一个文件——用弃权词、结局与窗口按同一套规范化算一个。"""
    if run.candidate_set is not None:
        return run.candidate_set.content_fingerprint()
    if run.result is None:
        raise ValueError("这一轮没有判定结果，没有可指纹的内容")
    payload = {
        "canonicalization": CANONICALIZATION_VERSION,
        "profile_external_id": run.store.profile_id,
        "outcome": run.outcome.value,
        "window": [run.window[0].isoformat(), run.window[1].isoformat()],
        "evaluated_count": run.result.evaluated_count,
        "distinct_search_terms": run.result.distinct_search_terms,
        "abstains": sorted((a.search_term, a.reason.value) for a in run.result.abstains),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 写文件


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_store_files(export_dir: Path, run: StoreRun) -> tuple[Path | None, Path]:
    """写 否定词-<stem>.csv（只在有冻结集合时）与 报表-<stem>.html；返回 (csv 或 None, html)。"""
    if run.result is None:
        raise ValueError("这一轮没有判定结果，没有文件可写")
    export_dir.mkdir(parents=True, exist_ok=True)
    stem = file_stem(run.store, run.window[1], fingerprint_of(run))
    csv_path: Path | None = None
    if run.candidate_set is not None:
        csv_path = export_dir / f"否定词-{stem}.csv"
        _atomic_write(
            csv_path, render_bulk_csv(to_bulk_rows(run.candidate_set), name_of=lambda *_: None)
        )
    html_path = export_dir / f"报表-{stem}.html"
    _atomic_write(html_path, render_report_html(run))
    return csv_path, html_path


# ------------------------------------------------------------------ 报表 HTML

_CSS = """
body{font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif;margin:24px;
color:#222;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f3f3f3}.num{white-space:nowrap;font-variant-numeric:tabular-nums}
.muted{color:#666;font-size:12px}.note{background:#fff8e1;border:1px solid #e6c866;padding:8px 12px}
code{font-family:Menlo,monospace;font-size:13px;word-break:break-all}
"""

_HEADLINES = {
    "CANDIDATES": "要否定 {n} 个词（看了 {evaluated} 组，去重 {distinct} 个词）",
    "NO_CANDIDATES": "看了 {evaluated} 组（去重 {distinct} 个词），没有要否定的词。"
    "这不等于没有浪费：门槛以下的词不算",
    "ALL_ASIN": "花了钱没出单的全是 ASIN（{asin} 个），否定词挡不住",
    "ALL_ABSTAINED": "数据太旧（超过 {stale} 小时），这次没法判断",
}


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _day_span(start: datetime, end: datetime) -> str:
    return f"{start.date().isoformat()} 到 {(end - timedelta(days=1)).date().isoformat()}"


def _moment(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="minutes")


def _name_cell(name: str | None, external_id: str) -> str:
    if name is None:
        return f"<span class='muted'>（无名）{_e(external_id)}</span>"
    return f"{_e(name)} <span class='muted'>{_e(external_id)}</span>"


def _candidate_row(c: NegationCandidate) -> list[str]:
    ev = c.evidence
    return [
        f"<code>{_e(c.search_term)}</code>",
        _name_cell(c.campaign_name, c.scope.parent_refs.campaign_external_id or ""),
        _name_cell(c.ad_group_name, c.scope.entity_external_id),
        f"<span class='num'>{_e(str(ev.spend.amount))} {_e(ev.spend.currency)}</span>",
        str(ev.clicks),
        "—" if ev.impressions is None else str(ev.impressions),
        str(ev.conversions),
        _e(_day_span(ev.window_start, ev.window_end)),
        _e(_moment(ev.data_as_of)),
    ]


def render_report_html(run: StoreRun) -> str:
    """自包含报表：候选表、否不掉的 ASIN、其余弃权、带前置单引号的词、取数账目、门槛、指纹。"""
    result, pack, fetch = run.result, run.pack, run.fetch
    if result is None or pack is None:
        raise ValueError("这一轮没有判定结果，没有报表可写")
    fingerprint = fingerprint_of(run)
    stem = file_stem(run.store, run.window[1], fingerprint)
    candidates = run.candidate_set.candidates if run.candidate_set is not None else ()
    headline = _HEADLINES[run.outcome.value].format(
        n=len(candidates),
        evaluated=result.evaluated_count,
        distinct=result.distinct_search_terms,
        asin=result.asin_abstain_count,
        stale=pack.max_data_staleness_hours,
    )
    parts: list[str] = [
        "<!doctype html>",
        f"<html lang='zh-CN'><head><meta charset='utf-8'><title>{_e(f'报表-{stem}')}</title>"
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_e(run.store.nickname)} · 花了钱却没出单的搜索词</h1>",
        f"<p><strong>{_e(headline)}。</strong></p>",
        "<p>统计 "
        + _e(_day_span(run.window[0], run.window[1]))
        + "（最后几天的订单还没结算完，不算进来）；门槛：花费 ≥ "
        + _e(f"{pack.min_spend.amount} {pack.min_spend.currency}")
        + f"、点击 ≥ {pack.min_clicks}；"
        f"回看 {pack.lookback_days} 天。</p>",
        f"<p class='muted'>内容指纹 <code>{_e(fingerprint)}</code>（文件名里是前 8 位）"
        + (
            f"；set_hash <code>{_e(run.candidate_set.set_hash or '')}</code>"
            if run.candidate_set is not None
            else ""
        )
        + "</p>",
    ]
    if run.truncated_from is not None:
        parts.append(
            f"<p class='note'>本次命中 {run.truncated_from} 个候选，这里与 CSV 只列了花费最高的 "
            f"{len(candidates)} 个；其余的这次没有列出来，处理完这批，"
            "敲 /new 回车，再敲 /fd 回车回车。</p>"
        )
    if candidates:
        parts.append(f"<h2>要否定的词（{len(candidates)}）</h2>")
        parts.append(
            _table(
                (
                    "搜索词",
                    "活动",
                    "广告组",
                    "花费",
                    "点击",
                    "曝光",
                    "订单",
                    "统计区间",
                    "数据时点",
                ),
                (_candidate_row(c) for c in candidates),
            )
        )
        defused = [c.search_term for c in candidates if c.search_term[:1] in FORMULA_LEADERS]
        if defused:
            listed = "、".join(f"<code>{_e(t)}</code>" for t in defused)
            parts.append(
                f"<p class='note'>有 {len(defused)} 个词以 = + - @ 这类字符开头（{listed}）。"
                "CSV 里它们前面多了一个单引号（公式引导符中和），防止表格软件把顾客搜索词当公式"
                "执行；去领星添加否定词时按上表显示的原词输入，不要带那个引号。</p>"
            )
    asins = [a for a in result.abstains if a.reason is AbstainReason.ASIN_NOT_A_KEYWORD]
    if asins:
        parts.append(f"<h2>否不掉的 ASIN（{len(asins)}）</h2>")
        parts.append(
            "<p>这些词花了钱没出单，但它们是 ASIN，否定关键词挡不住，"
            "要去领星「否定投放」单独处理。</p>"
        )
        parts.append(
            "<ul>" + "".join(f"<li><code>{_e(a.search_term)}</code></li>" for a in asins) + "</ul>"
        )
    others = [a for a in result.abstains if a.reason is not AbstainReason.ASIN_NOT_A_KEYWORD]
    if others:
        parts.append(f"<h2>其余弃权（{len(others)}）</h2>")
        parts.append(
            _table(
                ("搜索词", "原因", "说明"),
                (
                    [f"<code>{_e(a.search_term)}</code>", _e(a.reason.value), _e(a.detail)]
                    for a in others
                ),
            )
        )
    if fetch is not None:
        parts.append("<h2>取数账目</h2>")
        total = "—" if fetch.source_total is None else str(fetch.source_total)
        parts.append(
            _table(
                (
                    "上游总行数",
                    "汇总行",
                    "重复行",
                    "读不出来的行",
                    "可用行",
                    "整组没判断",
                    "无法归属的行",
                    "来自缓存",
                ),
                [
                    [
                        total,
                        str(fetch.skipped_summary_rows),
                        str(fetch.duplicate_rows),
                        str(fetch.unreadable_rows),
                        str(fetch.usable_rows),
                        str(len(fetch.unjudged_groups)),
                        str(fetch.unattributable_rows),
                        "是" if fetch.served_from_cache else "否",
                    ]
                ],
            )
        )
        parts.append(
            "<p class='muted'>上游总行数 = 读不出来的行 + 可用行；汇总行与重复行是上游在"
            "总行数之外多给的，不计入。"
            "「整组没判断」与「无法归属的行」单位不同，不相加；任一 > 0 时，"
            "「没有要否定的词」只说明在读得懂的那部分里没有。</p>"
        )
    parts.append(
        "<p class='muted'>否定词只在人把 CSV 交给领星之后才生效；本组件只读，不写领星，"
        "也不会自己再跑一次。</p></body></html>"
    )
    return "\n".join(parts)


# ------------------------------------------------------------------ 运行记录


def append_run_log(path: Path, run: StoreRun, *, now: datetime) -> None:
    """追加一行。表头与 UTF-8 BOM 只在新建时写（BOM 给 Excel，同 render_bulk_csv）。

    「时间」列是本机本地时间、带时区偏移（2026-09-19T20:00:00+08:00）：这一列是给管理员
    对着「刚才在 SFW 里敲的那次」核对用的，人看的是墙上的钟；写 UTC 的话 +08 的管理员会
    觉得每一行都早了 8 小时、对不上。偏移写在值里，换了机器时区也还原得出同一瞬间。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    result, frozen = run.result, run.candidate_set
    row = [
        now.astimezone().isoformat(timespec="seconds"),
        run.store.nickname,
        run.outcome.value,
        "" if result is None else str(result.evaluated_count),
        "" if result is None else str(result.distinct_search_terms),
        "" if result is None else str(len(frozen.candidates) if frozen is not None else 0),
        "" if result is None else str(len(result.abstains)),
        "" if result is None else str(result.asin_abstain_count),
        fingerprint_of(run) if run.html_path is not None else "",
        "" if frozen is None else (frozen.set_hash or ""),
        "" if run.csv_path is None else run.csv_path.name,
    ]
    fresh = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if fresh:
            handle.write("\ufeff")
            writer.writerow(RUN_LOG_HEADER)
        writer.writerow(row)
