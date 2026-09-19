"""零 Secret 守卫：仓库里不得出现真实的店铺 / Profile / 对象 ID。

SECURITY.md「密钥政策」第一条逐字要求：「代码、配置、fixture、文档、测试中不得出现
任何真实密钥、Cookie、店铺/Profile/对象真实 ID」。这条规矩此前只写在文档里，没有任何
可执行的守卫——而 2026-08-30 起 docs/evidence/ 要开始收真实响应的脱敏样例，
正是最需要机械校验的时刻：脱敏漏一个字段，靠人眼是看不出来的。

判定方式是**按 ID 字段名**取值，而不是泛泛扫长数字串：evidence 里的网关 request_id
也是长数字（毫秒时间戳），但它不标识任何店铺或对象，不在禁止清单内。

新增一个合成 ID 时，把它加进 SYNTHETIC_IDS。这点摩擦是有意的——它强迫每一个进入
仓库的对象 ID 都被人过一遍眼，回答「这个是我编的，还是从真实账户里粘来的？」
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: 领星/Amazon 侧标识对象与店铺的字段名。
ID_FIELDS = (
    "profile_id",
    "profile_ids",
    "campaign_id",
    "ad_group_id",
    "ad_id",
    "target_id",
    "keyword_id",
    "portfolio_id",
    "sid",
    "store_id",
    "shop_id",
    "seller_id",
)

#: 真实 ID 的形状：纯数字、且足够长（领星 Profile 实测 16 位，活动/广告组同量级）。
#: 带字母或连字符的占位（p-1 / ag-2 / demo-sid-1）一望即知是编的，不在判定范围内——
#: 守卫要抓的是「从真实账户粘过来的那种东西」，不是所有标识符。
_REAL_ID_SHAPE = re.compile(r"^\d{9,}$")

#: 形状上像真实 ID、但确由手工编造的值。它们刻意保留了真实 ID 的形状
#: （16 位、前导零），因为「超长与前导零 ID 无损」这条合同测试需要这种形状才测得出东西。
SYNTHETIC_IDS = frozenset(
    {
        "1000000000000001",
        "1000000000000002",
        "2000000000000001",
        "2000000000000002",
        "2000000000000007",
        "2000000000000009",
        "000000999",
        "000000123456789012",
        "286101123467242",
    }
)

SCANNED_SUFFIXES = {".py", ".md", ".json", ".js", ".html", ".toml", ".yaml", ".yml", ".example"}
SKIP_DIRS = {
    ".git",
    ".claude",  # Claude Code 的 worktree 会建在仓库内；那是别的检出，不归这条守卫扫
    ".venv",
    "__pycache__",
    "node_modules",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
}

#: "campaign_id": "123"  /  campaign_id="123"  /  campaign_id: '123'
_ASSIGNMENT = re.compile(
    r"""["']?(?P<field>""" + "|".join(ID_FIELDS) + r""")["']?\s*[:=]\s*["'](?P<value>[^"']*)["']"""
)


def _is_suspect(value: str) -> bool:
    """这个值像不像从真实账户里粘出来的对象 ID。"""
    if "{" in value:
        return False  # f-string 模板（200000000000000{i}）不是字面 ID
    return bool(_REAL_ID_SHAPE.match(value)) and value not in SYNTHETIC_IDS


def _offenders_in_text(path: Path, text: str) -> list[str]:
    found: list[str] = []
    for match in _ASSIGNMENT.finditer(text):
        value = match.group("value")
        if _is_suspect(value):
            line = text[: match.start()].count("\n") + 1
            found.append(f"{path.relative_to(REPO)}:{line}: {match.group('field')}={value!r}")
    return found


def _declares_sanitized(text: str) -> bool:
    """文件是否自报「已脱敏」。

    合成 ID 刻意保留了真实 ID 的形状（同长度、同前导零、同 JSON 类型），因为
    「超长与前导零 ID 无损」那条合同测试需要这种形状才测得出东西——所以守卫凭形状
    分辨不出合成与真实，只能靠这个声明。
    """
    try:
        document = json.loads(text)
    except ValueError:
        return False
    return isinstance(document, dict) and isinstance(document.get("sanitized"), dict)


def _offenders_in_json(path: Path, text: str) -> list[str]:
    """JSON 另走一遍结构化扫描：嵌套值不一定长成 "field": "value" 的字面形状。"""
    try:
        document = json.loads(text)
    except ValueError:
        return []
    if isinstance(document, dict) and isinstance(document.get("sanitized"), dict):
        return []
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_FIELDS:
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, str | int) and _is_suspect(str(item)):
                            found.append(f"{path.relative_to(REPO)}: {key}={item!r}")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found


def test_no_real_object_ids_anywhere_in_the_repo() -> None:
    offenders: list[str] = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        # 只看仓库内的相对路径：仓库自己可能就检出在 .claude/worktrees/ 之下，
        # 按绝对路径判断会把整个检出都跳过，守卫就成了恒真（2026-09-19 反向验证发现）。
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.suffix == ".json":
            offenders.extend(_offenders_in_json(path, text))
            if _declares_sanitized(text):
                continue  # 文本扫描也一并跳过，理由见 _offenders_in_json
        offenders.extend(_offenders_in_text(path, text))
    assert offenders == [], (
        "疑似真实店铺/Profile/对象 ID 进入仓库（SECURITY.md 密钥政策）：\n"
        + "\n".join(sorted(set(offenders)))
        + "\n\n若确为手工编造，请加入本文件的 SYNTHETIC_IDS 并说明来源。"
    )


def test_guard_actually_catches_a_realistic_id() -> None:
    """守卫本身要能被证伪——否则它可能只是恒真。

    探针拼接而成，源码里不出现完整数字串：否则这条测试自己就会被上面那条扫到，
    守卫抓住的第一个「泄露」将是它自己。
    """
    probe = "3999888" + "777666555"
    here = Path(__file__)
    assert _offenders_in_text(here, f'"campaign_id": "{probe}"')
    assert not _offenders_in_text(here, '"campaign_id": "1000000000000001"')  # 在白名单里
    assert not _offenders_in_text(here, '"campaign_id": "c-9"')  # 形状上就不是真实 ID
