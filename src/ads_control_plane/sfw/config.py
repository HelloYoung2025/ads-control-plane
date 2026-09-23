"""Pack 配置文件：一个 0600 的私有 TOML，装着领星密钥、店铺表与按币种的花费门槛。

凭据与准入表放在同一个文件里，是为了让「谁能读密钥」与「哪些店能被读」由同一道
文件权限决定：

- 文件必须 0600 且属主是服务用户（`check_private_file`）。组件以专用系统用户常驻，
  孩子账号下的任何进程读这个文件都会被内核拒绝——密钥不进环境变量、不进仓库、
  不进 MCP 登记 JSON，只在这里。权限一旦放宽（0644/0640），`load_config` 直接拒绝，
  而不是带着一个别人也能读的密钥继续跑。
- 绑定表纯由配置构造（`PackConfig.bindings`）：`[[stores]]` 五项齐全的店才进表，
  运行期不查领星名录。少一个网络机制，「店铺表配错」就不会和「名录查不到」长成一个样。
- 金额一律写成带引号的字符串再转 Decimal（安全公理 AX-01：金额链路禁 float）。
  TOML 没有十进制小数类型，`20.50` 这种裸数字进来就是二进制浮点；这里连裸整数也不收，
  免得「20 能过、20.50 不能过」在人眼里像是随机的。

所有语义校验都在 `parse_config` 里，每个错误都是一个 `ConfigError`：`code` 给程序分辨，
`str(exc)` 是一句能直接给人看的中文——server 层会原样把它塞进「配置错误：…」那句话里。
"""

from __future__ import annotations

import re
import stat
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from ads_control_plane.canonical.money import Money
from ads_control_plane.providers.lingxing.search_terms import LingxingProfileBinding
from ads_control_plane.strategies.negation import NegationParameterPack

DEFAULT_ROOT = Path("/Library/Application Support/amazon-ads")
DEFAULT_CONFIG_PATH = DEFAULT_ROOT / "config.toml"
DEFAULT_LOG_DIR = DEFAULT_ROOT / "logs"
DEFAULT_EXPORT_DIR = Path("/Users/Shared/amazon-ads/导出")
DEFAULT_RUN_LOG = Path("/Users/Shared/amazon-ads/运行记录.csv")

# 插件形态（SFW 的「定制化 → 插件」装的那种）：一个人装在自己账号里，引擎用 stdio
# 直接把进程拉起来。没有系统用户、没有 LaunchDaemon、没有端口，所以路径全在自己家里。
# 上面那组 DEFAULT_* 是给「管理员给别人装、要把密钥挡在另一个 uid 后面」的形态用的，
# 两组不混用：形态由入口决定（sfw/plugin.py 用下面这组，sfw/__main__.py 用上面那组）。
USER_ROOT = Path.home() / ".amazon-ads"
USER_CONFIG_PATH = USER_ROOT / "config.toml"
USER_EXPORT_DIR = Path.home() / "否定词导出"
USER_RUN_LOG = USER_ROOT / "运行记录.csv"
DEFAULT_PORT = 8790
SERVICE_USER = "_amazonads"
DEFAULT_TIME_BUDGET_SECONDS = 2700

#: 店铺昵称会进文件名与 Markdown 链接 `[名字](/路径)`，所以不含空格、不含路径分隔符；
#: 首字符不能是「-」：运行记录.csv 里以 - 开头的格会被 Excel 当公式，文件名会被 shell 当选项。
NICKNAME_RE = re.compile(r"^[\w一-鿿][\w一-鿿-]{0,19}$")

#: SFW → 组件的口令：安装时随机生成的十六进制串，至少 32 位。
_BEARER_RE = re.compile(r"^[0-9A-Fa-f]{32,}$")

#: 链接语法 `[名字](/路径)` 里，路径遇到空白或括号就断——export_dir 不能含这些字符。
_UNLINKABLE_RE = re.compile(r"[\s()]")

