"""包内依赖方向守卫（评审 pe ENG-02 / handoff §12.3 的现存子集）。

策略、Provider、适配器三层只认 canonical 与 strategies 的端口，对组件壳
（`ads_control_plane.sfw`、MCP 服务端、uvicorn、Web 框架）零 import。
壳是唯一的组合根：它 import 这三层，这三层永远不 import 它——反过来的话，
壳里的凭据、端口、进程模型就会顺着 import 渗进只读的策略代码。
"""

from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[2] / "src" / "ads_control_plane"


def _violations(root: Path, forbidden: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for name in forbidden:
                if f" {name}" in f" {stripped}".replace("import ", " ").replace(
                    "from ", " "
                ) or stripped.startswith((f"import {name}", f"from {name}")):
                    found.append(f"{path.relative_to(root.parent)}:{line_no}: {stripped}")
    return found


#: 领星 provider 的依赖方向：只认 canonical 与 strategies 的端口，不认组件壳。
#: 它自己声明结构化 LxReadPort，具体绑定由组合根决定（同 adapters/lx_read.py 的约定）。
PROVIDER_SRC = CORE_SRC / "providers" / "lingxing"
FORBIDDEN_IN_PROVIDER = (
    "fastapi",
    "starlette",
    "mcp.server",
    "uvicorn",
    "ads_control_plane.sfw",
)


def test_lingxing_provider_stays_below_the_orchestration_layer() -> None:
    assert _violations(PROVIDER_SRC, FORBIDDEN_IN_PROVIDER) == []


def test_strategies_never_import_a_provider() -> None:
    """策略只认端口，不认任何具体 Provider（ADR-003 端口先行）。"""
    assert _violations(CORE_SRC / "strategies", ("ads_control_plane.providers",)) == []


#: 组件壳的三个标志：包本身、MCP 服务端、HTTP 服务器。适配器可以用 mcp 的**客户端**
#: （adapters/lx_read.py 读领星就是经 MCP 客户端），所以禁的是 mcp.server 而不是 mcp。
_PACK_SHELL = ("ads_control_plane.sfw", "mcp.server", "uvicorn")


def test_strategies_providers_and_adapters_never_import_the_pack_shell() -> None:
    found: list[str] = []
    for layer in ("strategies", "providers", "adapters"):
        found.extend(_violations(CORE_SRC / layer, _PACK_SHELL))
    assert found == []
