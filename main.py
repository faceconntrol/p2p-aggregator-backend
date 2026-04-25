from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import json
import time

app = FastAPI(title="P2P Aggregator API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════
# КЭШ + ИСТОРИЯ ЦЕН (для трендов)
# ═══════════════════════════════════════════
CACHE: Dict[str, tuple] = {}
CACHE_TTL = 10
PRICE_HISTORY: Dict[str, List[float]] = {}  # Для отслеживания трендов

# ═══════════════════════════════════════════
# ВЕСА БИРЖ (доверие к площадке)
# ═══════════════════════════════════════════
EXCHANGE_WEIGHT = {
    "bybit": 1.0,    # Крупнейшая P2P площадка РФ
    "htx": 0.88,     # Huobi, меньше ликвидности
    "bitget": 0.85,  # Растущая, но меньше мерчантов
    "gateio": 0.80,  # Меньше объёмов в РФ
}

# ═══════════════════════════════════════════
# МАППИНГ ПЛАТЁЖЕК
# ═══════════════════════════════════════════
PAYMENT_ID_MAP = {
    "14": "Tinkoff",
    "40": "Sberbank",
    "18": "VTB",
    "90": "SBP",
    "1": "QIWI",
    "5": "YooMoney",
    "6": "AdvCash",
    "28": "AlfaBank",
    "30": "Raiffeisen",
    "31": "Gazprom",
    "21": "RosBank",
    "17": "PostBank",
}

# ═══════════════════════════════════════════
# ПРОФЕССИОНАЛЬНЫЙ РАНЖИРОВЩИК
# ═══════════════════════════════════════════
def score_merchant(m: Dict[str, Any], amount: float) -> float:
    """
    Multi-exchange scoring algorithm.
    Учитывает: цену, надёжность, ликвидность, вес биржи, количество сделок.
    Чем НИЖЕ score — тем ЛУЧШЕ предложение.
    """
    price = m["price"]
    reliability = m["success_rate"] / 100.0  # 0..1
    trades = m["completed_trades"]
    exchange_weight = EXCHANGE_WEIGHT.get(m["exchange"], 0.8)
    
    # Ликвидность: насколько объём превышает запрошенную сумму
    liquidity = min(m["max_amount"] / amount, 2.0)  # кэп на 2x
    
    # Штраф за мало сделок (доверие к мерчанту)
    if trades < 10:
        trust_penalty = 0.20      # Новый мерчант — высокий риск
    elif trades < 50:
        trust_penalty = 0.08      # Мало истории
    elif trades < 200:
        trust_penalty = 0.02      # Нормально
    else:
        trust_penalty = 0.0       # Опытный мерчант
    
    # Итоговый скор (НИЖЕ = ЛУЧШЕ)
    # Делим на reliability, exchange_weight и liquidity — они повышают качество
    score = (
        price
        * (1 + trust_penalty)
        / max(reliability * exchange_weight * liquidity, 0.01)
    )
    
    return score


def get_confidence_level(m: Dict[str, Any]) -> str:
    """
    Определяет уровень надёжности сделки.
    """
    if m["success_rate"] >= 95 and m["completed_trades"] >= 100:
        return "best"      # 🟢 Best deal
    elif m["success_rate"] >= 90 and m["completed_trades"] >= 50:
        return "verified"  # ⭐ Verified
    elif m["success_rate"] >= 80 and m["completed_trades"] >= 10:
        return "ok"        # ✅ OK
    else:
        return "risky"     # ⚠️ Risky


def calculate_price_trend(exchange: str, current_price: float) -> str:
    """
    Отслеживает тренд цены на бирже.
    """
    key = f"{exchange}"
    if key not in PRICE_HISTORY:
        PRICE_HISTORY[key] = []
    
    history = PRICE_HISTORY[key]
    history.append(current_price)
    
    # Храним последние 5 значений
    if len(history) > 5:
        history.pop(0)
    
    if len(history) < 2:
        return "stable"
    
    prev_avg = sum(history[:-1]) / (len(history) - 1)
    
    if current_price < prev_avg * 0.995:
        return "down"    # 🔽 Цена падает (выгоднее покупать)
    elif current_price > prev_avg * 1.005:
        return "up"      # 🔼 Цена растёт
    else:
        return "stable"  # ➡️ Стабильно


# ═══════════════════════════════════════════
# BYBIT P2P API
# ═══════════════════════════════════════════
async def fetch_bybit_merchants(
    crypto: str, fiat: str, amount: float, methods: List[str]
) -> List[Dict[str, Any]]:

    url = "https://api2.bybit.com/fiat/otc/item/online"

    payload = {
        "tokenId": crypto,
        "currencyId": fiat,
        "side": "1",
        "size": "30",
        "page": "1",
        "amount": str(int(amount))
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.bybit.com",
        "Referer": "https://www.bybit.com/",
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.post(url, json=payload)

            if response.status_code != 200:
                print(f"❌ Bybit status: {response.status_code}")
                return []

            data = response.json()

            if data.get("ret_code") != 0:
                print(f"❌ Bybit error: {data.get('ret_msg')}")
                return []

            items = data.get("result", {}).get("items", [])
            print(f"📊 Bybit: {len(items)} items")

            merchants = []

            for item in items:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minAmount", 0))
                    max_amount = float(item.get("maxAmount", 0))
                    quantity = float(item.get("quantity", 0))

                    completed_rate_raw = float(item.get("recentExecuteRate", 0))
                    completed_rate = completed_rate_raw / 100 if completed_rate_raw > 1 else completed_rate_raw
                    
                    completed_count = int(item.get("recentOrderNum", item.get("recentExecuteNum", 0)))

                    nickname = item.get("nickname", item.get("nickName", "Unknown"))
                    adv_no = item.get("advNo", "")
                    payment_ids = item.get("paymentIds", item.get("payments", []))
                    payments = [PAYMENT_ID_MAP.get(str(p), str(p)) for p in payment_ids]

                    if methods:
                        methods_lower = [m.lower() for m in methods]
                        payments_lower = [p.lower() for p in payments]
                        if not any(m in payments_lower for m in methods_lower):
                            continue

                    if amount < min_amount or amount > max_amount:
                        continue

                    if completed_rate < 0.50:
                        continue

                    m = {
                        "id": str(adv_no) if adv_no else str(abs(hash(nickname + str(price)))),
                        "exchange": "bybit",
                        "merchant_name": nickname,
                        "price": price,
                        "available_amount": quantity,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": round(completed_rate * 100, 1),
                        "completed_trades": completed_count,
                        "payment_methods": payments,
                        "is_verified": completed_rate >= 0.85 and completed_count >= 10,
                        "deep_link": f"https://www.bybit.com/fiat/trade/otc/detail?advNo={adv_no}" if adv_no else "https://www.bybit.com/fiat/trade/otc",
                        "web_link": "https://www.bybit.com/fiat/trade/otc"
                    }
                    m["confidence"] = get_confidence_level(m)
                    m["score"] = round(score_merchant(m, amount), 2)
                    merchants.append(m)

                except Exception:
                    continue

            merchants.sort(key=lambda m: m["score"])
            print(f"  ✅ Bybit ranked: {len(merchants)}")
            return merchants[:20]

        except Exception as e:
            print(f"❌ Bybit exception: {e}")
            return []

# ═══════════════════════════════════════════
# HTX P2P API
# ═══════════════════════════════════════════
async def fetch_htx_merchants(
    crypto: str, fiat: str, amount: float, methods: List[str]
) -> List[Dict[str, Any]]:
    
    coin_map = {"USDT": "2", "BTC": "1", "ETH": "3", "USDC": "4"}
    currency_map = {"RUB": "11", "USD": "2", "EUR": "3"}
    
    url = "https://www.htx.com/-/x/otc/v1/data/trade-market"
    params = {
        "coinId": coin_map.get(crypto, "2"),
        "currency": currency_map.get(fiat, "11"),
        "tradeType": "buy",
        "currPage": "1",
        "payMethod": "0",
        "online": "1",
        "amount": str(int(amount)),
        "blockType": "general",
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Origin": "https://www.htx.com",
        "Referer": "https://www.htx.com/ru-ru/fiat-crypto/trade",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Connection": "keep-alive",
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.get(url, params=params)
            
            if response.status_code != 200:
                return []
            
            data = response.json()
            
            if data.get("code") == 200:
                items = data.get("data", [])
                
                if isinstance(items, dict):
                    items = items.get("data", items.get("list", []))
                
                if not items:
                    return []
                
                print(f"📊 HTX: {len(items)} items")
                
                merchants = []
                for item in items[:20]:
                    try:
                        price = float(item.get("price", 0))
                        min_amount = float(item.get("minTradeLimit", item.get("minAmount", 0)))
                        max_amount = float(item.get("maxTradeLimit", item.get("maxAmount", 0)))
                        
                        finish_rate = float(item.get("monthFinishRate", item.get("finishRate", 0.95)))
                        order_count = int(item.get("monthOrderCount", item.get("orderCount", 0)))
                        
                        if finish_rate < 0.50 or max_amount < amount:
                            continue
                        
                        ad_id = str(item.get("adId", item.get("id", "")))
                        
                        pay_raw = item.get("payMethods", item.get("payments", []))
                        pay_methods = []
                        for p in pay_raw:
                            if isinstance(p, dict):
                                pay_methods.append(p.get("name", ""))
                            else:
                                pay_methods.append(str(p))
                        
                        if methods:
                            methods_lower = [m.lower() for m in methods]
                            pay_lower = [p.lower() for p in pay_methods]
                            if not any(m in pay_lower for m in methods_lower):
                                continue
                        
                        m = {
                            "id": ad_id if ad_id else str(abs(hash(item.get("userName", "") + str(price)))),
                            "exchange": "htx",
                            "merchant_name": item.get("userName", item.get("nickname", "Unknown")),
                            "price": price,
                            "available_amount": max_amount,
                            "min_amount": min_amount,
                            "max_amount": max_amount,
                            "success_rate": round(finish_rate * 100 if finish_rate < 1 else finish_rate, 1),
                            "completed_trades": order_count,
                            "payment_methods": pay_methods,
                            "is_verified": item.get("isOnline", False) and order_count >= 10,
                            "deep_link": f"https://www.htx.com/ru-ru/fiat-crypto/trade/detail?adId={ad_id}" if ad_id else "https://www.htx.com/ru-ru/fiat-crypto/trade",
                            "web_link": "https://www.htx.com/ru-ru/fiat-crypto/trade"
                        }
                        m["confidence"] = get_confidence_level(m)
                        m["score"] = round(score_merchant(m, amount), 2)
                        merchants.append(m)
                    except (ValueError, TypeError):
                        continue
                
                merchants.sort(key=lambda m: m["score"])
                print(f"  ✅ HTX ranked: {len(merchants)}")
                return merchants[:15]
            
            return []
            
        except Exception as e:
            print(f"❌ HTX exception: {e}")
            return []

# ═══════════════════════════════════════════
# API ENDPOINT
# ═══════════════════════════════════════════
@app.get("/api/p2p/merchants")
async def get_p2p_merchants(
    crypto: str = Query("USDT"),
    fiat: str = Query("RUB"),
    amount: float = Query(10000),
    payment_methods: str = Query("")
):
    methods = [m.strip() for m in payment_methods.split(",")] if payment_methods else []
    print(f"🚀 Request: {crypto}/{fiat}, {amount} RUB, methods: {methods}")
    
    cache_key = f"{crypto}_{fiat}_{amount}_{payment_methods}"
    if cache_key in CACHE:
        ts, cached_data = CACHE[cache_key]
        if time.time() - ts < CACHE_TTL:
            print(f"📦 Returning cached data")
            return cached_data
    
    tasks = [
        fetch_bybit_merchants(crypto, fiat, amount, methods),
        fetch_htx_merchants(crypto, fiat, amount, methods),
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_merchants = []
    for result in results:
        if not isinstance(result, Exception):
            all_merchants.extend(result)
    
    if not all_merchants:
        result = {
            "error": "No liquidity available",
            "merchants": [],
            "stats": {},
            "best_rate": 0,
            "best_exchange": "none",
            "spread": 0,
            "trends": {},
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        CACHE[cache_key] = (time.time(), result)
        return result
    
    # Сортируем по профессиональному скору
    all_merchants.sort(key=lambda m: m.get("score", 999))
    
    # Фильтруем по доступной сумме
    filtered = [m for m in all_merchants if amount >= m["min_amount"]][:40]
    
    # Лучший курс
    best = filtered[0] if filtered else None
    best_rate = best["price"] if best else 0
    best_exchange = best["exchange"] if best else "none"
    
    # Спред (разница между лучшей и худшей ценой)
    prices = [m["price"] for m in filtered if m["price"] > 0]
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 0
    spread = round(max_price - min_price, 2)
    
    # Тренды цен по биржам
    trends = {}
    for ex in ["bybit", "htx"]:
        ex_merchants = [m for m in filtered if m["exchange"] == ex]
        if ex_merchants:
            best_ex_price = min(m["price"] for m in ex_merchants)
            trends[ex] = {
                "best_price": best_ex_price,
                "trend": calculate_price_trend(ex, best_ex_price)
            }
    
    # Статистика по биржам
    stats: Dict[str, Any] = {}
    for m in filtered:
        ex = m["exchange"]
        if ex not in stats:
            stats[ex] = {"exchange": ex, "prices": [], "merchant_count": 0, "confidences": []}
        stats[ex]["prices"].append(m["price"])
        stats[ex]["confidences"].append(m.get("confidence", "ok"))
    
    for ex, data in stats.items():
        prices_list = data.pop("prices")
        confidences = data.pop("confidences")
        data["buy_price"] = min(prices_list)
        data["merchant_count"] = len(prices_list)
        data["avg_price"] = round(sum(prices_list) / len(prices_list), 2)
        data["best_deals"] = confidences.count("best")
        data["verified_deals"] = confidences.count("verified")
    
    result = {
        "merchants": filtered,
        "stats": stats,
        "best_rate": best_rate,
        "best_exchange": best_exchange,
        "best_confidence": best["confidence"] if best else "none",
        "spread": spread,
        "trends": trends,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    
    CACHE[cache_key] = (time.time(), result)
    
    print(f"🎯 Returning {len(filtered)} merchants from {len(stats)} exchanges, best=₽{best_rate}, spread=₽{spread}")
    return result

@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return {"name": "P2P Aggregator API", "version": "3.0.0", "features": ["multi-exchange ranking", "confidence levels", "spread", "trends"]}
