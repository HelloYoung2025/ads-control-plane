"""本地演示组合根集成测试：三个面（审批 API + /mcp + /ui）在同一进程内的集成缝隙。

覆盖点与 UI 合同对齐（ui_static/app.js 实际消费的字段）：
- /dev/identities 的形状与两个 demo 身份（人 + AI；审批者与运营人员已合二为一）；
- 单人世界下 SoD 仍可达：人不能批准自己经 MCP 生成的集合；
- GET / 重定向到 /ui/，index.html 引用的每个静态资源经 /ui/ 可达；
- /mcp 无 Bearer 401（认证先于一切）；
- 授权书签发的成功/403 OBJECTIVE_NOT_READY/422 白名单/AI 403；
- 全链路：Codex 经 MCP 生成冻结集合 → AI 批 403 → 错 hash 409 → 人批 200 → CSV 导出；
- /dev/runtime-config 的通道两态判定（布尔 + 计数，绝不回显 key/URL/店铺 ID）
  与 index.html 不再静态硬编码「全部是 Mock」（2026-08-29 ui-4/runtime-1）。
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ads_control_plane.api.local_demo import (
    DEMO_CODEX_TOKEN,
    DEMO_OWNER_TOKEN,
    build_local_demo_app,
)
from ads_control_plane.api.workbench_api import (
    ENV_LX_MCP_KEY,
    ENV_LX_MCP_URL,
    ENV_SYNC_PROFILES,
)
from ads_control_plane.canonical.ids import new_canonical_id


def _banner_lines(channel: dict[str, object]) -> list[str]:
    """载入启动横幅脚本里的纯函数（scripts/ 不是包，按路径加载）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "serve_local_demo.py"
    spec = importlib.util.spec_from_file_location("_serve_local_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.channel_banner_lines(channel))


def _flip(value: object) -> object:
    """把一个通道状态取值换成另一个取值，用来看横幅输出会不会跟着变。"""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    return f"{value}-OTHER"


#: MCP 会话必须以 127.0.0.1:<port> 为 Host（SDK 默认开 DNS-rebinding 防护）。
BASE_URL = "http://127.0.0.1:8788"

ISSUE_BODY = {
    "profile_external_id": "profile-A",
    "objective": "WASTED_SPEND_REMOVED",
    "statement": "清除近 30 天零转化高花费搜索词造成的广告浪费",
    "lookback_days": 30,
    "min_spend_amount": "20.00",
    "currency": "USD",
    "min_clicks": 25,
    "max_data_staleness_hours": 24,
    "max_runs_per_day": 1,
    "max_candidates_per_run": 50,
    "valid_days": 7,
    "run_interval_minutes": 1440,
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_dev_identities_matches_ui_contract() -> None:
    client = TestClient(build_local_demo_app())
    res = client.get("/dev/identities")
    assert res.status_code == 200
    payload = res.json()
    assert "LOCAL DEMO ONLY" in payload["warning"]
    identities = payload["identities"]
    assert [i["token"] for i in identities] == [DEMO_OWNER_TOKEN, DEMO_CODEX_TOKEN]
    for row in identities:
        # app.js 消费：token、display_name（显示名回退链 display_name→identity→token）、
        # principal_type（HUMAN 判定）、roles、capabilities（一句话能力摘要）。
        assert row["display_name"] and row["identity"] and row["capabilities"]
        assert isinstance(row["roles"], list) and row["roles"]
    human, codex = identities
    assert human["principal_type"] == "HUMAN" and human["human_person_id"] == "owner-1"
    # 合二为一：审批者与运营人员是同一个人 → 并集持两个角色（域层角色目录未变）。
    assert sorted(human["roles"]) == ["APPROVER", "OPERATOR"]
    assert codex["principal_type"] == "AI_CLIENT"
    assert codex["human_initiator_person_id"] == "owner-1"
    assert codex["human_person_id"] is None


def test_demo_has_exactly_one_human_and_one_ai() -> None:
    """合并后的身份结构锚点：恰一人一 AI，且 AI 的委托链指向那个真实存在的人。

    委托人指向一个已被删除的人（原 ops-1）是一条断链——审计里会出现一个谁也
    找不到的发起人。这条断言让它不可能悄悄发生。
    """
    client = TestClient(build_local_demo_app())
    identities = client.get("/dev/identities").json()["identities"]
    assert len(identities) == 2
    assert {i["principal_type"] for i in identities} == {"HUMAN", "AI_CLIENT"}
    human = next(i for i in identities if i["principal_type"] == "HUMAN")
    ai = next(i for i in identities if i["principal_type"] == "AI_CLIENT")
    assert ai["human_initiator_person_id"] == human["human_person_id"]


def test_root_redirects_to_ui() -> None:
    client = TestClient(build_local_demo_app())
    res = client.get("/", follow_redirects=False)
    # RedirectResponse 默认 307；语义上任何指向 /ui/ 的临时重定向都符合规格。
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/ui/"


def test_ui_index_and_every_referenced_asset_served() -> None:
    client = TestClient(build_local_demo_app())
    res = client.get("/ui/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assets = [
        ref
        for ref in re.findall(r'(?:href|src)="([^"]+)"', res.text)
        if not ref.startswith(("http://", "https://", "#", "data:"))
    ]
    assert sorted(assets) == ["app.js", "style.css"]  # 页面自包含：只允许相对路径静态资源
    for asset in assets:
        asset_res = client.get(f"/ui/{asset}")
        assert asset_res.status_code == 200, asset


def test_mcp_post_without_bearer_is_401() -> None:
    client = TestClient(build_local_demo_app())
    res = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert res.status_code == 401


def test_approval_api_requires_bearer() -> None:
    client = TestClient(build_local_demo_app())
    res = client.get("/candidate-sets")
    assert res.status_code == 401
    assert res.json()["detail"] == "AUTHENTICATION_REQUIRED"


def test_owner_lists_candidate_sets_initially_empty() -> None:
    client = TestClient(build_local_demo_app())
    res = client.get("/candidate-sets", headers=bearer(DEMO_OWNER_TOKEN))
    assert res.status_code == 200
    assert res.json() == {"candidate_sets": []}


def test_owner_issues_mandate_with_fields_ui_renders() -> None:
    client = TestClient(build_local_demo_app())
    res = client.post("/mandates", headers=bearer(DEMO_OWNER_TOKEN), json=ISSUE_BODY)
    assert res.status_code == 200
    mandate = res.json()
    assert mandate["state"] == "ACTIVE"
    assert mandate["objective"] == "WASTED_SPEND_REMOVED"
    assert mandate["profile_external_id"] == "profile-A"
    assert mandate["issued_by_person_id"] == "owner-1"
    # app.js mandateRow 消费的嵌套字段
    assert mandate["parameter_pack"]["min_spend"] == {"amount": "20.00", "currency": "USD"}
    assert mandate["bounds"]["run_interval_minutes"] == 1440
    assert mandate["bounds"]["max_runs_per_day"] == 1
    listed = client.get("/mandates", headers=bearer(DEMO_OWNER_TOKEN)).json()["mandates"]
    assert [m["mandate_id"] for m in listed] == [mandate["mandate_id"]]


def test_mandate_objective_not_ready_is_403() -> None:
    client = TestClient(build_local_demo_app())
    body = {**ISSUE_BODY, "objective": "CLEARANCE_VELOCITY"}
    res = client.post("/mandates", headers=bearer(DEMO_OWNER_TOKEN), json=body)
    assert res.status_code == 403
    assert res.json()["detail"] == "OBJECTIVE_NOT_READY"


def test_mandate_run_interval_below_whitelist_is_422() -> None:
    """拒绝必须说清是哪个参数、允许范围是什么。

    前端词典对 PARAMETER_REJECTED 的中文是「可能是幅度越界、数值形状不合法、
    或理由为空」——一句让人在三种可能里猜的话，而服务端此刻就攥着确切原因。
    app.js 的 api() 早就认识 {code, message} 并会把 message 拼在中文后面
    （第 483~486 行），一直没有服务端用它。
    """
    client = TestClient(build_local_demo_app())
    body = {**ISSUE_BODY, "run_interval_minutes": 10}
    res = client.post("/mandates", headers=bearer(DEMO_OWNER_TOKEN), json=body)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "PARAMETER_REJECTED"
    assert "run_interval_minutes" in detail["message"]
    assert "60" in detail["message"]  # 人得能照着这句改


def test_codex_cannot_issue_mandate() -> None:
    client = TestClient(build_local_demo_app())
    res = client.post("/mandates", headers=bearer(DEMO_CODEX_TOKEN), json=ISSUE_BODY)
    assert res.status_code == 403
    assert res.json()["detail"] == "AI_CANNOT_ISSUE_MANDATE"


async def test_full_chain_codex_generates_owner_approves_and_exports() -> None:
    """Codex(MCP) 生成 → codex 批 403 → 错 hash 409 → owner 批 200 → CSV 导出。"""
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):  # 宿主 lifespan 启动 MCP session manager
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_CODEX_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            assert tools == [
                "generate_negation_candidate_set",
                "list_authorized_scopes",
                "list_negation_candidate_sets",
                "whoami",
            ]
            result = await session.call_tool(
                "generate_negation_candidate_set", {"profile_external_id": "profile-A"}
            )
            assert not result.is_error
            payload = getattr(result, "structured_content", None) or json.loads(
                result.content[0].text  # type: ignore[union-attr]
            )
            assert payload["evaluated_ad_group_terms"] == 10
            assert payload["candidate_count"] == 3
            assert payload["profile_has_data_source"] is True
            # ASIN 型排在过旧之前：两类弃权都要人做事，但只有前者是一个具体的、
            # 人现在就能去领星处理掉的词，截断时不能先丢它。
            assert [a["search_term"] for a in payload["abstains"]] == [
                "b0demo0001",
                "vintage widget manual",
            ]
            assert [a["reason"] for a in payload["abstains"]] == [
                "ASIN_NOT_A_KEYWORD",
                "STALE_DATA",
            ]
            assert payload["asin_abstain_count"] == 1
            set_id, set_hash = payload["set_id"], payload["set_hash"]
            assert set_id and set_hash

            # 2026-08-29 排查结论 runtime-2：演示组合根只给 profile-A 种了搜索词数据，
            # 对任何真实店铺，Codex 看到的返回必须自报"没接数据源"，不得与"查了
            # 确实没有浪费"逐字相同。
            absent = await session.call_tool(
                "generate_negation_candidate_set", {"profile_external_id": "real-shop-1"}
            )
            assert not absent.is_error
            absent_payload = getattr(absent, "structured_content", None) or json.loads(
                absent.content[0].text  # type: ignore[union-attr]
            )
            assert absent_payload["profile_has_data_source"] is False
            assert absent_payload["evaluated_ad_group_terms"] == 0
            assert absent_payload["candidate_count"] == 0
            assert absent_payload["set_id"] is None

        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            denied = await api.post(
                f"/candidate-sets/{set_id}/approve",
                headers=bearer(DEMO_CODEX_TOKEN),
                json={"expected_hash": set_hash},
            )
            assert denied.status_code == 403
            assert denied.json()["detail"] == "AI_CANNOT_APPROVE"

            wrong_hash = await api.post(
                f"/candidate-sets/{set_id}/approve",
                headers=bearer(DEMO_OWNER_TOKEN),
                json={"expected_hash": "0" * 64},
            )
            assert wrong_hash.status_code == 409
            assert wrong_hash.json()["detail"] == "HASH_MISMATCH"

            approved = await api.post(
                f"/candidate-sets/{set_id}/approve",
                headers=bearer(DEMO_OWNER_TOKEN),
                json={"expected_hash": set_hash},
            )
            assert approved.status_code == 200
            assert approved.json()["state"] == "APPROVED"
            assert approved.json()["approved_by_person_id"] == "owner-1"

            csv_res = await api.get(
                f"/candidate-sets/{set_id}/export.csv", headers=bearer(DEMO_OWNER_TOKEN)
            )
            assert csv_res.status_code == 200
            assert csv_res.headers["content-type"].startswith("text/csv")
            lines = csv_res.text.strip().splitlines()
            assert len(lines) == 4  # 表头 + 3 个候选
            assert "cheap widget holder" in csv_res.text


async def test_single_human_cannot_approve_own_generated_set() -> None:
    """合并成一个人之后，职责分离仍然可达：人不能批准自己生成的集合。

    这是「合二为一 ≠ 取消职责分离」的唯一演示层证据。合并前 demo-ops-token 从未
    生成或批准过任何东西，这条路径在演示里零覆盖；补上它，否则「合并把 SoD 变成
    了一条恒真断言」将无法被证伪。

    expected_hash 必须传**对**的 hash：传错的会先命中 409 HASH_MISMATCH，锚点失效。
    """
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_OWNER_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            # 人持 OPERATOR → PROPOSAL_CREATE_DRAFT 命中 Grant，可经 MCP 生成候选。
            result = await session.call_tool(
                "generate_negation_candidate_set", {"profile_external_id": "profile-A"}
            )
            assert not result.is_error
            payload = getattr(result, "structured_content", None) or json.loads(
                result.content[0].text  # type: ignore[union-attr]
            )
            set_id, set_hash = payload["set_id"], payload["set_hash"]

        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            denied = await api.post(
                f"/candidate-sets/{set_id}/approve",
                headers=bearer(DEMO_OWNER_TOKEN),
                json={"expected_hash": set_hash},
            )
            assert denied.status_code == 403
            assert denied.json()["detail"] == "CREATOR_CANNOT_APPROVE"


def test_demo_tokens_survive_an_overnight_run() -> None:
    """演示服务跑过夜后，demo token 不得静默过期。

    2026-08-29 现场：服务前一天 00:34 启动，次日 10:00 起每个请求都是 401，而界面
    只说得出「未认证：缺少或无效的 Bearer Token」——人无从想到真因是「服务开太久」。
    根因是 demo 身份的 expires_at 只给了 8 小时。演示 token 是硬编码常量、身份全
    Mock、只监听回环，短有效期在这里没有安全收益，只制造这一个坑。
    """
    verifier = build_local_demo_app().state.demo_verifier
    # 18 小时覆盖「下班前启动、次日上班再用」这个演示服务最常见的使用形态。
    overnight = datetime.now(UTC) + timedelta(hours=18)
    for token in (DEMO_OWNER_TOKEN, DEMO_CODEX_TOKEN):
        actor = verifier.actor_for(token)
        assert actor is not None, f"{token} 未注册"
        assert not actor.is_expired(overnight), f"{token} 在服务运行 18 小时后过期"


def test_runtime_config_reports_mock_when_channel_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配通道：布尔 False、计数 0——UI 据此保留原样的 MOCK 徽章与页脚。"""
    for var in (ENV_LX_MCP_KEY, ENV_LX_MCP_URL, ENV_SYNC_PROFILES):
        monkeypatch.delenv(var, raising=False)
    res = TestClient(build_local_demo_app()).get("/dev/runtime-config")
    assert res.status_code == 200
    # 形状全等：端点只许有这两个键，多出任何字段都可能夹带配置细节。
    assert res.json() == {
        "lx_channel_configured": False,
        "sync_profile_count": 0,
        "search_term_source": "MOCK",
        "search_term_profile_count": 0,
    }


def test_runtime_config_reports_real_channel_count_but_never_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已配通道：布尔 True + 白名单计数；key/URL/店铺 ID 绝不出现在响应里。

    2026-08-29 排查结论（ui-4/runtime-1）：env 配齐时该进程挂着真实领星通道，
    而徽章/横幅硬编码「全部是 Mock」。本端点是 UI 徽章与启动横幅的唯一判定来源，
    职责止于布尔与计数——凭据与店铺清单即使对本机 UI 也不该多吐一个字节。
    """
    monkeypatch.setenv(ENV_LX_MCP_KEY, "test-key-not-a-real-secret")
    monkeypatch.setenv(ENV_LX_MCP_URL, "https://lx.invalid/test-mcp")
    monkeypatch.setenv(ENV_SYNC_PROFILES, "test-shop-1, test-shop-2")
    res = TestClient(build_local_demo_app()).get("/dev/runtime-config")
    assert res.status_code == 200
    assert res.json() == {
        "lx_channel_configured": True,
        "sync_profile_count": 2,
        # 同步通道配齐 ≠ 策略面接了真实搜索词源：后者要 ADS_CP_STRATEGY_LX_ENABLED
        # 显式打开，且绑定成立。开关没开时这里必须仍报 MOCK。
        "search_term_source": "MOCK",
        "search_term_profile_count": 0,
    }
    for secret in ("test-key-not-a-real-secret", "lx.invalid", "test-shop-1", "test-shop-2"):
        assert secret not in res.text


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("test-key", None),  # 只有 key：无 URL 出不了网
        (None, "https://lx.invalid/test-mcp"),  # 只有 URL：无 key 出不了网
        ("   ", "https://lx.invalid/test-mcp"),  # 空白 key 视同缺失（与 POST /sync 同口径）
    ],
)
def test_runtime_config_partial_env_is_not_a_configured_channel(
    monkeypatch: pytest.MonkeyPatch, key: str | None, url: str | None
) -> None:
    """key 与 URL 任缺一项都不算已配通道——与 POST /sync 的 fail-closed 判定同口径。

    半配置状态谎报「真实通道」会让人白紧张；谎报的另一半（配齐了还说 Mock）由上一
    条测试锚住。两条合起来钉死判定必须是 key 与 URL 的合取。
    """
    monkeypatch.delenv(ENV_SYNC_PROFILES, raising=False)
    for var, value in ((ENV_LX_MCP_KEY, key), (ENV_LX_MCP_URL, url)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    res = TestClient(build_local_demo_app()).get("/dev/runtime-config")
    assert res.json() == {
        "lx_channel_configured": False,
        "sync_profile_count": 0,
        "search_term_source": "MOCK",
        "search_term_profile_count": 0,
    }


def test_ui_no_longer_hardcodes_the_mock_claim() -> None:
    """index.html 不得再静态断言「全部数据为 Mock」（2026-08-29 ui-4 根因）。

    该文案只允许由 app.js 在 /dev/runtime-config 确认未配通道后写入；静态初始态
    是灰色「通道未知」。同时锚住 app.js 消费的两个挂载点 id——徽章与页脚少任何
    一个，三态切换就静默失效。
    """
    html = TestClient(build_local_demo_app()).get("/ui/").text
    assert "全部数据为 Mock" not in html
    assert 'id="channel-badge"' in html
    assert 'id="app-footer"' in html


async def test_the_runbook_csv_sample_is_the_csv_this_demo_actually_exports() -> None:
    """runbook §④ 那段样例标着「实测」，就必须真的是实测。

    它此前是 4 列，而实际导出 8 列（多 profile / shop 与两个名称列）——照着 runbook
    核对的人会怀疑自己下错了文件，或者以为名称列是导入模板的字段。这段样例是运营
    在「拿到 CSV 之后该干什么」这一步唯一的参照物，对不上就等于没有参照物。
    """
    runbook = (Path(__file__).resolve().parents[2] / "docs/runbook-local-demo.md").read_text(
        encoding="utf-8"
    )
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_CODEX_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "generate_negation_candidate_set", {"profile_external_id": "profile-A"}
            )
            payload = getattr(result, "structured_content", None) or json.loads(
                result.content[0].text  # type: ignore[union-attr]
            )
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            approved = await api.post(
                f"/candidate-sets/{payload['set_id']}/approve",
                headers=bearer(DEMO_OWNER_TOKEN),
                json={"expected_hash": payload["set_hash"]},
            )
            assert approved.status_code == 200
            csv_text = (
                await api.get(
                    f"/candidate-sets/{payload['set_id']}/export.csv",
                    headers=bearer(DEMO_OWNER_TOKEN),
                )
            ).text
    lines = csv_text.lstrip("\ufeff").splitlines()
    assert lines[0] in runbook, f"runbook 里的 CSV 表头与实际导出不符：实际是 {lines[0]}"
    for line in lines[1:]:
        assert line in runbook, f"runbook 缺这一行实测输出：{line}"


