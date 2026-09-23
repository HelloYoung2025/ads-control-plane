"""插件形态：入口行为，以及仓库里那三份清单彼此对得上。

清单那几条不是形式校验——它们钉的是「装上去之后会不会跑错版本、会不会指向一个
不存在的文件」。这类错在本机永远看不出来，只有别人装的时候才炸。
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path

from ads_control_plane.sfw import plugin
from ads_control_plane.sfw.config import (
    BEARER_NOTE_SYSTEM,
    REPO_URL,
    SHOPS_CMD_SYSTEM,
    USER_CONFIG_PATH,
    USER_EXPORT_DIR,
    parse_config,
)
from ads_control_plane.sfw.service import run_all, summarize
from tests.unit import test_sfw_pack as pack

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "amazon-ads"
MANIFEST = json.loads((PLUGIN_DIR / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
MCP = json.loads((PLUGIN_DIR / ".mcp.json").read_text(encoding="utf-8"))
MARKET = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
README = (REPO / "README.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 入口


def test_the_config_lives_in_the_users_own_home_not_library() -> None:
    assert USER_CONFIG_PATH.name == "config.toml"
    assert "/Library/" not in str(USER_CONFIG_PATH)


def test_first_run_writes_a_0600_template_with_the_users_own_paths(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    assert plugin.ensure_config(path) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    # 自助版说明：插件是自己装给自己用的，模板开头不能叫人去 sudo。
    assert "sudo" not in text
    assert str(USER_EXPORT_DIR) in text
    assert "/Library/Application Support/amazon-ads" not in text
    # 模板里每一句都得是这个形态下照着能做完的。系统形态那两句在插件形态下是死路：
    # 没有装 amazon-ads 这条命令，也没有「添加服务器」那张表单。
    assert SHOPS_CMD_SYSTEM not in text
    assert BEARER_NOTE_SYSTEM not in text
    assert tomllib.loads(text)["lingxing"] == {"url": "", "key": ""}


def test_second_run_does_not_touch_an_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# 人改过的", encoding="utf-8")
    os.chmod(path, 0o600)
    assert plugin.ensure_config(path) is False
    assert path.read_text(encoding="utf-8") == "# 人改过的"


def test_unwritable_location_does_not_raise(tmp_path: Path) -> None:
    """写不了模板也要让进程起来：报「配置文件不存在：<路径>」比连不上有用。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    assert plugin.ensure_config(blocker / "config.toml") is False


# ------------------------------------------------------------------ 清单


def test_the_three_manifests_agree_on_the_plugin_name() -> None:
    entry = next(p for p in MARKET["plugins"] if p["name"] == MANIFEST["name"])
    assert MANIFEST["name"] == PLUGIN_DIR.name
    assert entry["source"]["path"] == f"./plugins/{PLUGIN_DIR.name}"


def test_manifest_paths_point_at_files_that_exist() -> None:
    for field in ("mcpServers", "skills"):
        assert (PLUGIN_DIR / MANIFEST[field].removeprefix("./")).exists(), field


def test_the_installed_version_is_the_version_the_launcher_pulls() -> None:
    """插件版本、包版本、uvx 拉的那个 tag 必须是同一个。

    三者不一致时，SFW 里显示的是 A 版、实际跑起来的是 B 版，而界面上看不出任何异常——
    只有行为对不上，而那时候没人会怀疑是版本。
    """
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = pyproject["project"]["version"]
    script = MCP["mcpServers"]["amazon-ads"]["args"][1]
    ref = re.search(r"ads-control-plane@v([0-9][^\"]*)\"", script)
    assert ref is not None, "启动脚本里必须钉死一个 @v<版本> 的 tag，不能跟着分支跑"
    assert ref.group(1) == package_version == MANIFEST["version"]


def test_the_launcher_does_not_assume_uv_is_on_path() -> None:
    """SFW 是图形程序，PATH 里通常没有 ~/.local/bin；直接写 uvx 会「命令找不到」。"""
    server = MCP["mcpServers"]["amazon-ads"]
    assert server["command"] == "/bin/sh"
    script = server["args"][1]
    assert "$HOME/.local/bin/uvx" in script
    assert "/opt/homebrew/bin/uvx" in script
    # 找不到 uv 时要留下一句人能照着做的话，不能静悄悄地退出。
    assert "astral.sh/uv/install.sh" in script


