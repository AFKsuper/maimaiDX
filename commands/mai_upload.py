# -*- coding: utf-8 -*-
"""
成绩上传命令层（源自独立插件 maimai-update 融入 maimaiDX）

命令：
- mai绑定（别名 舞萌上传绑定 / 裸发 SGWCMAID 文本）——二维码换机台凭据
- mai绑定状态 —— 机台凭据 + 水鱼/落雪授权状态（凭据脱敏）
- mai上传 —— 拉取机台成绩并上传到已设置平台
- 自动上传成绩 —— 开关探测式自动更新（每人独立，重发即切换开/关）
  成绩有变化才上传（hot/warm/cold 分层探测），每天 03:50 兜底整体同步一次
- mai帮助 —— 查看本帮助

隐私：mai绑定 的回复会提醒用户撤回消息。

授权复用 maimaiDX 现有 OAuth 流程（dfbind / lxbind / 水鱼授权码 / 落雪授权码），
本模块不搬 mai绑定水鱼 / mai绑定落雪，也不提供手动 Import-Token / API-Secret
（用户明确不需要手动备用凭据）。

core 层（arcade_qr / arcade_store / uploader）并行开发可能未就绪，
导入失败只降级为「相关功能不可用」，绝不让模块加载崩掉。
"""

import asyncio
import base64
import hashlib
import json
import time
from typing import Optional

import httpx
from nonebot import NLPSession, NoneBot, get_bot, on_natural_language

from hoshino.typing import CQEvent

from ..config import dfconfig, log, lxnsconfig, maiconfig, sv

# ============================================================
# core 导入：失败不崩，运行时按需给安装提示
# ============================================================
_core_ok = True
_core_error = None
try:
    from ..core.arcade_qr import decode_image, extract_from_text
    from ..core.arcade_store import all as store_all  # 全部绑定记录（自动上传遍历用）
    from ..core.arcade_store import get, mask, now_str, set  # noqa: A001 —— set 即 arcade_store.set
    from ..core.uploader import (
        MsuError,
        MsuInputError,
        MsuTimeoutError,
        bind_arcade,
        fetch_scores,
        upload_scores,
    )
    from ..core.clients.divingfish.models.oauth import DivingFishScope
except Exception as e:  # noqa: BLE001
    _core_ok = False
    _core_error = e
    extract_from_text = None
    decode_image = None
    get = set = mask = now_str = None  # type: ignore[assignment]
    store_all = None  # type: ignore[assignment]  # core 缺失时任务提前返回，不会调用
    bind_arcade = fetch_scores = upload_scores = None  # type: ignore[assignment]

    class MsuError(Exception):
        pass

    class MsuInputError(MsuError):
        pass

    class MsuTimeoutError(MsuError, TimeoutError):
        pass

    class DivingFishScope:  # 占位，属性访问全部返回 0/False，不参与真实判断
        PROBER_RECORDS_WRITE = 8


# 错误翻译：uploader 自带中文翻译层，缺失时退化为 类名: 信息
try:
    from ..core.uploader import friendly_message as _msu_friendly
except Exception:  # noqa: BLE001
    _msu_friendly = None

# 用户数据库（落雪 token 查询/续期）：缺失时仅授权状态查询不可用
_db_ok = True
try:
    from ..core.clients.exceptions import UserNotBindError
    from ..core.clients.lxns.client import OAuth2
    from ..core.clients.lxns.models.oauth import BaseToken
    from ..core.database.qq import get_user, update_user
except Exception as e:  # noqa: BLE001
    _db_ok = False
    log.warning(f"[mai_upload] 用户数据库模块导入失败（{e}），落雪授权状态查询不可用")
    get_user = update_user = None  # type: ignore[assignment]

    class UserNotBindError(Exception):
        pass

    class OAuth2:  # 占位，_db_ok 为 False 时不会被调用
        pass

    class BaseToken:  # 占位
        pass


if not _core_ok:
    log.error(
        f"[mai_upload] core 模块导入失败（{_core_error}）；"
        "相关命令将提示依赖未就绪，请检查 core/ 与 requirements.txt"
    )


# ============================================================
# 帮助文本
# ============================================================
SV_HELP = """【maimaiDX 舞萌成绩上传】
1. mai绑定 <二维码图片或SGWCMAID文本>
   绑定机台账号，只需首次；凭据长期有效
2. 上传授权（OAuth，授权复用本插件现有绑定）：
   水鱼：发送 dfbind（或 水鱼授权码）完成授权
   落雪：发送 lxbind（或 落雪授权码）完成授权
   （授权命令只能在群里发送）
3. mai绑定状态 —— 查看绑定情况（凭据脱敏）
4. mai上传 —— 拉取机台成绩并上传到已设置平台
5. 自动上传成绩 —— 开关探测式自动更新（重发即切换）
   成绩有变化才上传（刚变化 15 分钟 / 近期 30 分钟 / 平时 60 分钟探测一次）
   每天 03:50 再兜底整体同步一次；自动上传成绩 开 / 关 / 状态
mai帮助 查看本帮助"""

