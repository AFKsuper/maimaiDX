# -*- coding: utf-8 -*-
"""
maimaiDX/core/uploader.py —— 机台绑定 / 成绩拉取 / 上传（自 maimai-update/core/service.py 融入）

职责：二维码换绑定、拉成绩、上传成绩到水鱼(diving-fish)/落雪(lxns)。
本模块是整个 maimaiDX 包里唯一允许 import maimai_py 的地方；缺依赖时
模块顶层不崩，函数内抛带中文提示的 MsuError。

【maimai-py 关键事实（已核实，本机 1.5.3）】
- import 名 maimai_py（官方文档写的 `from maimai import ...` 是官方笔误）。
- MaimaiClient 是进程级单例：二次构造只发 warning 且构造参数被忽略，
  所以本模块只懒创建一个全局 _client，所有方法复用它。
- `await _client.qrcode(qr_str, http_proxy=None) -> PlayerIdentifier`：
  参数必须以 SGWCMAID 开头，否则抛 AimeServerError；返回 .credentials 是加密 userId，
  永不过期可持久化（由调用方存 arcade_store）。
- `await _client.scores(identifier, provider=ArcadeProvider(...)) -> MaimaiScores`：
  identifier 必填首位参数；ArcadeProvider 无需 token，支持 http_proxy；
  内部先拉曲目表，首次调用较慢。
  .scores 是成绩列表，另有 .rating / .rating_b35 / .rating_b15。
- `await _client.updates(identifier, scores.scores, provider=...)`：
  第二参是成绩列表（不是 MaimaiScores 对象）；provider 只能是 DivingFishProvider 或 LXNSProvider；
  返回 None，失败抛异常。
- provider 参数默认值是无 token 的 LXNSProvider()，省略会抛 InvalidDeveloperTokenError，必须显式传。
- 上传走"个人 API"，不需要 developer_token：个人凭据放在 identifier.credentials
  （水鱼 Import-Token / 落雪 API-Secret）。
- 【OAuth 事实一】credentials 以 ref:/sub:/username: 开头会自动走 on-behalf-of 换票：
  水鱼 OAuth 用 DivingFishProvider(client_id=..., client_secret=...) +
  PlayerIdentifier(credentials='ref:'+sha256(f'{client_id}:{qq}'))，maimai-py 自己带
  Bearer POST /player/update_records，无需本层处理 token；前提是授权 scope 含
  prober.records.write，否则换票返回 consent_required（被翻译成 PlayerNotAuthorizedError）。
- 【OAuth 事实二】credentials 是 JWT 三段式
  （^[a-zA-Z0-9-_]+\\.[a-zA-Z0-9-_]+\\.[a-zA-Z0-9-_]+$）会被识别为落雪 access_token，
  走 user API + Authorization: Bearer；否则（API-Secret）走 X-User-Token。
  LXNSProvider() 不传 developer_token 时其 .headers property 会抛
  InvalidDeveloperTokenError，但 credentials（user API）路径不碰 headers——
  不要画蛇添足传 developer_token。
- 【http_proxy】只有 ArcadeProvider（qrcode / scores 链路）接受 http_proxy 参数；
  DivingFishProvider / LXNSProvider（上传链路）构造签名没有 http_proxy，
  上传走 MaimaiClient 共享的 httpx.AsyncClient，代理需要在客户端级别配置。
"""

import asyncio
import hashlib
import logging
import re
import struct
from typing import Any, Awaitable, Dict, List, Optional, Tuple, Type, TypeVar, Union

from ..config import dfconfig
from .arcade_store import mask

logger = logging.getLogger("maimaiDX.uploader")

T = TypeVar("T")

# ---------- 自定义异常：message 即面向用户的中文提示 ----------

class MsuError(Exception):
    """maimai 上传业务异常基类；str(e) 就是可直接发给用户的中文提示。"""


class MsuInputError(MsuError):
    """入参校验失败（如二维码前缀不对、凭据为空）。"""


class MsuTimeoutError(MsuError, TimeoutError):
    """整体超时（asyncio.wait_for 触发）。"""


# ---------- maimai_py 懒加载（模块顶层绝不因缺依赖而崩） ----------

_MAIMAI_MISSING: Optional[str] = None  # 缺依赖时记中文提示，函数入口据此抛 MsuError

