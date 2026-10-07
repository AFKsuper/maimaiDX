"""
成绩本地缓存：把机台拉取到的全量成绩落盘，在线查分失败时兜底绘制 B50。

设计要点：
- 只缓存「机台拉取到的原始成绩」（`MaimaiClient().scores()` 返回的 `ScoreExtend` 列表），
  而不是缓存最终图片或 B50 结果；B50 由缓存现场重算，成绩更新后缓存即刷新。
- 缓存按 QQ 号分文件条目存放，写在插件 `data/score_cache.json`（独立于 bindings.json，
  避免每次写成绩都把绑定库整体重写一遍）。
- 兜底仅在「在线查分」抛异常时启用（见 core/handler.py 的 draw_best50），
  命中缓存时会在图后附一句提示，明确这不是最新成绩。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import log
from ..resources import data_dir
from .clients.lxns.models.enum import LevelIndex
from .merge.models import Best50, PlayedResult, Player

CACHE_PATH: Path = data_dir / "score_cache.json"
CACHE_VERSION = 1

try:  # 版本分界线跟着 maimai-py 走，取不到时退回 PRISM PLUS(25500)
    from maimai_py.enums import Version as _MaiVersion

    _CURRENT_VERSION = int(_MaiVersion.current_version.value)
except Exception:  # noqa: BLE001
    _CURRENT_VERSION = 25500

# 只有这两类谱面成绩参与 B35/B15（UTAGE 宴会谱不计入 Rating）
_RANKED_TYPES = ("standard", "dx")

_data: Optional[Dict[str, Any]] = None


def _mai() -> Any:
    """延迟取 `core.service` 的单例（`service` <-> `image` 相互导入，只能运行时取）。"""
    from .service import mai

    return mai


# ------------------------------------------------------------------
# 落盘 / 读取
# ------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _store() -> Dict[str, Any]:
    """懒加载缓存文件；损坏时从空库起步（不删原文件，便于事后排查）。"""
    global _data
    if _data is None:
        raw: Any = None
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError) as e:  # noqa: BLE001
            log.warning(f"[score_cache] 读取缓存失败，将从空缓存开始：{type(e).__name__}: {e}")
            raw = None
        _data = raw if isinstance(raw, dict) else {}
        _data["version"] = CACHE_VERSION
        if not isinstance(_data.get("users"), dict):
            _data["users"] = {}
    return _data


def _save() -> None:
    """原子写：同目录 .tmp -> os.replace，避免写到一半掉电损坏缓存。"""
    store = _store()
    directory = os.path.dirname(str(CACHE_PATH)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = "%s.%d.tmp" % (CACHE_PATH, os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CACHE_PATH)
    except OSError as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise e


# ------------------------------------------------------------------
# 序列化：maimai_py 的 Score / ScoreExtend -> JSON
# ------------------------------------------------------------------


def _enum_value(v: Any) -> Any:
    """枚举取 value，普通值原样返回。"""
    return getattr(v, "value", v)


def _enum_name_lower(v: Any) -> Optional[str]:
    """rate/fc/fs 统一成小写字符串（与 core 的 RateType/FCType/FSType 字面量一致）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        return s or None
    name = getattr(v, "name", None)
    if name:
        return str(name).lower()
    value = getattr(v, "value", None)
    if isinstance(value, str):
        return value.strip().lower() or None
    return None


def _ser_score(score: Any) -> Dict[str, Any]:
    """把一条成绩压成可 JSON 化的 dict（只保留绘制 B50 需要的字段）。"""
    rec: Dict[str, Any] = {
        "id": int(getattr(score, "id", 0) or 0),
        "level": str(getattr(score, "level", "") or ""),
        "level_index": int(_enum_value(getattr(score, "level_index", 3)) or 0),
        "achievements": float(getattr(score, "achievements", 0.0) or 0.0),
        "dx_score": int(getattr(score, "dx_score", 0) or 0),
        "dx_rating": float(getattr(score, "dx_rating", 0.0) or 0.0),
        "type": str(_enum_value(getattr(score, "type", None)) or "").lower(),
        "rate": _enum_name_lower(getattr(score, "rate", None)),
        "fc": _enum_name_lower(getattr(score, "fc", None)),
        "fs": _enum_name_lower(getattr(score, "fs", None)),
    }
    # ScoreExtend 才有的补充字段；缺失时兜底时再从本地曲库补
    for key, attr in (
        ("title", "title"),
        ("version", "version"),
        ("level_value", "level_value"),
        ("dx_star", "dx_star"),
        ("level_dx_score", "level_dx_score"),
    ):
        value = getattr(score, attr, None)
        if value is not None:
            rec[key] = _enum_value(value)
    return rec