def test_tool_timeout_is_declared_because_the_hub_form_has_no_such_field() -> None:
    server = MCP["mcpServers"]["amazon-ads"]
    assert server["tool_timeout_sec"] >= 3600
    # 2026-09-22 实测：依赖缓存是冷的时候，第一次从 git 装要 159 秒。180 秒只差 21 秒就
    # 判失败，而判失败的后果是人以为「这东西不能用」。留足余量，反正找不到 uv 是秒退。
    assert server["startup_timeout_sec"] >= 600


def test_skill_frontmatter_says_when_to_use_it() -> None:
    """技能靠 description 被路由：插件带不了斜杠命令，说不清就永远不会被选中。"""
    text = (PLUGIN_DIR / "skills" / "wasted-search-terms" / "SKILL.md").read_text(encoding="utf-8")
    head = text.split("---")[1]
    assert re.search(r"^name:\s*wasted-search-terms$", head, re.M)
    assert re.search(r"^description:\s*\S", head, re.M)
    assert "否定词" in head and "find_wasted_search_terms" in head


def test_the_readme_installs_the_same_version_from_the_same_marketplace() -> None:
    """README 里那几行命令是别人唯一会照着敲的东西，得跟清单钉在一起。

    2026-09-22 真事：界面上的「安装」按钮对自己加的市场源报
    `plugin/install requires exactly one of marketplacePath or remoteMarketplaceName`，
    装不上，于是 README 改成给命令。命令一旦和版本号/市场名脱钩，别人装上的就是另一个
    版本——而装的过程一切正常，只有行为对不上。
    """
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    add = f"plugin marketplace add HelloYoung2025/ads-control-plane@v{version}"
    assert add in README
    # 同一块命令兼做升级：同名市场换版本号直接 add 会报 already added from a different
    # source（2026-09-23 实测），所以 remove 必须在 add 前面。
    remove = f"plugin marketplace remove {MARKET['name']} 2>/dev/null"
    assert remove in README and README.index(remove) < README.index(add)
    assert f"plugin add {MANIFEST['name']}@{MARKET['name']}" in README
    # 预热那行拉的必须是同一个 tag，否则预热的是 A 版、跑起来的是 B 版。
    assert f"ads-control-plane@v{version} amazon-ads --help" in README


def test_the_readme_does_not_tell_people_to_click_a_button_that_fails() -> None:
    """点击步骤实测装不上（见上一条）。README 里不许再出现「点安装」那套指引。"""
    steps = README.split("### 为什么不是在界面里点")[0]
    assert "点「安装」" not in steps
    assert "点它，点" not in steps


def test_plugin_answers_never_send_people_to_fd_or_an_admin(tmp_path: Path) -> None:
    """插件带不了斜杠命令，自己装的人也没有管理员：回答里出现这两样，照着做就是死路。

    最常走的那条路（有 CSV 的回答最后一句）此前写的是「把 CSV 交给管理员」。
    """
    (tmp_path / "空").mkdir()
    (tmp_path / "满").mkdir()
    empty_runs, _, _ = pack._every_empty_outcome(tmp_path / "空")
    empty = summarize(
        empty_runs,
        parse_config(pack.config_text(tmp_path / "空", pack.EMPTY_STORES)),
        plugin.PLUGIN_WORDING,
    )
    full_cfg = parse_config(pack.config_text(tmp_path / "满"))
    full = summarize(
        run_all(full_cfg, pack._seeded_mock(), now=pack.NOW), full_cfg, plugin.PLUGIN_WORDING
    )
    for text in (empty, full):
        assert "/fd" not in text and "/new" not in text and "管理员" not in text
    assert "开一个新对话再问一次" in empty  # 取数失败那家店：怎么重试
    assert REPO_URL in empty  # 重试解决不了时：去哪儿查
    assert full.endswith(plugin.PLUGIN_WORDING.hand_over)