#: 站点 → 报表金额的币种。**只给 `amazon-ads shops` 做建议**：配置里每家店的币种由人
#: 显式写下并逐店校验，表里没有的站点不猜。它依赖「领星报表的 spends 是站点本币」
#: 这个前提，前提是否已被实测证实去登记簿看 DEC-024，这里不复述。
MARKETPLACE_CURRENCY: dict[str, str] = {
    "US": "USD",
    "CA": "CAD",
    "MX": "MXN",
    "BR": "BRL",
    "UK": "GBP",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "IE": "EUR",
    "PL": "PLN",
    "SE": "SEK",
    "TR": "TRY",
    "AE": "AED",
    "SA": "SAR",
    "EG": "EGP",
    "IN": "INR",
    "JP": "JPY",
    "AU": "AUD",
    "SG": "SGD",
    "ZA": "ZAR",
}


class ConfigError(Exception):
    """配置文件有问题。`code` 给程序分辨；`str(exc)` 是一句能直接给人看的中文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class StoreConfig:
    profile_id: str
    sid: str
    marketplace: str
    currency: str
    nickname: str


@dataclass(frozen=True, kw_only=True)
class Thresholds:
    lookback_days: int
    min_clicks: int
    max_data_staleness_hours: int
    min_spend: Mapping[str, Decimal]


@dataclass(frozen=True, kw_only=True)
class PackConfig:
    organization_id: uuid.UUID
    connection_id: uuid.UUID
    sfw_bearer: str
    export_dir: Path
    run_log_path: Path
    time_budget_seconds: int
    lingxing_url: str
    lingxing_key: str
    stores: tuple[StoreConfig, ...]
    thresholds: Thresholds

    def pack_for(self, currency: str) -> NegationParameterPack:
        """该币种的参数包。缺门槛不猜、不折算，直接拒绝。"""
        amount = self.thresholds.min_spend.get(currency)
        if amount is None:
            raise ConfigError(
                "CURRENCY_THRESHOLD_MISSING",
                f"[thresholds.min_spend] 里没有 {currency} 的花费门槛",
            )
        return _build_pack(self.thresholds, currency, amount)

    def bindings(self) -> dict[str, LingxingProfileBinding]:
        """profile_id → 绑定。纯由配置构造，不查名录。"""
        return {
            store.profile_id: LingxingProfileBinding(
                profile_external_id=store.profile_id,
                organization_id=self.organization_id,
                provider_connection_id=self.connection_id,
                marketplace=store.marketplace,
                shop_external_id=store.sid,
                currency=store.currency,
            )
            for store in self.stores
        }


# ------------------------------------------------------------------ TOML 形状


class _RawStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: StrictStr
    sid: StrictStr
    marketplace: StrictStr
    currency: StrictStr
    nickname: StrictStr


class _RawLingxing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: StrictStr = ""
    key: StrictStr = ""


class _RawThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: StrictInt = 30
    min_clicks: StrictInt = 25
    max_data_staleness_hours: StrictInt = 24
    #: 值类型故意留 Any：「金额要写成带引号的字符串」这句话由 _parse_thresholds 自己说，
    #: 说得出该怎么改，而不是 pydantic 那句泛泛的 "Input should be a valid string"。
    min_spend: dict[StrictStr, Any] = {}


class _RawConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    connection_id: uuid.UUID
    sfw_bearer: StrictStr
    export_dir: Path = DEFAULT_EXPORT_DIR
    run_log_path: Path = DEFAULT_RUN_LOG
    time_budget_seconds: StrictInt = DEFAULT_TIME_BUDGET_SECONDS
    lingxing: _RawLingxing = _RawLingxing()
    stores: list[_RawStore] = []
    thresholds: _RawThresholds = _RawThresholds()


def _load_toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("CONFIG_SYNTAX", f"配置文件不是合法的 TOML：{exc}") from exc


def _shape_error(exc: ValidationError) -> ConfigError:
    """pydantic 的第一条错误 → 一句中文。最常见的两种（多了字段、缺了字段）直接说中文。"""
    errors = exc.errors()
    # 拼错一个字段名会同时报「缺了 X」与「不认识 Y」；先说后者，因为它点得出拼错的那个名字。
    first = next((e for e in errors if e["type"] == "extra_forbidden"), errors[0])
    where = ".".join(str(part) for part in first["loc"]) or "顶层"
    if first["type"] == "extra_forbidden":
        detail = "不认识这个字段（是不是拼错了？）"
    elif first["type"] == "missing":
        detail = "缺了这个字段"
    else:
        detail = str(first["msg"])
    more = f"（另有 {len(errors) - 1} 处）" if len(errors) > 1 else ""
    return ConfigError("CONFIG_SHAPE", f"配置文件字段有误{more}：{where}：{detail}")


def _lingxing_credentials(raw: _RawLingxing) -> tuple[str, str]:
    for field in ("url", "key"):
        value: str = getattr(raw, field)
        if not value or value != value.strip():
            raise ConfigError(
                "LINGXING_CREDENTIALS_MISSING",
                f"[lingxing] 的 {field} 还没填（或首尾带着空格）",
            )
    return raw.url, raw.key


def _build_pack(thresholds: Thresholds, currency: str, amount: Decimal) -> NegationParameterPack:
    """逐币种走一遍策略的参数白名单：门槛的取值范围只在 NegationParameterPack 里定义一次。"""
    try:
        return NegationParameterPack(
            lookback_days=thresholds.lookback_days,
            min_spend=Money(amount=amount, currency=currency),
            min_clicks=thresholds.min_clicks,
            max_data_staleness_hours=thresholds.max_data_staleness_hours,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        detail = str(first["msg"]).removeprefix("Value error, ")
        if first["loc"]:  # Money 自己的校验：币种代码形状、金额是否有限
            raise ConfigError(
                "MIN_SPEND_INVALID",
                f"[thresholds.min_spend] 的 {currency} 不合法：{detail}",
            ) from exc
        raise ConfigError(
            "THRESHOLD_OUT_OF_RANGE",
            f"[thresholds] 门槛越界（检查币种 {currency} 时发现）：{detail}",
        ) from exc


def _parse_thresholds(raw: _RawThresholds) -> Thresholds:
    if not raw.min_spend:
        raise ConfigError(
            "MIN_SPEND_EMPTY",
            "[thresholds.min_spend] 是空的：每个店铺币种都要有一档花费门槛",
        )
    min_spend: dict[str, Decimal] = {}
    for currency, value in raw.min_spend.items():
        if not isinstance(value, str):
            raise ConfigError(
                "MIN_SPEND_NOT_STRING",
                f"[thresholds.min_spend] 的 {currency} 要写成带引号的金额字符串"
                f'（例如 "20.00"），不能写成裸数字 {value!r}',
            )
        try:
            min_spend[currency] = Decimal(value)
        except InvalidOperation as exc:
            raise ConfigError(
                "MIN_SPEND_NOT_DECIMAL",
                f"[thresholds.min_spend] 的 {currency} 不是一个金额：{value!r}",
            ) from exc
    thresholds = Thresholds(
        lookback_days=raw.lookback_days,
        min_clicks=raw.min_clicks,
        max_data_staleness_hours=raw.max_data_staleness_hours,
        min_spend=min_spend,
    )
    for currency, amount in min_spend.items():
        _build_pack(thresholds, currency, amount)
    return thresholds


def _parse_stores(raw_stores: list[_RawStore], thresholds: Thresholds) -> tuple[StoreConfig, ...]:
    if not raw_stores:
        raise ConfigError(
            "STORES_EMPTY",
            "[[stores]] 店铺表是空的：至少要填一家店（sudo amazon-ads shops 会列出可选的店铺）",
        )
    stores: list[StoreConfig] = []
    seen_profiles: set[str] = set()
    seen_nicknames: set[str] = set()
    for index, raw in enumerate(raw_stores, start=1):
        for field in ("profile_id", "sid", "marketplace", "currency"):
            value: str = getattr(raw, field)
            if not value or re.search(r"\s", value):
                raise ConfigError(
                    "STORE_FIELD_INVALID",
                    f"第 {index} 个 [[stores]] 的 {field} 是空的或带着空格：{value!r}",
                )
        if not NICKNAME_RE.fullmatch(raw.nickname):
            raise ConfigError(
                "NICKNAME_INVALID",
                f"第 {index} 个 [[stores]] 的昵称 {raw.nickname!r} 不合规："
                "只能用中文、字母、数字、下划线、连字符，1 到 20 个字，不能有空格，"
                "不能以连字符开头",
            )
        if raw.profile_id in seen_profiles:
            raise ConfigError(
                "STORE_PROFILE_DUPLICATE",
                f"[[stores]] 里 profile_id {raw.profile_id} 出现了两次",
            )
        if raw.nickname in seen_nicknames:
            raise ConfigError(
                "NICKNAME_DUPLICATE",
                f"[[stores]] 里昵称「{raw.nickname}」出现了两次：文件名会撞车",
            )
        if raw.currency not in thresholds.min_spend:
            raise ConfigError(
                "CURRENCY_THRESHOLD_MISSING",
                f"店铺「{raw.nickname}」的币种 {raw.currency} "
                "在 [thresholds.min_spend] 里没有花费门槛",
            )
        seen_profiles.add(raw.profile_id)
        seen_nicknames.add(raw.nickname)
        stores.append(
            StoreConfig(
                profile_id=raw.profile_id,
                sid=raw.sid,
                marketplace=raw.marketplace,
                currency=raw.currency,
                nickname=raw.nickname,
            )
        )
    return tuple(stores)


def parse_config(text: str) -> PackConfig:
    """TOML 文本 → 配置对象。纯函数；所有语义校验都在这里。"""
    try:
        raw = _RawConfig.model_validate(_load_toml(text))
    except ValidationError as exc:
        raise _shape_error(exc) from exc
    if not _BEARER_RE.fullmatch(raw.sfw_bearer):
        raise ConfigError(
            "SFW_BEARER_INVALID",
            "sfw_bearer 要是至少 32 位的十六进制字符串（安装时自动生成，不要手改）",
        )
    for name, path in (("export_dir", raw.export_dir), ("run_log_path", raw.run_log_path)):
        if not path.is_absolute():
            raise ConfigError(
                "PATH_NOT_ABSOLUTE", f"{name} 要写成以 / 开头的绝对路径，现在是 {path}"
            )
    if _UNLINKABLE_RE.search(str(raw.export_dir)):
        raise ConfigError(
            "EXPORT_DIR_UNLINKABLE",
            f"export_dir 不能含空格或括号（链接语法 [名字](/路径) 遇到它们会断）：{raw.export_dir}",
        )
    if not 60 <= raw.time_budget_seconds <= 3000:
        raise ConfigError(
            "TIME_BUDGET_OUT_OF_RANGE",
            "time_budget_seconds 要在 60 到 3000 之间（SFW 登记的工具超时是 3600 秒，"
            "而预算在每家店开跑前才检查，最后一家可以整个跑出预算之外，要留余量），"
            f"现在是 {raw.time_budget_seconds}",
        )
    url, key = _lingxing_credentials(raw.lingxing)
    thresholds = _parse_thresholds(raw.thresholds)
    stores = _parse_stores(raw.stores, thresholds)
    return PackConfig(
        organization_id=raw.organization_id,
        connection_id=raw.connection_id,
        sfw_bearer=raw.sfw_bearer,
        export_dir=raw.export_dir,
        run_log_path=raw.run_log_path,
        time_budget_seconds=raw.time_budget_seconds,
        lingxing_url=url,
        lingxing_key=key,
        stores=stores,
        thresholds=thresholds,
    )


# ------------------------------------------------------------------ 文件


def check_private_file(path: Path, *, expect_uid: int | None) -> None:
    """配置文件必须只有属主能读：不存在、组/其他可读写、属主不对，都拒绝。

    expect_uid 为 None 时不查属主（测试与干跑用）；正式运行时传服务用户的 uid。
    """
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError("CONFIG_MISSING", f"配置文件不存在：{path}") from exc
    except OSError as exc:
        raise ConfigError("CONFIG_UNREADABLE", f"读不了配置文件 {path}：{exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError("CONFIG_NOT_A_FILE", f"配置文件不是普通文件：{path}")
    if st.st_mode & 0o077:
        raise ConfigError(
            "CONFIG_TOO_OPEN",
            f"配置文件 {path} 的权限是 {stat.S_IMODE(st.st_mode):04o}，别的用户也能读到里面的密钥；"
            "要改成 0600（chmod 600）",
        )
    if expect_uid is not None and st.st_uid != expect_uid:
        raise ConfigError(
            "CONFIG_WRONG_OWNER",
            f"配置文件 {path} 的属主是 uid {st.st_uid}，不是预期的 uid {expect_uid}",
        )


def _read_private_text(path: Path, *, expect_uid: int | None) -> str:
    check_private_file(path, expect_uid=expect_uid)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("CONFIG_UNREADABLE", f"读不了配置文件 {path}：{exc}") from exc


def load_config(path: Path, *, expect_uid: int | None) -> PackConfig:
    """= check_private_file + parse_config(read_text)。"""
    return parse_config(_read_private_text(path, expect_uid=expect_uid))


def read_lingxing_credentials(path: Path, *, expect_uid: int | None) -> tuple[str, str]:
    """只读 [lingxing] 的 url/key，不碰店铺表：`amazon-ads shops` 跑的时候店铺表还是空的。"""
    document = _load_toml(_read_private_text(path, expect_uid=expect_uid))
    section = document.get("lingxing")
    try:
        raw = _RawLingxing.model_validate(section if section is not None else {})
    except ValidationError as exc:
        raise _shape_error(exc) from exc
    return _lingxing_credentials(raw)


# ------------------------------------------------------------------ 模板

#: 模板即文档。示例 ID 是编的（见 tests/unit/test_no_real_ids_in_repo.py 的 SYNTHETIC_IDS）。
#: 公开仓库地址。插件形态下，模板里给人抄的命令要从这里拼出来。
REPO_URL = "https://github.com/HelloYoung2025/ads-control-plane"

#: 列店铺的命令。系统形态有装好的 amazon-ads；插件形态一条命令都没装，只能借 uvx 跑。
SHOPS_CMD_SYSTEM = "sudo amazon-ads shops"

#: sfw_bearer 这一项在两种形态下意思完全不同：HTTP 那条路靠它认人，stdio 这条路根本用不到。
#: 模板里写错一句，人就会去 SFW 里找一个不存在的登记表单——照着做不成的话不如不写。
BEARER_NOTE_SYSTEM = (
    "SFW 调用本组件时要带的口令：安装时生成，逐格填进 SFW「添加服务器」的认证凭据；"
    "改了这里要重新登记。"
)
BEARER_NOTE_USER = (
    "插件形态用不到这一项：引擎直接用 stdio 把组件拉起来，没有端口也没有登记表单。"
    "留着是给另一种装法（HTTP 常驻）用的，你不用管它。"
)


def user_shops_command() -> str:
    """插件形态下列店铺的那行命令。tag 取当前真正跑着的包版本，不写死。

    写死会漂：清单里 @v0.1.0、模板里抄成别的版本，人照着跑出来的是另一份代码，
    而界面上看不出任何异常。
    """
    from importlib.metadata import version

    return (
        f"uvx --from git+{REPO_URL}@v{version('ads-control-plane')} "
        f"amazon-ads shops --config {USER_CONFIG_PATH}"
    )


#: 两种形态各自的开头说明：改配置的人是谁、改完该做什么，两边不一样。
TEMPLATE_HEADER_SYSTEM = """\
# amazon-ads 配置。只有服务用户能读（0600）：别复制到别处，别把密钥贴进聊天。
# 改完执行 sudo amazon-ads doctor 检查，再 sudo amazon-ads start。"""

