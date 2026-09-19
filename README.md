# ads-control-plane

SFW 电商 Pack 里的**否定词组件**：一个只读工具 `find_wasted_search_terms`。它读领星的搜索词报表，找出「花了钱却没出单」的搜索词，写成否定词 CSV 和一份报表。它不改任何广告：否定词只在人把 CSV 交给领星「否定投放」之后才生效，那一步在本组件之外、由人做。

## 孩子怎么用（5 步）

1. 打开 SFW。项目芯片应写着「否定词」；不是的话点芯片，在列表里选「否定词」。
2. 敲 `/new`，回车——开一个新对话。
3. 敲 `/fd`，回车，再回车——第一下回车把那句话填进输入框，第二下才发送。
4. 只会弹一次窗：用鼠标点「批准」（别按回车、别按 Esc）。同一次对话弹第二个窗，一律点「拒绝」，找管理员。
5. 读回答：每家店两行，点蓝字链接看报表、拿 CSV；桌面「否定词导出」文件夹里是同一份文件。

一家店通常半分钟内（2026-08-30 实测 30 天窗口 27.6 秒）；店多时最长 55 分钟（时间预算），等着就行。

## 管理员一次性安装（10 步）

以下步骤 2026-09-19 尚未在真机上走过；首次安装时请逐步核对，对不上就停。

1. 仓库目录里 `uv build --wheel`，得到 `dist/ads_control_plane-*.whl`。
2. `sudo <仓库>/.venv/bin/python -m ads_control_plane.sfw install --wheel dist/<whl> --child-user <孩子的登录名>`：建系统用户 `_adspack`，把组件装进 `/Library/Application Support/ads-pack/`，写配置模板，建 `/Users/Shared/ads-pack/导出/`、LaunchDaemon、孩子的 `~/否定词/AGENTS.md`、`~/.codex/prompts/fd.md` 和桌面「否定词导出」链接，最后打印第 7 步要粘的 JSON。不启动服务。
3. `sudo -e "/Library/Application Support/ads-pack/config.toml"`：填 `[lingxing]` 的 `url` 与 `key`。密钥只在这一处（文件属 `_adspack`、0600，孩子的账号读不到）。
4. `sudo ads-pack shops`：列出可选店铺，打印可粘贴的 `[[stores]]` 段；粘进配置，给每家店起个昵称（不含空格），按币种填 `[thresholds.min_spend]`。
5. `sudo ads-pack doctor`：任一项不过，不进下一步。
6. `sudo ads-pack start`。日志在 `/Library/Application Support/ads-pack/logs/ads-pack.log`。
7. SFW 右栏「MCP」→「添加服务器」→「高级配置 · 本地命令 / JSON」→ 粘第 2 步打印的 JSON（`sudo ads-pack print-registration` 可再打一次）→「保存配置」。行状态应为「已连接」。
8. SFW 项目芯片 →「使用现有文件夹」→ 选 `~/否定词` →「打开」。
9. 重启一次 SFW（斜杠命令只在启动时加载），然后自己照上面 5 步跑一遍。
10. 培训三句话：① 敲 `/new` 回车，再敲 `/fd` 回车回车；② 只会弹一次窗，用鼠标点「批准」，弹第二次就点「拒绝」叫我；③ 文件在回答里的蓝字链接和桌面「否定词导出」里，报表点开看。

## 文件长什么样

都在 `/Users/Shared/ads-pack/导出/`（属 `_adspack`，孩子的账号只能读）：

- `否定词-<昵称>-<日期>-<指纹8>.csv`：交给领星的否定词表，按活动→广告组分组。日期是统计区间的最后一天，指纹是内容指纹的前 8 位——同一批数据再跑得到同一个文件名。
- `报表-<昵称>-<日期>-<指纹8>.html`：给人看的。每个候选词的花费、点击、曝光与订单（都是 0）、统计区间；否定词挡不住的 ASIN（要去领星「否定投放」单独处理）；生效的门槛；和 CSV 同一个指纹。
- `/Users/Shared/ads-pack/运行记录.csv`：每跑完一家店追加一行（时间、店铺、结局、各项数量、指纹、CSV 文件名）。

## 不承诺什么

- 不到点自己跑：每一次运行都由人在 SFW 里敲 `/fd` 发起，仓库里没有调度器（守卫：`tests/unit/test_nothing_runs_unattended.py`）。
- 不写领星：组件只有只读工具，白名单外的领星调用在发出任何网络请求之前就被拒。
- 不批准任何否定词：SFW 弹窗上的「批准」只是允许 AI 调一次这个工具；哪些词真的否定，由人对着报表把 CSV 交给领星决定。
- 不要把 SFW 切到「完全访问」档：那一档下不弹窗。本组件不依赖弹窗证成任何域层事实，但少了这道人肉闸。

## 出错了

- 助手说「工具没连上」：SFW 右栏「MCP」里点「重新连接」；还不行就 `sudo ads-pack doctor`。
- 助手说「配置错误」「取数失败」：`sudo ads-pack doctor` 会指出是哪一项。
- 再看日志：`/Library/Application Support/ads-pack/logs/ads-pack.log`。

开发门禁：`uv run ruff format src tests && uv run ruff check src tests && uv run mypy && uv run pytest -q`。安全边界见 [SECURITY.md](SECURITY.md)。
