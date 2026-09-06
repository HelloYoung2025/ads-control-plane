# R3 调研：MCP 规范、安全与 AI 客户端集成现状（用于广告控制平面设计文档假设校验）

调研执行日期：2026-08-28。所有事实均来自当日经 WebFetch/WebSearch 获取的公开文档；每条事实标注 [来源URL, 访问日期]。仅只读调研，未修改任何项目文件。

---

## 1. MCP 规范版本状态（截至 2026-08）

- **2025-06-18 已不是最新版本。** 官方 versioning 页明确："The **current** protocol version is **2026-07-28**"。中间还存在 2025-11-25 版本（现为过去版本）。修订状态分 Draft / Current / Final。[https://modelcontextprotocol.io/specification/versioning, 2026-08-28]
- 2026-07-28 是一次**向后不兼容的大改版**（"stateless core"）。主要变更（changelog 原文要点）：
  1. **移除协议级 session 与 `Mcp-Session-Id` 头**（SEP-2567）："Remove protocol-level sessions and the `Mcp-Session-Id` header from the Streamable HTTP transport. List endpoints … no longer vary per-connection. Servers that need cross-call state use explicit, server-minted handles passed as ordinary tool arguments."
  2. **移除 `initialize` 握手**，每个请求在 `_meta` 携带 `io.modelcontextprotocol/protocolVersion`、`clientCapabilities`、`clientInfo`（SEP-2575）；新增强制 RPC `server/discover`。
  3. **MRTR（Multi Round-Trip Requests）取代服务端主动发起的请求**：`roots/list`、`sampling/createMessage`、`elicitation/create` 不再作为 server→client 请求下发，而是服务器返回 `InputRequiredResult`（`resultType: "input_required"`），客户端带 `inputResponses` 重试原请求（SEP-2322）。
  4. 移除 GET SSE 长连接与 `Last-Event-ID` 可恢复流；变更通知改走 `subscriptions/listen`。
  5. **Roots、Sampling、Logging 三个特性被列为 Deprecated**（SEP-2577），迁移建议中 Sampling 的替代是"integrate directly with LLM provider APIs"。
  6. **Dynamic Client Registration (RFC 7591) 被列为 Deprecated**，由 Client ID Metadata Documents (CIMD, draft-ietf-oauth-client-id-metadata-document) 取代（PR #2858）。
  7. 新增 `Mcp-Method` / `Mcp-Name` 必需请求头与 `x-mcp-header` 参数镜像（SEP-2243），头体不一致返回 `-32020 HeaderMismatch`。
  [https://modelcontextprotocol.io/specification/2026-07-28/changelog, 2026-08-28]
- 版本协商改为逐请求：`_meta` 中的 `io.modelcontextprotocol/protocolVersion` + Streamable HTTP 的 `MCP-Protocol-Version` 头；不支持则返回 `UnsupportedProtocolVersionError`。与 2025-11-25 及更早"握手时代"版本的互操作有专门的 Backward Compatibility 规则。[https://modelcontextprotocol.io/specification/versioning, 2026-08-28]

**对设计文档的含义**：凡按 2025-06-18 描述"会话（Mcp-Session-Id）作为控制点/绑定点"的内容都需要标注版本适用范围——2026-07-28 起协议无 session，跨调用状态靠 server-minted handle（规范明言 handle "is a name, not a capability"，服务端每次调用都必须校验授权）。[https://modelcontextprotocol.io/specification/2026-07-28/server/tools（Stateful Tools 节）, 2026-08-28]

## 2. Streamable HTTP transport：会话与认证要求

- 2026-07-28 版（当前）：
  - "Servers **MUST** validate the `Origin` header on all incoming connections to prevent DNS rebinding attacks"（无效则 403）。
  - "When running locally, servers **SHOULD** bind only to localhost (127.0.0.1)"。
  - "Servers **SHOULD** implement proper authentication for all connections."
  - 每个 JSON-RPC 消息一个 POST；每个 POST 必须带 `MCP-Protocol-Version`、`Mcp-Method`（以及 tools/call 等的 `Mcp-Name`）；服务器必须校验头与 body 一致（`HeaderMismatch -32020`）。
  - 旧版 session 机制被显式移除："An `Mcp-Session-Id` header on a request: ignore it, and do not mint or echo session IDs."
  [https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http, 2026-08-28]
- 认证与 session 的关系（安全最佳实践，适用于仍实现 session 的 2025-03-26~2025-11-25 版）："MCP servers that implement authorization **MUST** verify all inbound requests. MCP Servers **MUST NOT** use sessions for authentication." 且 "MCP servers **MUST** use secure, non-deterministic session IDs"，并 SHOULD 将 session ID 绑定用户信息（如 `<user_id>:<session_id>`）。[https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices（站点当前渲染引用 2025-11-25 authorization 链接）, 2026-08-28]
- 授权与请求的关系（2025-06-18 与 2026-07-28 一致）："authorization **MUST** be included in every HTTP request from client to server"（2025-06-18 版还补充 "even if they are part of the same logical session"）；"Access tokens **MUST NOT** be included in the URI query string"。[https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization; https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization, 2026-08-28]

## 3. Authorization 章节：OAuth 2.1 / RFC 8707 / audience binding

两个版本（2025-06-18 与当前 2026-07-28）的核心规范性要求一致，逐条（均为原文引用）：

- OAuth 2.1："Authorization servers **MUST** implement OAuth 2.1 with appropriate security measures for both confidential and public clients."（基于 draft-ietf-oauth-v2-1-13）[两版 authorization 页, 2026-08-28]
- 资源发现："MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata (RFC9728)"；401 必须带 `WWW-Authenticate` 指向 resource metadata。[同上]
- **RFC 8707 resource 参数（客户端侧）**："MCP clients **MUST** implement Resource Indicators for OAuth 2.0 as defined in RFC 8707 … The `resource` parameter: 1. **MUST** be included in both authorization requests and token requests. 2. **MUST** identify the MCP server … 3. **MUST** use the canonical URI of the MCP server." 且 "MCP clients **MUST** send this parameter regardless of whether authorization servers support it."[同上]
- **Audience binding（服务端侧）**："MCP servers **MUST** validate that access tokens were issued specifically for them as the intended audience, according to RFC 8707 Section 2."；"MCP servers **MUST** only accept tokens specifically intended for themselves and **MUST** reject tokens that do not include them in the audience claim…"[同上；security-considerations 页同文]
- **Token passthrough 禁令**："MCP servers **MUST NOT** accept or transit any other tokens."；"If the MCP server makes requests to upstream APIs, it may act as an OAuth client to them. The access token used at the upstream API is a separate token, issued by the upstream authorization server. The MCP server **MUST NOT** pass through the token it received from the MCP client."[同上]
- PKCE："MCP clients **MUST** implement PKCE"；2026-07-28 追加 "**MUST** verify PKCE support before proceeding"（metadata 无 `code_challenge_methods_supported` 则必须拒绝继续）、"**MUST** use the `S256` code challenge method when technically capable"。[https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations, 2026-08-28]
- 2026-07-28 新增/变化：客户端注册优先 **Client ID Metadata Documents**（SHOULD），DCR 降为 MAY 且 Deprecated；RFC 9207 `iss` 校验（客户端 MUST 校验存在的 `iss`）；scope 挑战/step-up authorization flow（403 `insufficient_scope` + `WWW-Authenticate scope="…"`，客户端做 scope 并集后升级授权）。[https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization, 2026-08-28]
- **核心授权规范中没有 RFC 8693 (OAuth Token Exchange)**：2026-07-28 authorization 页的 Standards Compliance 列表不含 RFC 8693；token exchange 相关能力位于**授权扩展**（见 §5）。[同上, 2026-08-28]

## 4. Tool annotations（readOnlyHint/destructiveHint）规范原文

- Schema（2025-06-18 schema.ts `ToolAnnotations` JSDoc，逐字）：
  > "Additional properties describing a Tool to clients. NOTE: all properties in ToolAnnotations are **hints**. They are not guaranteed to provide a faithful description of tool behavior (including descriptive properties like `title`). Clients should never make tool use decisions based on ToolAnnotations received from untrusted servers."
  字段定义：`readOnlyHint`（"If true, the tool does not modify its environment. Default: false"）、`destructiveHint`（"If true, the tool may perform destructive updates … If false, only additive updates. Default: true"）、`idempotentHint`（Default: false）、`openWorldHint`（Default: true）。[https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2025-06-18/schema.ts, 2026-08-28]
- Tools 页 Warning（2025-06-18 与 2026-07-28 同文，规范性 MUST）：
  > "For trust & safety and security, clients **MUST** consider tool annotations to be untrusted unless they come from trusted servers."
  [https://modelcontextprotocol.io/specification/2025-06-18/server/tools; https://modelcontextprotocol.io/specification/2026-07-28/server/tools, 2026-08-28]
- 规范总纲 Security 原则："descriptions of tool behavior such as annotations should be considered untrusted, unless obtained from a trusted server."[https://modelcontextprotocol.io/specification/2025-06-18, 2026-08-28]
- 同页 Security Considerations 把授权职责压给服务端："Servers **MUST**: Validate all tool inputs / **Implement proper access controls** / Rate limit tool invocations / Sanitize tool outputs."[两版 tools 页, 2026-08-28]

**结论**：规范明确 annotations 是 hints、untrusted，不得作为安全边界；访问控制是服务端 MUST 义务。注意一个现实反差：Codex 的 `writes` 审批模式恰恰以 readOnly 注解为审批依据（见 §7），即客户端体验层在消费这个不可信信号——这正是设计文档要求服务端自行做授权的论据，而非反例。

## 5. Elicitation / Sampling 对"服务端不信任客户端"的影响

- Sampling：**2026-07-28 起 Deprecated**（连同 Roots、Logging），迁移建议为直接接入 LLM 提供商 API。设计文档不应把 sampling 作为长期依赖。[https://modelcontextprotocol.io/specification/2026-07-28/changelog, 2026-08-28]
- Elicitation（2026-07-28，含 2025-11-25 引入的 URL mode）：
  - "Servers **MUST NOT** use form mode elicitation to request sensitive information such as passwords, API keys, access tokens, or payment credentials"；此类信息 "**MUST** use URL mode"。
  - "Servers **MUST NOT** rely on client-provided user identification without server verification, as this can be forged. … Correct: Rely on authorization to identify the user."（服务端不信任客户端的直接规范文本）
  - "Servers **MUST** bind elicitation requests to the client and user identity"；URL mode 防钓鱼："the server **MUST** ensure that the user who started the elicitation request … is the same user who completes the authorization flow."
  - URL mode 第三方授权模式重申 passthrough 禁令："The MCP server **MUST NOT** use the client's credentials for the third-party service: That would be token passthrough, which is forbidden."；第三方凭据 "**MUST NOT** transit through the MCP client"。
  - 客户端义务：MUST 明示是哪个 server 在索取信息、MUST 展示完整 URL 并取得用户显式同意、MUST NOT 自动预取 URL。
  [https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation, 2026-08-28]

## 6. 官方 Security Best Practices（confused deputy / token passthrough / session hijacking）

页面（站点当前渲染版内部链接指向 2025-11-25/2026-07-28 路径；2025-06-18 版同名文档结构一致）：

- **Confused Deputy**：静态 client ID 的 MCP proxy + DCR + consent cookie 组合可被绕过同意。缓解（规范性）："MCP proxy servers **MUST** implement per-client consent"——按 client_id 维护同意注册表、在转发第三方授权前检查、consent cookie 用 `__Host-` 前缀 + Secure/HttpOnly/SameSite、redirect_uri 精确匹配、state 单次使用且仅在同意通过后设置。
- **Token Passthrough**："'Token passthrough' is an anti-pattern … Token passthrough is explicitly forbidden in the authorization specification"。缓解："MCP servers **MUST NOT** accept any tokens that were not explicitly issued for the MCP server." 风险列举含绕过限流/审计断裂/信任边界破坏。
- **Session Hijacking**："MCP servers that implement authorization **MUST** verify all inbound requests. MCP Servers **MUST NOT** use sessions for authentication."；"**MUST** use secure, non-deterministic session IDs"、SHOULD 绑定 `<user_id>:<session_id>`。
- 另含：SSRF（OAuth metadata 发现阶段，client MUST 考虑缓解，SHOULD 强制 HTTPS、封禁私网/169.254.169.254）、本地服务器一键安装 MUST 有同意机制、OAuth authorization URL 校验（MUST 仅允许 http/https、拒绝 `javascript:`/`data:` 等）、**Scope Minimization**（渐进最小权限、`WWW-Authenticate` scope 挑战、反对 omnibus scope）。
[https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices, 2026-08-28]

## 7. OpenAI Codex 的 MCP 配置（config.toml）

- 原 URL `https://developers.openai.com/codex/mcp` 现 **308 重定向**到 `https://learn.chatgpt.com/docs/extend/mcp?surface=cli`（设计文档引用 URL 仍可达但已非规范地址）。[访问 2026-08-28]
- `[mcp_servers.<name>]` 字段现状（官方页提取）：
  - Streamable HTTP：`url`（必填）、`auth`（`oauth` 或 `chatgpt`）、`bearer_token_env_var`、`http_headers`、`env_http_headers`；STDIO：`command`/`args`/`env`/`env_vars`/`cwd`。
  - 通用：**`enabled`**（bool）、**`enabled_tools`**（"Tool allow list"）、**`disabled_tools`**（"Tool deny list (applied after `enabled_tools`)"）、**`default_tools_approval_mode`**（取值 `auto`、`prompt`、`writes`、`approve`）、`startup_timeout_sec`（默认 10）、`tool_timeout_sec`（默认 60）、`required`。
  - OAuth：`[mcp_servers.<name>.oauth]` 支持 `client_id`/`callback_url`；登录命令 `codex mcp login <server-name>`；其他 CLI：`codex mcp add/list`。
  [https://learn.chatgpt.com/docs/extend/mcp?surface=cli（由 https://developers.openai.com/codex/mcp 重定向）, 2026-08-28]
- `writes` 模式语义（第三方发布说明，与官方字段值互证）：`writes` 于 Codex CLI v0.144.0（2026-07）引入，"prompts for tools that aren't marked read-only"——即依据工具声明的注解判断读写，只读工具免审批、写类工具触发审批。[https://codex.danielvaughan.com/2026/07/09/codex-cli-v0144-writes-approval-mode-mcp-auth-ga-usage-credits-ultra-concurrency/, 2026-08-28]
- 因此设计文档的示例 TOML（`url` / `enabled = true` / `enabled_tools = [...]` / `default_tools_approval_mode = "writes"`）与现状字段名、取值一致。注意：`writes` 的读写判定来源是服务器自报的 annotations（untrusted hints，见 §4），属客户端体验层控制。

## 8. OpenAI Responses API remote MCP tool

- 工具对象：`"type": "mcp"`，字段现状：`server_label`、`server_description`、`server_url`、`connector_id`（连接器型）、**`allowed_tools`**（数组，限制可用工具，如 `["roll"]`）、`defer_loading`、**`require_approval`**（`"never"` / `"always"` / 粒度对象如 `{"never": {"tool_names": ["ask_question","read_wiki_structure"]}}`）、**`authorization`**（"OAuth access token for authenticated servers"）、`headers`。
- 审批流：需要审批时 API 产出 `mcp_approval_request` 输出项（含工具名与参数）；调用方回传 `mcp_approval_response`（`approval_request_id` + `approve: true/false`）。**默认行为是每次调用都需审批**（"The tool defaults to requiring approvals for each call"）。
- 官方安全提示原文（要点）："Malicious MCP servers may include hidden instructions designed to make OpenAI models behave unexpectedly"; 建议选官方托管服务器、记录 MCP 通信、注意第三方数据保留策略独立于 OpenAI ZDR。
[https://developers.openai.com/api/docs/guides/tools-connectors-mcp, 2026-08-28]

## 9. Anthropic Claude API MCP connector（对比项）

- 文档已迁移：docs.claude.com → 302 → `https://platform.claude.com/docs/en/agents-and-tools/mcp-connector`。Beta header **`mcp-client-2025-11-20`**（旧版 `mcp-client-2025-04-04` 已弃用）。[访问 2026-08-28]
- 结构（当前版）：`mcp_servers` 数组只描述连接（`type: "url"`、`url`（必须 https）、`name`、`authorization_token`）；工具治理移入 `tools` 数组的 **`mcp_toolset`** 对象：`mcp_server_name` + `default_config` + 按工具名的 `configs`，每工具支持 `enabled`（默认 true）与 `defer_loading`。
  - Allowlist 等价写法：`default_config: {"enabled": false}` + 在 `configs` 中逐工具 `enabled: true`；Denylist：默认开启 + 指定工具 `enabled: false`。旧字段 `tool_configuration.allowed_tools` 标注 Deprecated。
- 能力对比要点：
  - 有：服务器级连接 + 工具 allowlist/denylist/逐工具配置、OAuth Bearer token 透传（调用方自行完成 OAuth 并续期）、多服务器、Streamable HTTP 与 SSE（不支持本地 STDIO）、仅支持 tools（不支持 prompts/resources 经 connector）。
  - **无 API 级审批回环**：不存在 `require_approval`/`mcp_approval_request` 等价物；文档建议用 denylist 关闭写/破坏性工具来实现"人确认后再变更"（确认动作由调用方应用自建）。
  - ZDR 不适用于 MCP connector 数据。
[https://platform.claude.com/docs/en/agents-and-tools/mcp-connector, 2026-08-28]

## 10. MCP 授权扩展：Enterprise-Managed Authorization（与断言 d 直接相关）

- `modelcontextprotocol/ext-auth` 仓库列出扩展：**Enterprise-Managed Authorization（Stable）**、Client Credentials（Draft）。[https://github.com/modelcontextprotocol/ext-auth, 2026-08-28]
- 官方扩展页（`io.modelcontextprotocol/enterprise-managed-authorization`）：企业 IdP 成为授权决策方。流程："The MCP Client requests a special type of token from the enterprise IdP called an Identity Assertion JWT Authorization Grant, or ID-JAG. The MCP Client then exchanges the ID-JAG for an access token from the MCP server's Authorization Server."；MCP AS 义务包括 "checking the token's audience, issuer, and expiration"。扩展定位为 Optional/Additive/Composable。[https://modelcontextprotocol.io/extensions/auth/enterprise-managed-authorization, 2026-08-28]
- 机制细节（多方来源一致）：ID-JAG 经 **RFC 8693 token exchange** 向 IdP 换取（"The MCP Client sends a Token Exchange [RFC8693] request to the Identity Provider including the ID Token or Refresh Token, and the identifier of the MCP Server"），IdP 评估企业策略后 "issues a short-lived, **audience-bound** ID-JAG"；随后按 JWT bearer grant（RFC 7523 family）在 MCP 服务器的 AS 换 access token。该扩展 2026-06 前后进入 Stable。[https://github.com/modelcontextprotocol/ext-auth/blob/main/specification/stable/enterprise-managed-authorization.mdx; https://www.scalekit.com/blog/what-is-enterprise-managed-authorization; https://techcommunity.microsoft.com/blog/appsonazureblog/mcp-enterprise-authorization-is-here-%e2%80%94-what-entra-and-app-service-can-do-today/4537433, 2026-08-28]

## 11. 攻击研究与缓解（2025-2026 公开研究）

- **Tool poisoning / 描述注入**：Invariant Labs 2025-04 首个公开 PoC——恶意指令藏在 tool description（LLM 可见、用户通常不可见），可致私有仓库/消息外泄。[https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks; https://simonwillison.net/2025/Apr/9/mcp-prompt-injection/, 2026-08-28]
- **Rug-pull（定义漂移）**：服务器在通过初次审核后变更工具行为/定义。CVE-2025-54136（CVSS 8.8，2025-07 披露，Cursor）证实"tool definition approval … does not survive subsequent server-side changes"。[https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-ai-agent-exfiltration-2/, 2026-08-28]
- **Tool shadowing / 跨服务器影子攻击**：三类变体（description poisoning、rug-pull、shadowing）共同根因是"MCP clients inherit trust from the servers they connect to without continuous verification"。[同上, 2026-08-28]
- **量化**：MCPTox 基准在 45+ 真实 MCP 服务器上测得投毒攻击成功率超 60%（最高模型 72.8%）。[https://arxiv.org/pdf/2508.14925, 2026-08-28]
- **生态测绘**：大规模 MCP 生态攻击分析（"Parasites in the Toolchain"）[https://arxiv.org/pdf/2509.06572]；元数据投毒防御 MindGuard [https://arxiv.org/pdf/2508.20412]；ETDI 提出 OAuth 增强工具定义 + 策略访问控制对抗 tool squatting/rug-pull [https://arxiv.org/pdf/2506.01333]。[均 2026-08-28]
- **推荐缓解（研究界共识）**：tool definition **hash pinning/版本 pin**（变更即重新审批）、服务器 allowlist、把一切工具返回内容当不可信输入、对触敏工具做 schema enforcement、mcp-scan 类静态扫描、schema 漂移检测。[https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-ai-agent-exfiltration-2/; https://labs.cloudsecurityalliance.org/agentic/agentic-mcp-security-best-practices-v1/, 2026-08-28]
- 与官方文档互证：OpenAI Responses API 文档自身承认 "Malicious MCP servers may include hidden instructions"（§8）；MCP 规范要求 client 把 annotations 视为 untrusted（§4）、把 tool 结果验证后再交给 LLM（tools 页 Security Considerations）。

---

## 对设计文档四个断言的裁定

### (a) "MCP 工具注解只可用于客户端体验，不是服务端授权" — **成立**（表述可再收紧）
依据：schema 原文 "all properties in ToolAnnotations are **hints** … not guaranteed to provide a faithful description … Clients should never make tool use decisions based on ToolAnnotations received from untrusted servers"；tools 页规范性 Warning "clients **MUST** consider tool annotations to be untrusted unless they come from trusted servers"；同页 "Servers **MUST** … Implement proper access controls"。[§4 各来源, 2026-08-28]
收紧建议：断言方向正确且有规范原文支撑。可补一句现实注脚——Codex `writes` 审批模式正是拿 readOnly 注解当客户端审批依据（§7），说明生态确实在消费这一不可信信号，因此服务端授权（scope/audience/服务端 ACL）必须独立成立，这与规范义务一致。

### (b) "Codex 可配置 enabled_tools 白名单与 writes 审批模式" — **成立**
依据：官方 MCP 配置页列出 `enabled_tools`（"Tool allow list"）、`disabled_tools`（deny list，后于 allow list 生效）、`default_tools_approval_mode` 取值含 `writes`；`enabled`、`url`、`bearer_token_env_var`、`codex mcp login` OAuth 均在。设计文档示例 TOML 的四个字段名与取值全部与现状一致。[§7, 2026-08-28]
注意事项：文档 URL 已 308 重定向至 learn.chatgpt.com（建议更新引用）；`writes` 的读/写判定基于服务器自报 annotations（untrusted），这是断言 (a) 的活例证。

### (c) "Responses API 可设 allowed_tools 与 require_approval" — **成立**
依据：官方文档确认 `type:"mcp"` 工具支持 `server_url`、`allowed_tools`（数组）、`require_approval`（`never`/`always`/`{never:{tool_names:[...]}}` 粒度对象）、`authorization`（OAuth access token）；默认每次调用要求审批，审批经 `mcp_approval_request`/`mcp_approval_response` 回环。[§8, 2026-08-28]

### (d) "OAuth token exchange 签发 audience-bound 降权 token 是 MCP 授权规范支持的模式" — **需修订**（方向成立，出处要改）
- 不成立的部分：**MCP 核心授权规范（2025-06-18 与当前 2026-07-28）均未引用 RFC 8693**，Standards Compliance 列表无 token exchange；核心规范对 audience 的机制是 RFC 8707 resource 参数（客户端 MUST 发送）+ 服务端 audience 校验（MUST）。把 token exchange 说成"MCP 授权规范（核心）支持的模式"不准确。[§3, 2026-08-28]
- 成立的部分：该模式在 MCP 官方**授权扩展**中已是 Stable——Enterprise-Managed Authorization（`io.modelcontextprotocol/enterprise-managed-authorization`）用 RFC 8693 token exchange 向企业 IdP 换取**短时效、audience-bound 的 ID-JAG**，再据此向 MCP 服务器的 AS 换 access token（AS MUST 校验 audience/issuer/expiration）。另外，核心规范的 passthrough 禁令要求 MCP 服务器调用上游时必须换用"上游 AS 签发的独立 token"（MUST NOT passthrough），token exchange 是实现这一要求的标准手段之一，但核心规范未指名 RFC 8693。[§3、§6、§10, 2026-08-28]
- 修订建议：改为"audience-bound token 是 MCP 授权规范的强制要求（RFC 8707 + 服务端 audience 校验）；经 OAuth token exchange（RFC 8693）签发短时效 audience-bound 降权 token 的具体流程由官方 Stable 扩展 Enterprise-Managed Authorization（ID-JAG）定义，属可选扩展而非核心规范；服务端向下游换发独立 token 则由核心规范的 token-passthrough 禁令（MUST NOT）间接强制"。

---

## 其余需要设计文档同步修订的点（非四断言）

1. 规范版本引用：2025-06-18 → 应改为"当前 2026-07-28（并注明 2025-11-25 中间版）"；凡依赖 `Mcp-Session-Id`、GET SSE 流、`initialize` 握手、sampling 的设计段落需按 2026-07-28 重写或标注版本区间。[§1-2]
2. DCR 弃用：若设计假设客户端经 RFC 7591 动态注册，需加 Client ID Metadata Documents 路线。[§3]
3. 外部文档 URL 漂移：`developers.openai.com/codex/mcp` → `learn.chatgpt.com/docs/extend/mcp`；`docs.claude.com` → `platform.claude.com`；Anthropic MCP connector beta header 应引用 `mcp-client-2025-11-20`（`tool_configuration.allowed_tools` 已弃用，改 `mcp_toolset`）。[§7、§9]
4. 若设计把"客户端审批"当控制面组成部分：三家能力不对齐——Codex（`writes` 注解驱动审批）、OpenAI API（`require_approval` 回环、默认全审批）、Claude API（无审批回环，仅 enable/disable）。控制平面不能假设统一的客户端审批语义，服务端 scope/audience/工具级鉴权是唯一公共分母。[§7-9]