# ============================================================
# 命令注册（前缀 trie 最长匹配，mai绑定状态 优先于 mai绑定）
# ============================================================
bind = sv.on_prefix("mai绑定", "舞萌上传绑定")
bind_raw = sv.on_rex(r"^\s*SGWCMAID", normalize=False)
status = sv.on_prefix("mai绑定状态")
upload = sv.on_prefix("mai上传")
auto_upload = sv.on_prefix("自动上传成绩", "mai自动上传")
help_cmd = sv.on_prefix("mai帮助")


# ============================================================
# 小工具
# ============================================================

# 单账号进行中标记（绑定/上传防并发重复执行；不 import builtin set，避免遮蔽 arcade_store.set）
_busy: dict = {}


def _acquire(qq) -> bool:
    key = str(qq)
    if key in _busy:
        return False
    _busy[key] = True
    return True


def _release(qq) -> None:
    _busy.pop(str(qq), None)


def _proxy() -> Optional[str]:
    """可选 HTTP 代理（.env 的 MAI_HTTP_PROXY），没配返回 None。"""
    return getattr(maiconfig, "mai_http_proxy", None) or None


def _setup_hint(*need: str) -> str:
    parts = ["⚠ 插件依赖未就绪，相关功能暂不可用："]
    parts.extend(f"· {n}" for n in need)
    parts.append("请在 Hoshino 环境执行 pip install -r requirements.txt 后重启。")
    return "\n".join(parts)


def _friendly_error(e: BaseException) -> str:
    """把 core/maimai_py 的异常翻成中文可操作提示。"""
    if _msu_friendly is not None:
        try:
            return str(_msu_friendly(e))
        except Exception:  # noqa: BLE001
            pass
    return f"{type(e).__name__}: {e}"


def _jwt_expired(token: str, margin: int = 60) -> bool:
    """解 JWT payload 的 exp 判断是否将过期；解析不了按未过期处理（交给上传层报错）。"""
    try:
        parts = str(token).split(".")
        if len(parts) != 3:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0)) <= time.time() + margin
    except Exception:  # noqa: BLE001
        return False


def _recall_hint(what: str = "二维码") -> str:
    """提醒用户撤回刚发出含隐私内容的消息（绑定 / 凭据类回复统一追加）。"""
    return (
        f"\n\n🔒 为保护账号隐私，请撤回你刚发出的{what}消息"
        "（长按消息 → 撤回；群里发的可请群管理协助撤回）"
    )


# ============================================================
# 图片加载（QQ 图片 url 易过期，找到就尽快下载；兜底读本机缓存文件）
# ============================================================

def _find_image_url(ev: CQEvent) -> Optional[str]:
    for seg in ev.message:
        if seg.type == "image":
            url = seg.data.get("url") or ""
            if not url:
                file_field = str(seg.data.get("file") or "")
                if file_field.startswith(("http://", "https://")):
                    url = file_field
            if url:
                return str(url)
    return None


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _load_image_bytes(ev: CQEvent) -> Optional[bytes]:
    url = _find_image_url(ev)
    if url:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content
        except Exception as e:  # noqa: BLE001
            log.warning(f"[mai_upload] 二维码图片下载失败: {e}")
    # 兜底：file 字段是本机缓存路径时直接读
    for seg in ev.message:
        if seg.type == "image":
            file_field = str(seg.data.get("file") or "")
            if file_field and _is_local_file(file_field):
                try:
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, _read_bytes, file_field)
                except OSError as e:
                    log.warning(f"[mai_upload] 读取本机图片失败: {e}")
    return None


def _is_local_file(path: str) -> bool:
    import os

    try:
        return os.path.isfile(path)
    except OSError:
        return False


# ============================================================
# 落雪 token：读 QQ 库，过期则按 core/handler.py 范式续期
# ============================================================

async def _lxns_valid_token(qq) -> tuple[Optional[str], Optional[str]]:
    """返回 (access_token, err)。无绑定 → (None, None)；续期失败 → (None, 中文错误)。"""
    if not _db_ok:
        return None, "用户数据库不可用"
    try:
        user = await get_user(qq)
    except UserNotBindError:
        return None, None
    except Exception as e:  # noqa: BLE001
        return None, _friendly_error(e)
    if not user.access_token:
        return None, None
    if not _jwt_expired(user.access_token):
        return user.access_token, None
    if not user.refresh_token:
        return None, "落雪 token 已过期且缺少 refresh_token"
    try:
        oauth = OAuth2()
        oauth.token = BaseToken(
            access_token=user.access_token, refresh_token=user.refresh_token
        )
        new_token = await oauth.refresh_token()
        await update_user(user.qqid, token=new_token)
        log.info(f"[mai_upload] 落雪 token 已续期 qq={qq}")
        return new_token.access_token, None
    except Exception as e:  # noqa: BLE001
        return None, _friendly_error(e)


