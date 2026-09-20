"""Pack 配置文件测试：0600 私有文件、店铺表、按币种门槛、模板零真实 ID。

文件权限那几条用 tmp_path + os.chmod 真做；属主检查用 expect_uid=os.getuid()+1 触发
「属主不符」——测试进程没法把文件 chown 给别人，但可以把「预期的人」说成别人。
每一个 ConfigError 都要满足同一份合同：带 code、str(exc) 是一句（单行）中文。
"""

import ast
import os
import re
import sys
import tomllib
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from ads_control_plane.canonical.entity import AdProduct
from ads_control_plane.canonical.money import Money
from ads_control_plane.sfw import config as config_module
from ads_control_plane.sfw.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_EXPORT_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_PORT,
    DEFAULT_ROOT,
    DEFAULT_RUN_LOG,
    MARKETPLACE_CURRENCY,
    NICKNAME_RE,
    SERVICE_USER,
    ConfigError,
    PackConfig,
    StoreConfig,
    check_private_file,
    load_config,
    parse_config,
    read_lingxing_credentials,
    render_config_template,
)
from ads_control_plane.strategies.negation import NegationParameterPack
from tests.unit.test_no_real_ids_in_repo import REPO, SYNTHETIC_IDS, _offenders_in_text

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")
CONN = uuid.UUID("00000000-0000-4000-8000-000000000002")
BEARER = "0123456789abcdef" * 2

VALID = f"""
organization_id = "{ORG}"
connection_id = "{CONN}"
sfw_bearer = "{BEARER}"
export_dir = "/Users/Shared/ads-pack/导出"
run_log_path = "/Users/Shared/ads-pack/运行记录.csv"
time_budget_seconds = 2700

[lingxing]
url = "http://lx.invalid/mcp"
key = "sk-test-key"

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

[thresholds]
lookback_days = 30
min_clicks = 25
max_data_staleness_hours = 24

[thresholds.min_spend]
USD = "20.00"
JPY = "3000"
"""

_CJK = re.compile(r"[一-鿿]")


def _refused(text: str, code: str) -> ConfigError:
    """parse_config 必须以指定 code 拒绝，且错误信息是一句能给人看的中文。"""
    with pytest.raises(ConfigError) as info:
        parse_config(text)
    _assert_contract(info.value, code)
    return info.value


def _assert_contract(exc: ConfigError, code: str) -> None:
    assert exc.code == code
    message = str(exc)
    assert _CJK.search(message), f"错误信息不是中文：{message!r}"
    assert "\n" not in message, f"错误信息不止一行：{message!r}"


