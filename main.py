from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any
from datetime import datetime, timezone
import json
import time
import re

app = FastAPI(title="P2P Aggregator API", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════ КЭШ ═══════════════════════════
CACHE: Dict[str, tuple] = {}
CACHE_TTL = 10
BANK_CACHE: Dict[str, tuple] = {}
BANK_CACHE_TTL = 3600

# ═══════════════════════════ МАППИНГ ПЛАТЁЖЕК ═══════════════════════════
PAYMENT_ID_MAP = {
    "14": "Tinkoff", "40": "Sberbank", "18": "VTB", "90": "SBP",
    "28": "AlfaBank", "30": "Raiffeisen", "31": "Gazprom",
}

BYBIT_FIAT_MAP = {
    "RUB": "RUB", "IDR": "IDR", "THB": "THB", "TRY": "TRY",
    "AED": "AED", "VND": "VND", "INR": "INR", "CNY": "CNY",
    "USD": "USD", "EUR": "EUR",
}

EXCHANGE_WEIGHT = {"bybit": 1.0, "htx": 0.88}

# ═══════════════════════════ РАНЖИРОВЩИК ═══════════════════════════
def score_merchant(m: Dict[str, Any], amount: float) -> float:
    price = m["price"]
    reliability = m["success_rate"] / 100.0
    trades = m["completed_trades"]
    exchange_weight = EXCHANGE_WEIGHT.get(m["exchange"], 0.8)
    liquidity = min(m["max_amount"] / amount, 2.0)
    
    if trades < 10: trust_penalty = 0.20
    elif trades < 50: trust_penalty = 0.08
    elif trades < 200: trust_penalty = 0.02
    else: trust_penalty = 0.0
    
    return price * (1 + trust_penalty) / max(reliability * exchange_weight * liquidity, 0.01)

def get_confidence_level(m: Dict[str, Any]) -> str:
    if m["success_rate"] >= 95 and m["completed_trades"] >= 100: return "best"
    elif m["success_rate"] >= 90 and m["completed_trades"] >= 50: return "verified"
    elif m["success_rate"] >= 80 and m["completed_trades"] >= 10: return "ok"
    else: return "risky"

# ═══════════════════════════ BYBIT P2P ═══════════════════════════
async def fetch_bybit_merchants(crypto: str, fiat: str, amount: float, methods: List[str]) -> List[Dict[str, Any]]:
    url = "https://api2.bybit.com/fiat/otc/item/online"
    bybit_fiat = BYBIT_FIAT_MAP.get(fiat, fiat)
    
    payload = {
        "tokenId": crypto, "currencyId": bybit_fiat,
        "side": "1", "size": "30", "page": "1",
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
            if response.status_code != 200: return []
            data = response.json()
            if data.get("ret_code") != 0: return []
            
            items = data.get("result", {}).get("items", [])
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
                        if not any(m in payments_lower for m in methods_lower): continue

                    if amount < min_amount or amount > max_amount: continue
                    if completed_rate < 0.50: continue

                    m = {
                        "id": str(adv_no) if adv_no else str(abs(hash(nickname + str(price)))),
                        "exchange": "bybit", "merchant_name": nickname,
                        "price": price, "available_amount": quantity,
                        "min_amount": min_amount, "max_amount": max_amount,
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
                except Exception: continue

            merchants.sort(key=lambda m: m["score"])
            return merchants[:20]
        except Exception as e:
            print(f"❌ Bybit {crypto}/{fiat}: {e}")
            return []

# ═══════════════════════════ P2P ENDPOINT ═══════════════════════════
@app.get("/api/p2p/merchants")
async def get_p2p_merchants(
    crypto: str = Query("USDT"), fiat: str = Query("RUB"),
    amount: float = Query(10000), payment_methods: str = Query("")
):
    methods = [m.strip() for m in payment_methods.split(",")] if payment_methods else []
    
    cache_key = f"{crypto}_{fiat}_{amount}_{payment_methods}"
    if cache_key in CACHE:
        ts, cached_data = CACHE[cache_key]
        if time.time() - ts < CACHE_TTL: return cached_data
    
    tasks = [fetch_bybit_merchants(crypto, fiat, amount, methods)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_merchants = []
    for result in results:
        if not isinstance(result, Exception): all_merchants.extend(result)
    
    if not all_merchants:
        result = {"error": "No liquidity", "merchants": [], "stats": {}, "best_rate": 0, "best_exchange": "none", "spread": 0, "trends": {}, "updated_at": datetime.now(timezone.utc).isoformat()}
        CACHE[cache_key] = (time.time(), result)
        return result
    
    all_merchants.sort(key=lambda m: m.get("score", 999))
    filtered = [m for m in all_merchants if amount >= m["min_amount"]][:40]
    
    best_rate = filtered[0]["price"] if filtered else 0
    best_exchange = filtered[0]["exchange"] if filtered else "none"
    prices = [m["price"] for m in filtered if m["price"] > 0]
    spread = round(max(prices) - min(prices), 2) if prices else 0
    
    stats: Dict[str, Any] = {}
    for m in filtered:
        ex = m["exchange"]
        if ex not in stats: stats[ex] = {"exchange": ex, "prices": [], "merchant_count": 0, "confidences": []}
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
        "merchants": filtered, "stats": stats,
        "best_rate": best_rate, "best_exchange": best_exchange,
        "best_confidence": filtered[0].get("confidence", "ok") if filtered else "none",
        "spread": spread, "trends": {},
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    CACHE[cache_key] = (time.time(), result)
    return result

# ═══════════════════════════ КУРСЫ БАНКОВ ═══════════════════════════

async def fetch_tinkoff_rate():
    """Т-Банк через официальный API"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.tinkoff.ru/v1/currency_rates",
                json={},
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("resultCode") == "OK":
                    rates = data.get("payload", {}).get("rates", [])
                    
                    # DebitCardsTransfers — категория для переводов между счетами
                    for rate in rates:
                        if (rate.get("category") == "DebitCardsTransfers" and
                            rate.get("fromCurrency", {}).get("strCode") == "840" and  # USD
                            rate.get("toCurrency", {}).get("strCode") == "643"):      # RUB
                            
                            api_buy = rate.get("buy", 0)    # Банк покупает USD у вас
                            api_sell = rate.get("sell", 0)  # Банк продаёт USD вам
                            
                            # Для нашего приложения: buy = банк продаёт вам USD
                            print(f"✅ Tinkoff: buy={api_sell}, sell={api_buy}")
                            return {"buy": api_sell, "sell": api_buy}
                    
                    # Fallback: любая категория USD→RUB
                    for rate in rates:
                        if (rate.get("fromCurrency", {}).get("strCode") == "840" and
                            rate.get("toCurrency", {}).get("strCode") == "643"):
                            api_buy = rate.get("buy", 0)
                            api_sell = rate.get("sell", 0)
                            print(f"⚠️ Tinkoff fallback: buy={api_sell}, sell={api_buy}")
                            return {"buy": api_sell, "sell": api_buy}
    except Exception as e:
        print(f"❌ Tinkoff: {e}")
    return None

async def fetch_sber_rate():
    """Сбербанк через парсинг страницы"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://www.sberbank.ru/ru/quotes/currencies",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            if response.status_code == 200:
                html = response.text
                match = re.search(r'"isoCode":"USD".*?"buyPrice":(\d+\.?\d*).*?"sellPrice":(\d+\.?\d*)', html)
                if match:
                    return {"buy": float(match.group(1)), "sell": float(match.group(2))}
    except Exception as e:
        print(f"❌ Sber: {e}")
    return None

async def fetch_banki_ru_rate(bank_code: str):
    """Banki.ru — агрегатор курсов"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://www.banki.ru/products/currency/cash/{bank_code}/moskva/",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if response.status_code == 200:
                html = response.text
                match = re.search(r'USD.*?(\d+\.\d+).*?(\d+\.\d+)', html, re.DOTALL)
                if match:
                    return {"buy": float(match.group(2)), "sell": float(match.group(1))}
    except Exception as e:
        print(f"❌ Banki.ru {bank_code}: {e}")
    return None

@app.get("/api/bank/rates")
async def get_bank_rates():
    cache_key = "bank_rates"
    if cache_key in BANK_CACHE:
        ts, cached = BANK_CACHE[cache_key]
        if time.time() - ts < BANK_CACHE_TTL:
            return cached
    
    rates = {}
    
    # Т-Банк — API
    t = await fetch_tinkoff_rate()
    if t and t["buy"] > 0:
        rates["tinkoff"] = t
        print(f"✅ Tinkoff: buy={t['buy']}, sell={t['sell']}")
    
    # Сбер — парсинг
    s = await fetch_sber_rate()
    if s and s["buy"] > 0:
        rates["sber"] = s
    else:
        s = await fetch_banki_ru_rate("sberbank")
        if s: rates["sber"] = s
    
    # Альфа — Banki.ru
    a = await fetch_banki_ru_rate("alfabank")
    if a: rates["alfa"] = a
    
    # ВТБ — Banki.ru
    v = await fetch_banki_ru_rate("vtb")
    if v: rates["vtb"] = v
    
    # Fallback
    if "tinkoff" not in rates: rates["tinkoff"] = {"buy": 78.5, "sell": 74.4}
    if "sber" not in rates: rates["sber"] = {"buy": 79.2, "sell": 71.7}
    if "alfa" not in rates: rates["alfa"] = {"buy": 81.8, "sell": 73.0}
    if "vtb" not in rates: rates["vtb"] = {"buy": 83.0, "sell": 71.5}
    
    result = {"rates": rates, "updated_at": datetime.now(timezone.utc).isoformat()}
    BANK_CACHE[cache_key] = (time.time(), result)
    return result

@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return {"name": "P2P Aggregator API", "version": "3.1.0"}