async def _lxns_token_state(qq) -> Optional[str]:
    """状态查询用：None=未绑定；否则返回 token 状态描述（不做网络续期）。"""
    if not _db_ok:
        return "查询失败（用户数据库不可用）"
    try:
        user = await get_user(qq)
    except UserNotBindError:
        return None
    except Exception as e:  # noqa: BLE001
        return f"查询失败（{type(e).__name__}）"
    if not user.access_token:
        return None
    if _jwt_expired(user.access_token):
        if user.refresh_token:
            return "已绑定（token 已过期，上传时自动续期）"
        return "已绑定（token 已过期且缺少 refresh_token）"
    return "已绑定（token 有效）"


# ============================================================
# 命令一：绑定（二维码 → 机台凭据）
# ============================================================

async def _handle_bind(bot: NoneBot, ev: CQEvent) -> None:
    qq = ev.user_id
    if not _acquire(qq):
        await bot.send(ev, "⏳ 上一个操作还在进行中，请稍候再试。", at_sender=True)
        return
    try:
        if not _core_ok:
            await bot.send(
                ev,
                _setup_hint("core.arcade_qr / core.arcade_store / core.uploader 模块") + _recall_hint(),
                at_sender=True,
            )
            return

        plain = ev.message.extract_plain_text().strip()

        # 方式一：文本里宽松提取 SGWCMAID 内容
        content = None
        if plain:
            try:
                content = extract_from_text(plain)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[mai_upload] extract_from_text 异常: {e}")
                content = None

        # 方式二：解码二维码图片（cv2 是阻塞调用，丢线程池）
        if content is None:
            data = await _load_image_bytes(ev)
            if data is None:
                await bot.send(
                    ev,
                    "没有识别到二维码。\n"
                    "用法：mai绑定 + 二维码图片，\n"
                    "或 mai绑定 SGWCMAID开头的二维码文本" + _recall_hint(),
                    at_sender=True,
                )
                return
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(None, decode_image, data)
            if content is None:
                await bot.send(
                    ev,
                    "二维码图片解码失败：请发更清晰的原图，"
                    "或改用文本方式（mai绑定 SGWCMAID开头的内容）" + _recall_hint(),
                    at_sender=True,
                )
                return

        # 调 core 完成绑定并落库
        try:
            creds = await bind_arcade(content, _proxy())
        except Exception as e:  # noqa: BLE001
            await bot.send(ev, f"❌ 绑定失败：{_friendly_error(e)}" + _recall_hint(), at_sender=True)
            return

        set(qq, arcade_creds=creds, bound_at=now_str())
        await bot.send(
            ev,
            "✅ 已绑定机台账号\n"
            "凭据已保存，后续上传无需再扫码\n"
            f"凭据：{mask(creds)}" + _recall_hint(),
            at_sender=True,
        )
    finally:
        _release(qq)


@bind
async def bind_cmd(bot: NoneBot, ev: CQEvent):
    await _handle_bind(bot, ev)


@bind_raw
async def bind_raw_qr(bot: NoneBot, ev: CQEvent):
    # 用户直接裸发以 SGWCMAID 开头的二维码文本（不带命令前缀）
    await _handle_bind(bot, ev)


# ============================================================
# 命令二：绑定状态
# ============================================================

async def _handle_status(bot: NoneBot, ev: CQEvent) -> None:
    if not _core_ok:
        await bot.send(ev, _setup_hint("core.arcade_store 模块"), at_sender=True)
        return
    qq = ev.user_id
    rec = get(qq) or {}
    lines = ["【maimaiDX 成绩上传 绑定状态】"]

    # 机台凭据
    arcade = rec.get("arcade_creds")
    if arcade:
        bound_at = rec.get("bound_at") or "未知时间"
        lines.append(f"· 机台账号：已绑定（凭据 {mask(arcade)}，{bound_at}）")
    else:
        lines.append("· 机台账号：未绑定（mai绑定 + 二维码）")

    # 水鱼授权状态：应用配置齐不齐 + 写 scope
    if dfconfig.oauth_enabled:
        has_write = bool(
            dfconfig.divingfish_scope & DivingFishScope.PROBER_RECORDS_WRITE
        )
        if has_write:
            lines.append("· 水鱼 OAuth：应用已配置（scope 含写入；用户授权发 dfbind / 水鱼授权码）")
        else:
            lines.append(
                "· 水鱼 OAuth：应用已配置，但 ⚠ DIVINGFISH_SCOPE 缺 prober.records.write"
                "（上传需补 scope、应用过审后重新授权）"
            )
    else:
        lines.append("· 水鱼 OAuth：未配置（.env 需 DIVINGFISH_CLIENT_ID / DIVINGFISH_CLIENT_SECRET）")

    # 落雪授权状态：应用配置 + QQ 库 token 有无
    lx_state = await _lxns_token_state(qq)
    lx_cfg_ok = bool(lxnsconfig.lx_client_id and lxnsconfig.lx_client_secret)
    if lx_state is None:
        if lx_cfg_ok:
            lines.append("· 落雪 OAuth：应用已配置，用户未绑定（发 lxbind / 落雪授权码）")
        else:
            lines.append("· 落雪 OAuth：未配置（.env 需 LX_CLIENT_ID / LX_CLIENT_SECRET；用户未绑定）")
    else:
        lines.append(f"· 落雪 OAuth：{lx_state}")

    # 手动备用凭据已按用户要求移除（不再支持 Import-Token / API-Secret）
    await bot.send(ev, "\n".join(lines), at_sender=True)


