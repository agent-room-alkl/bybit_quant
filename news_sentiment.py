#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻情绪分析模块 — 基于 GPT-4o API
每30分钟抓取加密新闻，用 GPT-4o 分析地缘政治/宏观经济对 SOL 的影响，
返回情绪分数 (-100 ~ +100) 供策略使用。
"""
import time
import json
import logging
import threading
from typing import Optional, Dict, Any

import requests

log = logging.getLogger("news_sentiment")

# ── 缓存配置 ──
CACHE_TTL_SEC = 30 * 60  # 30分钟缓存，避免频繁调 API
_cache: Dict[str, Any] = {
    "score": 0,
    "summary": "",
    "headlines": [],
    "timestamp": 0,
    "error": None,
}
_lock = threading.Lock()


# ── 新闻抓取 ──────────────────────────────────────────────────

def _fetch_crypto_news() -> list:
    """从多个免费源抓取加密货币新闻标题"""
    headlines = []

    # 源1: CoinGecko 状态/趋势 (免费)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            for coin in data.get("coins", [])[:5]:
                item = coin.get("item", {})
                name = item.get("name", "")
                score = item.get("score", 0)
                headlines.append(f"Trending: {name} (rank #{score + 1})")
    except Exception as e:
        log.warning(f"[NEWS] CoinGecko fetch failed: {e}")

    # 源2: CoinTelegraph RSS
    try:
        r = requests.get(
            "https://api.rss2json.com/v1/api.json?rss_url=https://cointelegraph.com/rss",
            timeout=10,
        )
        if r.ok:
            items = r.json().get("items", [])[:8]
            for item in items:
                title = item.get("title", "")
                if title:
                    headlines.append(f"CoinTelegraph: {title}")
    except Exception as e:
        log.warning(f"[NEWS] CoinTelegraph fetch failed: {e}")

    # 源3: CoinDesk RSS
    try:
        r = requests.get(
            "https://api.rss2json.com/v1/api.json?rss_url=https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
        )
        if r.ok:
            items = r.json().get("items", [])[:8]
            for item in items:
                title = item.get("title", "")
                if title:
                    headlines.append(f"CoinDesk: {title}")
    except Exception as e:
        log.warning(f"[NEWS] CoinDesk RSS fetch failed: {e}")

    return headlines


# ── GPT-4o API 分析 ──────────────────────────────────────────

ANALYSIS_PROMPT = """你是一个专业的加密货币宏观分析师。根据以下最新新闻标题，分析当前地缘政治和宏观经济环境对加密货币市场（特别是 Solana/SOL）的影响。

## 新闻标题：
{headlines}

## 分析要求：
1. 判断整体情绪：利好(正面) 还是 利空(负面)
2. 重点关注：
   - 美联储政策 (利率、CPI、PPI)
   - 地缘政治 (战争、停火、制裁)
   - 监管政策 (SEC、ETF、加密法案)
   - 机构动态 (BlackRock、Fidelity 等)
   - Solana 生态相关新闻
3. 给出情绪分数

