"""安装器测试：计划与渲染只看纯函数；执行层只在临时目录里、以本用户身份真跑。

这里没有一处会调 dscl/launchctl/sysadminctl，也不会写 /Library、/Users/Shared 或任何家目录
（规格 §0 第 1 条）。`execute` 真跑的只有计划里落在「孩子家目录」的那几步——家目录换成
tmp_path、root 门用注入的 geteuid 绕过、属主一律映射成本用户；它要钉的是路径处理
（2026-09-20 复审：跟着孩子预置的符号链接走），不是 chown 本身。
"""

from __future__ import annotations

import ast
import contextlib
import os
import plistlib
import re
import shutil
import socket
import stat
import sys
import threading
import tomllib
import types
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ads_control_plane.adapters.lx_read import LxTransportError
from ads_control_plane.sfw import __main__ as cli
from ads_control_plane.sfw import installer
from ads_control_plane.sfw.config import (
    DEFAULT_EXPORT_DIR,
    DEFAULT_ROOT,
    NICKNAME_RE,
    SERVICE_USER,
    ConfigError,
    parse_config,
    render_config_template,
)
from ads_control_plane.sfw.installer import (
    LOG_PATH,
    NICKNAME_PLACEHOLDER,
    REGISTRATION_KEYS,
    HostState,
    InstallerError,
    Step,
    doctor,
    execute,
    judge_child_cannot_read,
    judge_code_owner,
    judge_directory,
    judge_export_dir,
    judge_port,
    judge_registration,
    judge_service_user,
    plan_child,
    plan_install,
    plan_start,
    plan_stop,
    plan_system,
    registration_json,
    render_plist,
    render_shops,
    shops,
    wait_for_401,
)
from tests.unit.test_no_real_ids_in_repo import REPO, SYNTHETIC_IDS, _is_suspect, _offenders_in_text

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")
CONN = uuid.UUID("00000000-0000-4000-8000-000000000002")
BEARER = "0123456789abcdef" * 2
KEY = "sk-test-key-never-printed"
CJK = re.compile(r"[一-鿿]")
REAL_ASSETS = Path(installer.__file__).parent / "assets"

VALID_CONFIG = f"""
organization_id = "{ORG}"
connection_id = "{CONN}"
sfw_bearer = "{BEARER}"

[lingxing]
url = "http://lx.invalid/mcp"
key = "{KEY}"

[[stores]]
profile_id = "1000000000000001"
sid = "2000000000000001"
marketplace = "US"
currency = "USD"
nickname = "美国店"

[[stores]]
profile_id = "1000000000000002"
sid = "2000000000000002"
marketplace = "JP"
currency = "JPY"
nickname = "日本店"

[thresholds.min_spend]
USD = "20.00"
JPY = "3000"
"""

SHOP_ROWS: list[object] = [
    {"profile_id": "1000000000000001", "sid": "2000000000000001", "country": "us"},
    {"profile_id": 1000000000000002, "sid": 2000000000000002, "country": "JP"},
    {"profile_id": "1000000000000001", "sid": "2000000000000007", "country": "XX"},
    {"profile_id": "", "sid": "2000000000000009", "country": "DE"},  # 缺 profile_id → 跳过
    "not a row",
]


class FakeClient:
    calls: list[tuple[str, str, str, Mapping[str, object]]] = []
    rows: Sequence[object] = SHOP_ROWS
    error: Exception | None = None

    def __init__(self, url: str, key: str) -> None:
        self.url, self.key = url, key

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        FakeClient.calls.append((self.url, self.key, tool_id, params))
        if FakeClient.error is not None:
            raise FakeClient.error
        return {"rows": list(FakeClient.rows), "total": len(FakeClient.rows)}


@pytest.fixture(autouse=True)
def _reset_fake_client() -> None:
    FakeClient.calls = []
    FakeClient.rows = SHOP_ROWS
    FakeClient.error = None


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """随包资源换成临时目录：AGENTS.md 与 fd.md 由 WP-2 提供，这个 worktree 里可能还没有。"""
    directory = tmp_path / "assets"
    shutil.copytree(REAL_ASSETS, directory)
    (directory / "AGENTS.md").write_text("测试桩：给模型的纪律\n", encoding="utf-8")
    (directory / "fd.md").write_text("测试桩：那一句话\n", encoding="utf-8")
    monkeypatch.setattr(installer, "ASSETS", directory)
    return directory


@pytest.fixture
def root_owned_code(monkeypatch: pytest.MonkeyPatch) -> Path:
    """「代码属 root」要 stat 真实的 venv/bin/python；开发机上没有，拿 /bin/sh 顶——
    它在 macOS 与 Linux 上都属 root、0755，正是那一项要看到的样子。"""
    monkeypatch.setattr(installer, "VENV_PYTHON", Path("/bin/sh"))
    return Path("/bin/sh")


@pytest.fixture
def as_myself(monkeypatch: pytest.MonkeyPatch) -> None:
    """真执行时把所有属主都映射成本用户：非 root 只能 chown 给自己。"""
    monkeypatch.setattr(installer, "_ids", lambda owner: (os.getuid(), os.getgid()))


