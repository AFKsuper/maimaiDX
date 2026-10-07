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
from typing import Any, Awaitable, Dict, List, Optional, Tuple, Type, TypeVar

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

MSG_AIME = "二维码无效或已过期，请重新扫码"
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
        raise MsuInputError("二维码无效或已过期，请重新扫码（内容需以 SGWCMAID 开头）")
    identifier = await with_timeout(
        _get_client().qrcode(qr_content.strip(), http_proxy=http_proxy),
        TIMEOUTS["qrcode"],
    )
    creds = getattr(identifier, "credentials", None)
    if not creds:
        raise MsuInputError("二维码兑换失败：未返回凭据，请重新扫码")
    logger.info("二维码兑换成功 creds=%s", mask(str(creds)))  # 脱敏，绝不打明文
    return str(creds)


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
                        timeout: float = 120) -> Dict[str, Any]:
    """
    把成绩并行上传到两平台（asyncio.gather，平台间互不影响）。

    凭据优先级：
    - 水鱼：divingfish_token（手动 Import-Token）优先；否则 dfconfig.oauth_enabled 且
      qq 给定 → DivingFishProvider(client_id/secret) + credentials='ref:'+sha256(...) 走
      on-behalf-of 自动换票；两者都无 → 该平台记 skipped。
    - 落雪：lxns_access_token（JWT，走 user API + Bearer）优先；回退 lxns_secret
      （API-Secret，走 X-User-Token）；都无 → 该平台记 skipped。
    - 单平台异常互不影响；msg 经 friendly_message 翻译成中文。

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
    if divingfish_token:
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
    "MsuError", "MsuInputError", "MsuTimeoutError",
    # 便于上层做 except 特判（按需引用，不必全部 try）
    "AimeServerError", "TitleServerBlockedError", "TitleServerNetworkError",
    "PrivacyLimitationError", "InvalidPlayerIdentifierError",
    "InvalidDeveloperTokenError", "RateLimitError",
]
