"""安装器：把组件装成 `_amazonads` 名下的 LaunchDaemon，并给孩子的家目录放三样东西。

每个副作用先由纯函数组装成 `Step`（做什么、写到哪、属谁、为什么），`execute` 再逐条做；
`--dry-run` 只打印。这样分层不是为了好看：`dscl`/`launchctl`/`chown` 一跑就改了系统，
而这些命令在开发机上不能试（规格 §0 第 1 条），测试能盯住的只有「计划」——所以计划
必须把每一步说全，执行层只做搬运，不再做任何决定。

隔离模型（计划 §1、§8 攻击 1/2/4 的修法）：组件以系统用户 `_amazonads` 常驻，代码、venv、
config 在 `/Library/Application Support/amazon-ads/`，产物在 `/Users/Shared/amazon-ads/导出/`。
孩子 uid 下的进程读 config 得 EACCES、写导出目录得 EACCES——「密钥读不走、文件改不了」
两条都由文件属主承载，不靠组件内的任何状态。代码也不交给服务用户：python/、venv/ 与
/usr/local/bin/amazon-ads 留 root:wheel 0755，`_amazonads` 名下只有 config.toml（0600）与
logs/（0755）——被攻破的 `_amazonads` 进程改不了自己下次启动要跑的代码。`doctor` 把这三条
按 stat 结果复核一遍。

本模块在开发机上从未真跑过（规格 §0 不允许）：`dscl` 建用户序列、`uv python install
--install-dir` 装进带空格的 `/Library` 路径、LaunchDaemon 以非 root `UserName` 跑 Python，
全部是计划 §9 列明的【假设】，等首次真机安装（计划 §7 第 8 项）验证。
"""

from __future__ import annotations

import contextlib
import grp
import os
import pwd
import re
import secrets
import socket
import stat
import string
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from ads_control_plane.adapters.lx_read import AUTH_SHOPS_TOOL_ID, LxMcpReadClient, LxReadError
from ads_control_plane.providers.lingxing.search_terms import LxReadPort
from ads_control_plane.sfw.config import (
    DEFAULT_EXPORT_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_PORT,
    DEFAULT_ROOT,
    DEFAULT_RUN_LOG,
    MARKETPLACE_CURRENCY,
    SERVICE_USER,
    ConfigError,
    PackConfig,
    StoreConfig,
    check_private_file,
    load_config,
    read_lingxing_credentials,
    render_config_template,
)

LABEL = "local.amazon-ads"
PLIST_PATH = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
LOG_PATH = DEFAULT_LOG_DIR / "amazon-ads.log"
BIN_LINK = Path("/usr/local/bin/amazon-ads")
#: launchd 起服务时打开的第一个文件；doctor 拿它当「代码属 root」的哨兵。
VENV_PYTHON = DEFAULT_ROOT / "venv" / "bin" / "python"
PYTHON_VERSION = "3.12"

#: SFW 登记 JSON 的键闭集（宿主合同 §3-3）与 name 形状（§3-1）；`tool_timeout_sec` 上限（§3-5）。
REGISTRATION_NAME = "amazon-ads"
REGISTRATION_KEYS = frozenset({"name", "url", "auth", "secret", "tool_timeout_sec"})
TOOL_TIMEOUT_SEC = 3600
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_AUTH_MODES = frozenset({"none", "bearer", "header", "oauth"})

#: macOS 留给系统服务账号的 uid/gid 段：200..400 里第一个没人用的号。
SERVICE_ID_RANGE = range(200, 401)

#: 斜杠命令文件名必须是 ASCII（宿主合同 §3-42）；孩子项目文件夹与桌面链接可以是中文。
PROMPT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PROMPT_FILE = "fd.md"
AGENTS_FILE = "AGENTS.md"
PROJECT_DIR = "否定词"
DESKTOP_LINK = "否定词导出"

DSCL = "/usr/bin/dscl"
LAUNCHCTL = "/bin/launchctl"
ENV = "/usr/bin/env"

#: 随包资源（plist 模板、config 示例、给孩子的 AGENTS.md 与 fd.md）。测试把它换成临时目录。
ASSETS: Traversable = files("ads_control_plane.sfw") / "assets"

StepKind = Literal["run", "write", "symlink", "mkdir", "chown", "chmod"]
Check = tuple[str, bool, str]
ClientFactory = Callable[[str, str], LxReadPort]
PortState = Literal["free", "ours", "other"]


class InstallerError(Exception):
    """安装/体检层的拒绝。str(exc) 是一句能直接给管理员看的中文。"""


@dataclass(frozen=True, kw_only=True)
class Step:
    """一个副作用。`content` 在 write 里是文件内容，在 symlink 里是链接指向；
    `owner` 写成 "用户" 或 "用户:组"；chown/chmod 只作用于路径自身，不跟符号链接。
    `keep_existing` 只对 mkdir 有意义：目录已存在时一步不动（孩子自己的 ~/.codex、
    Homebrew 名下的 /usr/local/bin）；缺省时已存在的目录属主必须已是 owner，否则拒绝。"""

    kind: StepKind
    argv: tuple[str, ...] | None = None
    path: Path | None = None
    content: str | None = None
    owner: str | None = None
    mode: int | None = None
    keep_existing: bool = False
    why: str


@dataclass(frozen=True, kw_only=True)
class HostState:
    """安装前从宿主读到的事实快照。读是只读查询（`inspect_host`），计划函数只看快照。"""

    #: `_amazonads` 已存在时它的 uid。再装一次不能换号：旧号名下的文件会全部成孤儿。
    service_uid: int | None = None
    #: 目录服务里已被占用的 uid 与 gid。
    taken_ids: frozenset[int] = frozenset()
    #: config.toml 已存在则保留：里面有管理员填的领星密钥和店铺表，重装不能把它冲掉。
    config_exists: bool = False