try:
    from maimai_py import (
        MaimaiClient,
        ArcadeProvider,
        DivingFishProvider,
        LXNSProvider,
        PlayerIdentifier,
    )
    from maimai_py.exceptions import (
        AimeServerError,
        TitleServerBlockedError,
        TitleServerNetworkError,
        PrivacyLimitationError,
        InvalidPlayerIdentifierError,
        InvalidDeveloperTokenError,
        RateLimitError,
    )
except ImportError:  # 缺依赖：给中文提示，函数入口抛 MsuError，模块本身可正常 import
    _MAIMAI_MISSING = "缺少依赖 maimai-py（import 名 maimai_py），请先安装：pip install maimai-py"
    MaimaiClient = ArcadeProvider = DivingFishProvider = LXNSProvider = PlayerIdentifier = None  # type: ignore
    AimeServerError = TitleServerBlockedError = TitleServerNetworkError = None  # type: ignore
    PrivacyLimitationError = InvalidPlayerIdentifierError = None  # type: ignore
    InvalidDeveloperTokenError = RateLimitError = None  # type: ignore

# 水鱼 OAuth 换票未完成（consent_required 的翻译结果）；异常类名做防御式导入，
# 万一 maimai_py.exceptions 没有它也不能让整个模块导入崩掉。
try:
    from maimai_py.exceptions import PlayerNotAuthorizedError  # type: ignore
except ImportError:  # pragma: no cover —— 老版本 maimai-py 可能没有该类
    PlayerNotAuthorizedError = None  # type: ignore[assignment]

# 好友码绑定要用 ffi 底层的 rival 免会话接口（maimai_py 没有暴露它）。
# 缺依赖不影响别的功能：probe_friend_code 入口会抛中文提示。
try:
    import maimai_ffi.request as _ffi_request  # type: ignore
except ImportError:  # pragma: no cover
    _ffi_request = None  # type: ignore[assignment]


def _require_maimai() -> None:
    """确认 maimai_py 可用；缺失抛带中文安装提示的 MsuError（不让用户看到裸 ModuleNotFoundError）。"""
    if _MAIMAI_MISSING:
        raise MsuError(_MAIMAI_MISSING)


# ---------- 可配整体超时（机台链路慢；签名固定，故用模块级配置调参） ----------

TIMEOUTS: Dict[str, float] = {
    "qrcode": 60.0,   # 二维码换绑定
    "scores": 180.0,  # 拉成绩（首次要先拉曲目表，给足余量）
}


def set_default_timeouts(qrcode: Optional[float] = None,
                         scores: Optional[float] = None) -> None:
    """调整模块级整体超时（秒）；传 None 表示不改。"""
    if qrcode is not None:
        TIMEOUTS["qrcode"] = float(qrcode)
    if scores is not None:
        TIMEOUTS["scores"] = float(scores)


# ---------- 网络异常集合（httpx/httpcore 兜底，均可能未装/版本差异，做防御式收集） ----------

_NET_ERRS: Tuple[Type[BaseException], ...] = ()
try:
    import httpx as _httpx
    _NET_ERRS += (_httpx.HTTPError,)  # 覆盖 TransportError / TimeoutException / HTTPStatusError
except ImportError:
    _httpx = None
try:
    import httpcore as _httpcore
    _NET_ERRS += tuple(
        c for c in (getattr(_httpcore, "HTTPError", None),
                    getattr(_httpcore, "TransportError", None))
        if isinstance(c, type)
    )
except ImportError:
    _httpcore = None
_NET_ERRS += (ConnectionError, OSError)


# ---------- 异常 → 用户可读中文提示（对上层唯一的翻译入口） ----------

