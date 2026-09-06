"""本地演示启动脚本：单进程挂审批 API + /mcp + /ui，只绑 127.0.0.1。

用法：uv run python scripts/serve_local_demo.py [--port 8788]
LOCAL DEMO ONLY：固定 demo token，禁止用于任何真实环境。
**数据是不是真的由 env 决定，不是由「LOCAL DEMO」这四个字决定**：
LX_MCP_KEY/LX_MCP_URL 配齐 → 「同步镜像」调用领星生产 API；
再加 ADS_CP_STRATEGY_LX_ENABLED=1 → 否定词候选也来自真实店铺的真实搜索词，
批准导出的 CSV 拿去领星执行会否掉真实关键词。启动横幅按实际状态逐条播报（见 main）。
"""

import argparse

import uvicorn

from ads_control_plane.api.local_demo import (
    DEMO_TOKEN_ROWS,
    build_local_demo_app,
    runtime_channel_status,
)

HOST = "127.0.0.1"  # 本地演示只绑回环地址，不对外监听


#: 候选仍是演示数据的成因 → 那一行横幅。每条只说这一种成因，并点名要动的那个开关；
#: 不认识的取值走 UNKNOWN_MOCK_REASON——宁可说「说不出成因」，不许替它挑一个。
UNKNOWN_MOCK_REASON = "MOCK_REASON_UNKNOWN"
MOCK_REASON_LINES = {
    "MOCK": "否定词候选仍为演示 Mock（未设 ADS_CP_STRATEGY_LX_ENABLED）",
    "MOCK_NO_CREDENTIALS": "⚠ 否定词候选仍为演示 Mock：策略开关已开，但 LX_MCP_KEY/LX_MCP_URL 缺失",
    "MOCK_NO_PROFILES": "⚠ 否定词候选仍为演示 Mock：策略开关已开，但同步白名单 "
    "ADS_CP_SYNC_PROFILES 是空的（空 = 一个店都不许碰）",
    "MOCK_LOOKUP_FAILED": "⚠ 否定词候选仍为演示 Mock：策略开关已开，但取店铺名录时"
    "网关调用失败（key/URL/网络）——不是绑定配错了",
    "MOCK_NO_BINDINGS": "⚠ 否定词候选仍为演示 Mock：策略开关已开、名录也取到了，"
    "但白名单内没有一个店同时拿得到 sid 与币种",
    UNKNOWN_MOCK_REASON: "⚠ 否定词候选仍为演示 Mock（成因未知："
    "服务端报了一个本脚本不认识的通道取值）",
}


def channel_banner_lines(channel: dict[str, object]) -> list[str]:
    """通道状态 → 横幅正文。抽成纯函数是为了能被测试直接断言。

    只打印计数与布尔，key / URL / 店铺 ID 一律不上 stdout。
    """
    if not channel["lx_channel_configured"]:
        return ["  ads-control-plane LOCAL DEMO · MOCK DATA ONLY · 无真实凭据 / 无真实店铺"]
    lines = [
        f"  ads-control-plane LOCAL DEMO · ⚠ 已配置领星真实通道："
        f"{channel['sync_profile_count']} 个店铺在同步白名单内",
        "  「同步镜像」将调用领星生产 API",
    ]
    # 候选是不是真的必须单独说。此前这里写死「身份与审批数据仍为演示 Mock」，
    # 而审批数据就是待批队列里那些候选——打开策略通道后它们来自真实店铺的真实
    # 搜索词，批准导出的 CSV 拿去领星执行会否掉真实关键词。一份点着名的清单
    # 停在半路，读起来就是说全了，这比含糊更坏（2026-08-30 排查 #11）。
    if channel["search_term_source"] == "LINGXING":
        lines.append(
            f"  ⚠ 否定词候选也读真实搜索词（{channel['search_term_profile_count']} 个店铺已绑定）："
            "待批队列里的词来自真实店铺，批准导出的 CSV 会否掉真实关键词"
        )
    else:
        # 挂 Mock 的成因逐个说。共用一句话时横幅只能点名其中一种，而点中的偏偏不是
        # 最常见的那种——ADS_CP_SYNC_PROFILES 在 .env.example 里缺省为空，首次运行
        # 必然落在 MOCK_NO_PROFILES，人却被叫去查店铺 sid 和币种，两件都白做。
        lines.append(
            "  "
            + MOCK_REASON_LINES.get(
                str(channel["search_term_source"]), MOCK_REASON_LINES[UNKNOWN_MOCK_REASON]
            )
        )
    lines.append("  身份（demo token）始终是演示用的固定串")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="ads-control-plane 本地演示服务")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（默认 8788）")
    args = parser.parse_args()
    base = f"http://{HOST}:{args.port}"

    # 横幅按数据通道的真实状态播报，不再硬编码「MOCK DATA ONLY」——2026-08-29
    # 排查结论（runtime-1）：env 配齐领星通道时，硬编码文案与部署状态逐字相反，
    # 人会把真实店铺数据当假的随便点。判定与 /dev/runtime-config 同一口径；
    # 横幅只打印计数，key/URL/店铺 ID 一律不上 stdout。
    #
    # **必须先建 app 再读状态**（2026-08-30 排查 #11）：搜索词通道是在组合根
    # build_local_demo_app() → _build_search_term_source 里才决定的。此前这两行
    # 是反的，横幅读到的恒是模块默认值 MOCK，而同一进程的 /dev/runtime-config
    # 随后回 LINGXING——横幅与端点当场自相矛盾，且横幅那句是假的。
    app = build_local_demo_app()
    channel = runtime_channel_status()
    line = "=" * 72
    print(line)
    for text in channel_banner_lines(channel):
        print(text)
    print(line)
    print(f"  UI   {base}/ui/")
    print(f"  API  {base}/candidate-sets  /mandates  (Authorization: Bearer <token>)")
    print(f"  MCP  {base}/mcp  (streamable HTTP, 同一套 Bearer token)")
    print(f"  DEV  {base}/dev/identities  (demo token 清单)")
    print(line)
    for token, display_name, identity, capabilities in DEMO_TOKEN_ROWS:
        print(f"  {token:<18} {display_name}  ({identity})")
        print(f"  {'':<18} {capabilities}")
    print(line, flush=True)  # stdout 被重定向时也保证横幅先于 uvicorn 日志落盘

    uvicorn.run(app, host=HOST, port=args.port)


if __name__ == "__main__":
    main()