def _private(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _template() -> str:
    return render_config_template(sfw_bearer=BEARER, organization_id=ORG, connection_id=CONN)


_COMMENTED_STORE_LINE = re.compile(
    r'^# (\[\[stores\]\]|(profile_id|sid|marketplace|currency|nickname) = "[^"]*")$'
)


def _uncomment_store_example(template: str) -> str:
    """把模板里注释掉的 [[stores]] 示例放出来——只放键值行，不碰旁边的说明文字。"""
    lines = [
        line[2:] if _COMMENTED_STORE_LINE.match(line) else line for line in template.splitlines()
    ]
    return "\n".join(lines) + "\n"


def _fill_lingxing(text: str) -> str:
    return text.replace('url = ""', 'url = "http://lx.invalid/mcp"').replace(
        'key = ""', 'key = "sk-test-key"'
    )


# ------------------------------------------------------------------ 模板


def test_template_is_valid_toml_with_the_documented_defaults() -> None:
    document = tomllib.loads(_template())
    assert document["organization_id"] == str(ORG)
    assert document["connection_id"] == str(CONN)
    assert document["sfw_bearer"] == BEARER
    assert document["export_dir"] == str(DEFAULT_EXPORT_DIR)
    assert document["run_log_path"] == str(DEFAULT_RUN_LOG)
    assert document["time_budget_seconds"] == 2700
    assert document["lingxing"] == {"url": "", "key": ""}
    assert "stores" not in document, "店铺表只能是注释示例：安装时还不知道有哪些店"
    assert document["thresholds"] == {
        "lookback_days": 30,
        "min_clicks": 25,
        "max_data_staleness_hours": 24,
        "min_spend": {"USD": "20.00"},
    }


def test_template_has_no_real_id_shapes() -> None:
    """模板会被写进 /Library 下的真实配置；它自己不能带任何像真实 ID 的东西。"""
    text = _template()
    assert _offenders_in_text(REPO / "config.toml", text) == []
    example_ids = set(re.findall(r'^# (?:profile_id|sid) = "(\d+)"$', text, flags=re.MULTILINE))
    assert len(example_ids) == 2, "示例 [[stores]] 里应有一对 16 位形状的合成 ID，否则示例教不会人"
    assert example_ids <= SYNTHETIC_IDS


def test_untouched_template_is_refused_until_the_admin_fills_it() -> None:
    """模板不是能直接跑的配置：领星凭据空着、店铺表空着，都要被拦住。"""
    _refused(_template(), "LINGXING_CREDENTIALS_MISSING")
    _refused(_fill_lingxing(_template()), "STORES_EMPTY")


def test_template_becomes_a_working_config_once_stores_and_lingxing_are_filled() -> None:
    cfg = parse_config(_fill_lingxing(_uncomment_store_example(_template())))
    assert cfg.organization_id == ORG
    assert cfg.connection_id == CONN
    assert cfg.sfw_bearer == BEARER
    assert cfg.export_dir == DEFAULT_EXPORT_DIR
    assert cfg.run_log_path == DEFAULT_RUN_LOG
    assert cfg.time_budget_seconds == 2700
    assert (cfg.lingxing_url, cfg.lingxing_key) == ("http://lx.invalid/mcp", "sk-test-key")
    assert cfg.stores == (
        StoreConfig(
            profile_id="1000000000000001",
            sid="2000000000000001",
            marketplace="US",
            currency="USD",
            nickname="美国店",
        ),
    )
    assert cfg.pack_for("USD") == NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount=Decimal("20.00"), currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )


# ------------------------------------------------------------------ 私有文件


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606])
def test_check_private_file_refuses_group_or_world_readable_modes(
    tmp_path: Path, mode: int
) -> None:
    path = _private(tmp_path, VALID, mode)
    with pytest.raises(ConfigError) as info:
        check_private_file(path, expect_uid=None)
    _assert_contract(info.value, "CONFIG_TOO_OPEN")
    assert f"{mode:04o}" in str(info.value)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_check_private_file_accepts_owner_only_modes(tmp_path: Path, mode: int) -> None:
    path = _private(tmp_path, VALID, mode)
    check_private_file(path, expect_uid=None)
    check_private_file(path, expect_uid=os.getuid())


def test_check_private_file_refuses_a_wrong_owner(tmp_path: Path) -> None:
    path = _private(tmp_path, VALID)
    with pytest.raises(ConfigError) as info:
        check_private_file(path, expect_uid=os.getuid() + 1)
    _assert_contract(info.value, "CONFIG_WRONG_OWNER")
    assert str(os.getuid() + 1) in str(info.value)


def test_check_private_file_refuses_a_missing_file_or_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        check_private_file(tmp_path / "missing.toml", expect_uid=None)
    _assert_contract(info.value, "CONFIG_MISSING")
    directory = tmp_path / "config.toml"
    directory.mkdir()
    os.chmod(directory, 0o700)
    with pytest.raises(ConfigError) as info:
        check_private_file(directory, expect_uid=None)
    _assert_contract(info.value, "CONFIG_NOT_A_FILE")


def test_load_config_reads_a_private_file_and_refuses_an_open_one(tmp_path: Path) -> None:
    cfg = load_config(_private(tmp_path, VALID), expect_uid=os.getuid())
    assert [store.nickname for store in cfg.stores] == ["美国店", "日本店"]
    with pytest.raises(ConfigError) as info:
        load_config(_private(tmp_path, VALID, 0o644), expect_uid=os.getuid())
    _assert_contract(info.value, "CONFIG_TOO_OPEN")