MSG_AIME = "二维码兑换失败：机台服务器返回未知错误，请稍后重试"
# errorID 54（UnknownError）曾长期占据：2026-10-20 华立给 AimeDB 加 Chime 强校验后，
# /wc_aime/api/get_data 对任何请求恒回 54。已于 2026-11-01 由 maimai-ffi 0.7.2 修复
# （https + titlekey 字段 + 紧凑 JSON），现在 54 只会在极旧版本 ffi 上出现。
MSG_AIME_54 = (
    "二维码兑换失败：机台服务器暂时无法验证二维码"
    "（华立 AimeDB 接口异常，errorID 54）。\n"
    "这通常意味着 maimai-py / maimai-ffi 版本过旧，请联系管理员升级到 1.6.1+。\n"
    "已绑定过的账号不受影响，自动上传照常运行。"
)
# errorID 1（ReaderSetupFail）：读卡器侧拒绝，通常是二维码本身无效/已过期/非本机种。
# 这才是真正该让用户「重新扫码」的情形，旧版把 54 也翻成这句属于误导。
MSG_AIME_1 = (
    "二维码无效或已过期，请重新扫码。\n"
    "注意：二维码有效时间很短，扫完请立刻发给机器人，不要隔几分钟再发。"
)
_AIME_ERROR_HINTS: Dict[int, str] = {1: MSG_AIME_1, 54: MSG_AIME_54}
_AIME_ID_RE = re.compile(r"errorID\s*[:：]?\s*(\d+)", re.IGNORECASE)


def aime_error_id(exc: BaseException) -> Optional[int]:
    """从 AimeServerError 的文本里抠 errorID（ffi 不提供结构化字段，只能正则）。"""
    try:
        m = _AIME_ID_RE.search(str(exc))
    except Exception:  # noqa: BLE001 —— 异常对象异常时不影响上层提示
        return None
    return int(m.group(1)) if m else None

MSG_BLOCKED = "机台服务器拒绝了请求(IP可能被封)，稍后再试或配置 http_proxy"
MSG_NET_TITLE = "连接机台服务器失败，请稍后再试"
MSG_PRIVACY = "落雪新用户需先用落雪官方代理上传一次并同意隐私设置，否则无法用 API 上传"
MSG_BAD_IDENTIFIER = "绑定凭据无效，请重新绑定"
MSG_BAD_TOKEN = "token 已失效，请重新绑定"
MSG_RATE = "接口限流，请稍后再试"
# maimai-py 把 on-behalf-of 换票的 consent_required 翻译成 PlayerNotAuthorizedError。
# 这句话按 .env 的 DIVINGFISH_SCOPE 是否含 prober.records.write 分流：
# - 含写入 → 缺的只是用户本人授权，直接给绑定方法（dfbind）；
# - 不含写入 → 用户怎么授权都传不上去，这句是给管理员的 .env 配置提示。
MSG_OAUTH_NOT_AUTHORIZED = (
    "水鱼 OAuth 未完成授权或授权范围不含写入"
    "（.env 需在 DIVINGFISH_SCOPE 加 prober.records.write 并重新授权）"
)
MSG_OAUTH_BIND_GUIDE = (
    "水鱼授权未完成。\n"
    "请发送「dfbind」（或 水鱼授权码）完成授权，再重新发送 mai上传"
    "（群聊、私聊均可）。\n"
    "如果之前已经授权过仍失败，请联系机器人管理员："
    ".env 的 DIVINGFISH_SCOPE 需包含 prober.records.write 且应用过审后重新授权。"
)


def _oauth_error_message() -> str:
    """水鱼换票 consent_required 的提示：按 .env 的 scope 配置分流（见上两条注释）。

    .env 配好了（应用凭据齐 + scope 含 prober.records.write）→ 缺的只是用户
    自己发一次 dfbind，给绑定方法；没配好 → 用户做什么都没用，给管理员的 .env 提示。
    """
    try:
        if dfconfig.oauth_enabled and (
            "prober.records.write" in dfconfig.divingfish_oauth_scope
        ):
            return MSG_OAUTH_BIND_GUIDE
    except Exception:  # noqa: BLE001 —— 配置对象异常时退回保守文案
        pass
    return MSG_OAUTH_NOT_AUTHORIZED

_FAMILIES: Tuple[Tuple[Type[BaseException], str], ...] = tuple(
    (cls, msg)
    for cls, msg in (
        (AimeServerError, MSG_AIME),
        (TitleServerBlockedError, MSG_BLOCKED),
        (TitleServerNetworkError, MSG_NET_TITLE),
        (PrivacyLimitationError, MSG_PRIVACY),
        (InvalidPlayerIdentifierError, MSG_BAD_IDENTIFIER),
        (InvalidDeveloperTokenError, MSG_BAD_TOKEN),
        (RateLimitError, MSG_RATE),
        # PlayerNotAuthorizedError 由 friendly_message 按 .env 配置分流处理，不在这里静态映射
    )
    if cls is not None
)


