# -*- coding: utf-8 -*-
"""
maimaiDX/core/version_watch.py —— 国服版本哨兵 + AimeDB 恢复哨兵

两个独立的守望任务，都是为了「华立升级时我们不再被动」：

1) 版本哨兵
   国服版本号没有公开接口。唯一稳定来源是 isMaiDown（mai.chongxi.us）的
   /api/status，其中 probe.net.desc 形如：
       配信 OPTION_CN155_A041 SDGB_A041_20261020134418_0.opt
   `CN155` 就是国服 1.55。同一条目还带 release_time / dl_size / order_time。
   注意：`latest_version` 字段（如 "26.10"）是 **isMaiDown 站点自己的前端版本**，
   不是游戏版本，绝不能拿来判断华立升级。

   为什么要盯版本：拉取成绩的 `Mai-Encoding` 头被服务端硬校验为 1.55。
   实测改成 1.56 / 1.54 / 1.60 / 1.41 全部 TitleServerBlockedError，只有 1.55 能用。
   也就是说国服一旦升到 1.56，成绩拉取会立刻全挂，必须等 maimai-ffi 发新版。
   提前一天知道，就能提前安排，而不是被用户骂上门才发现。

2) 恢复哨兵
   二维码换凭据（ai.sys-allnet.cn/wc_aime/api/get_data）自 2026-10-20 起对
   任何请求恒回 errorID 54。用户抱怨「二维码过期」其实是这个。
   2026-11-01 上游 maimai-ffi 0.7.2 修复（https + titlekey + 紧凑 JSON），
   现在探到 errorID=1（ReaderSetupFail）表示报文已能穿过格式层。
   哨兵保留的意义：以后再出现同类加固，errorID 会重新变回 54，能第一时间发现。

数据许可：版本信息来自 isMaiDown，按其许可要求对外必须标注来源
「数据来源于 isMaiDown by Chongxi」。本模块所有对外文案已内置该署名。

设计原则：
- 纯逻辑（解析、比对、文案）与网络分离，网络函数失败只返回 None，不抛。
- 状态落盘到 data/version_watch.json（原子写），避免重复告警。
- 任何异常都不允许打断调用方心跳。
"""

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import httpx

import logging

logger = logging.getLogger("maimaiDX.version_watch")

# ------------------------------------------------------------
# 常量
# ------------------------------------------------------------
STATUS_URL = "https://mai.chongxi.us/api/status"
STATUS_TIMEOUT = 15.0

AIME_URL = "https://ai.sys-allnet.cn/wc_aime/api/get_data"
AIME_UA = "WC_AIME_LIB"
AIME_TIMEOUT = 12.0
# 这两个常量与 maimai_ffi 里的一致（1.41 时代的 chipID 生成式 + COMMON_KEY）。
# 我们不是要绕过它，只是需要一个「格式合法」的请求，好让服务端走到业务层，
# 从而暴露真实 errorID。签名对不对不影响这个用途。
AIME_CHIP_ID = "A63E-01E68606624"
AIME_COMMON_KEY = "XcW5FW4cPArBXEk4vzKz3CIrMuA5EVVW"
# ffi 0.7.2 起必须带 titlekey，且 JSON 必须紧凑（无空格），否则服务端回 54。
# 详细对照实验见 README 的「AimeDB 报文格式」一节。
AIME_TITLEKEY = "SDGB"

# 历史上「被前置校验统一拒绝」的错误码。ffi 0.7.2 修复后这个值不再是 54，
# 所以探到 != 54 就说明报文能穿过格式层了。
AIME_BLOCKED_ERROR_ID = 54

ATTRIBUTION = "数据来源于 isMaiDown by Chongxi"

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_PATH = os.path.join(_DATA_DIR, "version_watch.json")

# OPTION_CN155_A041 之类
_RE_OPTION = re.compile(r"OPTION_CN(\d{2,4})_([A-Z0-9]+)", re.IGNORECASE)
_RE_SDGB = re.compile(r"SDGB_([A-Z0-9]+)_(\d{14})_(\d+)", re.IGNORECASE)


# ------------------------------------------------------------
# 纯逻辑：解析
# ------------------------------------------------------------
def cn_code_to_version(code: str) -> str:
    """把 CN155 这类码转成人类可读版本号。

    CN155 → 1.55，CN156 → 1.56，CN160 → 1.60。
    三位数按首位当大版本、后两位当小版本；不足两位的原样返回。
    """
    s = str(code or "").strip()
    if not s.isdigit():
        return s
    if len(s) == 2:
        return f"1.{s}"
    if len(s) == 3:
        return f"{s[0]}.{s[1:]}"
    return s


