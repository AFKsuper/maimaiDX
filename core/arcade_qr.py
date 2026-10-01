# -*- coding: utf-8 -*-
"""
maimaiDX/core/arcade_qr.py —— 机台二维码内容 / 图片解析（自 maimai-update/core/qr.py 融入）

职责：
- extract_from_text(text)：从用户发来的任意文本里宽松提取 `SGWCMAID` 开头的二维码内容，
  容忍换行、空格、粘贴杂质；正则 SGWCMAID[0-9A-Za-z_]+，原文 + 去空白两遍取更长匹配。
- decode_image(image_bytes)：用 opencv 的 cv2.QRCodeDetector 解码图片字节；
  cv2 缺失或解码失败一律返回 None（不抛异常），由上层给降级提示。

这两个都是同步函数，由上层自行丢线程池（cv2 是阻塞的）。
"""

import re
from typing import Optional

# 二维码内容固定以 SGWCMAID 开头（maimai 机台二维码前缀），后接字母数字下划线。
# 刻意不用 \w：Python3 的 \w 会匹配中文，宽松兜底拼接后可能把尾随汉字吞进二维码内容。
_QR_RE = re.compile(r"SGWCMAID[0-9A-Za-z_]+", re.IGNORECASE)

# 延迟导入的 cv2 / numpy（可选依赖）
_cv2 = None
_cv2_tried = False


def _load_cv2():
    """懒加载 cv2 与 numpy；任一缺失都返回 None（不抛异常）。"""
    global _cv2, _cv2_tried
    if _cv2_tried:
        return _cv2
    _cv2_tried = True
    try:
        import cv2  # opencv-python
        _cv2 = cv2
    except Exception:
        _cv2 = None
    return _cv2


def extract_from_text(text: str) -> Optional[str]:
    """
    从任意文本里宽松提取 SGWCMAID 开头的二维码内容。

    容忍换行、空格、粘贴杂质（前后的说明文字、URL 等）。
    返回 str（如 "SGWCMAIDxxxx..."）或 None（没找到）。
    """
    if not text:
        return None
    # 第一步：原文直接匹配（容忍二维码前后的说明文字）
    m = _QR_RE.search(text)
    best = m.group(0) if m else None
    # 第二步：剔除所有空白再匹配（容忍二维码被换行/空格截断）。
    # 若第一步已命中截断前半段，这里能拼回完整内容，故取两者中更长的。
    # 配合 _QR_RE 只收字母数字下划线，拼接不会吞进中文杂质。
    compact = re.sub(r"\s+", "", text)
    m = _QR_RE.search(compact)
    if m and (best is None or len(m.group(0)) > len(best)):
        best = m.group(0)
    return best


def decode_image(image_bytes: bytes) -> Optional[str]:
    """
    解码图片字节里的二维码，返回内容 str 或 None。

    - cv2 导入失败 / 图片解不开 / 图里没有二维码 → 一律返回 None（不抛异常），
      由上层给降级提示（例如提示用户直接粘贴二维码文本）。
    - cv2.imdecode 需要 np.frombuffer，这里内部处理。
    - 同步阻塞函数，上层请自行丢线程池执行。
    """
    if not image_bytes:
        return None
    cv2 = _load_cv2()
    if cv2 is None:
        return None
    try:
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        detector = cv2.QRCodeDetector()
        # detectAndDecode 兼容性最好：返回 (text, points, straight_qrcode)
        text, _points, _straight = detector.detectAndDecode(img)
        if text:
            return text
        # 部分版本/模糊图走 detectAndDecodeCurved 兜底
        try:
            text2, _pts, _st = detector.detectAndDecodeCurved(img)
            if text2:
                return text2
        except Exception:
            pass
        return None
    except Exception:
        # 任何解码异常都视为“解不出”，交给上层降级提示
        return None


__all__ = ["extract_from_text", "decode_image"]
