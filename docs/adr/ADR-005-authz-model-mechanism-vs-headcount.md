# ADR-005：授权模型——机制从严、人头从简

日期：2026-08-28 ｜ 状态：ACCEPTED

## 决定

两层分离（辩证评审五视角选边结论）：

1. **强制机制（security 视角胜）**：SoD 是按不可变 `human_person_id` 的请求级冲突矩阵（`authorization/sod.py`），同一自然人换登录账号不能绕过；AI 永不审批、永不对自己 draft 提交（代码级阻断，非配置）。矩阵必须闭合，增补两条原文缺失的冲突：
   - Credential Custodian ≠ Executor Administrator（合一即获得单人无审计裸写路径）；
   - Policy 审批人 ∩ 命中政策的 author 非空 → SOD_VIOLATION（可计算判据，Policy Bundle 携带 author）。
2. **人头配置（lean 视角胜）**：MVP 角色目录 6 个（Viewer/Analyst/Operator/Approver/Admin/Auditor）；原 17/18 角色降级为标签目录，不强制人头。最小人员配置模型（哪些冲突要求哪几个真实自然人）由 WP-00 写明并经业务 Owner 确认真实存在。
3. **审批粒度**：R2 逐笔独立人审改为"复核过的 Bulk Sheet 集合粒度"批量审批（集合冻结 + Hash 机制已有，非新发明）；审批时延分布与拒绝率进权限报表，作橡皮图章探测器。MVP 只开单人审批；双审推迟到 Gate 4 前。

## 依据

审批疲劳是控制失效模式：逐笔审批的摩擦若超过领星后台直改的成本，运营会绕过平台（采用率死亡模式），届时安全设计保护的是一个没人用的系统。
