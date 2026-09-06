"""本地对象镜像层（DEC-117）——四报表现值快照 + append-only 仓库 + 同步器。

镜像只服务浏览与选择；执行判定以实时读回为准（既有 expected_before + 写前重读门）。
本包只读不写：不存在任何对领星写工具（put_*/post_*）的调用路径。

仓库惯例：包 `__init__` 不做再导出，消费方一律按完整模块路径导入
（`ads_control_plane.mirror.snapshot` / `.repository` / `.sync`）。
"""
