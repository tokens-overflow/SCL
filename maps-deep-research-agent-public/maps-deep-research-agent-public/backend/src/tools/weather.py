"""天气助手 —— 用 wttr.in（免费、免 key）拉取锚点附近的多日预报。

刻意做成"尽力而为"：任何网络/解析异常都返回空串，绝不拖垮报告生成。
返回的是一段给 LLM 看的中文文本，标明这是"近期预报参考"，让模型据此给出
带雨备选、防晒/保暖、室内外取舍等**实用**建议。

注：之前用的 Open-Meteo 在部分网络环境不可达，改用 wttr.in（覆盖全球、含坐标查询）。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# wttr.in 支持 "lat,lng" 直接查询；j1 = JSON v1，含未来 3 天日级 + 逐时
_WTTR = "https://wttr.in/{lat},{lng}?format=j1"

# 常见英文天气描述 → 中文（命中则替换，未命中保留原文）
_DESC_ZH = {
    "Sunny": "晴", "Clear": "晴", "Partly cloudy": "晴间多云", "Cloudy": "多云",
    "Overcast": "阴", "Mist": "薄雾", "Fog": "雾", "Freezing fog": "雾凇",
    "Patchy rain possible": "局部有雨", "Patchy rain nearby": "局部有雨",
    "Light rain": "小雨", "Light rain shower": "小阵雨", "Moderate rain": "中雨",
    "Heavy rain": "大雨", "Patchy light rain": "零星小雨", "Light drizzle": "毛毛雨",
    "Thundery outbreaks possible": "可能有雷阵雨", "Patchy light drizzle": "零星毛毛雨",
    "Moderate or heavy rain shower": "中到大阵雨", "Torrential rain shower": "暴雨",
    "Light snow": "小雪", "Moderate snow": "中雪", "Heavy snow": "大雪",
    "Patchy snow possible": "局部有雪", "Light sleet": "雨夹雪",
    "Blizzard": "暴风雪", "Snow": "雪",
}


_DESC_ZH_CI = {k.lower(): v for k, v in _DESC_ZH.items()}


def _zh(desc: str) -> str:
    s = desc.strip()
    return _DESC_ZH_CI.get(s.lower(), s)


async def fetch_weather(lat: float, lng: float, days: int = 3) -> str:
    """拉取 (lat,lng) 处未来数天的日级预报，渲染成中文文本块。

    失败返回空串。调用方据此决定是否把天气信息拼进 prompt。
    """
    if not lat or not lng:
        return ""
    url = _WTTR.format(lat=round(lat, 4), lng=round(lng, 4))
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "cartograph/1.0"})
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # pragma: no cover - 尽力而为
        logger.warning("天气拉取失败（忽略）", exc_info=True)
        return ""

    weather = data.get("weather") or []
    if not weather:
        return ""

    lines = ["（近期天气预报，仅供参考；据此给出雨天备选、穿着与室内外取舍）"]
    for day in weather[: max(1, days)]:
        date = day.get("date", "?")
        hi = day.get("maxtempC", "?")
        lo = day.get("mintempC", "?")
        hourly = day.get("hourly") or []
        # 取中午时段的天气描述作为当天代表
        noon = next((h for h in hourly if h.get("time") in ("1200", "1300")), None)
        noon = noon or (hourly[len(hourly) // 2] if hourly else {})
        desc = ""
        if noon.get("weatherDesc"):
            desc = _zh(noon["weatherDesc"][0].get("value", ""))
        # 白天时段最大降水概率
        rain_vals = [
            int(h.get("chanceofrain", 0) or 0)
            for h in hourly
            if h.get("time") in ("900", "1200", "1500", "1800")
        ]
        rain = max(rain_vals) if rain_vals else 0
        lines.append(f"- {date}：{desc or '—'}，{lo}~{hi}℃，降水概率 {rain}%")
    return "\n".join(lines)