@status
async def status_cmd(bot: NoneBot, ev: CQEvent):
    await _handle_status(bot, ev)


# ============================================================
# 命令三：上传
# ============================================================

def _platform_line(name: str, active: bool, results: dict, skip_detail: str) -> str:
    """拼一行平台结果：未启用时说明「为什么没传」。"""
    if not active:
        return f"▫ {name}：{skip_detail}，已跳过"
    r = (results or {}).get("divingfish" if name == "水鱼" else "lxns")
    if not isinstance(r, dict):
        return f"❌ {name}：平台无返回结果"
    if r.get("ok"):
        return f"✅ {name}：{r.get('msg') or '上传成功'}"
    return f"❌ {name}：{r.get('msg') or '未知错误'}"


async def _handle_upload(bot: NoneBot, ev: CQEvent) -> None:
    qq = ev.user_id
    if not _acquire(qq):
        await bot.send(ev, "⏳ 上一个操作还在进行中，请稍候再试。", at_sender=True)
        return
    try:
        if not _core_ok:
            await bot.send(ev, _setup_hint("core.arcade_store / core.uploader 模块"), at_sender=True)
            return

        rec = get(qq) or {}
        creds = rec.get("arcade_creds")
        if not creds:
            await bot.send(
                ev,
                "⚠ 还未绑定机台账号，无法拉取成绩。\n"
                "请先发送 mai绑定 + 机台二维码（图片或 SGWCMAID 文本）完成绑定。",
                at_sender=True,
            )
            return

        # ---- 判定各平台可用性：水鱼=OAuth 应用配置；落雪=QQ 库 token ----
        proxy = _proxy()
        df_on = bool(dfconfig.oauth_enabled)
        df_skip = "" if df_on else "未配置 DIVINGFISH_CLIENT_ID/SECRET（.env）"

        lx_token, lx_err = await _lxns_valid_token(qq)
        lx_on = bool(lx_token)
        if lx_on:
            lx_skip = ""
        elif lx_err:
            lx_skip = f"落雪授权不可用（{lx_err}）"
        else:
            lx_skip = "未绑定落雪（发 lxbind / 落雪授权码）"

        if not df_on and not lx_on:
            await bot.send(
                ev,
                "⚠ 还未完成任何平台的上传授权，没有可上传的目标平台。\n"
                "· 水鱼：发送 dfbind（或 水鱼授权码）；需 .env 配置 DIVINGFISH_CLIENT_ID/SECRET\n"
                "· 落雪：发送 lxbind（或 落雪授权码）；需 .env 配置 LX_CLIENT_ID/SECRET\n"
                "（授权命令只能在群里发送）授权后重发 mai上传。",
                at_sender=True,
            )
            return

        # 拉取较慢，先回一句再执行
        await bot.send(ev, "⏳ 正在拉取成绩并上传…", at_sender=True)

        try:
            score_list, summary = await fetch_scores(creds, proxy)
        except Exception as e:  # noqa: BLE001
            await bot.send(ev, f"❌ 拉取成绩失败：{_friendly_error(e)}", at_sender=True)
            return

        try:
            results = await upload_scores(
                score_list,
                qq,
                lxns_access_token=lx_token,
                http_proxy=proxy,
            )
        except Exception as e:  # noqa: BLE001
            await bot.send(ev, f"❌ 上传失败：{_friendly_error(e)}", at_sender=True)
            return

        # core 在成绩列表为空等情况下返回 {"error": "..."}（键 error，而非平台结果）
        if isinstance(results, dict) and "error" in results:
            await bot.send(ev, f"⚠ 未上传：{results.get('error')}", at_sender=True)
            return

        lines = ["📊 成绩拉取完成"]
        if isinstance(summary, dict):
            count = summary.get("score_count")
            if count is not None:
                lines.append(f"共 {count} 条成绩")
            if summary.get("rating") is not None:
                lines.append(
                    f"Rating {summary.get('rating')}"
                    f"（B35 {summary.get('rating_b35', '-')} / "
                    f"B15 {summary.get('rating_b15', '-')}）"
                )
        lines.append("—— 上传结果 ——")
        lines.append(_platform_line("水鱼", df_on, results, df_skip))
        lines.append(_platform_line("落雪", lx_on, results, lx_skip))
        await bot.send(ev, "\n".join(lines), at_sender=True)
    finally:
        _release(qq)


@upload
async def upload_cmd(bot: NoneBot, ev: CQEvent):
    await _handle_upload(bot, ev)


# ============================================================
# 命令四：自动上传成绩（每人独立开关，由机器人进程每天定时执行）
# ============================================================

