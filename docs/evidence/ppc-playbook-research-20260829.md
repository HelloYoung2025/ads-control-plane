# 内置打法阈值出处（2026-08-29 检索）

UI 内置打法（`app.js MANDATE_PRESETS`）新增四卡的阈值依据。检索方式：3 个并行搜索智能体
（新品期 / 大促清仓 / ACOS 控制三个视角），2026-08-29 执行。阈值是行业常见做法的映射，
不是本系统实测结论；用哪张卡、要不要改数，签发人自己拍板。

## 新品保护期（HONEYMOON：14d / $30 / 30c / 每 3 天 / ≤20 / 14 天）

- 点击阈值 30 = 行业共识区间上限：LandingCube 建议攒够 25-30 次点击再判断；
  Emplicit/SalesDuo 对中价位产品建议 20-30 次零出单再否，并明确 5-10 次不足以下结论。
- 每 3 天节奏：Adverio 新品 launch timeline 建议首月每 3-5 天复盘搜索词报告、
  第 8-14 天集中加否。
- 蜜月期「少否、勤看」：myamazonguy.com/honeymoon/；Marketplace Valet 警告勿误否潜力词。

## 新品大额止血（STOP_LOSS：7d / $100 / 50c / 每天 / ≤30 / 7 天）

- 阈值直接取自 AmazonGrowthLab 危险层规则：7 天花费 $100+ 零转化、或 50+ 次点击零出单
  应立即否定。原文两条件为「或」，本系统 min_spend 与 min_clicks 为「且」，只命中同时
  满足两条的最确凿亏损词，比原规则更保守。

## 大促前清扫（PRE_EVENT：60d / $20 / 20c / 每天 / ≤150 / 14 天）

- 60 天回看：BellaVix《Prime Day 2026 PPC Checklist》建议大促前把近 60 天有花费零转化
  的搜索词全部否掉；Power Digital 审计清单建议拉 60-90 天数据保证统计显著。
- 20 次点击 = Ad Badger/LandingCube 的 15-20 次行业标准取上限，防大促前误伤。
- 提前 2-3 周动手：BellaVix、ecombrainly（valid_days=14 覆盖备战窗口）。

## 稳健周清（WEEKLY：30d / $20 / 15c / 每周 / ≤50 / 30 天）

- 15 次点击 = 最常见的 15-20 次零转化规则下限（Ad Badger、CaptenAMZ、Sequence Commerce）。
- 每周节奏 = 行业公认最低有效节奏（Trellis、SalesDuo、Feedvisor）；每日调整会撞上
  24-72 小时归因延迟，被 Ad Badger 视为过度优化——staleness 因此放宽到 48h。
- 30 天回看保证 7-14 天归因窗口已关闭，且在 Amazon 约 60 天搜索词数据保留期内。

## 检索到但未入库

- 冲刺清仓急控（14d/$10/15c/12h）：与现有「激进清理」（14d/$8/15c/12h）几乎重合，不加。
- 高频止损（14d/$10/10c/每 3 天）：同上，区分度不足。

## 主要来源

- https://landingcube.com/amazon-ppc-negative-keywords/
- https://www.adbadger.com/blog/amazon-ppc-education/negative-keywords-amazon-ppc/
- https://emplicit.co/negative-keyword-strategies-amazon-ppc/
- https://salesduo.com/blog/amazon-negative-keywords/
- https://www.amazongrowthlab.com/blogs/amazon-negative-keywords-ppc-strategy
- https://www.adverio.io/amazon-product-launch-timeline/
- https://myamazonguy.com/honeymoon/
- https://www.bellavix.com/amazon-prime-day-2026-ppc-checklist-what-to-do-in-the-previous-weeks/
- https://powerdigitalmarketing.com/blog/amazon-ppc-audit/
- https://sequencecommerce.com/amazon-negative-keywords/
- https://gotrellis.com/resources/blog/amazon-search-term-report-workflow
- https://www.adbadger.com/blog/decoding-amazon-ppc-attribution/