def test_load_config_turns_undecodable_bytes_into_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff\xfe not utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError) as info:
        load_config(path, expect_uid=None)
    _assert_contract(info.value, "CONFIG_UNREADABLE")


# ------------------------------------------------------------------ 店铺表


def test_empty_store_table_is_refused() -> None:
    head, _, _ = VALID.partition("[[stores]]")
    _, _, thresholds = VALID.partition("[thresholds]")
    _refused(head + "[thresholds]" + thresholds, "STORES_EMPTY")


def test_duplicate_profile_id_is_refused() -> None:
    text = VALID.replace('profile_id = "1000000000000002"', 'profile_id = "1000000000000001"')
    exc = _refused(text, "STORE_PROFILE_DUPLICATE")
    assert "1000000000000001" in str(exc)


def test_duplicate_nickname_is_refused() -> None:
    exc = _refused(
        VALID.replace('nickname = "日本店"', 'nickname = "美国店"'), "NICKNAME_DUPLICATE"
    )
    assert "美国店" in str(exc)


@pytest.mark.parametrize(
    "nickname",
    ["美国 店", "", "店" * 21, "a/b", "..", "美国店\\n", "美国店 "],
)
def test_nickname_that_cannot_live_in_a_link_or_a_filename_is_refused(nickname: str) -> None:
    _refused(VALID.replace('nickname = "美国店"', f'nickname = "{nickname}"'), "NICKNAME_INVALID")


@pytest.mark.parametrize("nickname", ["-美国店", "-", "--", "-1"])
def test_nickname_starting_with_a_hyphen_is_refused(nickname: str) -> None:
    """以 - 开头的昵称：运行记录.csv 里那一格会被 Excel 当公式，文件名会被 shell 当选项。
    连字符只准出现在后面（US-1、店-）。"""
    text = VALID.replace('nickname = "美国店"', f'nickname = "{nickname}"')
    assert "连字符开头" in str(_refused(text, "NICKNAME_INVALID"))


@pytest.mark.parametrize("nickname", ["美国店", "US-1", "店-", "店铺_2", "店" * 20, "ＵＳ"])
def test_nickname_regex_accepts_words_in_any_script_without_spaces(nickname: str) -> None:
    assert NICKNAME_RE.fullmatch(nickname)
    cfg = parse_config(VALID.replace('nickname = "美国店"', f'nickname = "{nickname}"'))
    assert cfg.stores[0].nickname == nickname


def test_store_currency_without_a_threshold_is_refused() -> None:
    exc = _refused(VALID.replace('JPY = "3000"\n', ""), "CURRENCY_THRESHOLD_MISSING")
    assert "日本店" in str(exc) and "JPY" in str(exc)


@pytest.mark.parametrize(
    "field",
    ["profile_id", "sid", "marketplace", "currency"],
)
def test_store_identity_fields_with_whitespace_or_empty_are_refused(field: str) -> None:
    original = next(line for line in VALID.splitlines() if line.startswith(f"{field} = "))
    value = original.split(" = ", 1)[1].strip('"')
    _refused(VALID.replace(original, f'{field} = " {value}"', 1), "STORE_FIELD_INVALID")
    _refused(VALID.replace(original, f'{field} = ""', 1), "STORE_FIELD_INVALID")


def test_marketplace_is_not_checked_against_the_advice_table() -> None:
    """MARKETPLACE_CURRENCY 只给 shops 做建议；配置里的站点码由人负责，不在这里猜。"""
    cfg = parse_config(VALID.replace('marketplace = "JP"', 'marketplace = "XX"'))
    assert cfg.stores[1].marketplace == "XX"
    assert "XX" not in MARKETPLACE_CURRENCY


# ------------------------------------------------------------------ 门槛