@pytest.fixture
def private_config(tmp_path: Path) -> Path:
    """0600 的合法配置；导出目录指向临时目录（真目录在 /Users/Shared，测试不碰）。"""
    export_dir = tmp_path / "导出"
    export_dir.mkdir()
    path = tmp_path / "config.toml"
    paths = f'export_dir = "{export_dir}"\nrun_log_path = "{tmp_path / "运行记录.csv"}"\n'
    path.write_text(paths + VALID_CONFIG, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _plan(assets: Path, tmp_path: Path, **overrides: object) -> tuple[Step, ...]:
    kwargs: dict[str, object] = {
        "wheel": tmp_path / "dist" / "ads_control_plane-0.1.0-py3-none-any.whl",
        "child_user": "kid",
        "child_home": tmp_path / "home" / "kid",
        "uv": tmp_path / "bin" / "uv",
        "sfw_bearer": BEARER,
        "organization_id": ORG,
        "connection_id": CONN,
    }
    kwargs.update(overrides)
    return plan_install(**kwargs)  # type: ignore[arg-type]


def _child_home_steps(assets: Path, tmp_path: Path) -> tuple[Step, ...]:
    """计划里落在孩子家目录（tmp_path/home/kid）的那几步：三个 mkdir、两个 write、一个 symlink。"""
    home = tmp_path / "home" / "kid"
    return tuple(
        s for s in _plan(assets, tmp_path) if s.path is not None and s.path.is_relative_to(home)
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------------ 登记 JSON


def test_registration_json_obeys_the_host_contract() -> None:
    payload = registration_json(BEARER)
    assert set(payload) <= REGISTRATION_KEYS
    assert set(payload) == REGISTRATION_KEYS
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(payload["name"]))
    assert payload["url"] == "http://127.0.0.1:8790/mcp"
    timeout = payload["tool_timeout_sec"]
    assert type(timeout) is int and 1 <= timeout <= 3600
    assert payload["auth"] == "bearer" and payload["secret"] == BEARER
    for forbidden in ("env", "cwd", "command", "args"):
        assert forbidden not in payload
    assert judge_registration(payload)[1] is True
    assert registration_json(BEARER, port=8791)["url"] == "http://127.0.0.1:8791/mcp"


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        ({"env": {}}, "不认的键"),
        ({"tool_timeout_sec": 3601}, "tool_timeout_sec"),
        ({"tool_timeout_sec": True}, "tool_timeout_sec"),
        ({"tool_timeout_sec": "3600"}, "tool_timeout_sec"),
        ({"name": "ads pack"}, "name"),
        ({"url": "http://127.0.0.1:8790/mcp#x"}, "片段"),
        ({"url": "http://u:p@127.0.0.1:8790/mcp"}, "用户名"),
        ({"url": "http://example.invalid/mcp"}, "https"),
        ({"url": "http://127.0.0.1:notaport/mcp"}, "解析"),
        ({"auth": "basic"}, "auth"),
        ({"secret": "a\nb"}, "secret"),
    ],
)
def test_registration_self_check_names_each_violation_in_chinese(
    mutation: Mapping[str, object], fragment: str
) -> None:
    payload: dict[str, object] = {**registration_json(BEARER), **mutation}
    name, passed, detail = judge_registration(payload)
    assert (name, passed) == ("登记 JSON", False)
    assert fragment in detail and CJK.search(detail)


# ------------------------------------------------------------------ plist


def _rendered(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "venv_python": DEFAULT_ROOT / "venv" / "bin" / "python",
        "config_path": DEFAULT_ROOT / "config.toml",
        "port": 8790,
        "log_path": installer.LOG_PATH,
    }
    kwargs.update(overrides)
    text = render_plist(**kwargs)  # type: ignore[arg-type]
    document = plistlib.loads(text.encode("utf-8"))
    assert isinstance(document, dict)
    return document


def test_plist_runs_as_the_service_user_with_absolute_paths() -> None:
    plist = _rendered()
    assert plist["Label"] == "local.amazon-ads"
    assert plist["UserName"] == "_amazonads"
    assert "EnvironmentVariables" not in plist
    argv = plist["ProgramArguments"]
    assert isinstance(argv, list) and Path(argv[0]).is_absolute()
    assert argv == [
        "/Library/Application Support/amazon-ads/venv/bin/python",
        "-m",
        "ads_control_plane.sfw",
        "serve",
        "--config",
        "/Library/Application Support/amazon-ads/config.toml",
        "--port",
        "8790",
    ]
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["WorkingDirectory"] == "/Library/Application Support/amazon-ads"
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert plist["StandardOutPath"] == "/Library/Application Support/amazon-ads/logs/amazon-ads.log"


def test_plist_escapes_xml_and_refuses_relative_paths() -> None:
    plist = _rendered(venv_python=Path("/opt/a&b/<venv>/bin/python"))
    argv = plist["ProgramArguments"]
    assert isinstance(argv, list) and argv[0] == "/opt/a&b/<venv>/bin/python"
    with pytest.raises(InstallerError, match="绝对路径"):
        render_plist(
            venv_python=Path("venv/bin/python"),
            config_path=DEFAULT_ROOT / "config.toml",
            port=8790,
            log_path=installer.LOG_PATH,
        )


# ------------------------------------------------------------------ config 示例


def test_config_template_has_only_placeholders() -> None:
    text = (REAL_ASSETS / "config.example.toml").read_text(encoding="utf-8")
    assert text == render_config_template(
        sfw_bearer="<安装时自动生成的口令，不要手改>", organization_id=ORG, connection_id=CONN
    ), "示例文件必须与 render_config_template 的输出一致，否则两份文档会各说各话"
    assert _offenders_in_text(REPO / "config.example.toml", text) == []
    # 带引号的长数字串只能是合成 ID（UUID 里的数字段带连字符，不算）。
    numbers = set(re.findall(r'"(\d{9,})"', text))
    assert numbers and not any(_is_suspect(n) for n in numbers)
    assert numbers <= SYNTHETIC_IDS
    assert isinstance(tomllib.loads(text), dict)
    with pytest.raises(ConfigError) as caught:  # 占位口令不是十六进制：照抄示例跑不起来
        parse_config(text)
    assert caught.value.code == "SFW_BEARER_INVALID"


# ------------------------------------------------------------------ 安装计划


def _by_path(steps: Sequence[Step], path: Path) -> list[Step]:
    return [s for s in steps if s.path == path]


