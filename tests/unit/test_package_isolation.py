"""部署隔离断言（评审 pe ENG-02 / handoff §12.3）。

依赖方向单向：ads_write_executor -> ads_control_plane。
控制平面主包不得 import 执行器包；执行器包不得引入 LLM SDK / Web 框架。
生产中这对应"主应用镜像不含 Write Adapter、执行器镜像不含 LLM/UI"。
"""

from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[2] / "src" / "ads_control_plane"
EXECUTOR_SRC = Path(__file__).resolve().parents[2] / "executor" / "src" / "ads_write_executor"

FORBIDDEN_IN_CORE = ("ads_write_executor",)
FORBIDDEN_IN_EXECUTOR = ("fastapi", "starlette", "openai", "anthropic", "mcp.server", "uvicorn")


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


def test_core_never_imports_executor() -> None:
    assert _violations(CORE_SRC, FORBIDDEN_IN_CORE) == []


def test_executor_never_imports_llm_or_web() -> None:
    assert _violations(EXECUTOR_SRC, FORBIDDEN_IN_EXECUTOR) == []


#: 领星 provider 的依赖方向：只认 canonical 与 strategies 的端口，不认编排层与镜像。
#: 它自己声明结构化 LxReadPort，具体绑定由组合根决定（同 adapters/lx_read.py 的约定）。
PROVIDER_SRC = CORE_SRC / "providers" / "lingxing"
FORBIDDEN_IN_PROVIDER = (
    "fastapi",
    "starlette",
    "mcp.server",
    "ads_control_plane.mirror",
    "ads_control_plane.api",
)


def test_lingxing_provider_stays_below_the_orchestration_layer() -> None:
    assert _violations(PROVIDER_SRC, FORBIDDEN_IN_PROVIDER) == []


def test_strategies_never_import_a_provider() -> None:
    """策略只认端口，不认任何具体 Provider（ADR-003 端口先行）。"""
    assert _violations(CORE_SRC / "strategies", ("ads_control_plane.providers",)) == []