TEMPLATE_HEADER_USER = """\
# amazon-ads 配置。这个文件只有你自己能读（0600）：别复制到别处，别把密钥贴进聊天。
# 下面 [lingxing] 的两项填完就能用；改完回 SFW 开一个新对话即可，不用重启什么。"""

_TEMPLATE = """\
{header}

# 平台内部身份：安装时生成，固定不变。
organization_id = "{organization_id}"
connection_id = "{connection_id}"

# {bearer_note}
sfw_bearer = "{sfw_bearer}"

# 产物目录与运行记录；路径里不能有空格。
export_dir = "{export_dir}"
run_log_path = "{run_log_path}"

# 一次调用最多跑多少秒（60 到 3000）；没轮到的店下次再跑。
time_budget_seconds = {time_budget_seconds}

# 领星网关：url 填领星 MCP 的网关地址；key 填领星 ERP 后台
# 【业务配置 → 开放接口 → MCP】里当前账号生成的鉴权密钥（不是开放平台的 appId/appSecret）。
# 这两项填好后，下面这行能列出可选店铺：
#   {shops_cmd}
# 密钥继承该账号的店铺权限：shops 列出来的，就是这个账号能看到的店。
[lingxing]
url = ""
key = ""

# 店铺表：每家店一段，五项都要填（上面那行 shops 会打印可直接粘贴的段落）。
# nickname 是给人看的名字，会进文件名：中文、字母、数字、下划线、连字符，
# 不超过 20 个字，不能有空格，不能以连字符开头。
# currency 要在下面 [thresholds.min_spend] 里有一档门槛。
# [[stores]]
# profile_id = "1000000000000001"
# sid = "2000000000000001"
# marketplace = "US"
# currency = "USD"
# nickname = "美国店"

# 判定门槛：回看天数 7 到 90；点击至少 10。
# max_data_staleness_hours 量的是**取数缓存的年龄**，不是领星那边数据有多陈：
# 缓存最长 1 小时，所以填 2 以上等同于关闭这道门（合法范围仍是 1 到 72）。
[thresholds]
lookback_days = 30
min_clicks = 25
max_data_staleness_hours = 24

# 按币种的花费门槛：金额写成带引号的字符串（"20.00"），不要写裸数字。
[thresholds.min_spend]
USD = "20.00"
"""


def render_config_template(
    *,
    sfw_bearer: str,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    export_dir: Path = DEFAULT_EXPORT_DIR,
    run_log_path: Path = DEFAULT_RUN_LOG,
    header: str = TEMPLATE_HEADER_SYSTEM,
    shops_cmd: str = SHOPS_CMD_SYSTEM,
    bearer_note: str = BEARER_NOTE_SYSTEM,
) -> str:
    """安装时写下的初始配置：[lingxing] 留空、[[stores]] 只有注释示例、门槛取缺省。

    两种形态写的是同一份模板，只有开头那两行说明和三条路径不同——插件形态在自己家里、
    自己改自己的文件，系统形态在 /Library 里、要 sudo。
    """
    return _TEMPLATE.format(
        header=header,
        shops_cmd=shops_cmd,
        bearer_note=bearer_note,
        organization_id=organization_id,
        connection_id=connection_id,
        sfw_bearer=sfw_bearer,
        export_dir=export_dir,
        run_log_path=run_log_path,
        time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
    )