def friendly_message(exc: BaseException) -> str:
    """
    把任意异常翻译成可直接发给用户的中文提示。
    上层只需 `except Exception as e: msg = friendly_message(e)`，不要把堆栈裸抛给用户。
    """
    # 水鱼 OAuth 未完成授权：文案按 .env 的 scope 配置分流，不走 _FAMILIES 静态映射
    if PlayerNotAuthorizedError is not None and isinstance(exc, PlayerNotAuthorizedError):
        return _oauth_error_message()
    # AimeServerError 按 errorID 分流：54 是华立侧故障，不是「二维码过期」。
    # 不依赖 _FAMILIES（那里是静态元组，缺依赖时为 None 会被过滤掉）。
    if AimeServerError is not None and isinstance(exc, AimeServerError):
        eid = aime_error_id(exc)
        if eid is not None:
            hint = _AIME_ERROR_HINTS.get(eid)
            if hint:
                return hint
            return "%s（errorID %d）" % (MSG_AIME, eid)
        return MSG_AIME
    for cls, msg in _FAMILIES:
        if isinstance(exc, cls):
            return msg
    if isinstance(exc, MsuError):        # 自定义异常：message 已是中文
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "操作超时，请稍后再试"
    if _NET_ERRS and isinstance(exc, _NET_ERRS):
        return "网络请求失败或超时，请稍后再试"
    text = str(exc).strip()
    return ("操作失败：%s" % text) if text else type(exc).__name__


# ---------- 带 asyncio.wait_for 的整体超时包装 ----------

