"""安装器：把组件装成 `_adspack` 名下的 LaunchDaemon，并给孩子的家目录放三样东西。

每个副作用先由纯函数组装成 `Step`（做什么、写到哪、属谁、为什么），`execute` 再逐条做；
`--dry-run` 只打印。这样分层不是为了好看：`dscl`/`launchctl`/`chown` 一跑就改了系统，
而这些命令在开发机上不能试（规格 §0 第 1 条），测试能盯住的只有「计划」——所以计划
必须把每一步说全，执行层只做搬运，不再做任何决定。

隔离模型（计划 §1、§8 攻击 1/2/4 的修法）：组件以系统用户 `_adspack` 常驻，代码、venv、
config 在 `/Library/Application Support/ads-pack/`，产物在 `/Users/Shared/ads-pack/导出/`。
孩子 uid 下的进程读 config 得 EACCES、写导出目录得 EACCES——「密钥读不走、文件改不了」
两条都由文件属主承载，不靠组件内的任何状态；`doctor` 就是把这两条按 stat 结果复核一遍。

本模块在开发机上从未真跑过（规格 §0 不允许）：`dscl` 建用户序列、`uv python install
--install-dir` 装进带空格的 `/Library` 路径、LaunchDaemon 以非 root `UserName` 跑 Python，
全部是计划 §9 列明的【假设】，等首次真机安装（计划 §7 第 8 项）验证。
"""

from __future__ import annotations

import grp
import os
import pwd
import re
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

LABEL = "local.ads-pack"
PLIST_PATH = Path("/Library/LaunchDaemons") / f"{LABEL}.plist"
LOG_PATH = DEFAULT_LOG_DIR / "ads-pack.log"
BIN_LINK = Path("/usr/local/bin/ads-pack")
PYTHON_VERSION = "3.12"