## 输出格式（严格JSON，不要额外文字）：
{{
    "score": <整数, -100到+100, 0=中性, 正=利好, 负=利空>,
    "confidence": <浮点数, 0.0到1.0, 你对判断的信心>,
    "key_factors": ["因素1", "因素2", "因素3"],
    "summary": "一句话总结当前宏观环境",
    "risk_level": "<low/medium/high>",
    "suggested_action": "<aggressive_buy/cautious_buy/hold/cautious_sell/aggressive_sell>"
}}
"""


def _call_llm_api(api_key: str, headlines: list, model: str = "gpt-4o") -> Dict:
    """调用 GPT-4o API 分析新闻情绪"""
    headlines_text = "\n".join(f"- {h}" for h in headlines)
    prompt = ANALYSIS_PROMPT.format(headlines=headlines_text)

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 512,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": "你是加密货币宏观分析师，只输出JSON格式。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30,
        )

        if r.status_code != 200:
            log.error(f"[NEWS] GPT API error {r.status_code}: {r.text[:200]}")
            return {"score": 0, "confidence": 0, "summary": f"API error: {r.status_code}",
                    "key_factors": [], "risk_level": "medium", "suggested_action": "hold"}

        resp = r.json()
        text = resp["choices"][0]["message"]["content"].strip()

        # 解析 JSON（去掉可能的 markdown 代码块包裹）
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        result = json.loads(text.strip())

        # 校验 score 范围
        result["score"] = max(-100, min(100, int(result.get("score", 0))))
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))

        log.info(f"[NEWS] GPT 情绪分析: score={result['score']}, "
                 f"conf={result['confidence']:.0%}, action={result.get('suggested_action', 'hold')}")
        log.info(f"[NEWS] 摘要: {result.get('summary', 'N/A')}")

        return result

    except json.JSONDecodeError as e:
        log.error(f"[NEWS] GPT 返回非 JSON: {e}")
        return {"score": 0, "confidence": 0, "summary": "Parse error",
                "key_factors": [], "risk_level": "medium", "suggested_action": "hold"}
    except Exception as e:
        log.error(f"[NEWS] GPT API 调用失败: {e}")
        return {"score": 0, "confidence": 0, "summary": str(e),
                "key_factors": [], "risk_level": "medium", "suggested_action": "hold"}


# ── 公开接口 ──────────────────────────────────────────────────

def get_news_sentiment(api_key: str, model: str = "gpt-4o") -> Dict[str, Any]:
    """
    获取新闻情绪分数（带30分钟缓存）。

    Returns:
        {
            "score": int,          # -100 ~ +100
            "confidence": float,   # 0.0 ~ 1.0
            "summary": str,
            "key_factors": list,
            "risk_level": str,
            "suggested_action": str,
            "headlines": list,
            "cached": bool,
            "age_min": float,      # 缓存已存在多少分钟
        }
    """
    global _cache

    with _lock:
        age = time.time() - _cache["timestamp"]
        if _cache["timestamp"] > 0 and age < CACHE_TTL_SEC:
            return {**_cache, "cached": True, "age_min": round(age / 60, 1)}
        # 立即占位，防止其他线程也发起 API 调用
        _cache["timestamp"] = time.time()

    # 缓存过期，重新分析
    if not api_key:
        log.warning("[NEWS] 无 API key，跳过新闻情绪分析")
        return {"score": 0, "confidence": 0, "summary": "No API key",
                "key_factors": [], "risk_level": "medium", "suggested_action": "hold",
                "headlines": [], "cached": False, "age_min": 0}

    # 1. 抓新闻
    headlines = _fetch_crypto_news()
    if not headlines:
        log.warning("[NEWS] 未抓取到任何新闻")
        return {"score": 0, "confidence": 0, "summary": "No news fetched",
                "key_factors": [], "risk_level": "medium", "suggested_action": "hold",
                "headlines": [], "cached": False, "age_min": 0}

    log.info(f"[NEWS] 抓取到 {len(headlines)} 条新闻，调用 GPT-4o 分析...")

    # 2. GPT 分析
    result = _call_llm_api(api_key, headlines, model)

    # 3. 更新缓存并在锁内复制返回值
    with _lock:
        _cache = {
            **result,
            "headlines": headlines[:],  # 深拷贝列表
            "timestamp": time.time(),
            "error": None,
        }
        ret = {**_cache, "cached": False, "age_min": 0}

    return ret


def get_cached_score() -> int:
    """快速获取缓存的情绪分数，不触发 API 调用"""
    with _lock:
        return _cache.get("score", 0)


def get_cached_sentiment() -> Dict[str, Any]:
    """快速获取缓存的完整情绪数据"""
    with _lock:
        age = time.time() - _cache["timestamp"] if _cache["timestamp"] > 0 else 999
        return {**_cache, "cached": True, "age_min": round(age / 60, 1)}