async def with_timeout(awaitable: Awaitable[T], timeout: float = 60.0) -> T:
    """
    整体超时包装：超时抛 MsuTimeoutError（friendly_message 会给中文提示）。
    用法：`await with_timeout(client.xxx(...), 60)`
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise MsuTimeoutError("操作超时（%.0f 秒），请稍后再试" % timeout) from e


# ---------- 进程级唯一 client（二次构造会被忽略，务必复用；懒创建避免缺依赖时崩） ----------

_client: Any = None


def _get_client() -> Any:
    """取进程级唯一 MaimaiClient（懒创建）；缺 maimai_py 抛 MsuError 中文提示。"""
    global _client
    _require_maimai()
    if _client is None:
        _client = MaimaiClient()
    return _client


# ---------- 对外接口 1：扫码绑定 ----------

async def bind_arcade(qr_content: str, http_proxy: Optional[str] = None) -> str:
    """
    用机台二维码内容兑换凭据，返回加密 userId（identifier.credentials 字符串），由调用方存库。

    - qr_content：SGWCMAID 开头的二维码内容（可用 core.arcade_qr.extract_from_text 先提取）。
    - http_proxy：透传给 ArcadeProvider（qrcode 链路支持代理）。
    - 失败抛 maimai_py 异常（AimeServerError 等）或 Msu* 异常；
      用 friendly_message(e) 转成中文提示。
    """
    _require_maimai()
    if not qr_content or "SGWCMAID" not in qr_content.upper():
        raise MsuInputError("二维码内容格式不正确：需以 SGWCMAID 开头，请重新扫码")
    identifier = await with_timeout(
        _get_client().qrcode(qr_content.strip(), http_proxy=http_proxy),
        TIMEOUTS["qrcode"],
    )
    creds = getattr(identifier, "credentials", None)
    if not creds:
        raise MsuInputError("二维码兑换失败：未返回凭据，请重新扫码")
    logger.info("二维码兑换成功 creds=%s", mask(str(creds)))  # 脱敏，绝不打明文
    return str(creds)


# ---------- 对外接口 1-B：好友码绑定（绕开已失效的二维码兑换） ----------
#
# 背景：2026-10-20 起华立给二维码兑换链路加了 Chime 强校验，
# `http://ai.sys-allnet.cn/wc_aime/api/get_data` 对任何请求恒回 errorID 54，
# 这条路整条死掉（详见 MSG_AIME_54 注释）。但机台游戏服务
# `maimai-gm.wahlap.com:42081/Maimai2Servlet` 一直正常 —— 它只认一样东西：
# 用固定 Fernet 密钥加密的 userId。
#
# 而「好友码」就是机台 userId（实测：好友码 11619035 拉到的 1325 条成绩，
# 与该号在水鱼的数据 rating 完全一致；好友码不需要先加好友，也不需要扫码）。
# 所以改让用户报好友码，本地生成凭据，完全不碰 wc_aime。
#
# 说明：`_CREDS_FERNET_KEY` 与 maimai_ffi 内部用的常量相同，用它加密
# struct.pack(">Q", userId) 得到的字符串，和原先扫码换出来的 creds 等价，
# 可直接喂给 fetch_scores / upload_scores。

_CREDS_FERNET_KEY = b"C4H2HV3vIbMFzLJvosbie9qDEqH32zkqPZYr8cJUiDg="

# 好友码：机台个人资料里的纯数字编号，实测 8 位；放宽到 8–10 位以兼容扩容。
# 用 [0-9] 而不是 \d：Python 的 \d 会匹配全角数字（１、２），int() 却解不了。
FRIEND_CODE_RE = re.compile(r"^[0-9]{8,10}$")

MSG_FRIEND_CODE_NOT_FOUND = (
    "好友码不存在：机台服务器查不到这个好友码。\n"
    "请打开舞萌个人资料页核对「好友码」（8 位数字），重新发送。"
)

MSG_FRIEND_CODE_BAD_INPUT = (
    "好友码格式不正确：应为 8 位数字（机台个人资料页里的「好友码」）。"
)


def friend_code_to_creds(user_id: int) -> str:
    """把机台 userId（= 好友码）编码成可持久化的机台凭据字符串。

    等价于原二维码兑换的产物，只是不需要联网。
    """
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415 —— 缺依赖时给中文提示
    except ImportError as e:  # pragma: no cover
        raise MsuError(
            "缺少依赖 cryptography，请先安装：pip install cryptography"
        ) from e
    token = Fernet(_CREDS_FERNET_KEY).encrypt(struct.pack(">Q", int(user_id)))
    return token.decode("ascii")


async def probe_friend_code(user_id: int,
                            http_proxy: Optional[str] = None) -> Optional[str]:
    """用 rival 免会话接口确认好友码存在，返回机台昵称；查不到返回 None。

    该接口不需要二维码、不需要登录会话，只按 userId 查。服务器 IP 被华立
    屏蔽，所以必须带 http_proxy（手机跳板），否则抛 TitleServerBlockedError。
    """
    _require_maimai()
    if _ffi_request is None:  # pragma: no cover
        raise MsuError("缺少依赖 maimai-ffi，请先安装：pip install maimai-ffi")
    uid = int(user_id)
    payload = {"rivalId": uid, "userId": uid}

    async def _call() -> Any:
        gen = _ffi_request.AsyncClientGenerator(http_proxy=http_proxy)
        async with gen as pool:
            return await _ffi_request.request(
                "GetUserRivalDataApi", payload, pool, uid
            )

    data = await with_timeout(_call(), TIMEOUTS["qrcode"])
    rival = (data or {}).get("userRivalData") or {}
    name = rival.get("rivalName")
    if not name or not str(name).strip():
        return None
    return str(name).strip()


async def bind_by_friend_code(friend_code: str,
                              http_proxy: Optional[str] = None) -> Tuple[str, str]:
    """用好友码完成绑定，返回 (credentials, 机台昵称)。

    - friend_code：8 位数字字符串（可含首尾空白）。
    - http_proxy：机台域名对服务器 IP 屏蔽，必须传手机跳板代理。
    - 好友码不存在抛 MsuInputError(MSG_FRIEND_CODE_NOT_FOUND)；
      格式错抛 MsuInputError(MSG_FRIEND_CODE_BAD_INPUT)。
    """
    _require_maimai()
    code = str(friend_code or "").strip()
    if not FRIEND_CODE_RE.match(code):
        raise MsuInputError(MSG_FRIEND_CODE_BAD_INPUT)
    uid = int(code)
    nickname = await probe_friend_code(uid, http_proxy=http_proxy)
    if nickname is None:
        raise MsuInputError(MSG_FRIEND_CODE_NOT_FOUND)
    creds = friend_code_to_creds(uid)
    logger.info("好友码绑定成功 user_id=%s nickname=%s creds=%s",
                uid, nickname, mask(creds))
    return creds, nickname


# ---------- 对外接口 2：拉成绩 ----------

async def fetch_scores(creds: str,
                       http_proxy: Optional[str] = None) -> Tuple[List[Any], Dict[str, Any]]:
    """
    用持久化的机台凭据拉取成绩。

    返回 (score_list, summary)：
    - score_list：成绩列表（即 MaimaiScores.scores，list[ScoreExtend]）。
    - summary：{"rating", "score_count", "rating_b35", "rating_b15"}。
    失败抛 maimai_py 异常 / Msu* 异常，用 friendly_message(e) 翻译。
    """
    _require_maimai()
    if not creds:
        raise MsuInputError("绑定凭据无效，请重新绑定")
    identifier = PlayerIdentifier(credentials=creds)
    provider = ArcadeProvider(http_proxy=http_proxy) if http_proxy else ArcadeProvider()
    # 必须显式传 provider：默认 LXNSProvider() 无 token 会抛 InvalidDeveloperTokenError
    maimai_scores = await with_timeout(
        _get_client().scores(identifier, provider=provider),
        TIMEOUTS["scores"],
    )
    score_list = list(getattr(maimai_scores, "scores", None) or [])
    summary = {
        "rating": getattr(maimai_scores, "rating", None),
        "score_count": len(score_list),
        "rating_b35": getattr(maimai_scores, "rating_b35", None),
        "rating_b15": getattr(maimai_scores, "rating_b15", None),
    }
    logger.info("拉取成绩完成：%d 条 rating=%s", len(score_list), summary["rating"])
    return score_list, summary


# ---------- 对外接口 3：并行上传 ----------

def _df_subject_ref(client_id: str, qq: Any) -> str:
    """
    水鱼 on-behalf-of 换票用的 subject 凭据：'ref:' + sha256(f'{client_id}:{qq}')（小写 64 位 hex）。

    与 maimai-update/core/oauth.py 的 df_subject_ref、目标仓库
    core/clients/divingfish/oauth.py 的 subject_ref 公式一致（同一 client_id + QQ 恒定映射）。
    """
    raw = "%s:%s" % (client_id, qq)
    return "ref:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _df_provider() -> Any:
    """构造水鱼 provider：dfconfig OAuth 配置齐全时带上 client_id/client_secret（on-behalf-of 换票用）。"""
    if dfconfig.oauth_enabled:
        return DivingFishProvider(
            client_id=dfconfig.divingfish_client_id,
            client_secret=dfconfig.divingfish_client_secret,
        )
    return DivingFishProvider()


async def _upload_one(provider: Any, identifier: Any,
                      score_list: List[Any], timeout: float) -> Dict[str, Any]:
    """单平台上传：内部兜住所有异常，绝不让一个平台的失败影响另一个平台。"""
    try:
        await asyncio.wait_for(
            _get_client().updates(identifier, score_list, provider=provider),  # 第二参是成绩列表
            timeout,
        )
        return {"ok": True, "msg": "成功 %d 条" % len(score_list)}
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "msg": "上传超时（%.0f 秒），请稍后再试" % timeout}
    except Exception as e:
        msg = friendly_message(e)
        logger.warning("上传失败：%s", msg)  # 只记中文提示，不打凭据/堆栈进日志
        return {"ok": False, "msg": msg}


async def upload_scores(score_list: List[Any],
                        qq: Optional[Any] = None,
                        lxns_access_token: Optional[str] = None,
                        lxns_secret: Optional[str] = None,
                        divingfish_token: Optional[str] = None,
                        http_proxy: Optional[str] = None,
                        timeout: float = 120,
                        disable_divingfish: bool = False) -> Dict[str, Any]:
    """
    把成绩并行上传到两平台（asyncio.gather，平台间互不影响）。

    凭据优先级：
    - 水鱼：divingfish_token（手动 Import-Token）优先；否则 dfconfig.oauth_enabled 且
      qq 给定 → DivingFishProvider(client_id/secret) + credentials='ref:'+sha256(...) 走
      on-behalf-of 自动换票；两者都无 → 该平台记 skipped。
    - 落雪：lxns_access_token（JWT，走 user API + Bearer）优先；回退 lxns_secret
      （API-Secret，走 X-User-Token）；都无 → 该平台记 skipped。
    - 单平台异常互不影响；msg 经 friendly_message 翻译成中文。

    disable_divingfish：调用方已经确认「水鱼这个 QQ 写不了」（未授权 / 授权范围不含
    prober.records.write）时置 True，直接把水鱼记成 skipped，不再白发一次注定失败的
    请求，也让 report_ok 的「参与平台」不含水鱼。

    http_proxy：上传链路的 DivingFish/LXNS provider 构造签名无 http_proxy 参数
    （见模块 docstring），此参数仅透传给 Arcade 链路调用方使用，上传本身不受影响。

    返回：
    - 正常 → {"divingfish": {"ok", "msg", ...}, "lxns": {...}}（skipped 平台 ok=False 且带 "skipped"）；
    - 入参错误（成绩列表为空 / maimai_py 缺失） → {"error": "…"}。
    """
    if not score_list:
        return {"error": "成绩列表为空，无需上传"}
    if _MAIMAI_MISSING:
        return {"error": _MAIMAI_MISSING}

    jobs: List[Tuple[str, Awaitable[Dict[str, Any]]]] = []
    report: Dict[str, Any] = {}

    # 水鱼凭据：手动 Import-Token > OAuth（ref: subject）
    if disable_divingfish and not divingfish_token:
        report["divingfish"] = {"ok": False, "skipped": True,
                                "msg": "水鱼上传权限不可用，已跳过"}
    elif divingfish_token:
        jobs.append(("divingfish", _upload_one(
            _df_provider(),
            PlayerIdentifier(credentials=divingfish_token),  # 个人 API：Import-Token 走 credentials
            score_list, timeout)))
    elif dfconfig.oauth_enabled and qq is not None:
        oauth_id = str(dfconfig.divingfish_client_id or "")
        jobs.append(("divingfish", _upload_one(
            _df_provider(),
            # credentials 为 ref: 开头 → maimai-py 自动 on-behalf-of 换票并带 Bearer
            PlayerIdentifier(credentials=_df_subject_ref(oauth_id, qq)),
            score_list, timeout)))
    else:
        report["divingfish"] = {"ok": False, "skipped": True,
                                "msg": "未提供水鱼凭据（Import-Token 或 OAuth 未启用），已跳过"}

    # 落雪凭据：OAuth JWT > API-Secret
    if lxns_access_token:
        jobs.append(("lxns", _upload_one(
            LXNSProvider(),  # 不传 developer_token：credentials(user API) 路径不碰 headers
            PlayerIdentifier(credentials=lxns_access_token),  # JWT 三段式 → user API + Bearer
            score_list, timeout)))
    elif lxns_secret:
        jobs.append(("lxns", _upload_one(
            LXNSProvider(),
            PlayerIdentifier(credentials=lxns_secret),  # 个人 API：API-Secret 走 credentials
            score_list, timeout)))
    else:
        report["lxns"] = {"ok": False, "skipped": True,
                          "msg": "未提供落雪凭据（access_token 或 secret），已跳过"}

    # 并行上传，return_exceptions 兜底（_upload_one 内部已 try，此处双保险）
    results = await asyncio.gather(*(c for _, c in jobs), return_exceptions=True)
    for (name, _), res in zip(jobs, results):
        if isinstance(res, BaseException):
            report[name] = {"ok": False, "msg": friendly_message(res)}
        else:
            report[name] = res
    return report


def report_ok(report: Dict[str, Any]) -> bool:
    """判断 upload_scores 返回的报告是否“有平台且全部成功”。"""
    if not report or "error" in report:
        return False
    return all(isinstance(v, dict) and v.get("ok") for v in report.values())


__all__ = [
    "bind_arcade", "fetch_scores", "upload_scores", "report_ok",
    "with_timeout", "friendly_message", "set_default_timeouts", "TIMEOUTS",
    # 好友码绑定（绕过已失效的二维码兑换）
    "bind_by_friend_code", "probe_friend_code", "friend_code_to_creds",
    "FRIEND_CODE_RE", "MSG_FRIEND_CODE_NOT_FOUND", "MSG_FRIEND_CODE_BAD_INPUT",
    "MSG_AIME_54", "aime_error_id",
    "MsuError", "MsuInputError", "MsuTimeoutError",
    # 便于上层做 except 特判（按需引用，不必全部 try）
    "AimeServerError", "TitleServerBlockedError", "TitleServerNetworkError",
    "PrivacyLimitationError", "InvalidPlayerIdentifierError",
    "InvalidDeveloperTokenError", "RateLimitError",
]
