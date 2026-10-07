import re
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from hoshino import priv
from hoshino.config import NICKNAME
from hoshino.service import Service

from .core.clients.divingfish.models.oauth import (
    DIVINGFISH_SCOPE_NAMES,
    DIVINGFISH_SCOPE_VALUES,
    DivingFishScope,
)
from .log import logger as log  # noqa: F401

SV_HELP = "请使用 帮助maimaiDX 查看帮助"
sv = Service("maimaiDX", manage_priv=priv.ADMIN, enable_on_default=True, help_=SV_HELP)


Root = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Root / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        return (
            init_settings,
            env_settings,
            _ScopeFixSource(settings_cls, Root / ".env"),
            dotenv_settings,
            file_secret_settings,
        )


# 水鱼权限串里每个权限各自一对双引号时的容错，例：
#   DIVINGFISH_SCOPE="profile prober.profile.read" "prober.records.read"
_SCOPE_STMT_RE = re.compile(r"^\s*DIVINGFISH_SCOPE\s*=\s*(?P<raw>.+?)\s*$", re.IGNORECASE)
_SCOPE_ITEM_RE = re.compile(r'"([^"]*)"')


def _merge_quoted_scope(raw: str) -> str | None:
    """把被拆成多个引号段的一行权限串重新合并。

    python-dotenv 对同一行出现多个引号段会判为无法解析（
    "could not parse statement starting from line N"）并整行丢弃，
    这里把引号段里的内容按空格拼回一个完整权限列表。

    只在「两个以上引号对」时才介入（此时 dotenv 必定丢弃整行）；
    一个引号对（整串加引号）或完全不加引号都由 dotenv 正常解析，返回 None。
    """
    raw = raw.split("#", 1)[0]  # 先去掉行内注释
    if raw.count('"') < 4 or raw.count('"') % 2:
        return None
    parts = [p.strip() for p in _SCOPE_ITEM_RE.findall(raw)]
    merged = " ".join(p for p in parts if p)
    return merged or None


class _ScopeFixSource(PydanticBaseSettingsSource):
    """修正 python-dotenv 无法解析的多引号段 scope 写法。

    python-dotenv 遇到 DIVINGFISH_SCOPE="a" "b" 这种多引号段会整行丢弃，
    这里直接读 .env 原始文本，把引号段内容拼成完整权限串喂给配置。
    优先级低于真实环境变量（env_settings 在后面覆盖它）。
    """

    def __init__(self, settings_cls: type[BaseSettings], env_file: Path):
        super().__init__(settings_cls)
        self._env_file = env_file

    def __call__(self) -> dict[str, Any]:
        fixed: dict[str, Any] = {}
        if not self._env_file.exists():
            return fixed
        text = self._env_file.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            m = _SCOPE_STMT_RE.match(line)
            if not m:
                continue
            # 只有 dotenv 必定丢弃的「多引号段」写法才需要介入；
            # 其余写法（不加引号 / 整串一个引号）dotenv 自己能正确解析。
            merged = _merge_quoted_scope(m.group("raw"))
            if merged:
                fixed["divingfish_scope"] = merged
        return fixed

    def get_field_value(self, field: FieldInfo, field_name: str):
        return None, field_name, False


class BaseConfig(Settings):
    maimaidx_path: str
    maimaidx_alias_proxy: bool = False
    maimaidx_alias_push: bool = True
    save_in_memory: bool | None = True
    assets_online: bool | None = True
    # 成绩上传（maimai-update 融入）：拉取/上传成绩用的 HTTP 代理，留空则直连
    mai_http_proxy: str | None = None
    bot_name: str = (
        NICKNAME
        if isinstance(NICKNAME, str)
        else (list(NICKNAME)[0] if NICKNAME else "Sakura")
    )


class DivingFishConfig(Settings):
    divingfish_prober_proxy: bool = False
    divingfish_token: str | None = None
    divingfish_client_id: str | None = None
    divingfish_client_secret: str | None = None
    divingfish_auth_url: str = "https://auth.diving-fish.com"
    divingfish_scope: DivingFishScope = DivingFishScope.PROBER_RECORDS_READ

    @field_validator("divingfish_scope", mode="before")
    @classmethod
    def validate_divingfish_scope(cls, value: str) -> DivingFishScope:
        if isinstance(value, DivingFishScope):
            return value

        if isinstance(value, int):
            return DivingFishScope(value)

        if not isinstance(value, str):
            raise TypeError("divingfish_scope 必须是字符串或整数")

        value = value.split("#", 1)[0].strip()  # 容错：去掉可能残留的行内注释
        value = value.replace('"', " ")
        if not value:
            raise ValueError("divingfish_scope 不能为空")

        result = DivingFishScope(0)

        for name in value.split():
            scope = DIVINGFISH_SCOPE_VALUES.get(name)
            if scope is None:
                valid_names = ", ".join(DIVINGFISH_SCOPE_VALUES)
                raise ValueError(
                    f"未知的 DivingFish scope: {name!r}；可选值：{valid_names}"
                )
            result |= scope

        return result

    @property
    def divingfish_oauth_scope(self) -> str:
        return " ".join(
            name
            for scope, name in DIVINGFISH_SCOPE_NAMES.items()
            if self.divingfish_scope & scope
        )

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.divingfish_client_id and self.divingfish_client_secret)


class LxnsConfig(Settings):
    lxns_dev_token: str | None = None
    lx_client_id: str | None = None
    lx_client_secret: str | None = None
    redirect_uri: str | None = None


maiconfig = BaseConfig()
dfconfig = DivingFishConfig()
lxnsconfig = LxnsConfig()