def parse_version(status: Any) -> Optional[Dict[str, Any]]:
    """从 /api/status 的 JSON 里抽出真实的国服版本信息。

    返回 dict：{version, option_code, pack, desc, release_time, dl_size, order_time}
    找不到就返回 None。
    """
    if not isinstance(status, dict):
        return None
    probe = status.get("probe")
    if not isinstance(probe, dict):
        return None

    # net 与 delivery 两条都带这套信息，优先 net，回退 delivery
    for key in ("net", "delivery"):
        node = probe.get(key)
        if not isinstance(node, dict):
            continue
        desc = str(node.get("desc") or "")
        m = _RE_OPTION.search(desc)
        if not m:
            continue
        option_code = m.group(1)
        pack = m.group(2)
        sdgb = _RE_SDGB.search(desc)
        return {
            "version": cn_code_to_version(option_code),
            "option_code": option_code,
            "pack": pack.upper(),
            "desc": desc,
            "release_time": node.get("release_time"),
            "order_time": node.get("order_time"),
            "dl_size": node.get("dl_size"),
            "build": sdgb.group(2) if sdgb else None,
        }
    return None


def parse_aime_error(flag: Optional[int]) -> Optional[int]:
    """把响应里的 errorID 规整成 int；拿不到返回 None。"""
    if flag is None:
        return None
    try:
        return int(flag)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------
# 纯逻辑：状态比对
# ------------------------------------------------------------
def load_state(path: str = STATE_PATH) -> Dict[str, Any]:
    """读状态文件；不存在或坏了都返回空 dict。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[version_watch] 状态文件读取失败：{e}")
        return {}


def save_state(state: Dict[str, Any], path: str = STATE_PATH) -> None:
    """原子写状态文件；失败只记日志，不影响调用方。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[version_watch] 状态文件写入失败：{e}")


