"""adapter 之间共享的小工具。"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def safe_parse_json(raw: str) -> dict | list:
    """容错解析 JSON：先严格解析，再尝试截取最外层 ``{}`` 或 ``[]``。"""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM JSON 解析失败，尝试容错恢复；前 200 字符=%s", raw[:200])

    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        start = raw.find(start_ch)
        end = raw.rfind(end_ch)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    return {}