def test_ui_static_assets_demand_revalidation() -> None:
    """静态资源必须带 Cache-Control: no-cache——否则浏览器按启发式缓存留住旧
    app.js，服务端修好的界面用户看不到，还得自己想到强刷（2026-08-29 实测）。"""
    client = TestClient(build_local_demo_app())
    for asset in ("/ui/", "/ui/app.js", "/ui/style.css"):
        res = client.get(asset)
        assert res.status_code == 200, asset
        assert res.headers.get("cache-control") == "no-cache", asset
        assert res.headers.get("etag"), asset  # no-cache 靠 ETag 协商才便宜


def test_strategy_source_stays_mock_until_the_dedicated_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 key 配齐 ≠ 策略面接了真实源。

    generate_negation_candidate_set 是 AI 可调用工具，而 POST /sync 明确要求 HUMAN；
    即席模式还没有任何配额。所以真实取数的接入必须是一次显式动作，
    不能配了同步 key 就顺带打开。
    """
    monkeypatch.setenv(ENV_LX_MCP_KEY, "test-key")
    monkeypatch.setenv(ENV_LX_MCP_URL, "https://lx.invalid/test-mcp")
    monkeypatch.delenv("ADS_CP_STRATEGY_LX_ENABLED", raising=False)
    body = TestClient(build_local_demo_app()).get("/dev/runtime-config").json()
    assert body["lx_channel_configured"] is True
    assert body["search_term_source"] == "MOCK"


def test_strategy_source_falls_back_to_mock_when_bindings_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """名录取到了、白名单也非空，但没有一个店推得出币种 → 仍挂 Mock，且成因说对。

    报「装配结果」而不是「开关状态」：这两者在绑定失败时并不相同，而说成 LINGXING
    会让人以为眼前的候选花的是真钱。

    这条此前的 docstring 写着「绑定不成立（没声明币种）」，而它**从不设**
    ADS_CP_SYNC_PROFILES——实际走的是空白名单那条早退，币种判定一次都没执行到
    （2026-08-30 外审指出）。写文案的人自己在这里就归错了因，正说明成因塌成一个
    取值之后谁都恢复不了它。现在把这条测试改成真的走到币种那一支。
    """
    from ads_control_plane.api import local_demo

    monkeypatch.setenv(ENV_LX_MCP_KEY, "test-key")
    monkeypatch.setenv(ENV_LX_MCP_URL, "https://lx.invalid/test-mcp")
    monkeypatch.setenv("ADS_CP_STRATEGY_LX_ENABLED", "1")
    monkeypatch.setenv(ENV_SYNC_PROFILES, "profile-synthetic-1")
    monkeypatch.delenv("ADS_CP_LX_PROFILE_CURRENCY", raising=False)

    class _Catalog:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def fetch_page(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            # 站点 XX 不在 MARKETPLACE_CURRENCY 里 → 推不出币种 → 不绑这个店。
            # 猜一个币种等于凭空给出一个可能差几倍的花费门槛。
            return {
                "rows": [{"profile_id": "profile-synthetic-1", "sid": "sid-1", "country": "XX"}]
            }

    monkeypatch.setattr(local_demo, "LxMcpReadClient", _Catalog)
    body = TestClient(build_local_demo_app()).get("/dev/runtime-config").json()
    assert body["search_term_source"] == "MOCK_NO_BINDINGS"
    assert body["search_term_profile_count"] == 0


def test_a_failed_shop_catalog_call_is_not_reported_as_a_binding_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取店铺名录失败 ≠ 绑定配错了。两者要人做的事完全不同。

    名录调用失败（key 写错、DNS 不通、网关拒绝）时 LxReadError 被就地吞掉，错误码
    连同原因一起消失。若这一支还与「白名单内没有店拿得到 sid 与币种」共用一个取值，
    人会去逐个核对店铺配置——而没有任何店铺配置是错的，错的是他根本没连上。
    """
    from ads_control_plane.adapters.lx_read import LxReadError
    from ads_control_plane.api import local_demo

    monkeypatch.setenv(ENV_LX_MCP_KEY, "test-key")
    monkeypatch.setenv(ENV_LX_MCP_URL, "https://lx.invalid/test-mcp")
    monkeypatch.setenv("ADS_CP_STRATEGY_LX_ENABLED", "1")
    monkeypatch.setenv(ENV_SYNC_PROFILES, "profile-synthetic-1")

    class _Down:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def fetch_page(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise LxReadError("LX_TRANSPORT_ERROR", "gateway unreachable")

    monkeypatch.setattr(local_demo, "LxMcpReadClient", _Down)
    body = TestClient(build_local_demo_app()).get("/dev/runtime-config").json()
    assert body["search_term_source"] == "MOCK_LOOKUP_FAILED"


def test_currency_is_derived_from_marketplace_and_unknown_sites_are_not_guessed() -> None:
    """2026-08-30 Owner 确认 spends 是站点本币，币种因此可按站点推出——
    在这条确认之前这张表是不许存在的，它等于把一个未经证实的假设藏进代码。

    但「可推」不等于「可猜」：表里没有的站点一律推不出币种，对应的店铺不进绑定表。
    金额没有币种就无法与门槛比较，猜一个等于凭空给出一个可能差几倍的结论。
    """
    from ads_control_plane.api.local_demo import MARKETPLACE_CURRENCY

    assert MARKETPLACE_CURRENCY["US"] == "USD"
    assert MARKETPLACE_CURRENCY["DE"] == MARKETPLACE_CURRENCY["FR"] == "EUR"
    assert MARKETPLACE_CURRENCY["UK"] == "GBP"
    assert "XX" not in MARKETPLACE_CURRENCY
    # 全部取值必须是合法的 ISO-4217 三位大写，否则 Money 会在构造记录时才炸。
    assert all(len(c) == 3 and c.isupper() for c in MARKETPLACE_CURRENCY.values())


def test_every_runtime_config_field_reaches_the_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端算出来的通道状态，必须有人能在界面上看到。

    2026-08-30 发现的复发：search_term_source / search_term_profile_count 被算好、
    被返回，前端从不读。于是打开 ADS_CP_STRATEGY_LX_ENABLED 的人看到的页脚仍然
    只列「同步与镜像数据是真实的」——一份**看起来完整的清单**，恰好漏掉了他正要
    批准的候选花的是真店里的真钱。含糊的「已连接真实通道」只是没说全；一份点着名
    的清单停在半路，读起来就是说全了，这比含糊更坏。与 ui-4/runtime-1 同一个病。

    所以这条测的不是某一个字段，而是这个类别：端点新增状态字段而界面不读，
    就是又造了一次同样的沉默。
    """
    for var in (ENV_LX_MCP_KEY, ENV_LX_MCP_URL, ENV_SYNC_PROFILES):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(build_local_demo_app())
    status = client.get("/dev/runtime-config").json()
    fields = status.keys()
    # 只扫**代码**：整行注释里出现字段名不算「读了它」。本文件第 576 行就有一句
    # 注释同时提到 search_term_source 与 search_term_profile_count——按整篇扫子串，
    # 光凭那句注释这条守卫就已「通过」，而界面一个字都没改。判据改成属性访问
    # （`.字段名`），这正是「读它」在 JS 里长的样子。
    code = "\n".join(
        line
        for line in client.get("/ui/app.js").text.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    unread = [name for name in fields if f".{name}" not in code]
    assert unread == [], f"/dev/runtime-config 有字段前端从不读，人看不到：{unread}"

    # 启动横幅是第二个消费者，而且往往是**唯一**被读到的那个：起服务的人先看它。
    # 2026-08-30 排查 #11：徽章补了 search_term_source，横幅没跟着补，于是横幅
    # 继续写死「审批数据仍为演示 Mock」，而候选此时读的是真实店铺的真实搜索词。
    # 只扫 app.js 的守卫覆盖不到这里，同一个病换个出口就复发了。
    # 判据是「改这个字段，横幅输出会不会变」——不是「字段名有没有出现在源码里」。
    # 子串扫描挡不住注释：`_build_search_term_source` 这个函数名里就含
    # search_term_source，光凭它守卫就已「通过」，而横幅一个字都没改。
    base = {**status, "lx_channel_configured": True, "search_term_source": "LINGXING"}
    printed = _banner_lines(base)
    unspoken = [
        name for name in fields if _banner_lines({**base, name: _flip(base[name])}) == printed
    ]
    assert unspoken == [], f"/dev/runtime-config 有字段启动横幅从不播报：{unspoken}"


def test_banner_reads_the_channel_after_the_app_decides_it() -> None:
    """横幅必须在建 app 之后读通道状态，否则读到的恒是模块默认值。

    搜索词通道是在组合根 build_local_demo_app() → _build_search_term_source 里
    才决定的。两行写反的后果不是「少一句话」，是横幅与同一进程的
    /dev/runtime-config 当场自相矛盾，而人先读到的是横幅那句假的。
    """
    banner = (Path(__file__).resolve().parents[2] / "scripts" / "serve_local_demo.py").read_text()
    build_at = banner.index("app = build_local_demo_app()")
    status_at = banner.index("channel = runtime_channel_status()")
    assert build_at < status_at, "横幅在组合根决定通道之前就读了通道状态"


def test_banner_says_candidates_are_real_when_they_are() -> None:
    """打开策略通道后，横幅必须点名说「待批队列里的词来自真实店铺」。

    此前这里写死「身份与审批数据仍为演示 Mock」——而审批数据就是待批队列里那些
    候选。人据此把队列当演示内容，批得随意、拿去演示、或直接下载 CSV 当样例；
    真相是批准导出的 CSV 拿去领星执行，会在真实亚马逊账户里否掉真实关键词。
    """
    real = _banner_lines(
        {
            "lx_channel_configured": True,
            "sync_profile_count": 8,
            "search_term_source": "LINGXING",
            "search_term_profile_count": 8,
        }
    )
    text = "\n".join(real)
    assert "真实关键词" in text
    assert "仍为演示 Mock" not in text.replace("否定词候选仍为演示 Mock", "")

    mock = "\n".join(
        _banner_lines(
            {
                "lx_channel_configured": True,
                "sync_profile_count": 8,
                "search_term_source": "MOCK",
                "search_term_profile_count": 0,
            }
        )
    )
    # 同步是真的、候选还是假的——这是两件事，横幅必须分开说。
    assert "领星生产 API" in mock
    assert "否定词候选仍为演示 Mock" in mock
    assert "真实关键词" not in mock


def test_banner_tells_apart_the_two_reasons_candidates_are_still_mock() -> None:
    """挂 Mock 的每个成因，横幅都得说对——说错了人就去改一个本来就是对的设置。

    成因决定他该动哪个开关：没开开关 → 去开；白名单空 → 去填 ADS_CP_SYNC_PROFILES；
    名录取不到 → 去查 key/URL/网络；确实没店能绑 → 才轮到查 sid 与币种。
    共用一句文案时界面只能点名其中一个，另外几种人会在错误的地方反复试，
    而且始终以为自己没打开真实通道——真相是他打开了。

    ADS_CP_SYNC_PROFILES 在 .env.example 里缺省为空，所以「白名单空」是首次运行的
    常态，恰恰最不该被误报成别的。
    """

    def banner(source: str) -> str:
        return "\n".join(
            _banner_lines(
                {
                    "lx_channel_configured": True,
                    "sync_profile_count": 8,
                    "search_term_source": source,
                    "search_term_profile_count": 0,
                }
            )
        )

    causes = [
        "MOCK",
        "MOCK_NO_CREDENTIALS",
        "MOCK_NO_PROFILES",
        "MOCK_LOOKUP_FAILED",
        "MOCK_NO_BINDINGS",
    ]
    texts = {c: banner(c) for c in causes}
    # 每个成因一句自己的话：任何两个相同都意味着有一种成因被说成了另一种。
    assert len(set(texts.values())) == len(causes), "有成因共用了同一句横幅"
    # 每一支都必须点名它自己要人去动的那个东西。
    assert "未设 ADS_CP_STRATEGY_LX_ENABLED" in texts["MOCK"]
    assert "LX_MCP_KEY" in texts["MOCK_NO_CREDENTIALS"]
    assert "ADS_CP_SYNC_PROFILES" in texts["MOCK_NO_PROFILES"]
    assert "网关调用失败" in texts["MOCK_LOOKUP_FAILED"]
    assert "sid 与币种" in texts["MOCK_NO_BINDINGS"]
    # 开关已经设过的那几支，绝不许再叫人去设它。
    for cause in causes[1:]:
        assert "未设 ADS_CP_STRATEGY_LX_ENABLED" not in texts[cause], cause
    # 分成因不能把结论说丢了：每一支都仍要说清候选是假的，且都不许提真实关键词。
    for cause, text in texts.items():
        assert "否定词候选仍为演示 Mock" in text, cause
        assert "真实关键词" not in text, cause
    # 不认识的取值不许被当成任何一种已知成因蒙混过去。
    unknown = banner("MOCK_SOMETHING_NEW")
    assert "成因未知" in unknown
    assert unknown not in texts.values()


def test_empty_sync_whitelist_binds_no_store_for_the_strategy_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个 ADS_CP_SYNC_PROFILES，两条链路此前失败方向相反。

    SyncEngine 空白名单直接抛 SYNC_NO_ALLOWED_PROFILES（sync.py:382，§2 fail-closed），
    而策略侧的绑定写的是 `allowed and pid not in allowed`——空集合短路成假，
    于是**整个账户的每个店**都被绑上。有人清空白名单正是为了停止碰真实店铺，
    候选生成却照样在读全部店；界面上还会并排显示「0 个店铺在同步白名单内」
    与「N 个店铺已绑定」这一对自相矛盾的数字。

    本测试不触网：白名单为空时必须在任何网关调用之前就返回空表。
    """
    from ads_control_plane.api import local_demo

    monkeypatch.setenv(ENV_LX_MCP_KEY, "test-key")
    monkeypatch.setenv(ENV_LX_MCP_URL, "https://lx.invalid/test-mcp")
    monkeypatch.setenv("ADS_CP_STRATEGY_LX_ENABLED", "1")
    monkeypatch.setenv(ENV_SYNC_PROFILES, "")

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("空白名单下不得发起任何网关调用")

    monkeypatch.setattr(local_demo, "LxMcpReadClient", explode)
    assert local_demo._lx_search_term_bindings(new_canonical_id()) == ({}, "MOCK_NO_PROFILES")
    body = TestClient(build_local_demo_app()).get("/dev/runtime-config").json()
    # 成因必须点到这一条上：白名单空。它是 .env.example 的缺省态，也就是首次运行的
    # 常态——界面若说「拿不到 sid / 没声明币种」，人会去查店铺 sid、去配币种覆盖，
    # 两件都白做，而真正要设的 ADS_CP_SYNC_PROFILES 一个字都没被提到。
    assert body["search_term_source"] == "MOCK_NO_PROFILES"
    assert body["search_term_profile_count"] == 0


def test_mandate_currency_must_match_the_store_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """签发期就拒币种不符，别让人签一份物理上跑不通的合同。

    放行到运行期的后果不是「运行时报个错」：生成走 MCP 面，CURRENCY_MISMATCH
    在 Web 界面上一次都不会出现。授权书卡片一直显示「生效中」，「待批」页签一直
    空着并安慰人说候选会在生成之后出现在这里。人于是一直等，而每一块界面都在说一切正常。

    mandate.py:157 的 _scope_belongs_to_this_profile 为完全相同的病写了兄弟闸，
    理由逐字相同。币种这一条一直没配上。
    """
    for var in (ENV_LX_MCP_KEY, ENV_LX_MCP_URL, ENV_SYNC_PROFILES):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(build_local_demo_app())
    res = client.post(
        "/mandates", headers=bearer(DEMO_OWNER_TOKEN), json={**ISSUE_BODY, "currency": "EUR"}
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "CURRENCY_MISMATCH"
    # 报错必须点名两个币种：只说「不一致」等于让人继续猜。
    assert "USD" in detail["message"] and "EUR" in detail["message"]
    # 对得上的照签不误。
    assert client.post(
        "/mandates", headers=bearer(DEMO_OWNER_TOKEN), json=ISSUE_BODY
    ).status_code in (200, 201)


def test_unknown_store_currency_does_not_block_issuing(monkeypatch: pytest.MonkeyPatch) -> None:
    """查不到该店币种时不设闸：装作能校验比不校验更坏。"""
    for var in (ENV_LX_MCP_KEY, ENV_LX_MCP_URL, ENV_SYNC_PROFILES):
        monkeypatch.delenv(var, raising=False)
    from ads_control_plane.api import local_demo

    assert local_demo.profile_currency("profile-never-seen") is None


def test_candidate_evidence_carries_the_window_on_every_surface() -> None:
    """「这批数字统计的是哪一段」此前在三个出口上全都不存在。

    CandidateEvidence 一直带着 window_start / window_end / data_as_of
    （negation.py:117），序列化时被丢掉。于是卡片上「生成于今天」与表里的
    「转化 0」并排出现，人读成「今天查的，这个词到今天一单没出」——而窗口右端
    被归因滞后刻意往回推了几天，最近那几天根本没看。两个数字各自都对，
    摆在一起就把一个刻意的滞后变成了不存在。
    """
    client = TestClient(build_local_demo_app())
    app_js = client.get("/ui/app.js").text
    # 前端必须真的读它，否则又是一个「服务端算了、人看不到」。
    assert "window_start" in app_js and "统计区间" in app_js


async def test_mandate_run_from_mcp_shows_up_on_the_web_face() -> None:
    """MCP 面跑一次 → Web 面的授权书卡片上看得见。

    2026-08-30 排查 #23：授权书签发之后这张卡片再也不会变化。四种「这份授权根本
    跑不通」与「一切正常」在界面上逐字同形。修法要求 MCP 工具面与审批面共用同一份
    运行流水——各建一个的话每处单测都会通过，而人在界面上永远看到「还没跑过」。
    这条端到端就是那个共用关系的守卫。
    """
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            issued = await api.post("/mandates", headers=bearer(DEMO_OWNER_TOKEN), json=ISSUE_BODY)
            assert issued.status_code == 200
            mandate = issued.json()
            # 签完还没跑过：说「不知道」，不说「一切正常」。
            assert mandate["run_count_known"] is False
            assert mandate["last_outcome"] is None

        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_CODEX_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "generate_negation_candidate_set",
                {
                    "profile_external_id": "profile-A",
                    "mandate_id": mandate["mandate_id"],
                },
            )
            assert not result.is_error
            payload = getattr(result, "structured_content", None) or json.loads(
                result.content[0].text  # type: ignore[union-attr]
            )
            assert payload["set_id"]

        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            listed = await api.get("/mandates", headers=bearer(DEMO_OWNER_TOKEN))
            row = next(
                m for m in listed.json()["mandates"] if m["mandate_id"] == mandate["mandate_id"]
            )
            assert row["run_count_known"] is True
            assert row["last_outcome"] == "CANDIDATES"
            assert row["needs_attention"] is False
            assert row["last_run_at"] is not None
            assert row["recent_runs"][0]["set_id"] == payload["set_id"]


async def test_run_that_produces_nothing_is_visible_and_costs_the_quota() -> None:
    """跑不出候选的运行同样要留痕、同样消耗配额。

    此前配额从**候选集合数**上算，跑不出候选就不计数——而跑不出候选正是配置错了的
    表现（币种签错、没接数据源、作用域全挡掉、整批太旧）。max_runs_per_day=1 的
    授权书于是可以被无限次触发，每次对真实源都是一轮多页读取（QPS=1）。

    这里用「作用域圈了一个本店不存在的广告组」构造出一次真正跑得起来、却一无所获的
    运行：旧口径下它不创建集合、不计配额，第二次调用会照常放行。
    """
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            issued = await api.post(
                "/mandates",
                headers=bearer(DEMO_OWNER_TOKEN),
                json={
                    **ISSUE_BODY,
                    "scope": {
                        "kind": "OBJECTS",
                        "items": [
                            {
                                "level": "ad_group",
                                "external_id": "ag-not-in-this-shop",
                                "profile_external_id": "profile-A",
                            }
                        ],
                    },
                },
            )
            assert issued.status_code == 200
            mandate = issued.json()

        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_CODEX_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            first = await session.call_tool(
                "generate_negation_candidate_set",
                {"profile_external_id": "profile-A", "mandate_id": mandate["mandate_id"]},
            )
            assert not first.is_error
            payload = getattr(first, "structured_content", None) or json.loads(
                first.content[0].text  # type: ignore[union-attr]
            )
            assert payload["set_id"] is None  # 一无所获：旧口径下这次运行等于没发生
            assert payload["scope_filtered_out"] == 10
            second = await session.call_tool(
                "generate_negation_candidate_set",
                {"profile_external_id": "profile-A", "mandate_id": mandate["mandate_id"]},
            )
            assert second.is_error
            assert "RUN_BUDGET_EXCEEDED" in second.content[0].text  # type: ignore[union-attr]

        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as api:
            row = next(
                m
                for m in (await api.get("/mandates", headers=bearer(DEMO_OWNER_TOKEN))).json()[
                    "mandates"
                ]
                if m["mandate_id"] == mandate["mandate_id"]
            )
            # 界面上这份授权不再只是「生效中 + 待批空空如也」。
            assert row["last_outcome"] == "SCOPE_EMPTY"
            assert row["needs_attention"] is True
            #: 第二次调用之所以被拒，正是因为配额用完了——而人在界面上要看得见
            #: 这件事，否则他还会再叫 AI 跑一次（那次拒绝同样不留痕）。
            assert row["runs_today"] == 1
            assert row["runs_remaining_today"] == 0
            assert row["next_run_allowed_at"] is not None


@pytest.mark.anyio
async def test_the_runbook_expected_output_is_what_the_demo_actually_returns() -> None:
    """runbook 第②步印着一组「预期输出」，人拿它逐项核对自己的第一次运行。

    这组数字漂过两次：改演示种子（加一条 ASIN 记录）时没跟着改，于是文档说
    9/9/1、实际 10/10/2，而同一份文档 §① 已经写了「10 条…3 条候选 + 2 条 ABSTAIN」——
    两处自相矛盾。更糟的是那条被写没的弃权正是演示专门埋的一课：ASIN 型的浪费
    否不掉。照着旧数字核对的人会以为自己跑出来的东西不对，或者干脆不去看第二条弃权。

    这道守卫把文档里的数字和演示实际返回绑死：改种子必须改文档，否则先在这里红。
    """
    runbook = (Path(__file__).resolve().parents[2] / "docs/runbook-local-demo.md").read_text(
        encoding="utf-8"
    )
    app = build_local_demo_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=BASE_URL, headers=bearer(DEMO_CODEX_TOKEN)
            ) as mcp_http,
            streamable_http_client(f"{BASE_URL}/mcp", http_client=mcp_http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "generate_negation_candidate_set", {"profile_external_id": "profile-A"}
            )
            payload = getattr(result, "structured_content", None) or json.loads(
                result.content[0].text  # type: ignore[union-attr]
            )

    for field in (
        "evaluated_ad_group_terms",
        "distinct_search_terms",
        "candidate_count",
        "asin_abstain_count",
    ):
        claim = f"`{field}: {payload[field]}`"
        assert claim in runbook, f"runbook 第②步的 {field} 与演示实际返回对不上（实际 {claim}）"

    # 每一条弃权都要在文档里点名——只给总数，人不知道第二条是另一种处境。
    for abstain in payload["abstains"]:
        assert abstain["search_term"] in runbook, f"弃权 {abstain['search_term']} 在 runbook 里没提"
        assert abstain["reason"] in runbook, f"弃权原因 {abstain['reason']} 在 runbook 里没提"