@pytest.mark.parametrize(
    ("original", "replacement", "wording"),
    [
        ("lookback_days = 30", "lookback_days = 91", "lookback_days must be within [7, 90]"),
        ("lookback_days = 30", "lookback_days = 6", "lookback_days must be within [7, 90]"),
        ("min_clicks = 25", "min_clicks = 9", "min_clicks below 10"),
        (
            "max_data_staleness_hours = 24",
            "max_data_staleness_hours = 73",
            "max_data_staleness_hours must be within [1, 72]",
        ),
        ('USD = "20.00"', 'USD = "0"', "min_spend must be positive"),
        ('USD = "20.00"', 'USD = "-1"', "min_spend must be positive"),
    ],
)
def test_threshold_out_of_range_reuses_the_whitelist_wording(
    original: str, replacement: str, wording: str
) -> None:
    """取值范围只在 NegationParameterPack 里定义一次；这里复用它的话，不另抄一份数字。"""
    exc = _refused(VALID.replace(original, replacement), "THRESHOLD_OUT_OF_RANGE")
    assert wording in str(exc)


@pytest.mark.parametrize("literal", ["20.0", "20", "true"])
def test_money_written_as_a_bare_number_is_refused(literal: str) -> None:
    """TOML 没有十进制类型：裸数字要么是二进制浮点，要么让「20 能过 20.50 不能过」显得随机。"""
    exc = _refused(VALID.replace('USD = "20.00"', f"USD = {literal}"), "MIN_SPEND_NOT_STRING")
    assert "USD" in str(exc)


def test_money_string_that_is_not_a_decimal_is_refused() -> None:
    _refused(VALID.replace('USD = "20.00"', 'USD = "twenty"'), "MIN_SPEND_NOT_DECIMAL")
    _refused(VALID.replace('USD = "20.00"', 'USD = ""'), "MIN_SPEND_NOT_DECIMAL")


def test_money_that_money_itself_rejects_is_refused_with_its_wording() -> None:
    exc = _refused(VALID.replace('USD = "20.00"', 'USD = "NaN"'), "MIN_SPEND_INVALID")
    assert "finite" in str(exc)
    exc = _refused(VALID.replace('JPY = "3000"', 'jpy = "3000"'), "MIN_SPEND_INVALID")
    assert "ISO-4217" in str(exc)


def test_empty_min_spend_table_is_refused_before_any_store_is_blamed() -> None:
    _refused(VALID.replace('USD = "20.00"\nJPY = "3000"\n', ""), "MIN_SPEND_EMPTY")


def test_pack_for_returns_the_threshold_of_that_currency_and_only_that_currency() -> None:
    cfg = parse_config(VALID)
    assert cfg.pack_for("USD").min_spend == Money(amount=Decimal("20.00"), currency="USD")
    assert cfg.pack_for("JPY").min_spend == Money(amount=Decimal("3000"), currency="JPY")
    assert cfg.pack_for("JPY").lookback_days == 30
    assert cfg.thresholds.min_spend == {"USD": Decimal("20.00"), "JPY": Decimal("3000")}
    with pytest.raises(ConfigError) as info:
        cfg.pack_for("EUR")
    _assert_contract(info.value, "CURRENCY_THRESHOLD_MISSING")


def test_pack_for_never_touches_float() -> None:
    """金额链路禁 float（AX-01）：解析出来的门槛得是 Decimal，不是 float 转出来的 Decimal。"""
    cfg = parse_config(VALID.replace('USD = "20.00"', 'USD = "0.1"'))
    amount = cfg.pack_for("USD").min_spend.amount
    assert isinstance(amount, Decimal)
    assert amount == Decimal("0.1") and str(amount) == "0.1"


# ------------------------------------------------------------------ 顶层字段


@pytest.mark.parametrize("budget", [59, 3001, 3600, 0, -1])
def test_time_budget_out_of_range_is_refused(budget: int) -> None:
    text = VALID.replace("time_budget_seconds = 2700", f"time_budget_seconds = {budget}")
    exc = _refused(text, "TIME_BUDGET_OUT_OF_RANGE")
    assert str(budget) in str(exc)


def test_time_budget_defaults_below_the_sfw_tool_timeout_and_accepts_both_ends() -> None:
    # 上限 3000 < SFW 登记的 tool_timeout_sec(3600)：预算在每家店开跑前才检查，
    # 最后一家可以整个跑出预算之外，越过 3600 孩子收到的就是一句假的「工具没连上」。
    assert (
        parse_config(VALID.replace("time_budget_seconds = 2700\n", "")).time_budget_seconds == 2700
    )
    for budget in (60, 3000):
        text = VALID.replace("time_budget_seconds = 2700", f"time_budget_seconds = {budget}")
        assert parse_config(text).time_budget_seconds == budget