# ------------------------------------------------------------------
# 对外：写入
# ------------------------------------------------------------------


def save_scores(qq: Any, scores: Any, summary: Optional[Dict[str, Any]] = None) -> bool:
    """
    缓存一次机台拉取到的全量成绩。

    - `scores` 为空时直接返回 False（避免「拉取失败返回空」把好缓存冲掉）。
    - 返回是否真的写入。
    """
    if not scores:
        return False
    records: List[Dict[str, Any]] = []
    for score in scores:
        try:
            records.append(_ser_score(score))
        except Exception as e:  # noqa: BLE001
            log.warning(f"[score_cache] 序列化成绩失败，跳过该条：{type(e).__name__}: {e}")
    if not records:
        return False

    store = _store()
    rec = store["users"].setdefault(str(qq), {})
    rec["scores"] = records
    rec["score_count"] = len(records)
    rec["saved_at"] = _now()
    if isinstance(summary, dict) and summary.get("rating") is not None:
        rec["rating"] = summary.get("rating")
    _save()
    return True


def save_player(qq: Any, player: Any) -> None:
    """记一小份玩家信息（昵称/Rating），供兜底出图时当标题用。"""
    if qq is None or player is None:
        return
    try:
        store = _store()
        rec = store["users"].setdefault(str(qq), {})
        rec["player"] = {
            "name": str(getattr(player, "name", "") or ""),
            "rating": int(getattr(player, "rating", 0) or 0),
        }
        rec["player_at"] = _now()
        _save()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[score_cache] 记录玩家信息失败：{type(e).__name__}: {e}")


# ------------------------------------------------------------------
# 对外：读取
# ------------------------------------------------------------------


def meta(qq: Any) -> Dict[str, Any]:
    """取缓存元信息（不含成绩本体），无缓存返回空 dict。"""
    rec = _store()["users"].get(str(qq)) or {}
    return {
        "saved_at": rec.get("saved_at"),
        "score_count": rec.get("score_count") or len(rec.get("scores") or []),
        "rating": rec.get("rating"),
        "player": rec.get("player"),
    }


def has_cache(qq: Any) -> bool:
    return bool((_store()["users"].get(str(qq)) or {}).get("scores"))


def _level_value_of(rec: Dict[str, Any], song_id: int, level_index: int) -> float:
    value = rec.get("level_value")
    if isinstance(value, (int, float)) and value:
        return float(value)
    try:
        table = getattr(_mai(), "total_level_value_map", None) or {}
        return float(table.get(f"{song_id}-{level_index}", 0) or 0)
    except Exception:  # noqa: BLE001 —— 曲库未就绪/循环导入都要能兜住
        return 0.0


def _to_played(rec: Dict[str, Any]) -> Optional[PlayedResult]:
    """缓存条目 -> `PlayedResult`（渲染层要的字段）；字段不合法时返回 None。"""
    try:
        stype = str(rec.get("type") or "").lower()
        raw_id = int(rec.get("id") or 0)
        # 机台成绩里的 id 对 DX 谱是「基础 id」，而插件侧统一用 水鱼 id（DX = +10000）
        song_id = raw_id + 10000 if stype == "dx" else raw_id
        level_index = int(rec.get("level_index") or 0)
        return PlayedResult(
            song_id=song_id,
            song_name=str(rec.get("title") or ""),
            level_index=LevelIndex(level_index),
            type=stype or "standard",
            rating=int(float(rec.get("dx_rating") or 0)),
            achievements=float(rec.get("achievements") or 0.0),
            rate=rec.get("rate"),
            level=str(rec.get("level") or ""),
            fc=rec.get("fc"),
            fs=rec.get("fs"),
            dx_score=int(rec.get("dx_score") or 0),
            dx_star=rec.get("dx_star"),
            level_value=_level_value_of(rec, song_id, level_index),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            f"[score_cache] 还原成绩失败（id={rec.get('id')!r}）：{type(e).__name__}: {e}"
        )
        return None