def test_install_plan_covers_the_eight_admin_steps(assets: Path, tmp_path: Path) -> None:
    home = tmp_path / "home" / "kid"
    steps = _plan(assets, tmp_path, host=HostState(taken_ids=frozenset({200, 201, 202})))
    argvs = [s.argv for s in steps if s.kind == "run" and s.argv is not None]

    # ① _amazonads：dscl 序列，uid 取 200..400 里第一个空号，无 shell、家目录 /var/empty、隐藏。
    dscl = [a for a in argvs if a[0] == "/usr/bin/dscl"]
    assert ("/usr/bin/dscl", ".", "-create", "/Users/_amazonads", "UniqueID", "203") in dscl
    assert ("/usr/bin/dscl", ".", "-create", "/Users/_amazonads", "PrimaryGroupID", "203") in dscl
    assert ("/usr/bin/dscl", ".", "-create", "/Groups/_amazonads", "PrimaryGroupID", "203") in dscl
    assert (
        "/usr/bin/dscl",
        ".",
        "-create",
        "/Users/_amazonads",
        "UserShell",
        "/usr/bin/false",
    ) in dscl
    assert (
        "/usr/bin/dscl",
        ".",
        "-create",
        "/Users/_amazonads",
        "NFSHomeDirectory",
        "/var/empty",
    ) in dscl
    assert ("/usr/bin/dscl", ".", "-create", "/Users/_amazonads", "IsHidden", "1") in dscl

    # ② 目录、托管 Python、venv、wheel；代码留给 root，没有 chown -R。
    uv = str(tmp_path / "bin" / "uv")
    python_dir = str(DEFAULT_ROOT / "python")
    assert (uv, "--no-config", "python", "install", "--install-dir", python_dir, "3.12") in argvs
    venv_cmd = next(a for a in argvs if a[0] == "/usr/bin/env")
    assert venv_cmd[1] == f"UV_PYTHON_INSTALL_DIR={python_dir}"
    assert venv_cmd[2:] == (
        uv,
        "--no-config",
        "venv",
        "--managed-python",
        "--python",
        "3.12",
        str(DEFAULT_ROOT / "venv"),
    )
    wheel = str(tmp_path / "dist" / "ads_control_plane-0.1.0-py3-none-any.whl")
    assert (
        uv,
        "--no-config",
        "pip",
        "install",
        "--python",
        str(DEFAULT_ROOT / "venv/bin/python"),
        wheel,
    ) in argvs
    # 代码不交给服务用户：没有 chown 步，根目录与 python/ 留 root:wheel；_amazonads 名下只有
    # config.toml、logs/ 与产物目录。被攻破的 _amazonads 进程于是改不了自己下次启动要跑的代码。
    assert not [s for s in steps if s.kind == "chown"]
    owners = {"": "root:wheel", "python": "root:wheel", "logs": "_amazonads:_amazonads"}
    for sub, owner in owners.items():
        [made] = [s for s in _by_path(steps, DEFAULT_ROOT / sub) if s.kind == "mkdir"]
        assert (made.owner, made.mode) == (owner, 0o755), sub
    handed_over = {s.path for s in steps if s.owner and s.owner.startswith(SERVICE_USER)}
    assert handed_over == {
        DEFAULT_ROOT / "config.toml",
        DEFAULT_ROOT / "logs",
        DEFAULT_EXPORT_DIR,
        DEFAULT_EXPORT_DIR.parent,
    }

    # ③ config 模板：属 _amazonads、0600、内容 = render_config_template。
    [config] = _by_path(steps, DEFAULT_ROOT / "config.toml")
    assert (config.kind, config.owner, config.mode) == ("write", SERVICE_USER, 0o600)
    assert config.content == render_config_template(
        sfw_bearer=BEARER, organization_id=ORG, connection_id=CONN
    )

    # ④ 导出目录属 _amazonads 0755；上一级（运行记录所在）同样。
    for directory in (DEFAULT_EXPORT_DIR, DEFAULT_EXPORT_DIR.parent):
        [made] = _by_path(steps, directory)
        assert (made.kind, made.owner, made.mode) == ("mkdir", "_amazonads:_amazonads", 0o755)

    # ⑤ plist root:wheel 0644，内容能解析且以 _amazonads 跑。
    [plist] = _by_path(steps, Path("/Library/LaunchDaemons/local.amazon-ads.plist"))
    assert (plist.kind, plist.owner, plist.mode) == ("write", "root:wheel", 0o644)
    assert plist.content is not None
    assert plistlib.loads(plist.content.encode())["UserName"] == "_amazonads"

    # ⑥ 孩子家目录三样，属孩子；内容来自随包资源；斜杠命令文件名是 ASCII。
    [agents] = _by_path(steps, home / "否定词" / "AGENTS.md")
    assert (agents.kind, agents.owner, agents.mode) == ("write", "kid", 0o644)
    assert agents.content == "测试桩：给模型的纪律\n"
    [prompt] = _by_path(steps, home / ".codex" / "prompts" / "fd.md")
    assert (prompt.kind, prompt.owner, prompt.content) == ("write", "kid", "测试桩：那一句话\n")
    assert installer.PROMPT_NAME_RE.fullmatch(prompt.path.name if prompt.path else "")
    [link] = _by_path(steps, home / "Desktop" / "否定词导出")
    assert (link.kind, link.content, link.owner) == ("symlink", str(DEFAULT_EXPORT_DIR), "kid")

    # /usr/local/bin/amazon-ads → venv 里的脚本，且必须排在孩子家目录那一组之前：
    # 那一组遇到障碍就会停（iCloud 把 ~/Desktop 做成符号链接是最常见的一种），
    # 而 README 第 4–6 步全要用 amazon-ads。管理员的命令不该被孩子家里的东西挡住。
    [bin_link] = _by_path(steps, Path("/usr/local/bin/amazon-ads"))
    paths = [str(s.path) for s in steps]
    assert paths.index("/usr/local/bin/amazon-ads") < min(
        i for i, p in enumerate(paths) if "/home/kid" in p
    ), "管理员命令要排在孩子家目录之前"
    assert (bin_link.kind, bin_link.content) == (
        "symlink",
        str(DEFAULT_ROOT / "venv/bin/amazon-ads"),
    )

    # 每一步都说得出为什么，且没有 chmod（新装的东西在建时就带权限）。
    assert all(s.why and CJK.search(s.why) for s in steps)
    assert not [s for s in steps if s.kind == "chmod"]