_FRESH_HOST = HostState()


def read_asset(name: str) -> str:
    return ASSETS.joinpath(name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 登记 JSON 与 plist


def registration_json(sfw_bearer: str, *, port: int = DEFAULT_PORT) -> dict[str, str | int]:
    """登记这台服务器要填的取值。键闭集见 REGISTRATION_KEYS。

    2026-09-22 真机实测更正（SFW 1.1.0 构建 10015）：这不是一段可以整体粘贴的 JSON。它的
    「高级配置 · 本地命令 / JSON」只收 stdio 形状（name/command/args），粘 HTTP 形状
    直接报「MCP JSON 格式错误」。HTTP 服务器必须把 name/url/auth/secret 逐个填进
    「添加服务器」那四个表单字段。tool_timeout_sec 在它的表单里**没有对应字段**，
    填不进去，实际用的是 SFW 的默认值——这个常量今天只用于自检，不构成宿主行为。
    """
    return {
        "name": REGISTRATION_NAME,
        "url": f"http://127.0.0.1:{port}/mcp",
        "auth": "bearer",
        "secret": sfw_bearer,
        "tool_timeout_sec": TOOL_TIMEOUT_SEC,
    }


def registration_problems(payload: Mapping[str, object]) -> list[str]:
    """登记 JSON 自校验（宿主合同 §3-1/§3-3/§3-5/§3-9/§3-10/§3-11/§3-13）。空列表 = 合规。"""
    problems: list[str] = []
    extra = sorted(set(payload) - REGISTRATION_KEYS)
    if extra:
        problems.append(f"多了 SFW 不认的键 {extra}（只准 {sorted(REGISTRATION_KEYS)}）")
    name = payload.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        problems.append("name 要是 1 到 80 位的字母、数字、下划线或短横线")
    url = payload.get("url")
    if not isinstance(url, str):
        problems.append("url 缺失或不是字符串")
    else:
        problems.extend(_url_problems(url))
    auth = payload.get("auth")
    if auth not in _AUTH_MODES:
        problems.append(f"auth 要是 {sorted(_AUTH_MODES)} 之一，现在是 {auth!r}")
    secret = payload.get("secret")
    if auth == "bearer" and (
        not isinstance(secret, str) or not secret or any(c in secret for c in "\r\n\x00")
    ):
        problems.append("bearer 模式下 secret 要是非空字符串，且不含回车、换行、NUL")
    timeout = payload.get("tool_timeout_sec")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        problems.append(f"tool_timeout_sec 要是 1 到 3600 的整数（不是布尔），现在是 {timeout!r}")
    return problems


def _url_problems(url: str) -> list[str]:
    problems: list[str] = []
    try:
        parts = urlsplit(url)
        _ = parts.port  # 端口不可解析时在这里抛（§3-9）
    except ValueError:
        return [f"url 解析不了（端口无效？）：{url}"]
    if parts.scheme not in {"http", "https"}:
        problems.append(f"url 的 scheme 要是 http 或 https：{url}")
    if not parts.hostname:
        problems.append(f"url 没有主机名：{url}")
    if parts.fragment:
        problems.append("url 不能带 # 片段")
    if parts.username is not None or parts.password is not None:
        problems.append("url 不能带用户名或密码（凭据只能走 secret）")
    loopback = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if not loopback and parts.scheme != "https":
        problems.append("非回环地址必须是 https")
    return problems


def render_plist(*, venv_python: Path, config_path: Path, port: int, log_path: Path) -> str:
    """把 assets/local.amazon-ads.plist 模板填成绝对路径。值先做 XML 转义再代入。"""
    for name, path in (
        ("venv_python", venv_python),
        ("config_path", config_path),
        ("log_path", log_path),
    ):
        if not path.is_absolute():
            raise InstallerError(f"plist 里的 {name} 要是绝对路径：{path}")
    template = string.Template(read_asset(f"{LABEL}.plist"))
    return template.substitute(
        venv_python=escape(str(venv_python)),
        config_path=escape(str(config_path)),
        port=str(port),
        log_path=escape(str(log_path)),
        working_directory=escape(str(config_path.parent)),
    )


# ------------------------------------------------------------------ 安装计划（纯函数）


def _run(argv: Sequence[str], why: str) -> Step:
    return Step(kind="run", argv=tuple(argv), why=why)


def _mkdir(path: Path, owner: str, mode: int, why: str, *, keep_existing: bool = False) -> Step:
    return Step(
        kind="mkdir", path=path, owner=owner, mode=mode, keep_existing=keep_existing, why=why
    )


def _write(path: Path, content: str, owner: str, mode: int, why: str) -> Step:
    return Step(kind="write", path=path, content=content, owner=owner, mode=mode, why=why)


def _symlink(path: Path, target: Path, owner: str | None, why: str) -> Step:
    return Step(kind="symlink", path=path, content=str(target), owner=owner, why=why)


def pick_service_id(taken: Collection[int]) -> int:
    """200..400 里第一个既不是 uid 也不是 gid 的号；用同一个号建组和用户。"""
    for candidate in SERVICE_ID_RANGE:
        if candidate not in taken:
            return candidate
    raise InstallerError("200 到 400 的系统账号段没有空号了：手工建 _amazonads 后再装")


def plan_service_user(host: HostState) -> tuple[Step, ...]:
    """建隐藏的系统用户 `_amazonads`：无 shell、家目录 /var/empty、不进登录窗。已存在则一步不动。"""
    if host.service_uid is not None:
        return ()
    ident = str(pick_service_id(host.taken_ids))
    group = f"/Groups/{SERVICE_USER}"
    user = f"/Users/{SERVICE_USER}"
    real_name = "amazon-ads service"

    def dscl(record: str, *attribute: str) -> Step:
        return _run((DSCL, ".", "-create", record, *attribute), f"建系统账号 {SERVICE_USER}")

    return (
        dscl(group),
        dscl(group, "PrimaryGroupID", ident),
        dscl(group, "RealName", real_name),
        dscl(user),
        dscl(user, "UniqueID", ident),
        dscl(user, "PrimaryGroupID", ident),
        dscl(user, "UserShell", "/usr/bin/false"),
        dscl(user, "NFSHomeDirectory", "/var/empty"),
        dscl(user, "RealName", real_name),
        dscl(user, "IsHidden", "1"),
        dscl(user, "Password", "*"),
    )


def _config_text(
    *, sfw_bearer: str, organization_id: uuid.UUID, connection_id: uuid.UUID, export_dir: Path
) -> str:
    text = render_config_template(
        sfw_bearer=sfw_bearer, organization_id=organization_id, connection_id=connection_id
    )
    if export_dir == DEFAULT_EXPORT_DIR:
        return text
    # 模板只认缺省目录；改了导出目录，运行记录跟着挪到它旁边，两行一起改。
    run_log = export_dir.parent / DEFAULT_RUN_LOG.name
    text = re.sub(r'^export_dir = ".*"$', f'export_dir = "{export_dir}"', text, flags=re.M)
    return re.sub(r'^run_log_path = ".*"$', f'run_log_path = "{run_log}"', text, flags=re.M)


def plan_system(
    *,
    wheel: Path | None,
    uv: Path | None,
    root: Path = DEFAULT_ROOT,
    export_dir: Path = DEFAULT_EXPORT_DIR,
    sfw_bearer: str,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    host: HostState = _FRESH_HOST,
) -> tuple[Step, ...]:
    """①～⑥：系统这一半，与孩子是谁无关，一台机器只做一次。纯函数。

    wheel 与 uv 只被②那三条 uv 命令用到，所以要么都给、要么都不给：
    - 都给 = 从仓库装（`uv build --wheel` 之后），②当场装 Python、建 venv、装 wheel；
    - 都不给 = .pkg 已经把 python/ 与 venv/ 铺进 root，②那三步跳过。
    混着给只会得到一个装了一半的计划，所以直接拒绝，不猜。
    """
    if (wheel is None) != (uv is None):
        raise InstallerError("wheel 与 uv 要么都给（从仓库装），要么都不给（安装包已铺好代码）")
    paths: list[tuple[str, Path]] = [("root", root)]
    if wheel is not None and uv is not None:
        paths += [("wheel", wheel), ("uv", uv)]
    for name, path in paths:
        if not path.is_absolute():
            raise InstallerError(f"{name} 要是绝对路径：{path}")
    if wheel is not None and wheel.suffix != ".whl":
        raise InstallerError(f"--wheel 要指向 uv build --wheel 打出来的 .whl 文件：{wheel}")
    if not re.fullmatch(r"[0-9A-Fa-f]{32,}", sfw_bearer):
        raise InstallerError("sfw_bearer 要是至少 32 位的十六进制串")

    service = f"{SERVICE_USER}:{SERVICE_USER}"
    python_dir = root / "python"
    venv = root / "venv"
    config_path = root / "config.toml"
    log_dir = root / "logs"
    steps: list[Step] = list(plan_service_user(host))

    # ② 代码与解释器：uv 装一份托管 Python 到 /Library 下，venv 指向它，再装 wheel。
    #    venv 那一步用 UV_PYTHON_INSTALL_DIR 而不是写死解释器路径：托管 Python 的目录名
    #    带完整小版本号（cpython-3.12.x-…），计划期不知道 x 是几。
    #    这三样以 root 装、留给 root（0755）：_amazonads 只需要读和执行，改不了——被攻破的
    #    服务进程于是改不了自己下次启动要跑的代码。它名下只有 config.toml 与 logs/。
    steps += [
        _mkdir(root, "root:wheel", 0o755, "组件的家：代码、venv、config、日志都在这"),
        _mkdir(python_dir, "root:wheel", 0o755, "uv 托管的 Python 放这里，不碰系统 Python"),
        _mkdir(log_dir, service, 0o755, "LaunchDaemon 的 stdout/stderr 落这里；服务自己能写"),
    ]
    #    .pkg 已经把 python/ 与 venv/ 原样铺进 root 时，下面这三条不跑：包里那份是在
    #    打包机上以同一个绝对路径建的（venv 用 --relocatable），到这台机器上直接可用。
    if wheel is not None and uv is not None:
        steps += [
            _run(
                (
                    str(uv),
                    "--no-config",
                    "python",
                    "install",
                    "--install-dir",
                    str(python_dir),
                    PYTHON_VERSION,
                ),
                f"装 Python {PYTHON_VERSION}（uv 托管版）",
            ),
            _run(
                (
                    ENV,
                    f"UV_PYTHON_INSTALL_DIR={python_dir}",
                    str(uv),
                    "--no-config",
                    "venv",
                    "--managed-python",
                    "--python",
                    PYTHON_VERSION,
                    str(venv),
                ),
                "建 venv，解释器只认上一步装的那份",
            ),
            _run(
                (
                    str(uv),
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    str(venv / "bin" / "python"),
                    str(wheel),
                ),
                "把组件 wheel 装进 venv",
            ),
        ]

    # ③ config：新装写模板（随机口令、内部身份）；已存在只校正属主与权限，内容一字不动。
    if host.config_exists:
        steps += [
            Step(
                kind="chown", path=config_path, owner=service, why="config.toml 已存在：只校正属主"
            ),
            Step(kind="chmod", path=config_path, mode=0o600, why="config.toml 已存在：只校正权限"),
        ]
    else:
        steps.append(
            _write(
                config_path,
                _config_text(
                    sfw_bearer=sfw_bearer,
                    organization_id=organization_id,
                    connection_id=connection_id,
                    export_dir=export_dir,
                ),
                SERVICE_USER,
                0o600,
                "配置模板：只有 _amazonads 能读，密钥以后填在这里",
            )
        )

    # ④ 产物目录：属 _amazonads，别人只读。上一级也归它——运行记录.csv 写在那里。
    #    /Users/Shared 是 1777，谁都能先建出 amazon-ads/：已存在且属主不是 _amazonads 就拒绝装
    #    （里面可能预置了文件），属主对才校正权限——不收编、也不假装校正过（2026-09-20 复审）。
    steps += [
        _mkdir(export_dir.parent, service, 0o755, "运行记录写在这一层"),
        _mkdir(export_dir, service, 0o755, "CSV 与报表落这里；孩子的账号只能读"),
    ]

    # ⑤ LaunchDaemon：root:wheel 0644 是 launchd 的硬要求。
    steps.append(
        _write(
            PLIST_PATH,
            render_plist(
                venv_python=venv / "bin" / "python",
                config_path=config_path,
                port=DEFAULT_PORT,
                log_path=LOG_PATH,
            ),
            "root:wheel",
            0o644,
            f"LaunchDaemon {LABEL}：以 {SERVICE_USER} 常驻，KeepAlive",
        )
    )

    # ⑥ 管理员命令：sudo amazon-ads …
    #    排在孩子家目录之前：那一组遇到障碍就会停（例如 ~/Desktop 被 iCloud「桌面与文稿」
    #    做成符号链接），而 README 第 4–6 步全要用 amazon-ads。管理员自己的命令不该被
    #    孩子家里的东西挡住——两组之间没有任何依赖。
    #    /usr/local/bin 在 Intel Mac 上常归 Homebrew 的管理员账号所有，已存在就不碰它的属主。
    steps += [
        _mkdir(
            BIN_LINK.parent, "root:wheel", 0o755, "/usr/local/bin 有时不存在", keep_existing=True
        ),
        _symlink(
            BIN_LINK,
            venv / "bin" / "amazon-ads",
            None,
            "管理员命令：sudo amazon-ads shops/doctor/start",
        ),
    ]
    return tuple(steps)


def plan_child(
    *, child_user: str, child_home: Path, export_dir: Path = DEFAULT_EXPORT_DIR
) -> tuple[Step, ...]:
    """⑦：孩子那一半。每多一个孩子跑一次，和系统那半没有任何依赖。纯函数。"""
    if not child_home.is_absolute():
        raise InstallerError(f"child_home 要是绝对路径：{child_home}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", child_user):
        raise InstallerError(f"孩子的登录名不像 macOS 用户名：{child_user!r}")
    if not PROMPT_NAME_RE.fullmatch(PROMPT_FILE):
        raise InstallerError(f"斜杠命令文件名必须是 ASCII：{PROMPT_FILE}")
    steps: list[Step] = []
    # ⑦ 孩子的家目录只放三样：项目文件夹里的 AGENTS.md、斜杠命令、桌面上指向导出目录的链接。
    project_dir = child_home / PROJECT_DIR
    prompts_dir = child_home / ".codex" / "prompts"
    #    孩子家里已有的目录一步不动（keep_existing）：~/.codex 里有他的登录态，不能被放开。
    steps += [
        _mkdir(project_dir, child_user, 0o755, "孩子在 SFW 里打开的项目文件夹", keep_existing=True),
        _write(
            project_dir / AGENTS_FILE, read_asset(AGENTS_FILE), child_user, 0o644, "给模型的纪律"
        ),
        _mkdir(
            prompts_dir.parent,
            child_user,
            0o755,
            "SFW 读斜杠命令的目录（已存在则不动）",
            keep_existing=True,
        ),
        _mkdir(prompts_dir, child_user, 0o755, "斜杠命令目录", keep_existing=True),
        _write(
            prompts_dir / PROMPT_FILE, read_asset(PROMPT_FILE), child_user, 0o644, "/fd 那一句话"
        ),
        _symlink(
            child_home / "Desktop" / DESKTOP_LINK, export_dir, child_user, "桌面上能点开导出目录"
        ),
    ]

    return tuple(steps)


def plan_install(
    *,
    wheel: Path,
    child_user: str,
    child_home: Path,
    uv: Path,
    root: Path = DEFAULT_ROOT,
    export_dir: Path = DEFAULT_EXPORT_DIR,
    sfw_bearer: str,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    host: HostState = _FRESH_HOST,
) -> tuple[Step, ...]:
    """从仓库一把装完：①～⑥ 接 ⑦。.pkg 走的是 plan_system + plan_child 两步。"""
    return plan_system(
        wheel=wheel,
        uv=uv,
        root=root,
        export_dir=export_dir,
        sfw_bearer=sfw_bearer,
        organization_id=organization_id,
        connection_id=connection_id,
        host=host,
    ) + plan_child(child_user=child_user, child_home=child_home, export_dir=export_dir)


def plan_start(*, loaded: bool, plist_path: Path = PLIST_PATH) -> tuple[Step, ...]:
    """已登记过的 daemon 再 bootstrap 会报错，所以先问 launchd（`daemon_loaded`）；
    kickstart -k 无论如何都把服务起（或重启）一次。"""
    kickstart = _run((LAUNCHCTL, "kickstart", "-k", f"system/{LABEL}"), "现在就起（或重启）服务")
    if loaded:
        return (kickstart,)
    return (
        _run((LAUNCHCTL, "bootstrap", "system", str(plist_path)), "登记 LaunchDaemon"),
        kickstart,
    )


def plan_stop() -> tuple[Step, ...]:
    return (_run((LAUNCHCTL, "bootout", f"system/{LABEL}"), "停服务并注销 LaunchDaemon"),)


def daemon_loaded(run: Callable[[Sequence[str]], int] | None = None) -> bool:
    """`launchctl print system/<label>` 退出码 0 = 已登记。只读查询。"""
    probe = run or _exit_code
    return probe((LAUNCHCTL, "print", f"system/{LABEL}")) == 0


def _exit_code(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), capture_output=True, check=False).returncode