def _in_local_library(song_id: int, level_index: int) -> bool:
    """
    本地曲库里是否有这张谱面。

    渲染层（core/image/base.py 的 whiledraw）会 `mai.total_list.by_id(song_id)`
    取谱面定数，取不到就是 None → `AttributeError`。缓存里可能存着本地曲库还没
    同步到的新曲（曲库每天凌晨才刷新），所以兜底出图前先滤一遍。

    曲库尚未加载时无法判定，返回 True 不滤（该场景由 cached_best50 提前拦下）。
    """
    try:
        total_list = getattr(_mai(), "total_list", None)
    except Exception:  # noqa: BLE001
        return True
    if total_list is None:
        return True
    try:
        song = total_list.by_id(song_id)
    except Exception:  # noqa: BLE001
        return True
    if song is None:
        return False
    try:
        return any(d.level_index == LevelIndex(level_index) for d in song.difficulties)
    except Exception:  # noqa: BLE001
        return False


def _is_new(rec: Dict[str, Any]) -> bool:
    """按谱面版本号判断属于 B15（新曲）还是 B35（旧曲）。"""
    version = rec.get("version")
    if version is None:
        return False
    try:
        return int(version) >= _CURRENT_VERSION
    except (TypeError, ValueError):
        return False


def build_best50(qq: Any) -> Optional[Tuple[Player, Best50, str]]:
    """
    用本地缓存重算 B35/B15。

    返回 `(player, best50, note)`；无缓存返回 None。
    `note` 是要附在图后的提示语（提醒这不是最新的在线成绩）。
    """
    rec = _store()["users"].get(str(qq)) or {}
    scores = rec.get("scores")
    if not scores:
        return None

    sd: List[PlayedResult] = []
    dx: List[PlayedResult] = []
    skipped = 0
    for item in scores:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() not in _RANKED_TYPES:
            continue  # 宴会谱不参与 Rating
        played = _to_played(item)
        if played is None:
            continue
        if not _in_local_library(played.song_id, int(played.level_index)):
            skipped += 1
            continue  # 本地曲库还没有这张谱面，渲染会崩，直接跳过
        (dx if _is_new(item) else sd).append(played)

    if not sd and not dx:
        return None

    key = lambda r: (r.rating or 0, r.dx_score or 0, r.achievements or 0)  # noqa: E731
    sd.sort(key=key, reverse=True)
    dx.sort(key=key, reverse=True)
    sd = sd[:35]
    dx = dx[:15]

    sd_total = int(sum(r.rating or 0 for r in sd))
    dx_total = int(sum(r.rating or 0 for r in dx))
    rating = sd_total + dx_total

    snapshot = rec.get("player") if isinstance(rec.get("player"), dict) else {}
    name = str(snapshot.get("name") or "").strip() or "缓存成绩"
    player = Player(name=name, rating=rating)

    saved_at = rec.get("saved_at") or rec.get("player_at") or "未知时间"
    note = (
        f"⚠ 在线查分器暂时不可用，以下为本地缓存成绩（缓存于 {saved_at}），可能不是最新。\n"
        f"（共 {len(scores)} 条缓存成绩"
    )
    if skipped:
        note += f"，其中 {skipped} 条本地曲库暂无数据已跳过"
    note += "）\n"
    return player, Best50(sd_total=sd_total, dx_total=dx_total, sd=sd, dx=dx), note


def cached_best50(qq: Any) -> Optional[Tuple[Player, Best50, str]]:
    """
    兜底入口：曲库未就绪或没有缓存时返回 None（调用方据此决定是否原样抛错）。
    """
    if not qq or not has_cache(qq):
        return None
    mai = _mai()
    if getattr(mai, "total_list", None) is None:
        log.warning("[score_cache] 本地曲库尚未加载，无法用缓存出图")
        return None
    if getattr(mai, "total_level_value_map", None) is None:
        log.warning("[score_cache] 本地定数表尚未加载，无法用缓存出图")
        return None
    return build_best50(qq)


__all__ = [
    "CACHE_PATH",
    "save_scores",
    "save_player",
    "meta",
    "has_cache",
    "build_best50",
    "cached_best50",
]
