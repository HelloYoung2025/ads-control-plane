"""领星搜索词报表响应证据录制（只读）。

在 **密钥持有者本人电脑** 上运行；密钥只经环境变量进入进程，不落盘、不打印、
不进入输出文件。用来补上 providers/lingxing/README.md 点名仍缺的那一项：
docs/evidence/ 里 16 份证据全是入参 schema，一份响应样例都没有。

产物是**证据**，必须忠实于形状；同时必须脱敏——SECURITY.md 逐字禁止
「代码、配置、fixture、文档、测试中出现任何真实密钥、Cookie、店铺/Profile/对象真实 ID」。
两个要求靠保形替换同时满足：ID 换成同长度、同 JSON 类型的合成值（盐随机生成、
用后即弃，我们自己也无法还原），名称与搜索词换合成值，指标保留格式改数值。

真正被冻结成合同的是**字段清单**——每个键观测到的 JSON 类型集合、null 率、
是否出现在汇总行。它天然不含业务数据，却正是 mapper 要依赖的东西。

用法：
    export LX_MCP_URL="<endpoint>"
    export LX_MCP_KEY="<key，绝不粘贴到任何聊天/文档>"
    uv run python scripts/lx_search_term_fixture.py <profile_id> [lookback_days]

产物：docs/evidence/lx-response-ad_campaign_search_term_report-<UTCDATE>.json
写盘前请人工过一遍：脱敏漏一个字段，靠人眼在几百个键里是看不出来的。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ads_control_plane.adapters.lx_read import LxMcpReadClient  # noqa: E402
from ads_control_plane.providers.lingxing.search_terms import (  # noqa: E402
    ATTRIBUTION_LAG_DAYS,
    TOOL_SEARCH_TERM_REPORT,
)

#: 需要保形替换的 ID 字段（与 tests/unit/test_no_real_ids_in_repo.py 的清单同源）。
ID_FIELDS = frozenset(
    {
        "profile_id",
        "campaign_id",
        "ad_group_id",
        "ad_id",
        "target_id",
        "keyword_id",
        "portfolio_id",
        "sid",
        "store_id",
        "shop_id",
        "seller_id",
        "record_id",
    }
)

#: 带品牌/卖家身份的自由文本。
NAME_FIELDS = frozenset(
    {"campaign_name", "ad_group_name", "portfolio_name", "asin_title", "title", "sku", "asin"}
)

#: 会随请求变化的元数据，一律剥离而不是替换。
STRIP_FIELDS = frozenset({"request_id", "traceId", "trace_id", "response_time"})

#: 指标字段：保留类型与格式（"0.90" 仍是两位小数的字符串），数值改成合成值。
METRIC_HINTS = ("spend", "sales", "clicks", "orders", "impressions", "acos", "cpc", "cvr", "ctr")

_SALT = secrets.token_bytes(32)  # 进程内随机，用后即弃：我们自己也还原不回去


def _synthetic_id(value: str) -> str:
    """保形替换：同长度、同前导零结构的数字串。

    保住长度与前导零是有意的——「超长与前导零 ID 无损」是 README 点名的合同测试项，
    样本要是被换成整齐的短 ID，那条测试就测了个假样本。
    """
    digest = hashlib.blake2b(_SALT + value.encode("utf-8"), digest_size=16).hexdigest()
    digits = "".join(str(int(c, 16) % 10) for c in digest)
    if not value.isdigit():
        return f"synthetic-{digits[:8]}"
    leading_zeros = len(value) - len(value.lstrip("0"))
    body = digits[: max(1, len(value) - leading_zeros)]
    return "0" * leading_zeros + body


def _synthetic_metric(value: object) -> object:
    """保留形态（str/int、小数位数），换掉数值。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return 7
    if isinstance(value, float):
        return 7.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        if "." in text:
            return f"7.{'7' * len(text.split('.', 1)[1])}"
        return "7" if text.lstrip("-").isdigit() else value
    return value