def diff_version(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Optional[str]:
    """比对两次版本信息，返回变化类型；无变化返回 None。

    返回值：
      "first"  —— 首次记录（不算升级，但要让管理员知道哨兵开始工作了）
      "patch"  —— 同一版本号下换了包（A041 → A042），可能是热修
      "upgrade"—— 版本号变了（1.55 → 1.56），这才是要警惕的
    """
    if not new:
        return None
    if not old or not old.get("version"):
        return "first"
    if old.get("version") != new.get("version"):
        return "upgrade"
    if old.get("pack") and new.get("pack") and old["pack"] != new["pack"]:
        return "patch"
    return None


def format_version_alert(kind: str, new: Dict[str, Any], old: Optional[Dict[str, Any]]) -> str:
    """把版本变化渲染成给人看的短消息（含 isMaiDown 署名）。"""
    ver = new.get("version")
    pack = new.get("pack")
    if kind == "upgrade":
        old_ver = (old or {}).get("version") or "?"
        head = f"🚨 国服版本升级：{old_ver} → {ver}"
        tail = (
            "注意：拉成绩的 Mai-Encoding 被服务端硬校验，"
            f"如果 1.55 之外的版本不再被接受，成绩拉取会全挂，需要等 maimai-ffi 更新。"
        )
    elif kind == "patch":
        old_pack = (old or {}).get("pack") or "?"
        head = f"📦 国服换包：{old_pack} → {pack}（版本仍为 {ver}）"
        tail = "同版本换包通常是热修，一般不影响我们，但值得盯一眼。"
    else:
        head = f"👀 版本哨兵已启动：当前国服 {ver}（{pack}）"
        tail = "之后国服换版本或换包，我会自动通知你。"

    lines = [
        head,
        f"包名：{new.get('desc') or '—'}",
    ]
    if new.get("release_time"):
        lines.append(f"下发时间：{new['release_time']}")
    if new.get("dl_size"):
        try:
            lines.append(f"补丁大小：{int(new['dl_size']) / 1048576:.1f} MiB")
        except (TypeError, ValueError):
            pass
    lines.append(tail)
    lines.append(f"（{ATTRIBUTION}）")
    return "\n".join(lines)


def format_aime_recovered(error_id: Optional[int]) -> str:
    """AimeDB 恢复通知。"""
    return (
        "✅ 二维码换凭据接口已恢复\n"
        f"哨兵探测返回 errorID={error_id}（不再是 {AIME_BLOCKED_ERROR_ID}）。\n"
        "报文已经能穿过机台的格式校验层，说明扫码绑定链路可用。\n"
        "请让用户发一张新二维码实测；确认能绑上后，可以把引导文案改回扫码优先。"
    )


# ------------------------------------------------------------
# 网络（失败一律返回 None，绝不抛）
# ------------------------------------------------------------
async def fetch_status(proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """拉 isMaiDown 的状态 JSON。失败返回 None。"""
    try:
        kwargs: Dict[str, Any] = {
            "timeout": STATUS_TIMEOUT,
            "follow_redirects": True,
        }
        if proxy:
            kwargs["proxy"] = proxy
        else:
            kwargs["trust_env"] = False
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(STATUS_URL)
        if resp.status_code != 200:
            logger.info(f"[version_watch] 状态接口返回 {resp.status_code}")
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.info(f"[version_watch] 拉状态失败：{type(e).__name__}: {e}")
        return None


def _build_probe_qr() -> Tuple[str, str]:
    """造一个格式合法但内容随机的假码，返回 (qrCode 字段, timestamp)。

    目的不是兑换成功，而是让请求穿过格式校验、走到业务层，
    这样才能看到真实 errorID。内容随机（掺 os.urandom）保证它一定不是有效二维码，
    同一秒内连续调用也不会得到同一个串。
    """
    ts = time.strftime("%y%m%d%H%M%S")
    seed = f"version-watch-{ts}-{os.urandom(8).hex()}"
    body = hashlib.sha256(seed.encode()).hexdigest().upper()
    return body, ts


async def probe_aime(proxy: Optional[str] = None) -> Optional[int]:
    """探 wc_aime，返回 errorID；请求失败返回 None。

    必须与 maimai_ffi 0.7.2 的报文完全一致（https + titlekey + 紧凑 JSON），
    否则服务端只回 54，探不到真实状态。2026-11-02 实测：同一份字段，
    紧凑格式回 errorID=1，带空格格式回 errorID=54。
    """
    qr_body, ts = _build_probe_qr()
    key = hashlib.sha256(
        (AIME_CHIP_ID + ts + AIME_COMMON_KEY).encode()
    ).hexdigest().upper()
    payload = {
        "chipID": AIME_CHIP_ID,
        "openGameID": "MAID",
        "key": key,
        "qrCode": qr_body,
        "timestamp": ts,
        "titlekey": AIME_TITLEKEY,
    }
    # 必须紧凑序列化：服务端对 JSON 里的空格敏感（实测带空格恒回 54）
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        kwargs: Dict[str, Any] = {
            "timeout": AIME_TIMEOUT,
            "headers": {"User-Agent": AIME_UA, "Content-Type": "application/json"},
        }
        if proxy:
            kwargs["proxy"] = proxy
        else:
            kwargs["trust_env"] = False
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(AIME_URL, content=body)
        if resp.status_code != 200:
            logger.info(f"[version_watch] AimeDB 探测返回 HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return parse_aime_error(data.get("errorID"))
    except Exception as e:  # noqa: BLE001
        logger.info(f"[version_watch] AimeDB 探测失败：{type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------
# 组合：一次检查，返回要发给管理员的消息列表
# ------------------------------------------------------------
async def check_once(
    proxy: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
    path: str = STATE_PATH,
) -> Tuple[list, Dict[str, Any]]:
    """跑一轮哨兵。

    返回 (messages, new_state)。messages 是要私聊管理员的消息列表，
    空列表表示「没什么要说的」。
    """
    st = dict(state) if state is not None else load_state(path)
    messages: list = []

    # ---- 版本哨兵 ----
    status = await fetch_status(proxy)
    ver = parse_version(status) if status else None
    if ver:
        old_ver = st.get("version") or {}
        kind = diff_version(old_ver if isinstance(old_ver, dict) else {}, ver)
        if kind:
            messages.append(format_version_alert(kind, ver, old_ver if isinstance(old_ver, dict) else None))
            logger.info(f"[version_watch] 版本变化：{kind} -> {ver.get('version')} / {ver.get('pack')}")
        st["version"] = ver
        st["version_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 恢复哨兵 ----
    error_id = await probe_aime(proxy)
    if error_id is not None:
        st["aime_error_id"] = error_id
        st["aime_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        recovered_now = error_id != AIME_BLOCKED_ERROR_ID
        was_recovered = bool(st.get("aime_recovered"))
        if recovered_now and not was_recovered:
            messages.append(format_aime_recovered(error_id))
            logger.info(f"[version_watch] AimeDB 疑似恢复：errorID={error_id}")
            st["aime_recovered"] = True
        elif not recovered_now and was_recovered:
            # 又坏了（华立回滚或再次加固），把状态打回去，下次恢复还能再报一次
            st["aime_recovered"] = False
            logger.info(f"[version_watch] AimeDB 又变回被拦：errorID={error_id}")

    if messages:
        save_state(st, path)
    elif st.get("version") or st.get("aime_error_id") is not None:
        save_state(st, path)

    return messages, st


__all__ = [
    "STATUS_URL",
    "AIME_URL",
    "AIME_BLOCKED_ERROR_ID",
    "ATTRIBUTION",
    "STATE_PATH",
    "cn_code_to_version",
    "parse_version",
    "parse_aime_error",
    "load_state",
    "save_state",
    "diff_version",
    "format_version_alert",
    "format_aime_recovered",
    "fetch_status",
    "probe_aime",
    "check_once",
]
