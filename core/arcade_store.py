# -*- coding: utf-8 -*-
"""
maimaiDX/core/arcade_store.py —— 机台凭据存储层（JSON 文件当数据库，自 maimai-update/core/store.py 融入）

职责：
- 以 QQ 号字符串为 key，保存每个用户的机台凭据（arcade_creds）等字段。
- 启动时 load 一次进内存，之后全部读内存；每次变更后立即 save（原子写）。
- 原子写：先写同目录 .tmp 临时文件，再 os.replace 替换，防止进程中途被杀写坏文件。
- load 时 json 解析失败：把坏文件改名留档为 bindings.json.corrupt，告警并从空库起步，绝不崩。

数据结构（bindings.json 顶层 dict）：
{
  "123456789": {
      "arcade_creds": "...",        # 机台加密 userId，永不过期，可持久化
      "divingfish_token": "...",    # 水鱼 Import-Token（可选）
      "lxns_secret": "...",         # 落雪 API-Secret（可选）
      "nickname": "...",            # 可选
      "bound_at": "2024-01-01 00:00:00"  # 可选
  }
}
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("maimaiDX.arcade_store")

# 存储路径：插件（maimaiDX 包）目录下的 data/bindings.json
# （相对本文件解析：core -> maimaiDX，与 maimai-update 相同的“插件目录/data”惯例）
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STORE_PATH = os.path.join(_DATA_DIR, "bindings.json")
# 坏文件留档路径
CORRUPT_PATH = STORE_PATH + ".corrupt"


class BindingStore:
    """QQ 用户凭据存储：内存 dict + 原子落盘。"""

    def __init__(self, path: Optional[str] = None) -> None:
        """path 可自定义（测试用临时路径），默认插件 data/bindings.json。"""
        self.path = path or STORE_PATH
        self.corrupt_path = self.path + ".corrupt"
        self._data: Dict[str, Dict[str, Any]] = {}
        self.load()

    # ---------- 读盘 / 落盘 ----------

    def load(self) -> None:
        """启动时读一次盘进内存；解析失败改名留档、告警、从空库起步，绝不崩。"""
        self._data = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                # 归一化：key 统一 str，value 统一 dict
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self._data[str(k)] = v
            else:
                raise ValueError("顶层不是 dict: %s" % type(raw).__name__)
        except (ValueError, TypeError, OSError) as e:
            # json 解析失败 / 读取失败 -> 坏文件改名留档，从空库起步
            logger.warning("bindings.json 损坏（%s），已改名留档为 %s，从空库起步",
                           e, self.corrupt_path)
            try:
                os.replace(self.path, self.corrupt_path)
            except OSError as e2:
                logger.warning("坏文件留档失败: %s", e2)
            self._data = {}

    def save(self) -> None:
        """原子写：先写同目录 .tmp，再 os.replace 替换正式文件。"""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # 临时文件与正式文件同目录，保证 os.replace 同盘原子；带 pid 避免多写者互踩
        tmp_path = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())  # 落盘，防止掉电丢数据
            os.replace(tmp_path, self.path)  # 同盘原子替换
        except OSError:
            # 写失败时清理临时文件，别留垃圾
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    # ---------- 对外接口 ----------

    def get(self, qq: Any) -> Dict[str, Any]:
        """按 QQ 号取一条绑定记录；不存在返回空 dict（契约标注 -> dict）。"""
        return self._data.get(str(qq)) or {}

    def set(self, qq: Any, **fields: Any) -> Dict[str, Any]:
        """merge 写入一条绑定记录（只更新传入字段），合并后立即保存。QQ 号统一 str 化。"""
        key = str(qq)
        rec = self._data.get(key)
        if rec is None:
            rec = {}
            self._data[key] = rec
        for k, v in fields.items():
            if v is None:
                continue  # None 视为“不改”，避免误清空已有凭据
            rec[k] = v
        self.save()
        return rec

    def delete(self, qq: Any) -> bool:
        """删除一条绑定记录并保存；不存在返回 False。"""
        key = str(qq)
        if key in self._data:
            del self._data[key]
            self.save()
            return True
        return False

    def all(self) -> Dict[str, Dict[str, Any]]:
        """返回全部记录（内存 dict 的引用，只读用途）。"""
        return self._data


# ---------- 模块级默认单例（上层可直接用，也允许自行实例化做测试） ----------

_store: Optional[BindingStore] = None


def get_store() -> BindingStore:
    """取进程级默认单例 BindingStore（懒加载）。"""
    global _store
    if _store is None:
        _store = BindingStore()
    return _store


# ---------- 便捷函数（转发到默认单例，签名供命令层固定调用） ----------

def get(qq: Any) -> Dict[str, Any]:
    """取一个 QQ 用户的绑定记录，无则返回空 dict。"""
    return get_store().get(qq)


def set(qq: Any, **fields: Any) -> Dict[str, Any]:
    """merge 写入并立即保存。"""
    return get_store().set(qq, **fields)


def delete(qq: Any) -> bool:
    """删除并保存，返回是否存在。"""
    return get_store().delete(qq)


def all() -> Dict[str, Dict[str, Any]]:  # noqa: A001 —— 按需求固定名为 all
    """取全部绑定记录。"""
    return get_store().all()


# ---------- 安全：日志脱敏 ----------

def mask(value: Optional[str], keep: int = 4) -> Optional[str]:
    """
    脱敏显示：token/凭据只保留前 keep 位与后 keep 位，中间用 *** 代替。
    例：mask("abcdefghijklmnop") -> "abcd***mnop"
    None 或过短时整体打码。用于日志，绝不把明文 token 打进日志。
    """
    if value is None:
        return None
    s = str(value)
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "***" + s[-keep:]


def now_str() -> str:
    """当前时间字符串（用于 bound_at）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


__all__ = [
    "BindingStore", "get_store", "get", "set", "delete", "all",
    "mask", "now_str", "STORE_PATH", "CORRUPT_PATH",
]