# --- 演示数据自己不许自相矛盾（2026-09-07 排查） ---
#
# 演示是这套系统教人「真东西长什么样」的唯一途径。它此前有两处硬伤：
#
# 1. 每一行都没有 sales，而工作台有一整列「广告销售额」——那一列恒为「—」，
#    旁边却并排印着算得出来的 ACOS。人要么不信这些数，要么学会把不一致当常态。
# 2. target:t-1 是 orders=0 配 acos=0.63。零订单就是零销售额，零销售额的 ACOS
#    是无穷大；领星用 99999999 表示它，app.js 为此专门写了一颗「无销售」芯片，
#    注释还写着这类广告「最该被看见——花了钱一单没出」。演示一次都没展示过它。


ACOS_INFINITE = "99999999"


def _demo_seed_metrics() -> list[dict[str, str]]:
    """演示种子里所有对象的窗口指标。

    真把种子灌进仓库再读回来，而不是扫源码字面量——扫源码会把注释里的数字
    一并扫进来，且换个写法（把 dict 拆成变量）断言就静默失效。
    """
    from ads_control_plane.api.local_demo import DEMO_PROFILE, _seed_mirror
    from ads_control_plane.mirror.repository import InMemorySnapshotRepository

    repo = InMemorySnapshotRepository()
    _seed_mirror(repo, datetime.now(UTC))
    rows = [dict(s.metrics) for s in repo.current(DEMO_PROFILE) if s.metrics]
    assert rows, "演示种子一条带指标的对象都没有了"
    return rows