#: SFW 登记 JSON 的键闭集（宿主合同 §3-3）与 name 形状（§3-1）；`tool_timeout_sec` 上限（§3-5）。
REGISTRATION_NAME = "ads-pack"
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
    `owner` 写成 "用户" 或 "用户:组"；chown 一律递归。"""

    kind: StepKind
    argv: tuple[str, ...] | None = None
    path: Path | None = None
    content: str | None = None
    owner: str | None = None
    mode: int | None = None
    why: str


@dataclass(frozen=True, kw_only=True)
class HostState:
    """安装前从宿主读到的事实快照。读是只读查询（`inspect_host`），计划函数只看快照。"""

    #: `_adspack` 已存在时它的 uid。再装一次不能换号：旧号名下的文件会全部成孤儿。
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
    """管理员粘进 SFW Hub「高级配置 · 本地命令 / JSON」的那一段。键闭集见 REGISTRATION_KEYS。"""
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
    """把 assets/local.ads-pack.plist 模板填成绝对路径。值先做 XML 转义再代入。"""
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


def _mkdir(path: Path, owner: str, mode: int, why: str) -> Step:
    return Step(kind="mkdir", path=path, owner=owner, mode=mode, why=why)


def _write(path: Path, content: str, owner: str, mode: int, why: str) -> Step:
    return Step(kind="write", path=path, content=content, owner=owner, mode=mode, why=why)


def _symlink(path: Path, target: Path, owner: str | None, why: str) -> Step:
    return Step(kind="symlink", path=path, content=str(target), owner=owner, why=why)


def pick_service_id(taken: Collection[int]) -> int:
    """200..400 里第一个既不是 uid 也不是 gid 的号；用同一个号建组和用户。"""
    for candidate in SERVICE_ID_RANGE:
        if candidate not in taken:
            return candidate
    raise InstallerError("200 到 400 的系统账号段没有空号了：手工建 _adspack 后再装")


def plan_service_user(host: HostState) -> tuple[Step, ...]:
    """建隐藏的系统用户 `_adspack`：无 shell、家目录 /var/empty、不进登录窗。已存在则一步不动。"""
    if host.service_uid is not None:
        return ()
    ident = str(pick_service_id(host.taken_ids))
    group = f"/Groups/{SERVICE_USER}"
    user = f"/Users/{SERVICE_USER}"
    real_name = "ads-pack service"

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
    """计划 §1.2 第 2 步的 ①～⑦（⑧ 打印登记 JSON 由 __main__ 在执行后做）。纯函数。"""
    for name, path in (("wheel", wheel), ("child_home", child_home), ("uv", uv), ("root", root)):
        if not path.is_absolute():
            raise InstallerError(f"{name} 要是绝对路径：{path}")
    if wheel.suffix != ".whl":
        raise InstallerError(f"--wheel 要指向 uv build --wheel 打出来的 .whl 文件：{wheel}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", child_user):
        raise InstallerError(f"孩子的登录名不像 macOS 用户名：{child_user!r}")
    if not re.fullmatch(r"[0-9A-Fa-f]{32,}", sfw_bearer):
        raise InstallerError("sfw_bearer 要是至少 32 位的十六进制串")
    if not PROMPT_NAME_RE.fullmatch(PROMPT_FILE):
        raise InstallerError(f"斜杠命令文件名必须是 ASCII：{PROMPT_FILE}")

    service = f"{SERVICE_USER}:{SERVICE_USER}"
    python_dir = root / "python"
    venv = root / "venv"
    config_path = root / "config.toml"
    log_dir = root / "logs"
    steps: list[Step] = list(plan_service_user(host))

    # ② 代码与解释器：uv 装一份托管 Python 到 /Library 下，venv 指向它，再装 wheel。
    #    venv 那一步用 UV_PYTHON_INSTALL_DIR 而不是写死解释器路径：托管 Python 的目录名
    #    带完整小版本号（cpython-3.12.x-…），计划期不知道 x 是几。
    steps += [
        _mkdir(root, "root:wheel", 0o755, "组件的家：代码、venv、config、日志都在这"),
        _mkdir(python_dir, "root:wheel", 0o755, "uv 托管的 Python 放这里，不碰系统 Python"),
        _mkdir(log_dir, "root:wheel", 0o755, "LaunchDaemon 的 stdout/stderr 落这里"),
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
        Step(kind="chown", path=root, owner=service, why=f"整个目录树交给 {SERVICE_USER}"),
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
                "配置模板：只有 _adspack 能读，密钥以后填在这里",
            )
        )

    # ④ 产物目录：属 _adspack，别人只读。上一级也归它——运行记录.csv 写在那里。
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

    # ⑥ 孩子的家目录只放三样：项目文件夹里的 AGENTS.md、斜杠命令、桌面上指向导出目录的链接。
    project_dir = child_home / PROJECT_DIR
    prompts_dir = child_home / ".codex" / "prompts"
    steps += [
        _mkdir(project_dir, child_user, 0o755, "孩子在 SFW 里打开的项目文件夹"),
        _write(
            project_dir / AGENTS_FILE, read_asset(AGENTS_FILE), child_user, 0o644, "给模型的纪律"
        ),
        _mkdir(prompts_dir.parent, child_user, 0o755, "SFW 读斜杠命令的目录（已存在则不动）"),
        _mkdir(prompts_dir, child_user, 0o755, "斜杠命令目录"),
        _write(
            prompts_dir / PROMPT_FILE, read_asset(PROMPT_FILE), child_user, 0o644, "/fd 那一句话"
        ),
        _symlink(
            child_home / "Desktop" / DESKTOP_LINK, export_dir, child_user, "桌面上能点开导出目录"
        ),
    ]

    # ⑦ 管理员命令：sudo ads-pack …
    steps += [
        _mkdir(BIN_LINK.parent, "root:wheel", 0o755, "/usr/local/bin 有时不存在"),
        _symlink(
            BIN_LINK,
            venv / "bin" / "ads-pack",
            None,
            "管理员命令：sudo ads-pack shops/doctor/start",
        ),
    ]
    return tuple(steps)


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
            _apply(step)


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


def _apply(step: Step) -> None:
    if step.kind == "run":
        subprocess.run(list(step.argv or ()), check=True)
        return
    path = _require_path(step)
    if step.kind == "mkdir":
        if path.is_dir():
            return  # 已存在的目录不改属主与权限：可能是孩子自己的 ~/.codex
        path.mkdir(mode=step.mode if step.mode is not None else 0o755)
        if step.owner:
            os.chown(path, *_ids(step.owner))
        if step.mode is not None:
            os.chmod(path, step.mode)
    elif step.kind == "write":
        mode = step.mode if step.mode is not None else 0o644
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(step.content or "")
            if step.owner:
                os.fchown(fd, *_ids(step.owner))
            os.fchmod(fd, mode)
        os.replace(tmp, path)
    elif step.kind == "symlink":
        target = step.content or ""
        if path.is_symlink():
            if os.readlink(path) == target:
                return
            path.unlink()
        elif path.exists():
            raise InstallerError(f"{path} 已存在且不是符号链接，不动它")
        os.symlink(target, path)
        if step.owner:
            os.lchown(path, *_ids(step.owner))
    elif step.kind == "chown":
        uid, gid = _ids(step.owner or "")
        os.lchown(path, uid, gid)
        for directory, dirs, filenames in os.walk(path):
            for name in dirs + filenames:
                os.lchown(os.path.join(directory, name), uid, gid)
    elif step.kind == "chmod":
        os.chmod(path, step.mode if step.mode is not None else 0o644)


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
        return ("系统用户", False, f"还没有系统用户 {SERVICE_USER}：先 sudo ads-pack install")
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
    st: os.stat_result | None, expect_uid: int | None, child_uid: int | None
) -> Check:
    name = "导出目录"
    if st is None:
        return (name, False, "导出目录不存在：先 sudo ads-pack install")
    if not stat.S_ISDIR(st.st_mode):
        return (name, False, "导出目录不是目录")
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
        return (name, False, f"导出目录的属主就是孩子（uid {child_uid}）：他能改文件")
    return (name, True, f"属 uid {st.st_uid}、{stat.S_IMODE(st.st_mode):04o}：服务能写，孩子只能读")


def judge_registration(payload: Mapping[str, object]) -> Check:
    problems = registration_problems(payload)
    if problems:
        return ("登记 JSON", False, "；".join(problems))
    return ("登记 JSON", True, "键、名称、地址、口令、超时都合规")


def judge_port(state: PortState, port: int) -> Check:
    name = f"端口 {port}"
    if state == "free":
        return (name, True, "空闲，可以 start")
    if state == "ours":
        return (name, True, "已由本服务占用（GET /mcp 得 401），start 之后就该这样")
    return (name, False, "被别的程序占用（GET /mcp 没得到 401）：换端口或先停掉它")


def judge_directory(profile_ids: Collection[str], stores: Sequence[StoreConfig]) -> Check:
    name = "领星名录"
    missing = [s.nickname for s in stores if s.profile_id not in profile_ids]
    if missing:
        return (name, False, f"名录里有 {len(profile_ids)} 家店，但配置里的 {missing} 不在其中")
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
        f"回看 {t.lookback_days} 天；数据最多 {t.max_data_staleness_hours} 小时旧"
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
    if cfg is not None:
        checks.append(judge_registration(registration_json(cfg.sfw_bearer, port=port)))
    checks.append(judge_port(probe_port(port), port))
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
