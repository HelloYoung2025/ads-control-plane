# 本机 Codex CLI 接入本地演示 MCP

日期：2026-08-28 ｜ 实测环境：codex-cli 0.146.0（macOS 本机）

目标：让本机 Codex CLI 以 **AI 身份（demo-codex-token）** 连上本地演示服务的 Internal MCP 面，产出候选集合供人审批。演示服务的启动与十分钟走查见 [runbook-local-demo.md](runbook-local-demo.md)。

安全前提：demo-codex-token 是写死在演示组合根里的 Mock token，只在 127.0.0.1:8788 上有效，与真实领星/Amazon 零连接。真实部署会换成公司 OIDC token，本文全部内容仅适用本地演示。

## 结论（实测 2026-08-28）

本机 codex-cli 0.146.0 **原生支持 streamable HTTP MCP**，无需任何 experimental 开关：

- `codex mcp add <NAME> --url <URL> --bearer-token-env-var <ENV_VAR>` 是一等公民命令（`codex mcp add --help` 原文：`--url <URL>`＝"URL for a streamable HTTP MCP server"）。
- config.toml 的 url 型 `[mcp_servers.<name>]` 支持 `bearer_token_env_var`（推荐，token 不落盘）与 `http_headers`（手写 Authorization 头）。
- 当前官方 config reference 已无旧版的 `experimental_use_rmcp_client` 开关。若你的 codex 版本较旧、`codex mcp add --help` 里没有 `--url` 选项：先升级 codex；不升级则暂用 runbook 里的冒烟脚本验证 MCP 面（我们未在旧版本上实测，不做兼容性承诺）。

## 配置（二选一；我们不代改 ~/.codex/config.toml，请自行执行/粘贴）

前提：演示服务已按 runbook 启动，MCP 端点为 `http://127.0.0.1:8788/mcp`。

### 方式 A（推荐）：bearer_token_env_var，token 不写入 config.toml

```bash
# token 放环境变量；必须在启动 codex 的那个 shell 里可见（建议写入 ~/.zshrc）
export ADS_CP_MCP_TOKEN=demo-codex-token
codex mcp add ads-control-plane --url http://127.0.0.1:8788/mcp --bearer-token-env-var ADS_CP_MCP_TOKEN
```

等效 TOML（上述命令会写入 ~/.codex/config.toml；手工粘贴亦可）：

```toml
[mcp_servers.ads-control-plane]
url = "http://127.0.0.1:8788/mcp"
bearer_token_env_var = "ADS_CP_MCP_TOKEN"
```

### 方式 B：http_headers 静态头（token 明文落盘）

仅因为本演示 token 本来就是公开 Mock 值才可接受；任何真实 token 禁用此方式。

```toml
[mcp_servers.ads-control-plane]
url = "http://127.0.0.1:8788/mcp"
http_headers = { "Authorization" = "Bearer demo-codex-token" }
```

## 验证

```bash
codex mcp list                              # 应出现 ads-control-plane
codex mcp get ads-control-plane --json      # 查看已写入的配置
```

进入 `codex` 会话后：`/mcp` 查看连接状态；然后直接说"调用 ads-control-plane 的 whoami"——返回的 `principal_type` 应为 `AI_CLIENT`。身份由服务端从 Bearer token 解析，工具参数里没有任何身份字段可传。

## 坑（均为 2026-08-28 实测或官方文档记载）

1. **`bearer_token_env_var` 存的是环境变量名，不是 token 值。** 该变量必须在启动 codex 的进程环境里已 export，否则鉴权失败，且报错会误导性地建议 `codex mcp login`（上游 issue #26760 标题所述）。先 `export`，再启动 codex。
2. **`--env KEY=VALUE` 只对 stdio 型 MCP 有效**，不能用它给 HTTP 型传 token。
3. **URL 必须用 `127.0.0.1`（或 localhost）。** 服务端开启了 SDK 默认的 DNS-rebinding 防护，Host 非 127.0.0.1/localhost/[::1] 的请求会被 421 拒绝；服务也只绑 127.0.0.1。
4. **名称冲突自查：** `codex mcp list` 先看一眼现有条目；本机实测已有的 4 个节名与 `ads-control-plane` 不冲突。
5. **权限边界即演示看点：** demo-codex-token 是 AI 身份，工具面只有 whoami / list_authorized_scopes / generate_negation_candidate_set / list_negation_candidate_sets。让 Codex 生成候选可以；让它审批、签授权书，会在服务端被 SoD 拒绝（这不是故障，见 runbook 第 5 步）。

## 来源

- https://learn.chatgpt.com/docs/extend/mcp?surface=cli （developers.openai.com/codex 的 MCP 文档，308 重定向至此）
- https://learn.chatgpt.com/docs/config-file/config-reference （url 型字段：`bearer_token_env_var` / `http_headers` / `env_http_headers` / `startup_timeout_sec` 等）
- https://github.com/openai/codex/pull/4904 （streamable HTTP + bearer token 支持）