@pytest.mark.parametrize("bearer", ["abc", "0123456789abcdef0123456789abcde", "g" * 32, ""])
def test_sfw_bearer_must_be_at_least_32_hex_chars(bearer: str) -> None:
    _refused(VALID.replace(BEARER, bearer), "SFW_BEARER_INVALID")


def test_sfw_bearer_accepts_long_and_uppercase_hex() -> None:
    for bearer in ("ABCDEF0123456789" * 2, "0" * 64):
        assert parse_config(VALID.replace(BEARER, bearer)).sfw_bearer == bearer


def test_export_dir_and_run_log_must_be_absolute() -> None:
    _refused(VALID.replace('export_dir = "/Users', 'export_dir = "Users'), "PATH_NOT_ABSOLUTE")
    _refused(VALID.replace('run_log_path = "/Users', 'run_log_path = "Users'), "PATH_NOT_ABSOLUTE")


@pytest.mark.parametrize("bad_dir", ["/Users/Shared/ads pack/导出", "/Users/Shared/(x)/导出"])
def test_export_dir_that_would_break_a_markdown_link_is_refused(bad_dir: str) -> None:
    text = VALID.replace('export_dir = "/Users/Shared/ads-pack/导出"', f'export_dir = "{bad_dir}"')
    _refused(text, "EXPORT_DIR_UNLINKABLE")


def test_paths_default_to_the_shared_export_locations() -> None:
    text = VALID.replace('export_dir = "/Users/Shared/ads-pack/导出"\n', "").replace(
        'run_log_path = "/Users/Shared/ads-pack/运行记录.csv"\n', ""
    )
    cfg = parse_config(text)
    assert (cfg.export_dir, cfg.run_log_path) == (DEFAULT_EXPORT_DIR, DEFAULT_RUN_LOG)


def test_lingxing_credentials_must_be_filled_before_the_pack_runs() -> None:
    _refused(VALID.replace('key = "sk-test-key"', 'key = ""'), "LINGXING_CREDENTIALS_MISSING")
    _refused(
        VALID.replace('key = "sk-test-key"', 'key = "sk-test-key "'), "LINGXING_CREDENTIALS_MISSING"
    )
    _refused(VALID.replace("[lingxing]\n", ""), "CONFIG_SHAPE")


def test_unknown_key_missing_key_wrong_type_and_bad_toml_are_all_refused() -> None:
    exc = _refused(VALID.replace("sfw_bearer =", "sfw_bearrer ="), "CONFIG_SHAPE")
    assert "sfw_bearrer" in str(exc)
    exc = _refused(VALID.replace(f'organization_id = "{ORG}"\n', ""), "CONFIG_SHAPE")
    assert "organization_id" in str(exc)
    _refused(
        VALID.replace(f'organization_id = "{ORG}"', 'organization_id = "not-a-uuid"'),
        "CONFIG_SHAPE",
    )
    _refused(VALID.replace("lookback_days = 30", 'lookback_days = "30"'), "CONFIG_SHAPE")
    _refused(VALID.replace("min_clicks = 25", "min_clicks = 25.0"), "CONFIG_SHAPE")
    _refused(VALID.replace('nickname = "美国店"', "nickname = 1"), "CONFIG_SHAPE")
    _refused("this is = not toml", "CONFIG_SYNTAX")


# ------------------------------------------------------------------ 绑定表


def test_bindings_map_the_five_store_fields_one_to_one() -> None:
    cfg = parse_config(VALID)
    bindings = cfg.bindings()
    assert set(bindings) == {"1000000000000001", "1000000000000002"}
    us = bindings["1000000000000001"]
    assert us.profile_external_id == "1000000000000001"
    assert us.shop_external_id == "2000000000000001"
    assert us.marketplace == "US"
    assert us.currency == "USD"
    assert us.organization_id == ORG
    assert us.provider_connection_id == CONN
    assert us.ad_product is AdProduct.SP
    jp = bindings["1000000000000002"]
    assert (jp.shop_external_id, jp.marketplace, jp.currency) == ("2000000000000002", "JP", "JPY")