def test_install_command_plan_never_touches_the_child_home_except_three_paths(
    assets: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home" / "kid"
    allowed = {
        home / "否定词" / "AGENTS.md",
        home / ".codex" / "prompts" / "fd.md",
        home / "Desktop" / "否定词导出",
    }
    touched = [
        s for s in _plan(assets, tmp_path) if s.path is not None and s.path.is_relative_to(home)
    ]
    assert touched
    for step in touched:
        assert step.path is not None
        if step.path in allowed:
            continue
        assert step.kind == "mkdir", f"{step.path} 不是那三样，也不是它们的父目录"
        assert any(step.path in target.parents for target in allowed), step.path
        assert step.keep_existing, f"{step.path} 是孩子家里的目录，已存在就不能改属主与权限"
    for step in _plan(assets, tmp_path):
        if step.kind == "run":
            assert not any(str(home) in arg for arg in step.argv or ()), step.argv
    # 家目录之外只有 /usr/local/bin 保留已存在的属主（Intel Mac 上它常归 Homebrew 的管理员）；
    # 服务目录不 keep_existing：/Users/Shared 是 1777，amazon-ads/ 可能被任何账号先建出来，
    # 已存在时属主不对就拒绝（见 test_apply_refuses_a_service_dir_someone_else_built）。
    kept = {s.path for s in _plan(assets, tmp_path) if s.kind == "mkdir" and s.keep_existing}
    assert kept == {
        home / "否定词",
        home / ".codex",
        home / ".codex" / "prompts",
        Path("/usr/local/bin"),
    }


def test_install_plan_keeps_an_existing_config_and_user(assets: Path, tmp_path: Path) -> None:
    steps = _plan(assets, tmp_path, host=HostState(service_uid=333, config_exists=True))
    assert not [s for s in steps if s.kind == "run" and s.argv and s.argv[0] == "/usr/bin/dscl"]
    config_steps = _by_path(steps, DEFAULT_ROOT / "config.toml")
    assert [s.kind for s in config_steps] == ["chown", "chmod"]
    assert [s.path for s in steps if s.kind == "chown"] == [DEFAULT_ROOT / "config.toml"], (
        "重装也只校正 config.toml 的属主，代码树不交给服务用户"
    )
    assert config_steps[0].owner == "_amazonads:_amazonads" and config_steps[1].mode == 0o600
    assert not any(s.kind == "write" and s.content and BEARER in s.content for s in steps)


def test_install_plan_moves_the_run_log_next_to_a_custom_export_dir(
    assets: Path, tmp_path: Path
) -> None:
    export_dir = Path("/Volumes/外置/amazon-ads/导出")
    steps = _plan(assets, tmp_path, export_dir=export_dir)
    [config] = _by_path(steps, DEFAULT_ROOT / "config.toml")
    document = tomllib.loads(config.content or "")
    assert document["export_dir"] == str(export_dir)
    assert document["run_log_path"] == "/Volumes/外置/amazon-ads/运行记录.csv"
    [link] = _by_path(steps, tmp_path / "home" / "kid" / "Desktop" / "否定词导出")
    assert link.content == str(export_dir)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"wheel": Path("dist/x.whl")}, "绝对路径"),
        ({"wheel": Path("/dist/x.tar.gz")}, ".whl"),
        ({"child_user": "kid one"}, "登录名"),
        ({"sfw_bearer": "short"}, "十六进制"),
    ],
)
def test_install_plan_refuses_malformed_inputs(
    assets: Path, tmp_path: Path, overrides: Mapping[str, object], fragment: str
) -> None:
    with pytest.raises(InstallerError, match=fragment):
        _plan(assets, tmp_path, **overrides)


def test_service_id_is_the_first_free_number_in_the_system_range() -> None:
    assert installer.pick_service_id(frozenset()) == 200
    assert installer.pick_service_id(frozenset({200, 201, 250})) == 202
    with pytest.raises(InstallerError, match="没有空号"):
        installer.pick_service_id(frozenset(range(0, 500)))


def test_inspect_host_reads_the_directory_through_an_injected_query(tmp_path: Path) -> None:
    def query(argv: Sequence[str]) -> str:
        if "/Users" in argv:
            return "_www  70\nkid  501\n_amazonads  231\n"
        return "wheel  0\nstaff  20\n_amazonads  231\n"

    host = installer.inspect_host(config_path=tmp_path / "missing.toml", query=query)
    assert host == HostState(
        service_uid=231, taken_ids=frozenset({70, 501, 231, 0, 20}), config_exists=False
    )
    assert installer.parse_dscl_list("junk line\nname\n  x  -1\n") == {"x": -1}


def test_start_and_stop_plans_use_the_system_domain() -> None:
    kickstart = ("/bin/launchctl", "kickstart", "-k", "system/local.amazon-ads")
    assert [s.argv for s in plan_start(loaded=False)] == [
        ("/bin/launchctl", "bootstrap", "system", "/Library/LaunchDaemons/local.amazon-ads.plist"),
        kickstart,
    ]
    assert [s.argv for s in plan_start(loaded=True)] == [kickstart], (
        "登记过的只重启，不再 bootstrap"
    )
    assert [s.argv for s in plan_stop()] == [
        ("/bin/launchctl", "bootout", "system/local.amazon-ads")
    ]
    asked: list[Sequence[str]] = []

    def fake_exit_code(argv: Sequence[str]) -> int:
        asked.append(argv)
        return 0 if len(asked) == 1 else 113

    assert installer.daemon_loaded(run=fake_exit_code) is True
    assert installer.daemon_loaded(run=fake_exit_code) is False
    assert asked == [("/bin/launchctl", "print", "system/local.amazon-ads")] * 2


def test_wait_for_401_polls_until_the_bearer_gate_answers() -> None:
    answers = iter([None, 500, 401])
    slept: list[float] = []
    assert wait_for_401(8790, status=lambda url: next(answers), sleep=slept.append) is True
    assert slept == [1.0, 1.0]
    assert wait_for_401(8790, attempts=2, status=lambda url: 200, sleep=slept.append) is False


# ------------------------------------------------------------------ 执行