AUTO_UPLOAD_HOUR = 3
AUTO_UPLOAD_MINUTE = 50
_AUTO_UPLOAD_TIME = f"{AUTO_UPLOAD_HOUR:02d}:{AUTO_UPLOAD_MINUTE:02d}"

# —— 探测式自动更新（参考 maimai-score-hub 的 auto-update：先探测、有变化才写）——
AUTO_TICK_MINUTES = 5                                   # 调度心跳：每 5 分钟看谁到期
AUTO_PROBE_MINUTES = {"hot": 15, "warm": 30, "cold": 60}  # 活跃分层探测间隔
AUTO_HOT_MINUTES = 120                                  # 探测到变化后 2 小时内按 hot
AUTO_WARM_MINUTES = 720                                 # 12 小时内按 warm，之后 cold
AUTO_FAIL_BACKOFF_MINUTES = (15, 30, 60, 120)           # 连续失败后的退避间隔

_AUTO_ON_WORDS = ("开", "开启", "打开", "启用", "on")
_AUTO_OFF_WORDS = ("关", "关闭", "停", "停用", "取消", "off")
_AUTO_STATUS_WORDS = ("状态", "查询", "查看")


def _auto_upload_usage() -> str:
    return (
        "用法：\n"
        "· 自动上传成绩 —— 开关切换（已开启则关闭）\n"
        "· 自动上传成绩 开（或 关）\n"
        "· 自动上传成绩 状态\n"
        "开启后机器人会定期探测机台成绩，**发现成绩有变化才上传**"
        "（刚有变化 15 分钟一次 / 近期 30 分钟 / 平时 1 小时），"
        f"另外每天 {_AUTO_UPLOAD_TIME} 兜底整体上传一次。"
    )