# ------------------------------------------------------------------ 执行


def _describe(step: Step) -> str:
    if step.kind == "run":
        return " ".join(step.argv or ())
    detail = str(step.path)
    if step.kind == "symlink":
        detail += f" -> {step.content}"
    attrs = [a for a in (step.owner, f"{step.mode:04o}" if step.mode is not None else None) if a]
    if step.kind == "write" and step.content is not None:
        attrs.append(f"{len(step.content.encode('utf-8'))} 字节")  # 内容不印：config 里有口令
    return f"{detail} ({', '.join(attrs)})" if attrs else detail


def execute(
    steps: Sequence[Step], *, dry_run: bool, geteuid: Callable[[], int] | None = None
) -> None:
    """dry_run 只打印。真执行前先看 euid：不是 root 就一步都不做。"""
    if not dry_run and (geteuid or os.geteuid)() != 0:
        raise InstallerError("要 sudo：这些步骤会建系统用户、写 /Library 与 /Users/Shared")
    total = len(steps)
    for index, step in enumerate(steps, start=1):
        prefix = "[干跑] " if dry_run else ""
        print(f"{prefix}[{index}/{total}] {step.kind:<7} {_describe(step)} — {step.why}")
        if not dry_run:
            note = _apply(step)
            if note:
                print(f"    {note}")