def test_the_demo_never_shows_an_acos_without_the_sales_it_came_from() -> None:
    """有 ACOS 就必须有销售额——否则整列「—」旁边并排着算得出来的 ACOS。"""
    for m in _demo_seed_metrics():
        if "acos" in m:
            assert "sales" in m, f"这一行有 ACOS 却没有销售额：{m}"


def test_the_demo_numbers_actually_divide_out() -> None:
    """spends / sales 必须约等于印出来的 ACOS。

    钉的是算术，不是字段存在性：随手补一个 sales 数字上去照样能过存在性断言，
    而人一除就发现对不上。
    """
    for m in _demo_seed_metrics():
        if m.get("acos") in (None, ACOS_INFINITE):
            continue
        spends, sales, acos = (Decimal(m["spends"]), Decimal(m["sales"]), Decimal(m["acos"]))
        assert sales > 0, f"有限 ACOS 却零销售额：{m}"
        got = spends / sales
        assert abs(got - acos) <= Decimal("0.005"), (
            f"花费 {spends} ÷ 销售额 {sales} = {got:.4f}，而这一行印的 ACOS 是 {acos}"
        )


def test_zero_orders_in_the_demo_means_infinite_acos_not_a_tidy_number() -> None:
    """零订单的行必须走无穷大哨兵，不许配一个有限的 ACOS。"""
    for m in _demo_seed_metrics():
        if m.get("orders") == "0":
            assert m.get("acos") == ACOS_INFINITE, f"零订单却印着有限 ACOS，这在数学上不成立：{m}"
            sales = m.get("sales")
            assert sales is not None and Decimal(sales) == 0, f"零订单却有销售额：{m}"


