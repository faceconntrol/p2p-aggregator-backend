from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any
from datetime import datetime, timezone
import json
import time
import re

app = FastAPI(title="P2P Aggregator API", version="3.3.0")

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

EXCHANGE_WEIGHT = {"bybit": 1.0, "htx": 0.88, "binance": 0.95, "mexc": 0.9}

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
        except Exception: return []

# ═══════════════════════════ HTX P2P ═══════════════════════════
async def fetch_htx_merchants(crypto: str, fiat: str, amount: float, methods: List[str]) -> List[Dict[str, Any]]:
    coin_map = {"USDT": "2", "BTC": "1", "ETH": "3"}
    currency_map = {"RUB": "11", "USD": "2", "EUR": "3", "IDR": "6", "THB": "5", "TRY": "7"}
    
    url = "https://www.htx.com/-/x/otc/v1/data/trade-market"
    params = {
        "coinId": coin_map.get(crypto, "2"),
        "currency": currency_map.get(fiat, "11"),
        "tradeType": "buy", "currPage": "1", "payMethod": "0",
        "online": "1", "amount": str(int(amount)), "blockType": "general",
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.htx.com",
        "Connection": "keep-alive",
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.get(url, params=params)
            if response.status_code != 200: return []
            data = response.json()
            if data.get("code") != 200: return []
            
            items = data.get("data", [])
            if isinstance(items, dict): items = items.get("data", items.get("list", []))
            if not items: return []
            
            merchants = []
            for item in items[:20]:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minTradeLimit", item.get("minAmount", 0)))
                    max_amount = float(item.get("maxTradeLimit", item.get("maxAmount", 0)))
                    finish_rate = float(item.get("monthFinishRate", 0.95))
                    order_count = int(item.get("monthOrderCount", 0))
                    
                    if finish_rate < 0.50 or max_amount < amount: continue
                    if amount < min_amount or amount > max_amount: continue
                    
                    ad_id = str(item.get("adId", ""))
                    pay_methods = [p.get("name", "") if isinstance(p, dict) else str(p) for p in item.get("payMethods", [])]
                    
                    m = {
                        "id": ad_id if ad_id else str(abs(hash(item.get("userName", "") + str(price)))),
                        "exchange": "htx", "merchant_name": item.get("userName", "Unknown"),
                        "price": price, "available_amount": max_amount,
                        "min_amount": min_amount, "max_amount": max_amount,
                        "success_rate": round(finish_rate * 100 if finish_rate < 1 else finish_rate, 1),
                        "completed_trades": order_count, "payment_methods": pay_methods,
                        "is_verified": item.get("isOnline", False) and order_count >= 10,
                        "deep_link": f"https://www.htx.com/ru-ru/fiat-crypto/trade/detail?adId={ad_id}" if ad_id else "https://www.htx.com/ru-ru/fiat-crypto/trade",
                        "web_link": "https://www.htx.com/ru-ru/fiat-crypto/trade"
                    }
                    m["confidence"] = get_confidence_level(m)
                    m["score"] = round(score_merchant(m, amount), 2)
                    merchants.append(m)
                except: continue
            
            merchants.sort(key=lambda m: m["score"])
            return merchants[:15]
        except: return []

# ═══════════════════════════ BINANCE P2P ═══════════════════════════
async def fetch_binance_merchants(crypto: str, fiat: str, amount: float, methods: List[str]) -> List[Dict[str, Any]]:
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    
    payload = {
        "asset": crypto, "fiat": fiat, "tradeType": "BUY",
        "page": 1, "rows": 20, "transAmount": str(int(amount))
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://p2p.binance.com",
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.post(url, json=payload)
            if response.status_code != 200: return []
            data = response.json()
            items = data.get("data", [])
            
            merchants = []
            for item in items:
                try:
                    adv = item.get("adv", {})
                    advertiser = item.get("advertiser", {})
                    
                    price = float(adv.get("price", 0))
                    min_amount = float(adv.get("minSingleTransAmount", 0))
                    max_amount = float(adv.get("maxSingleTransAmount", 0))
                    quantity = float(adv.get("surplusAmount", 0))
                    success_rate = float(advertiser.get("monthFinishRate", 0.95)) * 100
                    trades = int(advertiser.get("monthOrderCount", 0))
                    nickname = advertiser.get("nickName", "Unknown")
                    adv_no = adv.get("advNo", "")
                    
                    if amount < min_amount or amount > max_amount: continue
                    if success_rate < 50: continue
                    
                    m = {
                        "id": str(adv_no), "exchange": "binance", "merchant_name": nickname,
                        "price": price, "available_amount": quantity,
                        "min_amount": min_amount, "max_amount": max_amount,
                        "success_rate": success_rate, "completed_trades": trades,
                        "payment_methods": [],
                        "is_verified": trades >= 10 and success_rate >= 85,
                        "deep_link": f"https://p2p.binance.com/ru/trade/detail?advNo={adv_no}" if adv_no else "https://p2p.binance.com/ru",
                        "web_link": "https://p2p.binance.com/ru"
                    }
                    m["confidence"] = get_confidence_level(m)
                    m["score"] = round(score_merchant(m, amount), 2)
                    merchants.append(m)
                except: continue
            
            merchants.sort(key=lambda m: m["score"])
            return merchants[:15]
        except: return []

# ═══════════════════════════ MEXC P2P (НОВЫЙ ENDPOINT) ═══════════════════════════
async def fetch_mexc_merchants(crypto: str, fiat: str, amount: float, methods: List[str]) -> List[Dict[str, Any]]:
    url = "https://www.mexc.com/api/platform/spot/market-v2/web/merchant/list"

    payload = {
        "tradeType": "BUY",
        "coinName": crypto,
        "fiatCurrency": fiat,
        "page": 1,
        "pageSize": 20
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.mexc.com",
        "Referer": "https://www.mexc.com/fiat/p2p"
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload, headers=headers)

            print("MEXC STATUS:", response.status_code)
            print(response.text[:500])

            if response.status_code != 200:
                return []

            data = response.json()
            items = data.get("data", {}).get("list", [])

            merchants = []
            for item in items:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minAmount", 0))
                    max_amount = float(item.get("maxAmount", 0))

                    if amount < min_amount or amount > max_amount:
                        continue

                    merchant = {
                        "id": str(item.get("id", "")),
                        "exchange": "mexc",
                        "merchant_name": item.get("nickName", "Unknown"),
                        "price": price,
                        "available_amount": float(item.get("quantity", 0)),
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": 95,
                        "completed_trades": 50,
                        "payment_methods": [],
                        "is_verified": True,
                        "deep_link": "https://www.mexc.com/fiat/p2p",
                        "web_link": "https://www.mexc.com/fiat/p2p"
                    }

                    merchant["confidence"] = get_confidence_level(merchant)
                    merchant["score"] = round(score_merchant(merchant, amount), 2)
                    merchants.append(merchant)

                except Exception as e:
                    print("MEXC parse:", e)

            merchants.sort(key=lambda x: x["score"])
            return merchants[:15]

    except Exception as e:
        print("MEXC exception:", e)
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
        ts, cached = CACHE[cache_key]
        if time.time() - ts < CACHE_TTL: return cached
    
    tasks = [
        fetch_bybit_merchants(crypto, fiat, amount, methods),
        fetch_htx_merchants(crypto, fiat, amount, methods),
        fetch_binance_merchants(crypto, fiat, amount, methods),
        fetch_mexc_merchants(crypto, fiat, amount, methods),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_merchants = []
    for r in results:
        if not isinstance(r, Exception): all_merchants.extend(r)
    
    if not all_merchants:
        result = {"error": "No liquidity", "merchants": [], "stats": {}, "best_rate": 0, "best_exchange": "none", "spread": 0, "updated_at": datetime.now(timezone.utc).isoformat()}
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
        if ex not in stats: stats[ex] = {"exchange": ex, "prices": [], "merchant_count": 0}
        stats[ex]["prices"].append(m["price"])
    
    for ex, data in stats.items():
        prices_list = data.pop("prices")
        data["buy_price"] = min(prices_list)
        data["merchant_count"] = len(prices_list)
        data["avg_price"] = round(sum(prices_list) / len(prices_list), 2)
    
    result = {
        "merchants": filtered, "stats": stats,
        "best_rate": best_rate, "best_exchange": best_exchange,
        "spread": spread,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    CACHE[cache_key] = (time.time(), result)
    return result

# ═══════════════════════════ КУРСЫ БАНКОВ ═══════════════════════════
async def fetch_tinkoff_rate():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.tinkoff.ru/v1/currency_rates", json={},
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("resultCode") == "OK":
                    for rate in data.get("payload", {}).get("rates", []):
                        if (rate.get("category") == "DebitCardsTransfers" and
                            rate.get("fromCurrency", {}).get("strCode") == "840" and
                            rate.get("toCurrency", {}).get("strCode") == "643"):
                            return {"buy": rate.get("sell", 80), "sell": rate.get("buy", 74)}
    except: pass
    return None

@app.get("/api/bank/rates")
async def get_bank_rates():
    cache_key = "bank_rates"
    if cache_key in BANK_CACHE:
        ts, cached = BANK_CACHE[cache_key]
        if time.time() - ts < BANK_CACHE_TTL: return cached
    
    rates = {}
    t = await fetch_tinkoff_rate()
    if t: rates["tinkoff"] = t
    
    if "tinkoff" not in rates: rates["tinkoff"] = {"buy": 78.5, "sell": 74.4}
    if "sber" not in rates: rates["sber"] = {"buy": 79.2, "sell": 71.7}
    if "alfa" not in rates: rates["alfa"] = {"buy": 81.8, "sell": 73.0}
    if "vtb" not in rates: rates["vtb"] = {"buy": 83.0, "sell": 71.5}
    
    result = {"rates": rates, "updated_at": datetime.now(timezone.utc).isoformat()}
    BANK_CACHE[cache_key] = (time.time(), result)
    return result

@app.get("/api/health")
async def health():
    return {"status": "healthy"}

@app.get("/")
async def root():
    return {"name": "P2P Aggregator", "version": "3.3.0", "exchanges": ["bybit", "htx", "binance", "mexc"]}