def _scores_hash(score_list) -> str:
    """成绩指纹：只取成绩相关字段。

    刻意排除 play_count / play_time —— 它们每次游玩都会变，会把「没涨分」的游玩
    也当成绩变化（score-hub 同样只看 achievement / dxScore 这类成绩字段）。
    """
    rows = []
    for s in score_list or []:
        rows.append((
            getattr(s, "id", None),
            getattr(s, "level_index", None),
            getattr(s, "achievements", None),
            getattr(s, "dx_score", None),
            str(getattr(s, "fc", "") or ""),
            str(getattr(s, "fs", "") or ""),
        ))
    rows.sort(key=lambda r: (str(r[0]), str(r[1])))
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _parse_ts(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt_ts(value) -> str:
    ts = _parse_ts(value)
    if not ts:
        return "无"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _auto_tier(rec: dict, now: float) -> str:
    """活跃分层（对应 score-hub 的 hot / warm / cold）。"""
    changed = _parse_ts(rec.get("auto_changed_at"))
    if not changed:
        return "cold"
    if now - changed <= AUTO_HOT_MINUTES * 60:
        return "hot"
    if now - changed <= AUTO_WARM_MINUTES * 60:
        return "warm"
    return "cold"


def _auto_due(rec: dict, now: float) -> bool:
    """到探测时间了吗（auto_next_at 为空视为立即到期）。"""
    return now >= _parse_ts(rec.get("auto_next_at"))


def _auto_schedule(rec: dict, now: float, changed: bool, ok: bool) -> dict:
    """算下一次探测时间与状态字段：成功按分层间隔，失败按退避间隔。"""
    if not ok:
        fail = int(rec.get("auto_fail") or 0) + 1
        idx = min(fail, len(AUTO_FAIL_BACKOFF_MINUTES)) - 1
        return {
            "auto_fail": fail,
            "auto_next_at": str(int(now + AUTO_FAIL_BACKOFF_MINUTES[idx] * 60)),
        }
    fields = {"auto_fail": 0, "auto_checked_at": _fmt_ts(now)}
    if changed:
        fields["auto_changed_at"] = str(int(now))
    tier = _auto_tier({**rec, **fields}, now)
    fields["auto_tier"] = tier
    fields["auto_next_at"] = str(int(now + AUTO_PROBE_MINUTES[tier] * 60))
    return fields


def _notify_target(ev: CQEvent) -> dict:
    """记录结果回执发往哪里：私聊优先（需要 self_id），群里额外记群号做兜底。"""
    group_id = getattr(ev, "group_id", None)
    return {
        "auto_upload_self": str(getattr(ev, "self_id", "") or ""),
        "auto_upload_group": "" if group_id is None else str(group_id),
    }


def _auto_upload_status_text(rec: dict, on: bool) -> str:
    lines = [f"【自动上传成绩】{'✅ 已开启' if on else '▫ 已关闭'}"]
    if on:
        now = time.time()
        tier = _auto_tier(rec, now)
        tier_name = {
            "hot": "hot（刚有变化，15 分钟探测一次）",
            "warm": "warm（近期有变化，30 分钟探测一次）",
            "cold": "cold（平时，1 小时探测一次）",
        }[tier]
        lines.append("模式：探测式自动更新（成绩有变化才上传）")
        lines.append(f"活跃分层：{tier_name}")
        lines.append(f"上次探测：{rec.get('auto_checked_at') or '尚未探测'}")
        lines.append(f"上次发现变化：{_fmt_ts(rec.get('auto_changed_at'))}")
        lines.append(f"下次探测：{_fmt_ts(rec.get('auto_next_at'))}")
        if rec.get("auto_fail"):
            lines.append(f"⚠ 连续失败 {rec.get('auto_fail')} 次（已自动退避重试）")
        lines.append(f"兜底全量：每天 {_AUTO_UPLOAD_TIME} 再整体上传一次")
        group_id = rec.get("auto_upload_group") or ""
        if group_id:
            lines.append(f"结果通知：先私聊；私聊失败时在群 {group_id} 里 @ 你")
        else:
            lines.append("结果通知：私聊（私聊失败不再群里兜底）")
    elif rec.get("auto_upload_off_at"):
        lines.append(f"关闭于：{rec.get('auto_upload_off_at')}")
    return "\n".join(lines)


async def _handle_auto_upload(bot: NoneBot, ev: CQEvent, arg: str = "") -> None:
    qq = ev.user_id
    if not _core_ok:
        await bot.send(ev, _setup_hint("core.arcade_store 模块"), at_sender=True)
        return

    arg = (arg or "").strip()
    rec = get(qq) or {}
    if arg in _AUTO_STATUS_WORDS:
        await bot.send(
            ev, _auto_upload_status_text(rec, bool(rec.get("auto_upload"))), at_sender=True
        )
        return
    if arg in _AUTO_OFF_WORDS:
        want = False
    elif arg in _AUTO_ON_WORDS:
        want = True
    elif not arg:
        want = not bool(rec.get("auto_upload"))
    else:
        await bot.send(ev, _auto_upload_usage(), at_sender=True)
        return

    if want:
        if not rec.get("arcade_creds"):
            await bot.send(
                ev,
                "⚠ 还未绑定机台账号，无法自动拉取成绩。\n"
                "请先发送 mai绑定 + 机台二维码（图片或 SGWCMAID 文本）完成绑定。",
                at_sender=True,
            )
            return
        rec = set(qq, auto_upload=True, auto_upload_at=now_str(),
                  auto_next_at=str(int(time.time())), **_notify_target(ev))
        await bot.send(
            ev, "✅ 已开启自动上传成绩\n" + _auto_upload_status_text(rec, True), at_sender=True
        )
    else:
        rec = set(qq, auto_upload=False, auto_upload_off_at=now_str())
        await bot.send(
            ev, "▫ 已关闭自动上传成绩\n" + _auto_upload_status_text(rec, False), at_sender=True
        )


@auto_upload
async def auto_upload_cmd(bot: NoneBot, ev: CQEvent):
    await _handle_auto_upload(bot, ev)


def _auto_platform_line(name: str, key: str, results: dict) -> str:
    """自动上传结果里的一行平台状态（skipped 平台单独标注，不显示成失败）。"""
    r = (results or {}).get(key)
    if not isinstance(r, dict):
        return f"❌ {name}：平台无返回结果"
    if r.get("skipped"):
        return f"▫ {name}：{r.get('msg') or '未提供凭据'}，已跳过"
    if r.get("ok"):
        return f"✅ {name}：{r.get('msg') or '上传成功'}"
    return f"❌ {name}：{r.get('msg') or '未知错误'}"


def _auto_report_ok(results: dict) -> bool:
    """有平台真的参与了上传、且参与的全都成功，才算这次成功。"""
    if not isinstance(results, dict) or "error" in results:
        return False
    active = [v for v in results.values() if isinstance(v, dict) and not v.get("skipped")]
    return bool(active) and all(v.get("ok") for v in active)


async def _push_scores(qq, score_list, summary, title: str) -> tuple:
    """把已拉取的成绩上传到水鱼 / 落雪，返回 (是否全部成功, 结果文本)。"""
    proxy = _proxy()
    df_on = bool(dfconfig.oauth_enabled)
    lx_token, _ = await _lxns_valid_token(qq)
    if not df_on and not lx_token:
        return False, "⚠ 未完成任何上传平台授权（水鱼 / 落雪），本次跳过"

    results = await upload_scores(
        score_list,
        qq,
        lxns_access_token=lx_token,
        http_proxy=proxy,
    )
    if isinstance(results, dict) and "error" in results:
        return False, f"⚠ 未上传：{results.get('error')}"

    lines = [title]
    if isinstance(summary, dict):
        if summary.get("score_count") is not None:
            lines.append(f"共 {summary.get('score_count')} 条成绩")
        if summary.get("rating") is not None:
            lines.append(
                f"Rating {summary.get('rating')}"
                f"（B35 {summary.get('rating_b35', '-')} / "
                f"B15 {summary.get('rating_b15', '-')}）"
            )
    lines.append("—— 上传结果 ——")
    lines.append(_auto_platform_line("水鱼", "divingfish", results))
    lines.append(_auto_platform_line("落雪", "lxns", results))
    lines.append("（不想再自动上传：发送「自动上传成绩」关闭）")
    return _auto_report_ok(results), "\n".join(lines)


async def _auto_probe(qq, rec: dict, *, force: bool = False) -> tuple:
    """探测一次机台成绩：有变化（或 force=True）才上传。

    返回 (changed, ok, text)；没有变化时 text 为空串，调用方无需发消息。
    """
    proxy = _proxy()
    df_on = bool(dfconfig.oauth_enabled)
    lx_token, _ = await _lxns_valid_token(qq)
    if not df_on and not lx_token:
        return False, False, "⚠ 未完成任何上传平台授权（水鱼 / 落雪），本次跳过"

    score_list, summary = await fetch_scores(rec.get("arcade_creds"), proxy)
    digest = _scores_hash(score_list)
    old = rec.get("auto_hash")
    changed = force or (old != digest)
    if not changed:
        return False, True, ""

    title = (
        f"⏰ 自动上传结果（{_AUTO_UPLOAD_TIME} 兜底全量）"
        if force
        else "🔔 探测到成绩变化，已自动上传"
    )
    ok, text = await _push_scores(qq, score_list, summary, title)
    if ok:
        try:
            set(qq, auto_hash=digest)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[mai_upload] 成绩指纹落盘失败 qq={qq}：{e}")
    return True, ok, text


async def _run_auto_upload(qq, rec: dict) -> tuple:
    """整体上传一次（每日兜底 / 手动触发），返回 (是否成功, 结果文本)。"""
    _changed, ok, text = await _auto_probe(qq, rec, force=True)
    return ok, text


async def _notify_auto_upload(qq, rec: dict, text: str) -> None:
    """结果先私聊；私聊失败且用户是在群里开启的，就在群里 @ 他兜底。"""
    try:
        bot = get_bot()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[mai_upload] 自动上传结果通知失败（无可用 bot）：{e}")
        return
    self_id = str(rec.get("auto_upload_self") or "")
    group_id = str(rec.get("auto_upload_group") or "")

    private_kwargs = {"user_id": int(qq), "message": text}
    if self_id:
        private_kwargs["self_id"] = int(self_id)
    try:
        await bot.send_private_msg(**private_kwargs)
        return
    except Exception as e:  # noqa: BLE001
        log.warning(f"[mai_upload] 自动上传结果私聊发送失败 qq={qq}：{e}")

    if not group_id:
        return
    group_kwargs = {"group_id": int(group_id), "message": f"[CQ:at,qq={qq}]\n{text}"}
    if self_id:
        group_kwargs["self_id"] = int(self_id)
    try:
        await bot.send_group_msg(**group_kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[mai_upload] 自动上传结果群内兜底失败 qq={qq}：{e}")


def _auto_targets() -> list:
    """所有「已开启自动上传且有凭据」的用户 [(qq, rec), ...]（快照，避免边遍历边改）。"""
    return [
        (str(qq), rec)
        for qq, rec in list(store_all().items())
        if isinstance(rec, dict) and rec.get("auto_upload") and rec.get("arcade_creds")
    ]


async def _auto_save_schedule(qq, rec: dict, changed: bool, ok: bool) -> None:
    """把下一次探测时间 / 分层 / 失败退避写回存储（写失败不影响主流程）。"""
    try:
        set(qq, **_auto_schedule(rec, time.time(), changed, ok))
    except Exception as e:  # noqa: BLE001
        log.warning(f"[mai_upload] 自动更新状态落盘失败 qq={qq}：{e}")


@sv.scheduled_job("cron", minute=f"*/{AUTO_TICK_MINUTES}")
async def auto_update_tick():
    """探测式自动更新心跳：每 5 分钟看一次谁到期，探测到成绩变化才上传。

    对应 maimai-score-hub auto-update 的 Rival Score Probe：
    先探测、diff 只用于判断是否触发，触发后再整体上传到水鱼 / 落雪。
    """
    if not _core_ok:
        return
    try:
        records = _auto_targets()
    except Exception as e:  # noqa: BLE001
        log.error(f"[mai_upload] 读取绑定记录失败，自动更新心跳结束：{e}")
        return

    now = time.time()
    due = [(qq, rec) for qq, rec in records if _auto_due(rec, now)]
    if not due:
        return
    log.info(f"[mai_upload] 自动更新探测：{len(due)} / {len(records)} 个用户到期")
    for qq, rec in due:
        if not _acquire(qq):
            log.info(f"[mai_upload] 自动更新跳过 qq={qq}（该用户有操作正在进行）")
            continue
        changed = ok = False
        text = ""
        try:
            changed, ok, text = await _auto_probe(qq, rec)
        except Exception as e:  # noqa: BLE001
            ok, text = False, f"❌ 自动更新失败：{_friendly_error(e)}"
            log.warning(f"[mai_upload] 自动更新探测异常 qq={qq}：{e}")
        finally:
            _release(qq)
        await _auto_save_schedule(qq, rec, changed, ok)
        log.info(f"[mai_upload] 自动更新探测完成 qq={qq} changed={changed} ok={ok}")
        if text:
            try:
                await _notify_auto_upload(qq, rec, text)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[mai_upload] 自动更新结果发送异常 qq={qq}：{e}")
        await asyncio.sleep(1)


@sv.scheduled_job("cron", hour=AUTO_UPLOAD_HOUR, minute=AUTO_UPLOAD_MINUTE)
async def auto_upload_job():
    """每天 03:50（Asia/Shanghai）兜底：给所有开启开关的用户整体上传一次。

    对应 score-hub 的 Daily Full Update —— 探测式更新可能漏掉 FC/FS 之类
    没有体现在成绩数字上的变化，每天固定再整体同步一次收尾。
    """
    if not _core_ok:
        log.warning("[mai_upload] core 未就绪，自动上传任务跳过")
        return
    try:
        records = _auto_targets()
    except Exception as e:  # noqa: BLE001
        log.error(f"[mai_upload] 读取绑定记录失败，自动上传任务结束：{e}")
        return

    log.info(f"[mai_upload] 每日兜底上传开始，共 {len(records)} 个用户")
    for qq, rec in records:
        if not _acquire(qq):
            log.warning(f"[mai_upload] 自动上传跳过 qq={qq}（该用户有操作正在进行）")
            continue
        try:
            ok, text = await _run_auto_upload(qq, rec)
            log.info(f"[mai_upload] 自动上传完成 qq={qq} ok={ok}")
        except Exception as e:  # noqa: BLE001
            ok, text = False, f"❌ 自动上传失败：{_friendly_error(e)}"
            log.warning(f"[mai_upload] 自动上传异常 qq={qq}：{e}")
        finally:
            _release(qq)
        await _auto_save_schedule(qq, rec, False, ok)
        try:
            await _notify_auto_upload(qq, rec, text)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[mai_upload] 自动上传结果发送异常 qq={qq}：{e}")
        await asyncio.sleep(1)


# ============================================================
# 帮助
# ============================================================

async def _handle_help(bot: NoneBot, ev: CQEvent, private: bool = False) -> None:
    text = SV_HELP
    if private:
        text += (
            "\n—— 私聊说明 ——\n"
            "以上命令私聊均可直接发送；\n"
            "但 dfbind / lxbind / 水鱼授权码 / 落雪授权码\n"
            "只能在群里发送——请先在任意群里完成授权，\n"
            "之后私聊 mai上传 / 自动上传成绩 即可。"
        )
    await bot.send(ev, text, at_sender=True)


@help_cmd
async def help_cmd_(bot: NoneBot, ev: CQEvent):
    await _handle_help(bot, ev)


# ============================================================
# 私聊通道
# Hoshino 只分发群消息（hoshino/msghandler.py:10 非 group 直接 return），
# 私聊消息走不到上面的 Hoshino 触发器。这里用 nonebot 自带的自然语言
# 处理器接住私聊，前缀语义与群内一致（前缀匹配、参数不要求空格）。
# 群消息即使落进本处理器也会被 detail_type 判断立即放行，
# 仍由 Hoshino 触发器处理——保留群开关/权限语义，也避免双响应。
# ============================================================

def _strip_command(text: str, *names: str) -> str:
    """剥掉开头的命令名，返回参数部分；都没匹配上返回空串。"""
    for name in names:
        if text.startswith(name):
            return text[len(name):].strip()
    return ""


# 关键词是子串匹配（natural_language.py:59），覆盖所有命令前缀与裸二维码文本
_PRIVATE_KEYWORDS = ("mai", "舞萌上传绑定", "SGWCMAID", "自动上传成绩")


@on_natural_language(
    keywords=_PRIVATE_KEYWORDS,
    only_to_me=False,
    only_short_message=False,  # SGWCMAID 二维码文本可能较长，放行
)
async def _private_dispatch(session: NLPSession) -> None:
    ev = session.event
    if ev.detail_type != "private":
        return  # 群消息仍归 Hoshino 触发器管
    bot = session.bot
    text = ev.message.extract_plain_text().strip()

    # 长前缀在前（mai绑定状态 优先于 mai绑定），与群内前缀 trie 最长匹配一致
    if text.startswith("mai绑定状态"):
        await _handle_status(bot, ev)
    elif text.startswith(("mai绑定", "舞萌上传绑定")) or text.startswith("SGWCMAID"):
        await _handle_bind(bot, ev)
    elif text.startswith(("自动上传成绩", "mai自动上传")):
        await _handle_auto_upload(
            bot, ev, _strip_command(text, "自动上传成绩", "mai自动上传")
        )
    elif text.startswith("mai上传"):
        await _handle_upload(bot, ev)
    elif text.startswith("mai帮助"):
        await _handle_help(bot, ev, private=True)
    # 其它只是碰巧含关键词的消息（如英文句子带 mai）：什么都不做，直接放行
