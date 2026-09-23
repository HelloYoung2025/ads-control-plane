"""插件形态的入口：SFW 的引擎用 stdio 把这个进程直接拉起来。

和 HTTP 那条路（sfw/__main__.py 的 serve）比，只有三处不同，业务逻辑一行都没变：

* 传输是 stdio——没有端口、没有 Bearer、没有 LaunchDaemon，进程活在引擎手里；
* 配置在自己家里（~/.amazon-ads/config.toml），不是 /Library，所以不用 sudo；
* 出错时的那句提示是「你自己去改」，不是「找管理员」——插件是自己装给自己用的。

**stdout 是 MCP 的通道**：任何一个 print 都会污染协议，日志必须走 stderr。
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import uuid
from pathlib import Path

from ads_control_plane.sfw.config import (
    BEARER_NOTE_USER,
    REPO_URL,
    TEMPLATE_HEADER_USER,
    USER_CONFIG_PATH,
    USER_EXPORT_DIR,
    USER_RUN_LOG,
    render_config_template,
    user_shops_command,
)
from ads_control_plane.sfw.service import Wording

logger = logging.getLogger("amazon-ads.plugin")

#: 配置缺了什么的时候，工具返回的那句话的结尾。纪律第 4 条要求模型把带「配置错误：」
#: 的报错原样念出来，所以这句话是直接给人看的——必须自己就能照着做完，头一件就是说清
#: 哪个文件：此前只说「这个文件」，而第一次问必然撞上的「url 还没填」里并没有路径
#: （2026-09-23 Codex 复审 P2）。
FIX_HINT = (
    f"打开 ~/{USER_CONFIG_PATH.relative_to(Path.home())} 把缺的填上，"
    "填完在 SFW 里开一个新对话再问一次"
)

#: 逐店那几行的插件版说法：没有 /fd，没有管理员，CSV 由用的人自己交给领星。
PLUGIN_WORDING = Wording(
    retry="开一个新对话再问一次",
    escalate=f"对照 {REPO_URL} 的「出错了」一节",
    hand_over="有文件的店：把 CSV 加进领星「否定词」才算数。",
)


def ensure_config(path: Path) -> bool:
    """配置不存在就写一份模板，返回是否新写。已存在则一个字节都不动。

    写失败不抛：让进程照常起来，由工具调用那里报「配置文件不存在：<路径>」——
    那条路径会被模型原样念给人听，比一个连不上的服务器有用得多。
    """
    if path.exists():
        return False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = render_config_template(
            sfw_bearer=secrets.token_hex(16),
            organization_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            export_dir=USER_EXPORT_DIR,
            run_log_path=USER_RUN_LOG,
            header=TEMPLATE_HEADER_USER,
            shops_cmd=user_shops_command(),
            bearer_note=BEARER_NOTE_USER,
        )
        # O_EXCL：两个 SFW 对话同时冷启动时，只有一个会写成功，另一个走 FileExistsError。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return False
    except OSError as exc:
        logger.warning("写不了配置模板 %s：%s", path, exc)
        return False
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # 延迟 import：拉起 MCP 服务端要花时间，而上面那些路径活儿不需要它。
    from ads_control_plane.sfw.server import build_server

    path = USER_CONFIG_PATH
    if ensure_config(path):
        logger.info("第一次启动，已写配置模板：%s（填 [lingxing] 的 url 与 key）", path)
    server = build_server(path, expect_uid=os.getuid(), fix_hint=FIX_HINT, wording=PLUGIN_WORDING)
    logger.info("amazon-ads 以 stdio 待命；配置 %s", path)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 console script 调用
    raise SystemExit(main())
