"""通过舞萌二维码上传成绩至查分器

玩家在游戏机的「出勤二维码」里拿到一串以 `SGWCMAID` 开头的文本，
`maimai.py` 用它向舞萌 title 服务器换取加密的 userId，再拉取全部成绩，
最后按玩家选定的数据源写回水鱼 / 落雪。

二维码换取 userId 必须由能直连 title 服务器的机器发出，境外或部分云服务器
会被拒绝，可通过 `MAIMAIDX_ARCADE_HTTP_PROXY` 指定中转代理。
"""

from dataclasses import dataclass

from maimai_py import (
    ArcadeProvider,
    DivingFishProvider,
    LXNSProvider,
    MaimaiClient,
    PlayerIdentifier,
)
from maimai_py.models import Score

from ..config import lxnsconfig, maiconfig
from .database.qq import User
from .merge.models import ServiceName

QRCODE_PREFIX = "SGWCMAID"

_client: MaimaiClient | None = None


class UploadError(Exception):
    """上传成绩相关的基类异常"""


class QRCodeFormatError(UploadError):
    """二维码内容格式错误"""


class UploadCredentialNotFoundError(UploadError):
    """未配置上传凭据"""

    def __init__(self, service: ServiceName) -> None:
        super().__init__(service)
        self.service = service


@dataclass
class UploadResult:
    service: ServiceName
    count: int


def get_client() -> MaimaiClient:
    """获取 `maimai.py` 客户端，`MaimaiClient` 本身是单例"""
    global _client
    if _client is None:
        _client = MaimaiClient()
    return _client


def get_arcade_provider() -> ArcadeProvider:
    return ArcadeProvider(http_proxy=maiconfig.maimaidx_arcade_http_proxy)


def normalize_qrcode(text: str) -> str:
    """校验并规整二维码文本"""
    code = text.strip()
    if not code.upper().startswith(QRCODE_PREFIX) or len(code) <= len(QRCODE_PREFIX):
        raise QRCodeFormatError
    return code


def build_upload_identifier(user: User) -> tuple[PlayerIdentifier, ServiceName]:
    """根据用户选定的数据源构造上传用的身份

    水鱼使用个人「导入 Token」，落雪优先使用个人 API 密钥，
    未提供密钥时退回到「开发者 Token + 好友码」。
    """
    if user.service == ServiceName.DIVINGFISH:
        if not user.divingfish_import_token:
            raise UploadCredentialNotFoundError(ServiceName.DIVINGFISH)
        return PlayerIdentifier(credentials=user.divingfish_import_token), user.service

    if user.lxns_personal_token:
        return PlayerIdentifier(credentials=user.lxns_personal_token), user.service
    if lxnsconfig.lxns_dev_token and user.friend_code:
        return PlayerIdentifier(friend_code=user.friend_code), user.service
    raise UploadCredentialNotFoundError(ServiceName.LXNS)


def get_upload_provider(service: ServiceName) -> DivingFishProvider | LXNSProvider:
    if service == ServiceName.DIVINGFISH:
        return DivingFishProvider()
    return LXNSProvider(developer_token=lxnsconfig.lxns_dev_token)


async def fetch_arcade_scores(qrcode: str) -> list[Score]:
    """用二维码从舞萌 title 服务器拉取全部成绩"""
    client = get_client()
    provider = get_arcade_provider()
    identifier = await client.qrcode(
        normalize_qrcode(qrcode), http_proxy=maiconfig.maimaidx_arcade_http_proxy
    )
    scores = await provider.get_scores_all(identifier, client)
    return scores


async def upload_scores(user: User, qrcode: str) -> UploadResult:
    """拉取二维码对应玩家的成绩并上传至其选定的查分器"""
    identifier, service = build_upload_identifier(user)
    scores = await fetch_arcade_scores(qrcode)
    await get_client().updates(
        identifier, scores, provider=get_upload_provider(service)
    )
    return UploadResult(service=service, count=len(scores))