def test_bindings_are_built_from_config_alone() -> None:
    """运行期零名录查询：绑定表只看 PackConfig 自己的字段，不碰任何客户端。"""
    cfg = parse_config(VALID)
    assert isinstance(cfg, PackConfig)
    source = ast.parse(Path(config_module.__file__).read_text(encoding="utf-8"))
    bindings_fn = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "bindings"
    )
    names = {node.id for node in ast.walk(bindings_fn) if isinstance(node, ast.Name)}
    assert names <= {"self", "store", "LingxingProfileBinding", "dict", "str"}, names


# ------------------------------------------------------------------ 只读凭据


def test_read_lingxing_credentials_works_while_the_store_table_is_still_empty(
    tmp_path: Path,
) -> None:
    path = _private(tmp_path, _fill_lingxing(_template()))
    assert read_lingxing_credentials(path, expect_uid=os.getuid()) == (
        "http://lx.invalid/mcp",
        "sk-test-key",
    )


def test_read_lingxing_credentials_refuses_blank_key_and_open_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        read_lingxing_credentials(_private(tmp_path, _template()), expect_uid=None)
    _assert_contract(info.value, "LINGXING_CREDENTIALS_MISSING")
    with pytest.raises(ConfigError) as info:
        read_lingxing_credentials(
            _private(tmp_path, _fill_lingxing(_template()), 0o644), expect_uid=None
        )
    _assert_contract(info.value, "CONFIG_TOO_OPEN")
    with pytest.raises(ConfigError) as info:
        read_lingxing_credentials(_private(tmp_path, "organization_id = 1\n"), expect_uid=None)
    _assert_contract(info.value, "LINGXING_CREDENTIALS_MISSING")


# ------------------------------------------------------------------ 合同与边界


def test_frozen_interface_constants() -> None:
    assert Path("/Library/Application Support/ads-pack") == DEFAULT_ROOT
    assert DEFAULT_CONFIG_PATH == DEFAULT_ROOT / "config.toml"
    assert DEFAULT_LOG_DIR == DEFAULT_ROOT / "logs"
    assert Path("/Users/Shared/ads-pack/导出") == DEFAULT_EXPORT_DIR
    assert Path("/Users/Shared/ads-pack/运行记录.csv") == DEFAULT_RUN_LOG
    assert DEFAULT_PORT == 8790
    assert SERVICE_USER == "_adspack"
    assert NICKNAME_RE.pattern == r"^[\w一-鿿][\w一-鿿-]{0,19}$"


def test_marketplace_currency_table_is_iso_codes_keyed_by_site() -> None:
    assert MARKETPLACE_CURRENCY["US"] == "USD"
    assert MARKETPLACE_CURRENCY["JP"] == "JPY"
    assert MARKETPLACE_CURRENCY["UK"] == MARKETPLACE_CURRENCY["GB"] == "GBP"
    assert all(re.fullmatch(r"[A-Z]{2}", site) for site in MARKETPLACE_CURRENCY)
    assert all(re.fullmatch(r"[A-Z]{3}", ccy) for ccy in MARKETPLACE_CURRENCY.values())


def test_config_module_imports_only_the_stdlib_pydantic_and_the_four_allowed_modules() -> None:
    """config.py 是其余工作包的公共依赖：它自己不能反过来依赖组件里的任何东西。"""
    allowed_repo_modules = {
        "ads_control_plane.strategies.negation",
        "ads_control_plane.canonical.money",
        "ads_control_plane.providers.lingxing.search_terms",
        "ads_control_plane.canonical.entity",
    }
    tree = ast.parse(Path(config_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.level == 0, "不许相对导入"
            imported.add(node.module)
    offenders = sorted(
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names
        and name.split(".")[0] != "pydantic"
        and name not in allowed_repo_modules
    )
    assert offenders == [], offenders
    assert "ads_control_plane.strategies.negation" in imported
    assert "ads_control_plane.providers.lingxing.search_terms" in imported