def test_the_demo_actually_exercises_the_no_sales_chip() -> None:
    """至少一行走无穷大分支——否则 app.js 里那颗「无销售」芯片谁都没见过。

    那段注释自己说这类广告「最该被看见」。演示不展示它，等于这套系统最想让人
    注意的那一类广告，在演示里根本不存在。
    """
    rows = _demo_seed_metrics()
    assert any(m.get("acos") == ACOS_INFINITE for m in rows), (
        "演示里没有一行是「花了钱一单没出」——app.js 的无销售分支从没被人看见过"
    )
    ui = (
        Path(__file__).resolve().parents[2] / "src/ads_control_plane/api/ui_static/app.js"
    ).read_text(encoding="utf-8")
    assert ACOS_INFINITE in ui, "界面已经不认这个哨兵了，那演示里这一行会印成一串数字"


def test_the_demo_still_works_after_the_server_has_been_up_all_day() -> None:
    """演示服务开一整天之后，还得是那份能走完的演示。

    种子的 data_as_of 是在进程启动那一刻打的（now - 2h），而默认参数包
    max_data_staleness_hours=24。服务开满约 22 小时后，同一批种子数据从
    「3 个候选 + 弃权若干」变成「数据太旧，全部弃权」——runbook 的第③④⑤步
    全部走不下去，而界面给出的下一步是「等新数据」：演示里永远不会有新数据，
    那句话等不到头（2026-09-07 排查）。

    修法是让种子跟着时钟一起变旧、**相对新旧不变**，而不是把新鲜度闸拆掉：
    故意做旧的那条（stale=True，48h）在任何时刻都仍然触发 STALE_DATA。
    """
    from datetime import UTC, datetime, timedelta

    from ads_control_plane.api.local_demo import DEMO_PROFILE, _build_search_term_source
    from ads_control_plane.canonical.ids import new_canonical_id
    from ads_control_plane.canonical.money import Money
    from ads_control_plane.strategies.negation import (
        NegationParameterPack,
        generate_negation_candidates,
    )

    boot = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    # 走真正的组合根：这条测试要管的正是「演示服务**实际**挂的那个源」。
    # 自己 new 一个 Mock 只能证明 Mock 有这个能力，证明不了演示打开了它。
    source = _build_search_term_source(new_canonical_id(), boot)

    pack = NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )

    def run_at(moment: datetime):
        fetch = source.fetch_search_term_performance(DEMO_PROFILE, pack.lookback_days, moment)
        return generate_negation_candidates(
            records=fetch.records, pack=pack, now=moment, id_factory=new_canonical_id
        )

    fresh = run_at(boot + timedelta(minutes=5))
    # 开机 30 小时后：种子本身早已「超过 24 小时」，但相对新旧没变。
    later = run_at(boot + timedelta(hours=30))

    assert len(fresh.candidates) > 0, "刚开机就没有候选，这条测试的前提就不成立"
    assert len(later.candidates) == len(fresh.candidates), (
        "开一天之后候选就没了——runbook 的第③④⑤步全部走不下去，"
        "而界面叫人「等新数据」，演示里永远等不到"
    )
    # 新鲜度闸没有被拆掉：故意做旧的那条仍然弃权。
    stale_reasons = {a.reason.value for a in later.abstains}
    assert "STALE_DATA" in stale_reasons, "把闸拆了才让演示不坏，那是另一个缺陷"
