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

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

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

#: 不得出现在仓库里的真实标识词，以 sha256 存放——真值写进这张表本身就是泄露。
#: 2026-09-20 加：当天查出真实店名、品牌词与本机用户名散落在证据文件、调研笔记与评审
#: 文书里三周无人发现，而上面那套按 ID 字段名取值的判定对「名字」一类完全看不见。
#: 新增一个词：本机跑 `python -c "import hashlib;print(hashlib.sha256(b'<词>').hexdigest())"`，
#: 把哈希贴进来，别贴词。
FORBIDDEN_TOKEN_HASHES = frozenset(
    {
        "31e8d56525a42776ea9e2aa4ac95a587aede11df1c116438769ae4a2000bed10",
        "04dbf032b698c403a6059b4e0b3fd51e7bff0befbf7e933b3c73b3df4fd0d49f",
        "29eacb030399fc3001c63e203fa292c6295eb54691ed5109958edd3a1a6169d9",
        "f55a714867f23769c4ecc2570b189feeff09fde247d9cc37394abf9ed821b380",
    }
)

#: 切词只为了把「店名-站点」这类值切成能逐个求哈希的片段，不求语言学上的正确。
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _forbidden_tokens_in(path: Path, text: str) -> list[str]:
    found: list[str] = []
    for match in _TOKEN.finditer(text):
        token = match.group(0).lower()
        if hashlib.sha256(token.encode()).hexdigest() in FORBIDDEN_TOKEN_HASHES:
            line = text[: match.start()].count("\n") + 1
            found.append(
                f"{path.relative_to(REPO)}:{line}: 禁用标识词（见 FORBIDDEN_TOKEN_HASHES）"
            )
    return found


# .sh 是 2026-09-22 补的：出包脚本里写了公司域名当 bundle id，这条守卫因为不扫 .sh 而
# 漏过去了，差一步就推进公开仓库。守卫挡不住的文件类型等于守卫不存在。
SCANNED_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".js",
    ".html",
    ".toml",
    ".yaml",
    ".yml",
    ".example",
    ".sh",
}
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


def _emitted_ids(text: str) -> frozenset[str] | None:
    """脱敏文件自带的 ID 白名单：`sanitized.ids_emitted` 里那些值，别的一律算泄露。

    合成 ID 刻意保留了真实 ID 的形状（同长度、同前导零、同 JSON 类型），守卫凭形状
    分辨不出合成与真实。此前的办法是「文件自报 sanitized 就整份跳过」——那是一张
    自己给自己开的免检单：2026-09-20 查出 lx-response 证据文件的 targeting_mark[]
    嵌套块从未被脱敏，真实 Profile ID、真实投放词在仓库里躺了三周，守卫全程绿灯。
    改为：脱敏脚本把自己**吐出**的每个 ID 列进 ids_emitted，扫描只放行这张单子上的值。
    漏掉的那一处留着的是原值，不在单子上，于是红。
    """
    try:
        document = json.loads(text)
    except ValueError:
        return None
    block = document.get("sanitized") if isinstance(document, dict) else None
    if not isinstance(block, dict):
        return None
    listed = block.get("ids_emitted")
    return frozenset(str(v) for v in listed) if isinstance(listed, list) else frozenset()


def _offenders_in_json(path: Path, text: str, allowed: frozenset[str]) -> list[str]:
    """JSON 另走一遍结构化扫描：嵌套值不一定长成 "field": "value" 的字面形状。"""
    try:
        document = json.loads(text)
    except ValueError:
        return []
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_FIELDS:
                    for item in value if isinstance(value, list) else [value]:
                        if (
                            isinstance(item, str | int)
                            and _is_suspect(str(item))
                            and str(item) not in allowed
                        ):
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
        allowed = _emitted_ids(text) if path.suffix == ".json" else None
        if path.suffix == ".json":
            offenders.extend(_offenders_in_json(path, text, allowed or frozenset()))
        offenders.extend(_forbidden_tokens_in(path, text))
        offenders.extend(
            line
            for line in _offenders_in_text(path, text)
            if allowed is None or line.rsplit("=", 1)[-1].strip("'\"") not in allowed
        )
    assert offenders == [], (
        "疑似真实店铺/Profile/对象 ID 进入仓库（SECURITY.md 密钥政策）：\n"
        + "\n".join(sorted(set(offenders)))
        + "\n\n若确为手工编造，请加入本文件的 SYNTHETIC_IDS 并说明来源。"
    )


def test_a_sanitized_file_only_gets_a_pass_for_the_ids_it_listed(tmp_path: Path) -> None:
    """自报脱敏不再是免检单：单子上没有的 ID 照样算泄露（2026-09-20 的漏网就是这一类）。"""
    listed = "4" + "111222333444555"
    missed = "3" + "999888777666555"
    document = {
        "sanitized": {"ids": "shape-preserving hash", "ids_emitted": [listed]},
        "rows": [{"profile_id": listed, "targeting_mark": [{"profile_id": missed}]}],
    }
    path = tmp_path / "evidence.json"
    text = json.dumps(document, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    allowed = _emitted_ids(text)
    assert allowed == frozenset({listed})
    offenders = _offenders_in_json(REPO / "docs" / "x.json", text, allowed)
    assert [o for o in offenders if missed in o], "嵌套里没列进单子的 ID 必须被抓住"
    assert not [o for o in offenders if listed in o], "单子上的值是脚本自己吐的，放行"
    assert _emitted_ids('{"sanitized": {}}') == frozenset(), "没有单子就等于一个都不放行"
    assert _emitted_ids('{"rows": []}') is None, "没声明脱敏的文件不走白名单这条路"


def test_the_forbidden_word_list_catches_a_listed_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """机制自检用合成词。真词连拼接的写法也不能进源码：2026-09-23 查出这里原来拼出了
    一个真实店名和本机用户名——拼接躲得过守卫，躲不过读者。"""
    word = "zzstorename"
    digest = hashlib.sha256(word.encode()).hexdigest()
    monkeypatch.setattr(sys.modules[__name__], "FORBIDDEN_TOKEN_HASHES", frozenset({digest}))
    path = Path(__file__)
    assert _forbidden_tokens_in(path, f'nickname = "{word.upper()}-US"')
    assert not _forbidden_tokens_in(path, 'nickname = "美国一店"')


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