def test_execute_dry_run_prints_without_touching_and_real_run_needs_root(
    assets: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home" / "kid"
    steps = _plan(assets, tmp_path)
    execute(steps, dry_run=True)
    out = capsys.readouterr().out
    assert out.count("[干跑]") == len(steps)
    assert BEARER not in out, "干跑打印不能把口令印出来"
    assert "UniqueID 200" in out and "否定词导出" in out
    assert not home.exists()

    with pytest.raises(InstallerError, match="sudo"):
        execute(steps, dry_run=False, geteuid=lambda: 501)
    assert not home.exists() and capsys.readouterr().out == ""


def test_execute_lays_out_the_child_home_and_leaves_an_existing_dotcodex_alone(
    assets: Path, tmp_path: Path, as_myself: None, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home" / "kid"
    (home / "Desktop").mkdir(parents=True)
    (home / ".codex").mkdir(mode=0o700)
    steps = _child_home_steps(assets, tmp_path)
    assert [s.kind for s in steps] == ["mkdir", "write", "mkdir", "mkdir", "write", "symlink"]
    execute(steps, dry_run=False, geteuid=lambda: 0)
    out = capsys.readouterr().out
    agents, prompt, link = (
        home / "否定词" / "AGENTS.md",
        home / ".codex/prompts/fd.md",
        (home / "Desktop" / "否定词导出"),
    )
    assert agents.read_text(encoding="utf-8") == "测试桩：给模型的纪律\n"
    assert prompt.read_text(encoding="utf-8") == "测试桩：那一句话\n"
    assert os.readlink(link) == str(DEFAULT_EXPORT_DIR)
    assert stat.S_IMODE((home / "否定词").stat().st_mode) == 0o755
    assert stat.S_IMODE(agents.stat().st_mode) == 0o644
    assert stat.S_IMODE((home / ".codex").stat().st_mode) == 0o700, "孩子自己的 ~/.codex 不能被放开"
    assert out.count("已存在，不动") == 1
    assert not [p for p in home.rglob("*") if p.name.endswith(".tmp")], "临时文件不能留下"
    # 再跑一遍：目录都在了、链接已指向目标、文件重写成同样内容——不报错、不留东西。
    execute(steps, dry_run=False, geteuid=lambda: 0)
    assert capsys.readouterr().out.count("已存在，不动") == 3
    assert os.readlink(link) == str(DEFAULT_EXPORT_DIR)
    assert sorted(p.name for p in (home / "否定词").iterdir()) == ["AGENTS.md"]


def test_execute_refuses_a_project_dir_the_child_turned_into_a_symlink(
    assets: Path, tmp_path: Path, as_myself: None
) -> None:
    """2026-09-20 复审复现的路径：孩子把 ~/否定词 换成指向自己另一个目录的链接，并在那里
    预置 AGENTS.md.tmp → 受害文件；旧实现 is_dir() 跟着链接早退，再以 root O_TRUNC 打开
    可预测的临时名，就把受害文件截断了。"""
    home = tmp_path / "home" / "kid"
    victim = tmp_path / "victim"
    victim.write_text("原样\n", encoding="utf-8")
    elsewhere = home / "atk"
    elsewhere.mkdir(parents=True)
    (elsewhere / "AGENTS.md.tmp").symlink_to(victim)
    (home / "否定词").symlink_to(elsewhere)
    with pytest.raises(InstallerError, match="符号链接"):
        execute(_child_home_steps(assets, tmp_path), dry_run=False, geteuid=lambda: 0)
    assert victim.read_text(encoding="utf-8") == "原样\n"
    assert sorted(p.name for p in elsewhere.iterdir()) == ["AGENTS.md.tmp"], "链接那头什么都不能多"
    assert (home / "否定词").is_symlink() and not (home / ".codex").exists(), "第一步就停"


def test_execute_replaces_a_planted_symlink_instead_of_writing_through_it(
    assets: Path, tmp_path: Path, as_myself: None
) -> None:
    home = tmp_path / "home" / "kid"
    victim = tmp_path / "victim"
    victim.write_text("原样\n", encoding="utf-8")
    project = home / "否定词"
    project.mkdir(parents=True)
    (project / "AGENTS.md").symlink_to(victim)  # 目标本身是链接：换掉链接，不写穿它
    (project / "AGENTS.md.tmp").symlink_to(victim)  # 旧实现可预测的临时名
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "Desktop").symlink_to(elsewhere)  # 桌面整个换成链接：上级目录是链接就停
    with pytest.raises(InstallerError, match="上级目录"):
        execute(_child_home_steps(assets, tmp_path), dry_run=False, geteuid=lambda: 0)
    assert victim.read_text(encoding="utf-8") == "原样\n"
    agents = project / "AGENTS.md"
    assert not agents.is_symlink()
    assert agents.read_text(encoding="utf-8") == "测试桩：给模型的纪律\n"
    assert (project / "AGENTS.md.tmp").is_symlink(), "预置的临时名没被碰"
    assert list(elsewhere.iterdir()) == [], "桌面链接没有落到链接那头"
    assert (home / ".codex" / "prompts" / "fd.md").is_file(), "停在桌面那一步，前面的都做完了"


def test_apply_refuses_a_service_dir_someone_else_built(
    tmp_path: Path, as_myself: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已存在的目录：服务目录属主对才校正权限，属主不对就拒绝（不收编别人预置的文件）；
    孩子的目录不动；符号链接一律拒绝。"""
    shared = tmp_path / "amazon-ads"
    shared.mkdir(mode=0o700)
    service = Step(kind="mkdir", path=shared, owner="_amazonads:_amazonads", mode=0o755, why="x")
    assert installer._apply(service) == "已存在，属主对，权限按上面校正"
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    planted = shared / "运行记录.csv"
    planted.write_text("x", encoding="utf-8")
    shared.chmod(0o700)
    monkeypatch.setattr(installer, "_ids", lambda owner: (os.getuid() + 1, os.getgid()))
    with pytest.raises(InstallerError, match="不收编别人建的目录"):
        installer._apply(service)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o700, "拒绝就一位都不改"
    assert planted.read_text(encoding="utf-8") == "x"
    monkeypatch.setattr(installer, "_ids", lambda owner: (os.getuid(), os.getgid()))
    dotcodex = tmp_path / ".codex"
    dotcodex.mkdir(mode=0o700)
    child = Step(kind="mkdir", path=dotcodex, owner="kid", mode=0o755, keep_existing=True, why="x")
    assert installer._apply(child) == "已存在，不动"
    assert stat.S_IMODE(dotcodex.stat().st_mode) == 0o700
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InstallerError, match="不是目录"):
        installer._apply(
            Step(kind="mkdir", path=link, owner="_amazonads:_amazonads", mode=0o755, why="x")
        )
    assert not (tmp_path / "elsewhere").exists()
    with pytest.raises(InstallerError, match="绝对路径"):
        installer._apply(Step(kind="mkdir", path=Path("relative/dir"), why="x"))


# ------------------------------------------------------------------ 体检


def test_probe_port_sees_a_listener_but_not_a_closed_connection_in_time_wait() -> None:
    """占用判断：有进程在 LISTEN 就是占用；刚关掉的服务留下的 TIME_WAIT 不是占用。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # uvicorn 也这么设
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.05)
    port = listener.getsockname()[1]
    with socket.create_connection(("127.0.0.1", port)):
        listener.accept()[0].close()  # 服务端先关、没传过数据：TIME_WAIT 留在服务端的端口上
    stop = threading.Event()

    def drain() -> None:  # 探测的 GET 要有人接、立刻挂断，否则要等 http_status 的 5 秒超时
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                listener.accept()[0].close()

    thread = threading.Thread(target=drain)
    thread.start()
    try:
        assert installer.probe_port(port) == "other", "监听中、但不是我们的 401"
    finally:
        stop.set()
        thread.join()
        listener.close()
    assert installer.probe_port(port) == "free", "TIME_WAIT 不算占用"


def _stat(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def test_doctor_checks_are_pure_and_name_each_failure_in_chinese() -> None:
    import stat as st

    failing = [
        judge_service_user(None),
        judge_child_cannot_read(None, 501),
        judge_child_cannot_read(_stat(st.S_IFREG | 0o644, 231), 501),
        judge_child_cannot_read(_stat(st.S_IFREG | 0o600, 501), 501),
        judge_export_dir(None, 231, 501),
        judge_export_dir(_stat(st.S_IFREG | 0o644, 231), 231, 501),
        judge_export_dir(_stat(st.S_IFDIR | 0o755, 0), 231, 501),
        judge_export_dir(_stat(st.S_IFDIR | 0o555, 231), 231, 501),
        judge_export_dir(_stat(st.S_IFDIR | 0o777, 231), 231, 501),
        judge_export_dir(_stat(st.S_IFDIR | 0o755, 501), None, 501),
        judge_export_dir(None, 231, 501, name="运行记录目录"),
        judge_code_owner(None, Path("/x/venv/bin/python")),
        judge_code_owner(_stat(st.S_IFREG | 0o755, 231), Path("/x/venv/bin/python")),
        judge_code_owner(_stat(st.S_IFREG | 0o777, 0), Path("/x/venv/bin/python")),
        judge_registration({**registration_json(BEARER), "env": {}}),
        judge_port("other", 8790, daemon_registered=False),
        judge_directory({"1000000000000002"}, parse_config(VALID_CONFIG).stores),
    ]
    for name, passed, detail in failing:
        assert passed is False, (name, detail)
        assert CJK.search(name) and CJK.search(detail), (name, detail)
    passing = [
        judge_service_user(231),
        judge_child_cannot_read(_stat(st.S_IFREG | 0o600, 231), 501),
        judge_child_cannot_read(_stat(st.S_IFREG | 0o600, 231), None),
        judge_export_dir(_stat(st.S_IFDIR | 0o755, 231), 231, 501),
        judge_code_owner(_stat(st.S_IFREG | 0o755, 0), Path("/x/venv/bin/python")),
        judge_registration(registration_json(BEARER)),
        judge_port("free", 8790, daemon_registered=False),
        judge_port("ours", 8790, daemon_registered=True),
        judge_directory(
            {"1000000000000001", "1000000000000002"}, parse_config(VALID_CONFIG).stores
        ),
    ]
    assert all(passed for _, passed, _ in passing), passing
    assert "日本店" in judge_directory({"1000000000000001"}, parse_config(VALID_CONFIG).stores)[2]
    run_log = judge_export_dir(_stat(st.S_IFDIR | 0o555, 231), 231, 501, name="运行记录目录")
    assert run_log == ("运行记录目录", False, "属主自己没有写权限：服务写不了文件")
    not_roots = judge_code_owner(_stat(st.S_IFREG | 0o755, 231), Path("/x/venv/bin/python"))[2]
    assert "uid 231" in not_roots and "chown -R root:wheel" in not_roots


def test_doctor_on_a_private_temp_config_passes_and_calls_the_directory_once(
    private_config: Path, root_owned_code: Path
) -> None:
    port = _free_port()
    checks = doctor(
        private_config,
        expect_uid=os.getuid(),
        child_uid=None,
        port=port,
        online=True,
        daemon_registered=False,
        client_factory=FakeClient,
    )
    assert [(name, passed) for name, passed, _ in checks] == [
        ("系统用户", True),
        ("配置文件私有", True),
        ("配置内容", True),
        ("孩子读不到密钥", True),
        ("导出目录", True),
        ("运行记录目录", True),
        ("代码属 root", True),
        ("登记 JSON", True),
        (f"端口 {port}", True),
        ("领星名录", True),
    ]
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0][1:] == (KEY, "ad_auth_shops", {})
    assert "美国店、日本店" in checks[2][2] and "20.00 USD" in checks[2][2]
    assert KEY not in "\n".join(detail for _, _, detail in checks)


def test_doctor_reports_a_busy_port_and_a_failed_directory_call(
    private_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeClient.error = LxTransportError("timed out")
    monkeypatch.setattr(installer, "http_status", lambda url, timeout=5.0: 500)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])
        checks = doctor(
            private_config,
            expect_uid=os.getuid(),
            child_uid=None,
            port=port,
            online=True,
            daemon_registered=False,
            client_factory=FakeClient,
        )
        offline = doctor(
            private_config,
            expect_uid=os.getuid(),
            child_uid=None,
            port=port,
            online=False,
            daemon_registered=False,
        )
    verdicts = {name: (passed, detail) for name, passed, detail in checks}
    passed, detail = verdicts[f"端口 {port}"]
    assert passed is False
    assert f"sudo lsof -nP -iTCP:{port} -sTCP:LISTEN" in detail, "要给出查是谁占了的命令"
    assert "local.amazon-ads.plist" in detail, "「换端口」不能是句悬空的话：端口写死在 plist 里"
    assert verdicts["领星名录"] == (False, "取数失败（LX_TRANSPORT_ERROR）：timed out")
    assert [name for name, _, _ in offline][-1] == f"端口 {port}"
    assert len(FakeClient.calls) == 1, "离线体检一次名录都不查"


