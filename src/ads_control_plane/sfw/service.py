"""一次「找浪费」的编排：逐店取数 → 判定 → 冻结 → 落盘 → 一段给人看的话。

这是组件里唯一调用策略的地方（tests/unit/test_nothing_runs_unattended.py 钉着这一点）：
运行入口只有一个——SFW 里的人敲 /fd、模型调一次工具。这里没有排程、没有定时器，
也不会自己再跑一次。

三条与此前 Web 台不同的裁定（2026-09-19 施工规格 §1/§3）：

- **绑定表纯由配置构造**：店铺表 `[[stores]]` 五项齐全的店才进绑定表，运行期不查领星名录。
  「店铺表配错」于是不会和「名录查不到」长成一个样。
- **单店失败不中断**：一家店取数失败只让那一家的那一行说「取数失败（<code>）」，其余店照常。
  此前一个店抛 ToolDenied 整次调用就没了，人拿不到别家已经算好的文件。
- **时间预算**：SFW 登记的工具超时是 3600 s，组件内预算缺省 2700 s、上限 3000 s；逐店顺序跑、
  预算用尽即停，已跑完的店文件已落盘。预算是在每家店**开跑前**检查的，最后一家可以整个跑出
  预算之外，所以上限必须留出余量——越过 3600 s 那一刻孩子收到的是一句假的「工具没连上」，
  整轮白跑。没轮到的店如实说「本轮没轮到」，不静默漏掉——漏掉的那家在回答里连一行都没有，
  人会把它读成「这家店没事」。

给人看的话（`summarize`）只印计数、链接、ASIN 形状的词与门槛；候选关键词是站外自由文本
（AX-15），永不进返回文本，只进文件。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from ads_control_plane.adapters.lx_read import LxMcpReadClient
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.providers.lingxing.search_terms import LingxingSearchTermSource, LxReadPort
from ads_control_plane.sfw import report
from ads_control_plane.sfw.config import PackConfig, StoreConfig, load_config
from ads_control_plane.strategies.negation import (
    AbstainReason,
    CandidateSetError,
    NegationCandidateSet,
    NegationParameterPack,
    NegationRunResult,
    generate_negation_candidates,
)
from ads_control_plane.strategies.ports import (
    SearchTermFetch,
    SearchTermReadPort,
    SearchTermSourceError,
    attribution_window,
)

logger = logging.getLogger("ads_control_plane.sfw")


#: 返回文本里最多列几个 ASIN：一家店几十个时，那一行词表会把整段回答淹掉。
MAX_ASIN_TERMS_IN_TEXT = 5

#: 取数缓存时长（秒），与 SFW 登记的 tool_timeout_sec 相同。缓存住在数据源实例里，
#: 所以数据源要跨调用复用（见 server.build_server）——一小时内再敲一次 /fd 不再拉领星。
SOURCE_CACHE_TTL_SECONDS = 3600.0

#: 能原样进返回文本的 ASIN 形状【假设】：is_asin 标记的词文本是否总是这种形状未观测
#: （providers/lingxing/search_terms.py 抽样 30 行全为 0）。不匹配的只印计数、指向报表。
ASIN_SHAPE = re.compile(r"^B0[A-Z0-9]{8}$")

#: 冻结集合上记的客户端标识：调用方恒是 SFW 里的模型。
CLIENT_ID = "sfw"


class RunOutcome(StrEnum):
    """一家店这一轮的结局。分这么细是因为每一项对应人**不同的下一步**：

    合并任意两项都会让人拿不准该干什么。ALL_ASIN 与 NO_CANDIDATES 是最贵的一种合并：
    「没有要否定的词」会让人什么都不做，而钱正在烧，只是本策略否不掉。
    """

    CANDIDATES = "CANDIDATES"
    NO_CANDIDATES = "NO_CANDIDATES"
    ALL_ABSTAINED = "ALL_ABSTAINED"
    ALL_ASIN = "ALL_ASIN"
    NO_ROWS = "NO_ROWS"
    NO_USABLE_ROWS = "NO_USABLE_ROWS"
    NO_DATA_SOURCE = "NO_DATA_SOURCE"
    SOURCE_ERROR = "SOURCE_ERROR"
    DATA_REJECTED = "DATA_REJECTED"
    NOT_RUN = "NOT_RUN"


#: 判定跑到了头、有东西给人看的结局：这几种落报表 HTML（只有 CANDIDATES 另有 CSV）。
REPORTED_OUTCOMES = frozenset(
    {
        RunOutcome.CANDIDATES,
        RunOutcome.NO_CANDIDATES,
        RunOutcome.ALL_ABSTAINED,
        RunOutcome.ALL_ASIN,
    }
)


@dataclass(frozen=True, kw_only=True)
class StoreRun:
    """一家店这一轮的全部事实。candidate_set 若有，恒为 FROZEN。"""

    store: StoreConfig
    outcome: RunOutcome
    window: tuple[datetime, datetime]
    pack: NegationParameterPack | None = None
    fetch: SearchTermFetch | None = None
    result: NegationRunResult | None = None
    candidate_set: NegationCandidateSet | None = None
    error_code: str | None = None
    csv_path: Path | None = None
    html_path: Path | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ 数据源


def build_source(cfg: PackConfig, *, client: LxReadPort | None = None) -> LingxingSearchTermSource:
    """组合根：领星只读客户端 + 纯配置构造的绑定表。构造不触网。"""
    read_port = (
        client if client is not None else LxMcpReadClient(cfg.lingxing_url, cfg.lingxing_key)
    )
    return LingxingSearchTermSource(
        read_port, bindings=cfg.bindings(), cache_ttl_seconds=SOURCE_CACHE_TTL_SECONDS
    )


# ------------------------------------------------------------------ 单店


def _empty_outcome(fetch: SearchTermFetch, result: NegationRunResult) -> RunOutcome:
    """空手而归的原因。判定顺序即因果顺序（搬自同日删除的 strategy_service，去作用域分支）：

    取回的行一条也读不出来 → 没取到行 → 浪费全是 ASIN 型（否不掉）→ 整批太旧 → 真的没有。
    「读不出来」必须排在「没取到行」前面：源侧给了几千行、我们一条都聚合不出来时，
    records 同样为空，落到 NO_ROWS 就成了「这段时间没有数据」，人会去查投放有没有开。
    """
    if not fetch.records and not fetch.is_complete:
        return RunOutcome.NO_USABLE_ROWS
    if not fetch.records:
        return RunOutcome.NO_ROWS
    if result.asin_abstain_count > 0 and result.asin_abstain_count == result.evaluated_count:
        return RunOutcome.ALL_ASIN
    if len(result.abstains) == result.evaluated_count:
        return RunOutcome.ALL_ABSTAINED
    return RunOutcome.NO_CANDIDATES


def run_store(
    cfg: PackConfig,
    store: StoreConfig,
    source: SearchTermReadPort,
    *,
    now: datetime,
    id_factory: Callable[[], uuid.UUID] = new_canonical_id,
) -> StoreRun:
    """一家店：取数 → 判定 → 冻结。不写文件（run_all 写），不抛端口/域层的带码异常。"""
    pack = cfg.pack_for(store.currency)
    window = attribution_window(lookback_days=pack.lookback_days, as_of=now)
    if not source.has_profile(store.profile_id):
        return StoreRun(store=store, outcome=RunOutcome.NO_DATA_SOURCE, window=window, pack=pack)
    try:
        fetch = source.fetch_search_term_performance(store.profile_id, pack.lookback_days, now)
    except SearchTermSourceError as exc:
        logger.error("%s 取数失败 %s：%s", store.nickname, exc.code, exc)
        return StoreRun(
            store=store,
            outcome=RunOutcome.SOURCE_ERROR,
            window=window,
            pack=pack,
            error_code=exc.code,
        )
    try:
        result = generate_negation_candidates(fetch.records, pack, now, id_factory)
    except CandidateSetError as exc:
        logger.error("%s 数据不合规 %s：%s", store.nickname, exc.code, exc)
        return StoreRun(
            store=store,
            outcome=RunOutcome.DATA_REJECTED,
            window=window,
            pack=pack,
            fetch=fetch,
            error_code=exc.code,
        )
    if not result.candidates:
        outcome = _empty_outcome(fetch, result)
        if outcome is RunOutcome.NO_USABLE_ROWS:
            # 这条路此前一行日志都不写，而它给孩子的回答以「找管理员」结尾：
            # 管理员拿着那句话去查，日志里什么都没有，doctor 也管不到这类。
            # 两种成因在这里分得开（坏行 / 同组行对不上活动），写出来的就是账目本身。
            logger.error(
                "%s 没有一组能判断：上游 %s 行，读不出来 %d 行，归不到组 %d 行，"
                "可用 %d 行，整组没判断 %d 组",
                store.nickname,
                fetch.source_total,
                fetch.unreadable_rows,
                fetch.unattributable_rows,
                fetch.usable_rows,
                len(fetch.unjudged_groups),
            )
        return StoreRun(
            store=store,
            outcome=outcome,
            window=window,
            pack=pack,
            fetch=fetch,
            result=result,
        )
    frozen = NegationCandidateSet(
        set_id=id_factory(),
        organization_id=cfg.organization_id,
        parameter_pack=pack,
        candidates=result.candidates,
        generated_at=now,
        created_by_client_id=CLIENT_ID,
        created_by_person_id=None,
        source="AI",
        asin_abstain_count=result.asin_abstain_count,
        asin_abstain_terms=tuple(_asin_terms(result)),
    ).freeze()
    return StoreRun(
        store=store,
        outcome=RunOutcome.CANDIDATES,
        window=window,
        pack=pack,
        fetch=fetch,
        result=result,
        candidate_set=frozen,
    )


# ------------------------------------------------------------------ 全部店


def run_all(
    cfg: PackConfig,
    source: SearchTermReadPort,
    *,
    now: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    id_factory: Callable[[], uuid.UUID] = new_canonical_id,
) -> tuple[StoreRun, ...]:
    """逐店跑；每店跑完立即落盘并追加运行记录。返回的顺序恒为店铺表顺序。

    预算在每家店**开跑前**检查：第一家永远会跑，用尽后其余店一律 NOT_RUN。
    不在跑到一半时中断——半家店的文件比没有文件更坏。

    **跑的先后按「最久没轮到的先跑」**，依据是运行记录（2026-09-23 Codex 复审 P2）：
    此前每次都按店铺表从头跑，预算不够时排在后面的店每一次都是 NOT_RUN，「开个新对话
    再问一次」永远推不到它们——而插件形态下取数缓存跟着进程走，新对话里前面那些店又得
    重拉一遍。从没轮到过的排最前（保持店铺表顺序）。运行记录读不懂就按店铺表顺序并写
    一行 WARNING：先后错了只是少推进一轮，不该让整次运行失败。

    于是预算**不是**整次调用的上界：最后开跑的那家店整个跑在预算之外。网关退化时
    单店可以跑很久（每页两次往返、每次 60 秒超时、最多 20 页），整次调用就可能越过
    SFW 登记的 tool_timeout_sec；越过那一刻孩子读到的是「工具没连上」，而服务其实
    还在跑。这里不猜一个「单店最坏耗时」去提前收手——那个数只有 Provider 知道，
    猜小了照样超时，猜大了会平白少跑几家店。能做的是留痕：超了就写一行 WARNING，
    让管理员查得到「这次跑了多久」，而不是面对一句假的「没连上」和一份全绿的体检。
    """
    started = monotonic()
    runs: list[StoreRun] = []
    for store in _longest_waiting_first(cfg):
        if monotonic() - started >= cfg.time_budget_seconds:
            run = StoreRun(
                store=store,
                outcome=RunOutcome.NOT_RUN,
                window=attribution_window(lookback_days=cfg.thresholds.lookback_days, as_of=now),
            )
        else:
            run = run_store(cfg, store, source, now=now, id_factory=id_factory)
            if run.outcome in REPORTED_OUTCOMES:
                csv_path, html_path = report.write_store_files(cfg.export_dir, run)
                run = replace(run, csv_path=csv_path, html_path=html_path)
        report.append_run_log(cfg.run_log_path, run, now=now)
        runs.append(run)
    elapsed = monotonic() - started
    if elapsed > cfg.time_budget_seconds:
        # 不引 installer 里那个 3600：服务运行期不该 import 管理员侧的安装器。
        logger.warning(
            "这次跑了 %.0f 秒，超过时间预算 %.0f 秒；越过 SFW 登记的工具超时那一刻，"
            "孩子读到的会是「工具没连上」，而服务其实还在跑",
            elapsed,
            cfg.time_budget_seconds,
        )
    # 回答按店铺表顺序念：人每次都在同一个位置找同一家店。运行记录保持真实的先后。
    position = {store.nickname: i for i, store in enumerate(cfg.stores)}
    return tuple(sorted(runs, key=lambda run: position[run.store.nickname]))


def _longest_waiting_first(cfg: PackConfig) -> list[StoreConfig]:
    turns = report.last_turns(cfg.run_log_path)
    if turns is None:
        logger.warning("运行记录 %s 读不懂，这次按店铺表顺序跑", cfg.run_log_path)
        return list(cfg.stores)
    never = datetime.min.replace(tzinfo=UTC)
    return sorted(cfg.stores, key=lambda store: turns.get(store.nickname, never))


# ------------------------------------------------------------------ 给人看的话


def _asin_terms(result: NegationRunResult) -> list[str]:
    """钱在烧、但本策略否不掉的那几个词（ASIN 型弃权），按弃权顺序。"""
    return [a.search_term for a in result.abstains if a.reason is AbstainReason.ASIN_NOT_A_KEYWORD]


def _asin_tail(result: NegationRunResult) -> str:
    """「…单独处理」后面跟什么：形状像 ASIN 的列几个，多了只给数、指向报表。"""
    terms = _asin_terms(result)
    shaped = [t for t in terms if ASIN_SHAPE.fullmatch(t.upper())]
    unshaped = len(terms) - len(shaped)
    shown = shaped[:MAX_ASIN_TERMS_IN_TEXT]
    tail = "：" + "、".join(shown) if shown else ""
    more = (
        f"（共 {len(shaped)} 个，只列了 {len(shown)} 个，其余见报表）"
        if len(shaped) > len(shown)
        else ""
    )
    note = f"（其中 {unshaped} 个的写法不像 ASIN，见报表）" if unshaped else ""
    return f"{tail}{more}{note}。"


def _asin_sentence(result: NegationRunResult) -> str:
    count = result.asin_abstain_count
    if count == 0:
        return ""
    return f"另有 {count} 个是 ASIN，否定词挡不住，要去领星「否定投放」单独处理{_asin_tail(result)}"


def _link(path: Path) -> str:
    return f"[{path.name}]({path})"


def _files_line(run: StoreRun) -> str:
    assert run.html_path is not None  # REPORTED_OUTCOMES 的店恒有报表
    if run.csv_path is None:
        return f"报表：{_link(run.html_path)}"
    return f"文件：{_link(run.csv_path)} · 报表：{_link(run.html_path)}"


@dataclass(frozen=True)
class Wording:
    """回答里随形态变的三句话。

    系统形态下用的人（孩子）什么都改不了：重试靠铺在他家里的 /fd，其余只能找管理员。
    插件形态是自己装给自己用的——没有 /fd（插件带不了斜杠命令），也没有管理员可找，
    照着那几句话做是死路，而它们出现在最常走的那条路上（有 CSV 的回答最后一句）。
    """

    retry: str  # 再跑一次怎么做
    escalate: str  # 重试解决不了时，那句话的结尾
    hand_over: str  # 有 CSV 时，回答的最后一句


SYSTEM_WORDING = Wording(
    retry="敲 /new 回车，再敲 /fd 回车回车",
    escalate="找管理员",
    hand_over="有文件的店：把 CSV 交给管理员，他在领星「否定词」里加上才算数。",
)


def _sentence(run: StoreRun, wording: Wording) -> str:
    """店名后面那句话。店名由 summarize 加：只差店名的几家要合成一行。"""
    outcome = run.outcome
    if outcome is RunOutcome.NOT_RUN:
        return f"本轮没轮到（时间不够）；{wording.retry}。"
    if outcome is RunOutcome.NO_DATA_SOURCE:
        return f"这家店没接上数据源，{wording.escalate}。"
    if outcome is RunOutcome.SOURCE_ERROR:
        return (
            f"取数失败（{run.error_code}），文件没有更新；等 1 分钟，"
            f"{wording.retry}，还不行{wording.escalate}。"
        )
    if outcome is RunOutcome.DATA_REJECTED:
        return f"数据不合规（{run.error_code}），文件没有更新，{wording.escalate}。"
    assert run.fetch is not None and run.result is not None and run.pack is not None
    fetch, result, pack = run.fetch, run.result, run.pack
    if outcome is RunOutcome.NO_USABLE_ROWS:
        rows = fetch.source_total if fetch.source_total is not None else fetch.unreadable_rows
        return f"取到了 {rows} 行，但没有一组能判断，{wording.escalate}。"
    if outcome is RunOutcome.NO_ROWS:
        return "这段时间没有搜索词数据。"
    if outcome is RunOutcome.ALL_ASIN:
        return (
            f"花了钱没出单的全是 ASIN（{result.asin_abstain_count} 个），否定词挡不住，"
            f"要去领星「否定投放」单独处理{_asin_tail(result)}"
        )
    if outcome is RunOutcome.ALL_ABSTAINED:
        return (
            f"数据太旧（超过 {pack.max_data_staleness_hours} 小时），这次没法判断；"
            f"晚点{wording.retry}。"
        )
    looked = f"看了 {result.evaluated_count} 组（去重 {result.distinct_search_terms} 个词），"
    if outcome is RunOutcome.NO_CANDIDATES:
        return (
            looked
            + "没有要否定的词。这不等于没有浪费：门槛以下的词不算。"
            + (_asin_sentence(result))
        )
    # CANDIDATES。CSV 与冻结集合恒含全部候选，所以这个数就是全部；多出来的只是
    # 报表表格没列全，而表格是给人看的、CSV 才是拿去执行的，两者的差别要说出口。
    count = f"要否定 {len(result.candidates)} 个"
    if len(result.candidates) > report.MAX_CANDIDATES_IN_TABLE:
        count += f"（报表表格只列花费最高的 {report.MAX_CANDIDATES_IN_TABLE} 个，CSV 里是全部）"
    return looked + count + "。" + _asin_sentence(result)


def _threshold_line(runs: Sequence[StoreRun], cfg: PackConfig) -> str:
    """末尾一行：统计区间、按本轮出现的币种列门槛、两笔没判断的账。"""
    start, end = runs[0].window
    last_day = (end - timedelta(days=1)).date().isoformat()
    currencies = list(dict.fromkeys(run.store.currency for run in runs))
    spend = "、".join(f"{cfg.thresholds.min_spend[c]} {c}" for c in currencies)
    unjudged = sum(len(run.fetch.unjudged_groups) for run in runs if run.fetch is not None)
    unattributable = sum(run.fetch.unattributable_rows for run in runs if run.fetch is not None)
    line = (
        f"门槛：统计 {start.date().isoformat()} 到 {last_day}"
        "（最后几天的订单还没结算完，不算进来）；"
        f"花费 ≥ {spend}（按店币种）；"
        f"点击 ≥ {cfg.thresholds.min_clicks}。"
    )
    if unjudged or unattributable:
        # 不再引「上面说的『没有要否定的词』」：那句话只在部分结局里出现，别的结局下
        # 孩子会去上面找一句不存在的话，于是唯一那条「结论可能不全」的警告落空。
        line += "有一些数据读不懂、已经跳过：上面每家店的结论只覆盖读得懂的那部分。"
    return line


def summarize(runs: Sequence[StoreRun], cfg: PackConfig, wording: Wording = SYSTEM_WORDING) -> str:
    """工具返回的全部文字：每店一到两行（有文件的两行），末尾一行门槛与账目。

    不设总长上限：每店固定行数线性增长，20 家店也只是 40 行。截断只会把第 5、6 家
    之后的店折掉，而折掉的那些店在回答里连一行都没有。

    只差店名的那句话合成一行、排在最后：2026-09-23 首次接真实数据，74 家店里 66 家
    是同一句「这段时间没有搜索词数据」，要看的 8 家被埋在 66 行一模一样的话中间。
    """
    sentences = [_sentence(run, wording) for run in runs]
    repeated = Counter(s for run, s in zip(runs, sentences, strict=True) if run.html_path is None)
    blocks: list[str] = []
    alike: dict[str, list[str]] = {}
    for run, sentence in zip(runs, sentences, strict=True):
        if run.html_path is not None:
            blocks.append(f"**{run.store.nickname}**：{sentence}\n{_files_line(run)}")
        elif repeated[sentence] > 1:
            alike.setdefault(sentence, []).append(run.store.nickname)
        else:
            blocks.append(f"**{run.store.nickname}**：{sentence}")
    blocks += [f"**{'、'.join(names)}**：{sentence}" for sentence, names in alike.items()]
    tail = _threshold_line(runs, cfg)
    # 无条件：孩子刚在弹窗上点了「批准」，这一句是他判断广告有没有被改的唯一依据。
    # 此前它挂在「有 CSV」这个条件下，于是全是 ASIN、没有要否定的词、取数全失败这三种
    # 结局里，一个刚按完批准的 10 岁孩子读不到任何一句说「我没动你的广告」。
    tail += "\n本工具不改任何广告。"
    if any(run.csv_path is not None for run in runs):
        tail += wording.hand_over
    return tail if not blocks else "\n\n".join([*blocks, tail])


def run_once(
    config_path: Path,
    *,
    expect_uid: int | None,
    now: datetime | None = None,
    source: SearchTermReadPort | None = None,
) -> str:
    """load_config → build_source → run_all → summarize。ConfigError 原样抛给调用方。

    server 层不走这里而是自己串这四步：它要跨调用复用数据源（取数缓存住在源实例里），
    而这里每次现建一个。
    """
    cfg = load_config(config_path, expect_uid=expect_uid)
    runs = run_all(
        cfg,
        source if source is not None else build_source(cfg),
        now=now if now is not None else _utc_now(),
    )
    return summarize(runs, cfg)