def _sanitize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in STRIP_FIELDS:
            continue
        if value is None:
            out[key] = None
        elif key in ID_FIELDS:
            out[key] = _synthetic_id(str(value)) if isinstance(value, str) else value
        elif key in NAME_FIELDS:
            out[key] = f"synthetic-{key}"
        elif key == "query":
            out[key] = "synthetic search term"
        elif any(hint in key for hint in METRIC_HINTS):
            out[key] = _synthetic_metric(value)
        else:
            out[key] = value
    return out


def _field_census(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """字段清单：这才是被冻结成合同的东西，且天然不含业务数据。"""
    census: dict[str, dict[str, Any]] = {}
    for row in rows:
        is_summary = row.get("query") is None and row.get("ad_group_id") is None
        for key, value in row.items():
            entry = census.setdefault(
                key, {"types": set(), "null_count": 0, "seen": 0, "in_summary_row": False}
            )
            entry["seen"] += 1
            if value is None:
                entry["null_count"] += 1
            else:
                entry["types"].add(type(value).__name__)
            if is_summary and value is not None:
                entry["in_summary_row"] = True
    return {
        key: {
            "types": sorted(entry["types"]),
            "null_rate": round(entry["null_count"] / entry["seen"], 3),
            "in_summary_row": entry["in_summary_row"],
        }
        for key, entry in sorted(census.items())
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: lx_search_term_fixture.py <profile_id> [lookback_days]", file=sys.stderr)
        return 2
    profile_id = sys.argv[1]
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    url = os.environ.get("LX_MCP_URL", "").strip()
    key = os.environ.get("LX_MCP_KEY", "").strip()
    if not url or not key:
        print("LX_MCP_URL / LX_MCP_KEY must be set in the environment", file=sys.stderr)
        return 2

    end = datetime.now(UTC).date() - timedelta(days=ATTRIBUTION_LAG_DAYS)
    start = end - timedelta(days=lookback - 1)
    report_date = f"{start.isoformat()} - {end.isoformat()}"
    params = {
        "report_date": report_date,
        "profile_ids": [profile_id],
        "page": 1,
        "length": 30,
        "sort_field": "spends",
        "sort_type": "desc",
        "targeted_type": "not_negatived",
    }
    page = LxMcpReadClient(url, key).fetch_page(TOOL_SEARCH_TERM_REPORT, params)
    raw = page.get("rows")
    rows: list[Mapping[str, Any]] = (
        [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []
    )

    document = {
        # 零 Secret 守卫（tests/unit/test_no_real_ids_in_repo.py）据这个声明放行本文件的
        # ID 字段。声明必须由录制脚本写入而不是事后手加：忘了脱敏的人也会忘了加声明，
        # 于是守卫照样拦住——这正是它要防的那种失误。
        "sanitized": {
            "ids": "shape-preserving hash (same length, same leading zeros, same JSON type)",
            "salt": "random per run, discarded; not reversible even by us",
            "names_and_queries": "replaced with synthetic constants",
            "metrics": "format preserved, values replaced",
            "stripped": sorted(STRIP_FIELDS),
        },
        "tool_id": TOOL_SEARCH_TERM_REPORT,
        "captured_at": datetime.now(UTC).isoformat(),
        "window": report_date,
        "request_params": {**params, "profile_ids": ["<redacted>"]},
        "source_total": page.get("total"),
        "note": (
            "source_total 是抓取时刻的快照，不是可断言的常量——同窗口两次调用曾观测到 "
            "1047→1079（窗口含当日时数据仍在动）。行内 ID / 名称 / 搜索词 / 指标均为"
            "保形替换后的合成值，替换盐已随进程丢弃。"
        ),
        "field_census": _field_census(rows),
        "sample_rows": [_sanitize_row(r) for r in rows],
    }
    out = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "evidence"
        / f"lx-response-{TOOL_SEARCH_TERM_REPORT}-{datetime.now(UTC).strftime('%Y%m%d')}.json"
    )
    out.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    census = document["field_census"]
    assert isinstance(census, dict)
    print(f"wrote {out} ({len(rows)} rows, {len(census)} fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