def test_doctor_fails_when_the_interpreter_it_would_launch_is_missing(
    private_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "VENV_PYTHON", tmp_path / "venv" / "bin" / "python")
    checks = doctor(
        private_config,
        expect_uid=os.getuid(),
        child_uid=None,
        port=_free_port(),
        online=False,
        daemon_registered=False,
    )
    verdicts = {name: (passed, detail) for name, passed, detail in checks}
    passed, detail = verdicts["代码属 root"]
    assert passed is False and "不存在" in detail and str(tmp_path) in detail


def test_doctor_fails_when_the_run_log_directory_cannot_be_written(
    tmp_path: Path, root_owned_code: Path
) -> None:
    """2026-09-20 复审：运行记录所在目录写不了时，工具整段失败、CSV 却已落盘；
    体检不能对此全绿。判定与导出目录同一套。"""
    export_dir = tmp_path / "导出"
    export_dir.mkdir()
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    path = tmp_path / "config.toml"
    paths = f'export_dir = "{export_dir}"\nrun_log_path = "{locked / "运行记录.csv"}"\n'
    path.write_text(paths + VALID_CONFIG, encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        checks = doctor(
            path,
            expect_uid=os.getuid(),
            child_uid=None,
            port=_free_port(),
            online=False,
            daemon_registered=False,
        )
    finally:
        os.chmod(locked, 0o755)
    names = [name for name, _, _ in checks]
    assert names.index("运行记录目录") == names.index("导出目录") + 1
    verdicts = {name: (passed, detail) for name, passed, detail in checks}
    assert verdicts["导出目录"][0] is True
    assert verdicts["运行记录目录"] == (False, "属主自己没有写权限：服务写不了文件")


def test_doctor_on_an_open_config_fails_the_file_checks_and_never_calls_lingxing(
    private_config: Path,
) -> None:
    os.chmod(private_config, 0o644)
    checks = doctor(
        private_config,
        expect_uid=os.getuid(),
        child_uid=None,
        port=_free_port(),
        online=True,
        daemon_registered=False,
        client_factory=FakeClient,
    )
    verdicts = {name: (passed, detail) for name, passed, detail in checks}
    assert verdicts["配置文件私有"][0] is False and "0644" in verdicts["配置文件私有"][1]
    assert verdicts["配置内容"][0] is False
    assert verdicts["孩子读不到密钥"][0] is False
    assert verdicts["领星名录"][0] is False and "CONFIG_TOO_OPEN" in verdicts["领星名录"][1]
    assert FakeClient.calls == [], "密钥文件不私有时，一次网络调用都不发"


# ------------------------------------------------------------------ shops


def test_shops_renders_a_pasteable_store_table_without_the_key(private_config: Path) -> None:
    text = shops(private_config, expect_uid=os.getuid(), client_factory=FakeClient)
    assert FakeClient.calls == [("http://lx.invalid/mcp", KEY, "ad_auth_shops", {})]
    assert KEY not in text
    assert "已授权店铺 3 家，另有 2 行缺 profile_id/sid/country、已跳过" in text
    blocks = text.split("[[stores]]")[1:]
    assert len(blocks) == 3
    us_block = "\n".join(
        (
            "[[stores]]",
            'profile_id = "1000000000000001"',
            'sid = "2000000000000001"',
            'marketplace = "US"',
            'currency = "USD"',
            'nickname = "<给它起个名字>"',
        )
    )
    assert us_block in text
    assert 'marketplace = "JP"\ncurrency = "JPY"' in text
    assert 'marketplace = "XX"\ncurrency = ""  # 站点 XX 不在建议表里' in text
    assert text.count('nickname = "<给它起个名字>"') == 3
    document = tomllib.loads(text[text.index("[[stores]]") :])  # 段落原样可粘
    assert [s["profile_id"] for s in document["stores"]] == [
        "1000000000000001",
        "1000000000000002",
        "1000000000000001",
    ]
    assert render_shops([]) == "领星没有返回可用的已授权店铺（收到 0 行，跳过 0 行）。"
    assert render_shops([{"profile_id": 'a"b', "sid": "1", "country": "US"}]).startswith(
        "领星没有返回可用"
    )


def test_shops_names_each_store_after_its_lingxing_alias() -> None:
    """74 家店逐个起名没人做得下来（2026-09-23 真事）。店名拿来即用，只做机械替换。"""
    rows = [
        {"profile_id": "1", "sid": 11, "country": "US", "alias": "美国一店"},
        {"profile_id": "2", "sid": 12, "country": "UK", "alias": "Brand Store (UK)"},
        {"profile_id": "3", "sid": 13, "country": "DE", "alias": "Brand Store [UK]"},
        {
            "profile_id": "4",
            "sid": 14,
            "country": "FR",
            "alias": "一个非常非常非常非常非常非常长的店铺名字超过二十个字",
        },
        {"profile_id": "5", "sid": 15, "country": "IT", "alias": "!!!"},
        {"profile_id": "6", "sid": 16, "country": "ES"},
    ]
    text = render_shops(rows)
    stores = tomllib.loads(text[text.index("[[stores]]") :])["stores"]
    names = [s["nickname"] for s in stores]
    assert names[:4] == [
        "美国一店",
        "Brand_Store_UK",
        "Brand_Store_UK-2",
        "一个非常非常非常非常非常非常长的店铺名字",
    ]
    assert names[4:] == [NICKNAME_PLACEHOLDER, NICKNAME_PLACEHOLDER]  # 做不出来就不猜
    assert "其中 2 家领星没给店名" in text
    # 由店名得来的昵称必须一次通过配置校验，否则等于没做。
    for name in names[:4]:
        assert NICKNAME_RE.fullmatch(name), name
    assert [s["sid"] for s in stores] == [
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
    ]  # sid 是 int 也照样可粘


def test_shops_refuses_an_open_config_before_any_network_call(private_config: Path) -> None:
    os.chmod(private_config, 0o644)
    with pytest.raises(ConfigError) as caught:
        shops(private_config, expect_uid=os.getuid(), client_factory=FakeClient)
    assert caught.value.code == "CONFIG_TOO_OPEN" and FakeClient.calls == []


# ------------------------------------------------------------------ 命令行


def test_serve_import_is_deferred_and_forwards_the_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    top_level = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "ads_control_plane.sfw.server" not in top_level, (
        "server 只能在 serve 子命令里延迟 import"
    )

    calls: list[tuple[Path, int, bool, int | None]] = []
    stub = types.ModuleType("ads_control_plane.sfw.server")

    def serve(config_path: Path, *, port: int, no_auth: bool, expect_uid: int | None) -> None:
        calls.append((config_path, port, no_auth, expect_uid))

    stub.serve = serve  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ads_control_plane.sfw.server", stub)
    argv = ["serve", "--config", "/x/config.toml", "--port", "9", "--no-auth", "--expect-uid", "7"]
    assert cli.main(argv) == 0
    assert calls == [(Path("/x/config.toml"), 9, True, 7)]
    assert cli.main(["serve"]) == 0
    assert calls[-1] == (
        Path("/Library/Application Support/amazon-ads/config.toml"),
        8790,
        False,
        os.geteuid(),
    )


def test_install_dry_run_prints_the_plan_and_a_placeholder_secret(
    assets: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wheel = tmp_path / "ads_control_plane-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(
        installer,
        "inspect_host",
        lambda *, config_path, query=None: HostState(taken_ids=frozenset(range(200, 205))),
    )
    argv = [
        "install",
        "--wheel",
        str(wheel),
        "--child-user",
        "kid",
        "--child-home",
        str(home),
        "--uv",
        "/opt/uv/bin/uv",
    ]
    assert cli.main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "不存在，将建（uid 取 205）" in out and "将写模板" in out
    assert "[干跑]" in out and "UniqueID 205" in out
    # 逐格打印，不是一段可粘贴的 JSON：SFW 没有吃 HTTP 形状 JSON 的输入框
    # （2026-09-22 在 1.1.0 上实测）。
    assert "{" not in out
    expected = registration_json("<安装后用 sudo amazon-ads print-registration 查看>")
    assert f"名称      {expected['name']}" in out
    assert f"服务地址   {expected['url']}" in out
    assert "认证方式   Bearer Token" in out
    assert f"认证凭据   {expected['secret']}" in out
    assert list(home.iterdir()) == []

    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert cli.main(argv) == 1
    assert "sudo" in capsys.readouterr().err
    assert list(home.iterdir()) == []


def test_print_registration_reads_the_bearer_from_the_config(
    private_config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(installer, "service_uid", os.getuid)
    assert cli.main(["print-registration", "--config", str(private_config)]) == 0
    out = capsys.readouterr().out
    # 同上：管理员抄的是这四行，不是一段 JSON。
    assert "{" not in out
    expected = registration_json(BEARER)
    assert f"名称      {expected['name']}" in out
    assert f"服务地址   {expected['url']}" in out
    assert "认证方式   Bearer Token" in out
    assert f"认证凭据   {expected['secret']}" in out
    os.chmod(private_config, 0o640)
    assert cli.main(["print-registration", "--config", str(private_config)]) == 1
    assert "0640" in capsys.readouterr().err


def test_shops_and_doctor_commands_use_the_service_uid_and_exit_codes(
    private_config: Path,
    root_owned_code: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(installer, "service_uid", os.getuid)
    monkeypatch.setattr(installer, "LxMcpReadClient", FakeClient)
    # doctor 会问 launchctl daemon 登记没有；不换掉就真去跑 /bin/launchctl——CI 的 Linux
    # 上没有这个程序，2026-09-23 就是这样红的。
    monkeypatch.setattr(installer, "daemon_loaded", lambda: False)
    assert cli.main(["shops", "--config", str(private_config)]) == 0
    out = capsys.readouterr().out
    assert "[[stores]]" in out and KEY not in out

    port = _free_port()
    argv = ["doctor", "--config", str(private_config), "--port", str(port), "--offline"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "[失败]" not in out and "9/9 项通过" in out
    assert len(FakeClient.calls) == 1, "doctor --offline 不查名录"

    os.chmod(private_config, 0o644)
    assert cli.main(argv) == 1
    out = capsys.readouterr().out
    assert "[失败] 配置文件私有" in out and "先修好再 start" in out


def test_a_registered_daemon_with_nobody_listening_is_a_failure_not_a_green_check() -> None:
    """服务死了的时候，doctor 必须说出来——否则管理员拿到一份全绿报告，下一步无处可去。

    2026-09-20 真事：助手说「工具没连上」，README 让管理员跑 doctor；服务已经死了，
    8790 空着，端口那项按「空闲，可以 start」判通过，十项全绿、退出码 0。
    「空闲」对 start 之前的体检是对的，对 start 之后就是假绿——差别只在 daemon 登记了没有。
    """
    name, passed, detail = judge_port("free", 8790, daemon_registered=True)
    assert passed is False
    assert "没起来" in detail and "amazon-ads start" in detail
    assert str(LOG_PATH) in detail, "报了故障就得给出下一步看哪里"
    assert judge_port("free", 8790, daemon_registered=False)[1] is True, (
        "还没 start 的时候，端口空闲就该是通过——这条路不能被上面那条压掉"
    )


def test_the_package_path_skips_exactly_the_three_uv_commands_and_nothing_else() -> None:
    """.pkg 已经铺好 python/ 与 venv/，②那三条 uv 命令就不该再跑——但只有那三条。

    钉这条是因为「少跑几步」最容易顺手少建一个目录或少改一次属主，而那种缺口
    要等到 doctor 或运行期才暴露。这里逐步比对：两条路径的差集必须恰好是三个 run。
    """
    common = {
        "sfw_bearer": "0" * 32,
        "organization_id": uuid.UUID(int=1),
        "connection_id": uuid.UUID(int=2),
    }
    from_repo = plan_system(wheel=Path("/tmp/w.whl"), uv=Path("/opt/uv/bin/uv"), **common)
    from_pkg = plan_system(wheel=None, uv=None, **common)
    skipped = [s for s in from_repo if s not in from_pkg]
    assert [s.kind for s in skipped] == ["run", "run", "run"]
    assert tuple(s for s in from_repo if s.kind != "run" or s in from_pkg) == from_pkg


def test_wheel_and_uv_must_be_given_together_or_not_at_all() -> None:
    """给一个不给另一个会装出一个「有 venv 目录、没 venv 内容」的半成品，直接拒绝。"""
    common = {
        "sfw_bearer": "0" * 32,
        "organization_id": uuid.UUID(int=1),
        "connection_id": uuid.UUID(int=2),
    }
    for wheel, uv in ((Path("/tmp/w.whl"), None), (None, Path("/opt/uv/bin/uv"))):
        with pytest.raises(InstallerError, match="要么都给"):
            plan_system(wheel=wheel, uv=uv, **common)


def test_install_is_exactly_system_then_child() -> None:
    """一把装 = 两半相接。两条路径共用同一份步骤定义，不许各写一份。"""
    common = {
        "sfw_bearer": "0" * 32,
        "organization_id": uuid.UUID(int=1),
        "connection_id": uuid.UUID(int=2),
    }
    wheel, uv = Path("/tmp/w.whl"), Path("/opt/uv/bin/uv")
    assert plan_install(
        wheel=wheel, uv=uv, child_user="kid", child_home=Path("/Users/kid"), **common
    ) == plan_system(wheel=wheel, uv=uv, **common) + plan_child(
        child_user="kid", child_home=Path("/Users/kid")
    )


def test_child_half_only_touches_that_child_home() -> None:
    """孩子那半不许碰家目录以外的任何路径——它会以孩子的属主写文件。"""
    home = Path("/Users/kid")
    for step in plan_child(child_user="kid", child_home=home):
        assert step.path is not None
        assert home in step.path.parents or step.path == home, step
