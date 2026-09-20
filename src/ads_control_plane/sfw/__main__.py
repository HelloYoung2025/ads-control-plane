"""`ads-pack` / `python -m ads_control_plane.sfw` 的入口。

serve 是 LaunchDaemon 跑的那条路，其余子命令（install/start/stop/shops/doctor/
print-registration）是管理员在终端里用 sudo 跑的，全部交给 installer。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from ads_control_plane.adapters.lx_read import LxReadError
from ads_control_plane.sfw import installer
from ads_control_plane.sfw.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PORT,
    SERVICE_USER,
    ConfigError,
)
from ads_control_plane.sfw.installer import InstallerError

#: 干跑时登记 JSON 的 secret 位置放这句话：口令要等真装完才写进 config，现在印一个假的没意义。
_SECRET_AFTER_INSTALL = "<安装后用 sudo ads-pack print-registration 查看>"

_UV_CANDIDATES = (Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"))


def _serve(args: argparse.Namespace) -> int:
    # 延迟 import：uvicorn 与 MCP 服务端只有这条路需要；install/doctor 不该为它付启动成本。
    from ads_control_plane.sfw.server import serve

    serve(Path(args.config), port=args.port, no_auth=args.no_auth, expect_uid=args.expect_uid)
    return 0


def _find_uv(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    found = shutil.which("uv")
    if found:
        return Path(found).resolve()
    for candidate in _UV_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise InstallerError(
        "找不到 uv：用 --uv 指定它的绝对路径（例如 /Users/<管理员>/.local/bin/uv）"
    )


def _install(args: argparse.Namespace) -> int:
    wheel = Path(args.wheel).resolve()
    if not wheel.is_file():
        raise InstallerError(f"找不到 wheel：{wheel}（先 uv build --wheel）")
    child_home = (
        Path(args.child_home).resolve() if args.child_home else installer.home_of(args.child_user)
    )
    try:
        host = installer.inspect_host(config_path=DEFAULT_CONFIG_PATH)
    except InstallerError as exc:
        if not args.dry_run:
            raise
        print(f"注意：{exc}；干跑按「{SERVICE_USER} 不存在」拟计划", file=sys.stderr)
        host = installer.HostState(config_exists=DEFAULT_CONFIG_PATH.exists())
    if host.config_exists and not args.dry_run:
        bearer = installer.read_sfw_bearer(DEFAULT_CONFIG_PATH, expect_uid=None)
    else:
        bearer = secrets.token_hex(16)
    steps = installer.plan_install(
        wheel=wheel,
        child_user=args.child_user,
        child_home=child_home,
        uv=_find_uv(args.uv),
        sfw_bearer=bearer,
        organization_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
        host=host,
    )
    user_line = (
        f"已存在（uid {host.service_uid}），不动"
        if host.service_uid is not None
        else f"不存在，将建（uid 取 {installer.pick_service_id(host.taken_ids)}）"
    )
    print(f"系统用户 {SERVICE_USER}：{user_line}")
    print(f"config.toml：{'已存在，保留内容' if host.config_exists else '将写模板'}")
    installer.execute(steps, dry_run=args.dry_run)
    payload = installer.registration_json(_SECRET_AFTER_INSTALL if args.dry_run else bearer)
    print()
    print("粘进 SFW「MCP → 添加服务器 → 高级配置 · 本地命令 / JSON」的登记 JSON：")
    print(json.dumps(payload, ensure_ascii=False))
    print()
    print(f"下一步：sudo -e '{DEFAULT_CONFIG_PATH}' 填 [lingxing]；")
    print("sudo ads-pack shops；把打印的 [[stores]] 段粘进配置；")
    print("sudo ads-pack doctor 全过之后 sudo ads-pack start；")
    print("再照 README「管理员一次性安装」第 7–10 步收尾——那几步要在孩子的账号里做，")
    print("且必须重启一次 SFW，否则 /fd 不存在。")
    return 0


def _start(args: argparse.Namespace) -> int:
    installer.execute(installer.plan_start(loaded=installer.daemon_loaded()), dry_run=False)
    if installer.wait_for_401(args.port):
        print(f"服务在 127.0.0.1:{args.port}，Bearer 生效（GET /mcp 得 401）。")
        return 0
    print(f"服务没在 {args.port} 上应答 401：看日志 {installer.LOG_PATH}", file=sys.stderr)
    return 1


def _stop(args: argparse.Namespace) -> int:
    if not installer.daemon_loaded():
        print("服务本来就没登记，不用停。")
        return 0
    installer.execute(installer.plan_stop(), dry_run=False)
    return 0


def _shops(args: argparse.Namespace) -> int:
    print(installer.shops(Path(args.config), expect_uid=installer.service_uid()))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    child_uid = installer.uid_of(args.child_user) if args.child_user else None
    checks = installer.doctor(
        Path(args.config),
        expect_uid=installer.service_uid(),
        child_uid=child_uid,
        port=args.port,
        online=not args.offline,
    )
    for name, passed, detail in checks:
        print(f"[{'通过' if passed else '失败'}] {name}：{detail}")
    failed = sum(1 for _, passed, _ in checks if not passed)
    print(
        f"{len(checks) - failed}/{len(checks)} 项通过" + ("" if failed == 0 else "；先修好再 start")
    )
    return 0 if failed == 0 else 1


def _print_registration(args: argparse.Namespace) -> int:
    bearer = installer.read_sfw_bearer(Path(args.config), expect_uid=installer.service_uid())
    print(json.dumps(installer.registration_json(bearer, port=args.port), ensure_ascii=False))
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "serve": _serve,
    "install": _install,
    "start": _start,
    "stop": _stop,
    "shops": _shops,
    "doctor": _doctor,
    "print-registration": _print_registration,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ads-pack", description="SFW 电商 Pack：只读否定词组件")
    commands = parser.add_subparsers(dest="command", required=True)

    def with_config(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config.toml 的路径")
        return sub

    serve = with_config(commands.add_parser("serve", help="常驻 MCP 服务（LaunchDaemon 用）"))
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--no-auth", action="store_true", help="不校验 Bearer——只给本机调试用")
    serve.add_argument(
        "--expect-uid",
        type=int,
        default=os.geteuid(),
        help="config.toml 必须属于这个 uid（缺省：本进程的 uid）",
    )

    install = commands.add_parser("install", help="装成 _adspack 名下的 LaunchDaemon（要 sudo）")
    install.add_argument("--wheel", required=True, help="uv build --wheel 打出来的 .whl")
    install.add_argument("--child-user", required=True, help="孩子的 macOS 登录名")
    install.add_argument("--child-home", help="孩子的家目录（缺省按登录名查）")
    install.add_argument("--uv", help="uv 的绝对路径（缺省在 PATH 与常见位置找）")
    install.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何事")

    start = commands.add_parser("start", help="launchctl bootstrap + kickstart，然后探 401")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    commands.add_parser("stop", help="launchctl bootout")

    with_config(commands.add_parser("shops", help="列出领星已授权店铺，打印可粘贴的 [[stores]]"))

    doctor = with_config(commands.add_parser("doctor", help="安装体检：任一项失败不要 start"))
    doctor.add_argument("--child-user", help="孩子的登录名：多查两条「孩子读不到、改不了」")
    doctor.add_argument("--port", type=int, default=DEFAULT_PORT)
    doctor.add_argument("--offline", action="store_true", help="不调领星名录")

    registration = with_config(commands.add_parser("print-registration", help="打印登记 JSON"))
    registration.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _HANDLERS[args.command](args)
    except (InstallerError, ConfigError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except LxReadError as exc:
        detail = getattr(exc, "error_details", None)
        print(
            f"错误：领星取数失败（{exc.code}）：{exc}"
            + (f"；网关原话：{detail}" if detail else "")
            + f"；核对 [lingxing] 的 url 与 key（sudo -e '{DEFAULT_CONFIG_PATH}'），改完重跑。",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            f"错误：命令失败（退出码 {exc.returncode}）：{' '.join(map(str, exc.cmd))}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:  # 找不到文件/命令、权限不够、目录位置被别的东西占着
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