def _ids(owner: str) -> tuple[int, int]:
    user, _, group = owner.partition(":")
    try:
        entry = pwd.getpwnam(user)
        gid = grp.getgrnam(group).gr_gid if group else entry.pw_gid
    except KeyError as exc:
        raise InstallerError(f"系统里没有这个用户或组：{owner}") from exc
    return entry.pw_uid, gid


def _require_path(step: Step) -> Path:
    if step.path is None:
        raise InstallerError(f"{step.kind} 步骤没有路径：{step.why}")
    return step.path


def _open_parent(path: Path) -> tuple[int, str]:
    """把绝对路径逐段以 O_NOFOLLOW 打开到父目录，返回 (父目录 fd, 最后一段名)。

    安装器以 root 跑，而它写的三样东西在孩子家里。孩子只要把 `~/否定词` 或 `~/Desktop`
    换成指向别处的符号链接、再在那里预置一个同名 `.tmp` 链接，root 跟着走一步就等于替他
    截断任何文件并把属主交给他（2026-09-20 复审以非 root 复现了跟随与截断）。所以每一段
    都用 O_NOFOLLOW 打开、后面的操作全部相对这个 fd：任何一段是符号链接就停，包括 macOS
    自带的 /tmp、/var——安装器不写那里。
    """
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise InstallerError(f"安装器只写绝对路径，且末段不能是 . 或 ..：{path}")
    *parents, name = path.parts[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parents:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        os.close(fd)
        raise InstallerError(f"{path} 的上级目录打不开或是符号链接，不动它：{exc}") from exc
    return fd, name


def _set_owner_mode(parent: int, name: str, ids: tuple[int, int] | None, mode: int | None) -> None:
    """给 parent 下的 name（文件或目录）设属主与权限：O_NOFOLLOW 打开后在 fd 上做，不跟链接。"""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if ids is not None:
            os.fchown(fd, *ids)
        if mode is not None:
            os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _apply_mkdir(parent: int, name: str, step: Step) -> str | None:
    mode = step.mode if step.mode is not None else 0o755
    ids = _ids(step.owner) if step.owner else None
    try:
        os.mkdir(name, mode, dir_fd=parent)
    except FileExistsError:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise InstallerError(f"{step.path} 已存在但不是目录（符号链接？），不动它") from None
        if step.keep_existing:
            return "已存在，不动"
        expected_uid = ids[0] if ids is not None else 0
        if st.st_uid != expected_uid:
            raise InstallerError(
                f"{step.path} 已存在，属 uid {st.st_uid} 而不是 {step.owner or 'root'}："
                "不收编别人建的目录（里面可能预置了文件），先 sudo rm -rf 它再装"
            ) from None
        _set_owner_mode(parent, name, ids, mode)
        return "已存在，属主对，权限按上面校正"
    _set_owner_mode(parent, name, ids, mode)
    return None


def _apply_write(parent: int, name: str, step: Step) -> None:
    mode = step.mode if step.mode is not None else 0o644
    ids = _ids(step.owner) if step.owner else None
    # 随机临时名 + O_EXCL|O_NOFOLLOW：预置好的同名链接只会让这里报错，不会被跟着走。
    # 写完 rename 到目标：目标若是符号链接，换掉的是链接本身，不是它指向的文件。
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode, dir_fd=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(step.content or "")
            if ids is not None:
                os.fchown(fd, *ids)
            os.fchmod(fd, mode)
        os.rename(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=parent)
        raise


def _apply_symlink(parent: int, name: str, step: Step) -> None:
    target = step.content or ""
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISLNK(st.st_mode):
            raise InstallerError(f"{step.path} 已存在且不是符号链接，不动它")
        if os.readlink(name, dir_fd=parent) == target:
            return
        os.unlink(name, dir_fd=parent)
    os.symlink(target, name, dir_fd=parent)
    if step.owner:
        os.chown(name, *_ids(step.owner), dir_fd=parent, follow_symlinks=False)


def _apply(step: Step) -> str | None:
    """做一步。返回值是紧跟在计划行下面印给管理员看的补充说明（没有就 None）。"""
    if step.kind == "run":
        subprocess.run(list(step.argv or ()), check=True)
        return None
    parent, name = _open_parent(_require_path(step))
    try:
        if step.kind == "mkdir":
            return _apply_mkdir(parent, name, step)
        if step.kind == "write":
            _apply_write(parent, name, step)
        elif step.kind == "symlink":
            _apply_symlink(parent, name, step)
        elif step.kind == "chown":
            _set_owner_mode(parent, name, _ids(step.owner or ""), None)
        elif step.kind == "chmod":
            _set_owner_mode(parent, name, None, step.mode if step.mode is not None else 0o644)
        return None
    finally:
        os.close(parent)


# ------------------------------------------------------------------ 宿主查询（只读）


def parse_dscl_list(text: str) -> dict[str, int]:
    """`dscl . -list /Users UniqueID` 的输出：每行「名字  数字」。"""
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
            result[parts[0]] = int(parts[-1])
    return result


def _capture(argv: Sequence[str]) -> str:
    try:
        return subprocess.run(list(argv), check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallerError(f"读不到目录服务（{' '.join(argv)}）：{exc}") from exc


def inspect_host(
    *, config_path: Path, query: Callable[[Sequence[str]], str] = _capture
) -> HostState:
    users = parse_dscl_list(query((DSCL, ".", "-list", "/Users", "UniqueID")))
    groups = parse_dscl_list(query((DSCL, ".", "-list", "/Groups", "PrimaryGroupID")))
    return HostState(
        service_uid=users.get(SERVICE_USER),
        taken_ids=frozenset(users.values()) | frozenset(groups.values()),
        config_exists=config_path.exists(),
    )


def service_uid() -> int | None:
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid
    except KeyError:
        return None


def _passwd(user: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(user)
    except KeyError as exc:
        raise InstallerError(f"系统里没有这个用户：{user}") from exc


def home_of(user: str) -> Path:
    return Path(_passwd(user).pw_dir)


def uid_of(user: str) -> int:
    return _passwd(user).pw_uid


def read_sfw_bearer(config_path: Path, *, expect_uid: int | None) -> str:
    """只读 sfw_bearer（print-registration 用）：店铺表还没填时 load_config 会拒，这里不需要它。"""
    check_private_file(config_path, expect_uid=expect_uid)
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError(f"读不了配置文件 {config_path}：{exc}") from exc
    bearer = document.get("sfw_bearer")
    if not isinstance(bearer, str) or not bearer:
        raise InstallerError("配置文件里没有 sfw_bearer：这个文件不是安装器写的")
    return bearer


# ------------------------------------------------------------------ 服务探测


def http_status(url: str, *, timeout: float = 5.0) -> int | None:
    """GET 一次，返回状态码；连不上返回 None。只用于回环地址。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def probe_port(port: int) -> PortState:
    """能 bind 就是空闲；bind 不了就 GET /mcp：401 = 我们的服务（Bearer 生效），其余 = 别的程序。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # 刚停的服务会留下 TIME_WAIT 连接，不设 SO_REUSEADDR 时 bind 会把它当成"被占用"
        # （2026-09-20 本机实测：停服后 30 秒内 bind 报 EADDRINUSE）。有进程在 LISTEN 时
        # 设了也照样 bind 失败，判断不受影响。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return "free"
        except OSError:
            pass
    return "ours" if http_status(f"http://127.0.0.1:{port}/mcp") == 401 else "other"


def wait_for_401(
    port: int,
    *,
    attempts: int = 10,
    status: Callable[[str], int | None] = http_status,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """start 之后等服务起来：GET /mcp 得 401 才算「服务在、Bearer 生效」。"""
    url = f"http://127.0.0.1:{port}/mcp"
    for attempt in range(attempts):
        if status(url) == 401:
            return True
        if attempt + 1 < attempts:
            sleep(1.0)
    return False


# ------------------------------------------------------------------ 体检（每项判定是纯函数）


def judge_service_user(uid: int | None) -> Check:
    if uid is None:
        return ("系统用户", False, f"还没有系统用户 {SERVICE_USER}：先 sudo amazon-ads install")
    return ("系统用户", True, f"{SERVICE_USER} 存在，uid {uid}")


def judge_child_cannot_read(st: os.stat_result | None, child_uid: int | None) -> Check:
    name = "孩子读不到密钥"
    if st is None:
        return (name, False, "配置文件不存在")
    if st.st_mode & 0o077:
        return (
            name,
            False,
            f"配置文件权限 {stat.S_IMODE(st.st_mode):04o}：别的账号也能读，要 chmod 600",
        )
    if child_uid is not None and st.st_uid == child_uid:
        return (name, False, f"配置文件的属主就是孩子（uid {child_uid}）：他能直接读密钥")
    return (name, True, f"0600 且属 uid {st.st_uid}；孩子的账号读它会得到 EACCES")


def judge_export_dir(
    st: os.stat_result | None,
    expect_uid: int | None,
    child_uid: int | None,
    *,
    name: str = "导出目录",
) -> Check:
    """导出目录与运行记录目录同一套判定：属 _amazonads、属主可写、别人改不了、不是孩子的。"""
    if st is None:
        return (name, False, f"{name}不存在：先 sudo amazon-ads install")
    if not stat.S_ISDIR(st.st_mode):
        return (name, False, f"{name}不是目录")
    if expect_uid is not None and st.st_uid != expect_uid:
        return (name, False, f"属主是 uid {st.st_uid}，不是 {SERVICE_USER}（uid {expect_uid}）")
    if not st.st_mode & stat.S_IWUSR:
        return (name, False, "属主自己没有写权限：服务写不了文件")
    if st.st_mode & 0o022:
        return (
            name,
            False,
            f"权限 {stat.S_IMODE(st.st_mode):04o}：别的账号也能改文件，要 chmod 755",
        )
    if child_uid is not None and st.st_uid == child_uid:
        return (name, False, f"{name}的属主就是孩子（uid {child_uid}）：他能改文件")
    return (name, True, f"属 uid {st.st_uid}、{stat.S_IMODE(st.st_mode):04o}：服务能写，孩子只能读")


def judge_code_owner(st: os.stat_result | None, path: Path) -> Check:
    """代码属 root：被攻破的 _amazonads 进程改不了自己下次启动要跑的解释器与包。

    只看 venv 里的解释器这一个哨兵：它是 launchd 起服务时打开的第一个文件，
    属主不对，整棵 python/、venv/ 多半都被交出去了。
    """
    name = "代码属 root"
    if st is None:
        return (name, False, f"{path} 不存在：先 sudo amazon-ads install")
    if st.st_uid != 0:
        fix = " ".join(f"'{DEFAULT_ROOT / sub}'" for sub in ("python", "venv"))
        return (
            name,
            False,
            f"{path} 属 uid {st.st_uid}，不是 root：{SERVICE_USER} 一旦被攻破就能改写自己的代码；"
            f"要 sudo chown -R root:wheel {fix}",
        )
    if st.st_mode & 0o022:
        return (
            name,
            False,
            f"{path} 权限 {stat.S_IMODE(st.st_mode):04o}：别的账号也能改它，要 chmod 755",
        )
    return (
        name,
        True,
        f"{path} 属 root、{stat.S_IMODE(st.st_mode):04o}：{SERVICE_USER} 只能读和执行",
    )


def judge_registration(payload: Mapping[str, object]) -> Check:
    problems = registration_problems(payload)
    if problems:
        return ("登记 JSON", False, "；".join(problems))
    return ("登记 JSON", True, "键、名称、地址、口令、超时都合规")


def judge_port(state: PortState, port: int, *, daemon_registered: bool) -> Check:
    name = f"端口 {port}"
    if state == "free":
        if daemon_registered:
            # 「登记了但没人听」就是服务死了。此前这里一律判通过，于是助手说「工具没连上」、
            # 管理员照 README 跑 doctor，拿到的是一份全绿报告和退出码 0，下一步无处可去。
            return (
                name,
                False,
                f"LaunchDaemon {LABEL} 已登记，但没人在听：服务没起来。"
                f"先看 {LOG_PATH}，再 sudo amazon-ads start",
            )
        return (name, True, "空闲，可以 start")
    if state == "ours":
        return (name, True, "已由本服务占用（GET /mcp 得 401），start 之后就该这样")
    return (
        name,
        False,
        f"被别的程序占用（GET /mcp 没得到 401）：用 sudo lsof -nP -iTCP:{port} -sTCP:LISTEN "
        "查是谁并停掉它；真要换端口得改 /Library/LaunchDaemons/local.amazon-ads.plist 里的 "
        "--port，再 sudo amazon-ads stop、sudo amazon-ads start --port <新端口>，"
        "并用 print-registration --port 重新登记",
    )


def judge_directory(profile_ids: Collection[str], stores: Sequence[StoreConfig]) -> Check:
    name = "领星名录"
    missing = [
        f"{s.nickname}（profile_id {s.profile_id}）"
        for s in stores
        if s.profile_id not in profile_ids
    ]
    if missing:
        return (
            name,
            False,
            f"名录里有 {len(profile_ids)} 家店，配置里这 {len(missing)} 家不在其中："
            + "、".join(missing)
            + "；用 sudo amazon-ads shops 重新对一遍",
        )
    return (
        name,
        True,
        f"ad_auth_shops 返回 {len(profile_ids)} 家店，配置里的 {len(stores)} 家都在",
    )


def _describe_config(cfg: PackConfig) -> str:
    stores = "、".join(s.nickname for s in cfg.stores)
    spend = "、".join(f"{amount} {ccy}" for ccy, amount in sorted(cfg.thresholds.min_spend.items()))
    t = cfg.thresholds
    return (
        f"{len(cfg.stores)} 家店（{stores}）；花费 ≥ {spend}；点击 ≥ {t.min_clicks}；"
        f"回看 {t.lookback_days} 天"
    )


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _shop_rows(
    config_path: Path, *, expect_uid: int | None, client_factory: ClientFactory | None
) -> Sequence[object]:
    url, key = read_lingxing_credentials(config_path, expect_uid=expect_uid)
    factory = client_factory or LxMcpReadClient
    page = factory(url, key).fetch_page(AUTH_SHOPS_TOOL_ID, {})
    rows = page.get("rows")
    return rows if isinstance(rows, list | tuple) else ()


def doctor(
    config_path: Path,
    *,
    expect_uid: int | None,
    child_uid: int | None,
    port: int,
    online: bool,
    daemon_registered: bool,
    client_factory: ClientFactory | None = None,
) -> list[Check]:
    """计划 §1.2 第 5 步。任一项失败都不该进下一步；online 项恰好调一次 ad_auth_shops。"""
    checks: list[Check] = [judge_service_user(expect_uid)]
    try:
        check_private_file(config_path, expect_uid=expect_uid)
        checks.append(("配置文件私有", True, f"{config_path} 存在、0600、属主正确"))
    except ConfigError as exc:
        checks.append(("配置文件私有", False, str(exc)))
    cfg: PackConfig | None = None
    try:
        cfg = load_config(config_path, expect_uid=expect_uid)
        checks.append(("配置内容", True, _describe_config(cfg)))
    except ConfigError as exc:
        checks.append(("配置内容", False, str(exc)))
    checks.append(judge_child_cannot_read(_stat_or_none(config_path), child_uid))
    export_dir = cfg.export_dir if cfg is not None else DEFAULT_EXPORT_DIR
    checks.append(judge_export_dir(_stat_or_none(export_dir), expect_uid, child_uid))
    # 运行记录写在导出目录的上一层；那一层写不了时工具整段失败，体检不能全绿（2026-09-20 复审）。
    run_log_dir = (cfg.run_log_path if cfg is not None else DEFAULT_RUN_LOG).parent
    checks.append(
        judge_export_dir(_stat_or_none(run_log_dir), expect_uid, child_uid, name="运行记录目录")
    )
    checks.append(judge_code_owner(_stat_or_none(VENV_PYTHON), VENV_PYTHON))
    if cfg is not None:
        checks.append(judge_registration(registration_json(cfg.sfw_bearer, port=port)))
    checks.append(judge_port(probe_port(port), port, daemon_registered=daemon_registered))
    if online:
        try:
            rows = _shop_rows(config_path, expect_uid=expect_uid, client_factory=client_factory)
        except (ConfigError, LxReadError) as exc:
            code = getattr(exc, "code", "")
            checks.append(("领星名录", False, f"取数失败（{code}）：{exc}"))
        else:
            ids = {_field(row, "profile_id") for row in rows if isinstance(row, Mapping)}
            checks.append(judge_directory(ids - {""}, cfg.stores if cfg is not None else ()))
    return checks


# ------------------------------------------------------------------ shops


_TOML_SAFE = re.compile(r"^[\w.-]+$")
NICKNAME_PLACEHOLDER = "<给它起个名字>"


def _field(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None or isinstance(value, bool) else str(value).strip()


def render_shops(rows: Sequence[object]) -> str:
    """把 ad_auth_shops 的行渲染成一张表 + 可直接粘进 config.toml 的 [[stores]] 段。

    只印 profile_id/sid/country 与建议币种；缺任一项、或值里有引号/空白之类进不了 TOML
    字符串的字符，该行跳过并计数——不猜、不修。
    """
    table: list[str] = []
    blocks: list[str] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, Mapping):
            skipped += 1
            continue
        pid, sid, country = (_field(row, k) for k in ("profile_id", "sid", "country"))
        country = country.upper()
        if not all(_TOML_SAFE.fullmatch(v) for v in (pid, sid, country)):
            skipped += 1
            continue
        currency = MARKETPLACE_CURRENCY.get(country, "")
        table.append(f"{pid:<20} {sid:<20} {country:<4} {currency or '（不知道，自己填）'}")
        hint = (
            "" if currency else f"  # 站点 {country} 不在建议表里：填这家店报表用的三字母币种代码"
        )
        blocks.append(
            "\n".join(
                (
                    "[[stores]]",
                    f'profile_id = "{pid}"',
                    f'sid = "{sid}"',
                    f'marketplace = "{country}"',
                    f'currency = "{currency}"{hint}',
                    f'nickname = "{NICKNAME_PLACEHOLDER}"',
                )
            )
        )
    if not table:
        return f"领星没有返回可用的已授权店铺（收到 {len(rows)} 行，跳过 {skipped} 行）。"
    note = f"，另有 {skipped} 行缺 profile_id/sid/country、已跳过" if skipped else ""
    header = f"{'profile_id':<20} {'sid':<20} {'站点':<4} 建议币种"
    return "\n".join(
        (
            f"已授权店铺 {len(table)} 家{note}：",
            header,
            *table,
            "",
            "把下面的段落粘进 config.toml，给每家店起个名字",
            "（中文、字母、数字，不超过 20 个字，不能有空格），",
            "再到 [thresholds.min_spend] 给每个币种填一档门槛：",
            "",
            "\n\n".join(blocks),
        )
    )


def shops(
    config_path: Path, *, expect_uid: int | None, client_factory: ClientFactory | None = None
) -> str:
    """调一次 ad_auth_shops。输出里没有密钥：key 只从配置读、只交给客户端。"""
    return render_shops(
        _shop_rows(config_path, expect_uid=expect_uid, client_factory=client_factory)
    )
