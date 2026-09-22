# ads-control-plane

SFW 电商 Pack 里的**否定词组件**：一个只读工具 `find_wasted_search_terms`。它读领星的搜索词报表，找出「花了钱却没出单」的搜索词，写成否定词 CSV 和一份报表。它不改任何广告：否定词只在人把 CSV 交给领星「否定词」之后才生效，那一步在本组件之外、由人做。

2026-09-22 在 SFW 1.0.8 真机上整条链路跑通过一次（假数据、同一账号、无 uid 隔离）：登记→连接→/fd→工具执行→回答逐字一致。权限隔离那部分仍未在真机验过，首次安装时逐步核对，对不上就停。
**必须把对话档位设成「完全访问权限」。** 1.0.8 的「请求批准」档下，MCP 工具调用会弹一个「MCP 请求你的输入」窗，窗上写着「此请求当前只允许拒绝」——点提交无反应，等满 5 分钟超时，工具永远调不通，而且那个窗会把整个界面锁住到重启 SFW。这不是可选项，是 1.0.8 唯一能用的档位。
管理员那节会建系统用户、写 `/Library` 与 `/Users/Shared`、装 LaunchDaemon。本仓库不提供任何担保，
出了问题自负；不接受这一点就不要跑第 2 步。

## 孩子怎么用（4 步）

1. 打开 SFW。项目芯片应写着「否定词」；不是的话点芯片，在列表里选「否定词」。
2. 敲 `/new`，回车——开一个新对话。
3. 敲 `/fd`，回车，再回车——第一下回车把那句话填进输入框，第二下才发送。
4. 读回答：每家店一到两行——有要否定的词才有 CSV，没有候选或出了问题的店只有一行，照那行说的做；桌面「否定词导出」文件夹里是同一份文件。

一家店通常半分钟内（2026-08-30 实测 30 天窗口 27.6 秒）；店多时约 45 分钟（时间预算），等着就行。
时间预算不是上界：它只决定「还要不要再开一家店」，开了的那家会跑完。网关很慢时整次调用可能超过
SFW 登记的 3600 秒，那时孩子读到的是「工具没连上」——日志里会有一行 WARNING 说这次跑了多久。

## 管理员一次性安装（10 步）

1. 仓库目录里 `uv sync --frozen && uv build --wheel`（sync 建出第 2 步要用的 `.venv`），得到 `dist/ads_control_plane-*.whl`。
2. `sudo <仓库>/.venv/bin/python -m ads_control_plane.sfw install --wheel dist/<whl> --child-user <孩子的登录名>`：建系统用户 `_amazonads`，把组件装进 `/Library/Application Support/amazon-ads/`，写配置模板，建 `/Users/Shared/amazon-ads/导出/`、LaunchDaemon、孩子的 `~/否定词/AGENTS.md`、`~/.codex/prompts/fd.md` 和桌面「否定词导出」链接，最后打印登记 JSON（第 7 步会在孩子账号里重新打印一次）。不启动服务。
3. `sudo -e "/Library/Application Support/amazon-ads/config.toml"`：填 `[lingxing]` 的 `url` 与 `key`。`key` 在领星 ERP 后台【业务配置 → 开放接口 → MCP】里生成，不是开放平台的 appId/appSecret；它继承该账号的店铺权限。密钥只在这一处（文件属 `_amazonads`、0600，孩子的账号读不到）。
4. `sudo amazon-ads shops`：列出可选店铺，打印可粘贴的 `[[stores]]` 段；粘进配置，给每家店起个昵称（不含空格），按币种填 `[thresholds.min_spend]`。
5. `sudo amazon-ads doctor`：任一项不过，不进下一步。
6. `sudo amazon-ads start`。日志在 `/Library/Application Support/amazon-ads/logs/amazon-ads.log`。
7. **切到孩子的 macOS 账号**（苹果菜单 → 快速用户切换，用第 2 步 `--child-user` 那个登录名）：第 7–10 步都在孩子的 SFW 里做，第 1–6 步是管理员账号的终端。SFW 的 MCP 登记按登录账号存，装好的 `~/否定词`、`/fd` 与桌面链接也都在他家里。剪贴板不跨账号，第 2 步那段 JSON 带不过来，得在孩子这边重新打印：打开「终端」，先 `su - <管理员登录名>` 输管理员密码，再 `sudo amazon-ads print-registration`（孩子的账号通常不是管理员，直接 `sudo` 会被拒）。接着在 SFW「定制化 → 连接器 → 添加服务器」里，把打印出来的四行**逐格填进表单**，点「保存并连接」，状态应为「已连接」。
   不要用「高级配置 · 本地命令 / JSON」那个框：2026-09-22 实测它只收本地命令（stdio）形状，粘 HTTP 形状报「MCP JSON 格式错误」。名称一格注意 macOS 会自动把首字母大写成 `Amazon-ads`，改回全小写再存。
8. SFW 项目芯片 →「使用现有文件夹」→ 选 `~/否定词` →「打开」。
9. 重启一次 SFW（斜杠命令只在启动时加载），然后自己照上面 4 步跑一遍。
10. 先把输入框左下角的档位设成「完全访问权限」（见开头那段，1.0.8 的「请求批准」档下工具调不通）。培训两句话：① 敲 `/new` 回车，再敲 `/fd` 回车回车；② 文件在回答里的蓝字链接和桌面「否定词导出」里，报表点开看。**不会弹任何窗**；真弹出「MCP 请求你的输入」就是档位被改回去了，叫管理员。

## 文件长什么样

都在 `/Users/Shared/amazon-ads/导出/`（属 `_amazonads`，孩子的账号只能读）：

- `否定词-<昵称>-<日期>-<指纹8>.csv`：交给领星的否定词表，按活动→广告组分组。日期是统计区间的最后一天，指纹是内容指纹的前 8 位——同一批数据再跑得到同一个文件名。
- `报表-<昵称>-<日期>-<指纹8>.html`：给人看的。每个候选词的花费、点击、曝光与订单（都是 0）、统计区间；否定词挡不住的 ASIN（要去领星「否定投放」单独处理）；生效的门槛；和 CSV 同一个指纹。
- `/Users/Shared/amazon-ads/运行记录.csv`：每跑完一家店追加一行（时间、店铺、结局、各项数量、指纹、CSV 文件名）。

## 不承诺什么

- 不到点自己跑：每一次运行都由人在 SFW 里敲 `/fd` 发起，仓库里没有调度器（守卫：`tests/unit/test_nothing_runs_unattended.py`）。
- 不写领星：组件只有只读工具，白名单外的领星调用在发出任何网络请求之前就被拒。
- 不批准任何否定词：哪些词真的否定，由人对着报表把 CSV 交给领星决定。组件这头没有 APPROVED 状态、没有 approve 入口。
- 不要把 SFW 切到「完全访问」档：那一档下不弹窗。本组件不依赖弹窗证成任何域层事实，但少了这道人肉闸。

## 出错了

- 助手说「工具没连上」：SFW 右栏「MCP」里点「重新连接」；还不行就 `sudo amazon-ads doctor`。
- 助手说「配置错误：…」：`sudo amazon-ads doctor` 会指出是哪一项。
- 助手说「取数失败」「数据不合规」「没有一组能判断」：doctor 查不到这类（它只看配置、权限、端口与名录），看日志里那一行带店名的 ERROR。
- 再看日志：`/Library/Application Support/amazon-ads/logs/amazon-ads.log`。

开发门禁：`uv run ruff format src tests && uv run ruff check src tests && uv run mypy && uv run pytest -q`。安全边界见 [SECURITY.md](SECURITY.md)。
