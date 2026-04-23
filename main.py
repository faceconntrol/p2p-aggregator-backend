from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any
from datetime import datetime, timezone
import json
import time

app = FastAPI(title="P2P Aggregator API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════
# КЭШ
# ═══════════════════════════════════════════
CACHE: Dict[str, tuple] = {}
CACHE_TTL = 10

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
# РАНЖИРОВЩИК
# ═══════════════════════════════════════════
def score_merchant(m: Dict[str, Any], amount: float) -> float:
    price_score = m["price"]
    reliability = m["success_rate"]
    
    if reliability < 80:
        reliability_penalty = (100 - reliability) * 0.5
    elif reliability < 95:
        reliability_penalty = (100 - reliability) * 0.2
    else:
        reliability_penalty = 0
    
    volume_penalty = 5 if m["max_amount"] < amount else 0
    
    trades = m["completed_trades"]
    if trades < 10:
        trades_penalty = 10
    elif trades < 50:
        trades_penalty = 2
    else:
        trades_penalty = 0
    
    return price_score + reliability_penalty + volume_penalty + trades_penalty

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

                    merchants.append({
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
                    })

                except Exception:
                    continue

            merchants.sort(key=lambda m: score_merchant(m, amount))
            print(f"  ✅ Bybit ranked: {len(merchants)}")
            return merchants[:20]

        except Exception as e:
            print(f"❌ Bybit exception: {e}")
            return []

# ═══════════════════════════════════════════
# BITGET P2P API (ПРАВИЛЬНЫЙ ENDPOINT)
# ═══════════════════════════════════════════
async def fetch_bitget_merchants(
    crypto: str, fiat: str, amount: float, methods: List[str]
) -> List[Dict[str, Any]]:

    url = "https://api.bitget.com/api/v2/p2p/advertise/queryList"

    payload = {
        "coin": crypto,
        "currency": fiat,
        "side": "buy",
        "pageNo": "1",
        "pageSize": "20",
        "amount": str(int(amount)),
        "payMethod": []
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.bitget.com",
        "Referer": "https://www.bitget.com/",
        "locale": "ru-RU"
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.post(url, json=payload)

            if response.status_code != 200:
                print(f"❌ Bitget status: {response.status_code}")
                return []

            data = response.json()

            if data.get("code") != "00000":
                print(f"❌ Bitget error: {data.get('msg')}")
                return []

            items = data.get("data", {}).get("list", [])
            print(f"📊 Bitget: {len(items)} items")

            merchants = []

            for item in items:
                try:
                    adv = item.get("advertisement", {})
                    user = item.get("advertiser", {})

                    price = float(adv.get("price", 0))
                    min_amount = float(adv.get("minTradeAmount", 0))
                    max_amount = float(adv.get("maxTradeAmount", 0))
                    available = float(adv.get("surplusAmount", 0))

                    success_rate = float(user.get("userRate", 0))
                    trades = int(user.get("orderCount", 0))

                    nickname = user.get("nickName", "Unknown")
                    adv_no = adv.get("advertisementId", "")

                    pay_methods = [p.get("name", "") for p in adv.get("payMethodList", [])]

                    if amount < min_amount or amount > max_amount:
                        continue

                    if success_rate < 50:
                        continue

                    if methods:
                        methods_lower = [m.lower() for m in methods]
                        pay_lower = [p.lower() for p in pay_methods]
                        if not any(m in pay_lower for m in methods_lower):
                            continue

                    merchants.append({
                        "id": adv_no,
                        "exchange": "bitget",
                        "merchant_name": nickname,
                        "price": price,
                        "available_amount": available,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": success_rate,
                        "completed_trades": trades,
                        "payment_methods": pay_methods,
                        "is_verified": trades >= 10 and success_rate >= 85,
                        "deep_link": f"https://www.bitget.com/ru/p2p/detail?advNo={adv_no}",
                        "web_link": "https://www.bitget.com/ru/p2p"
                    })

                except Exception as e:
                    print(f"⚠️ Bitget parse error: {e}")
                    continue

            merchants.sort(key=lambda m: score_merchant(m, amount))
            print(f"  ✅ Bitget ranked: {len(merchants)}")
            return merchants[:15]

        except Exception as e:
            print(f"❌ Bitget exception: {e}")
            return []

# ═══════════════════════════════════════════
# HTX P2P API
# ═══════════════════════════════════════════
async def fetch_htx_merchants(
    crypto: str, fiat: str, amount: float, methods: List[str]
) -> List[Dict[str, Any]]:
    
    coin_map = {"USDT": "2", "BTC": "1", "ETH": "3", "USDC": "4"}
    currency_map = {"RUB": "11", "USD": "1", "EUR": "2"}
    
    url = "https://www.htx.com/-/x/otc/v1/data/trade-market"
    params = {
        "coinId": coin_map.get(crypto, "2"),
        "currency": currency_map.get(fiat, "11"),
        "tradeType": "1",
        "currPage": "1",
        "payMethod": "0",
        "online": "1",
        "amount": str(int(amount))
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.htx.com",
        "Referer": "https://www.htx.com/ru-ru/fiat-crypto/trade"
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.get(url, params=params)
            
            if response.status_code != 200:
                print(f"❌ HTX status: {response.status_code}")
                return []
            
            data = response.json()
            
            if data.get("code") != 200:
                print(f"❌ HTX error: {data.get('message')}")
                return []
            
            items = data.get("data", [])
            print(f"📊 HTX: {len(items)} items")
            
            merchants = []
            for item in items[:20]:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minTradeLimit", 0))
                    max_amount = float(item.get("maxTradeLimit", 0))
                    finish_rate = float(item.get("monthFinishRate", 0.95))
                    order_count = int(item.get("monthOrderCount", 0))
                    
                    if finish_rate < 0.50 or max_amount < amount:
                        continue
                    
                    ad_id = str(item.get("adId", ""))
                    pay_methods = [p.get("name", "") for p in item.get("payMethods", [])]
                    
                    if methods:
                        methods_lower = [m.lower() for m in methods]
                        pay_lower = [p.lower() for p in pay_methods]
                        if not any(m in pay_lower for m in methods_lower):
                            continue
                    
                    merchants.append({
                        "id": ad_id if ad_id else str(abs(hash(item.get("userName", "") + str(price)))),
                        "exchange": "htx",
                        "merchant_name": item.get("userName", "Unknown"),
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
                    })
                except (ValueError, TypeError):
                    continue
            
            merchants.sort(key=lambda m: score_merchant(m, amount))
            print(f"  ✅ HTX ranked: {len(merchants)}")
            return merchants[:15]
            
        except Exception as e:
            print(f"❌ HTX exception: {e}")
            return []

# ═══════════════════════════════════════════
# GATE.IO P2P API
# ═══════════════════════════════════════════
async def fetch_gateio_merchants(
    crypto: str, fiat: str, amount: float, methods: List[str]
) -> List[Dict[str, Any]]:
    
    url = "https://www.gate.io/api/p2p/advertise/list"
    params = {
        "crypto": crypto,
        "fiat": fiat,
        "type": "buy",
        "page": "1",
        "limit": "20",
        "amount": str(int(amount))
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.gate.io",
        "Referer": "https://www.gate.io/ru/p2p"
    }

    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.get(url, params=params)
            
            if response.status_code != 200:
                print(f"❌ Gate.io status: {response.status_code}")
                return []
            
            data = response.json()
            items = data.get("list", data.get("data", []))
            print(f"📊 Gate.io: {len(items)} items")
            
            merchants = []
            for item in items:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minAmount", 0))
                    max_amount = float(item.get("maxAmount", 0))
                    available = float(item.get("amount", 0))
                    
                    rate = float(item.get("completedRate", 95))
                    trades = int(item.get("tradeCount", 0))
                    nickname = item.get("nickname", "Unknown")
                    ad_id = str(item.get("id", ""))
                    pay_methods = item.get("payMethods", [])
                    
                    if methods:
                        methods_lower = [m.lower() for m in methods]
                        pay_lower = [p.lower() for p in pay_methods]
                        if not any(m in pay_lower for m in methods_lower):
                            continue
                    
                    if amount < min_amount or amount > max_amount:
                        continue
                    
                    if rate < 50:
                        continue
                    
                    merchants.append({
                        "id": ad_id if ad_id else str(abs(hash(nickname + str(price)))),
                        "exchange": "gateio",
                        "merchant_name": nickname,
                        "price": price,
                        "available_amount": available,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": rate,
                        "completed_trades": trades,
                        "payment_methods": pay_methods,
                        "is_verified": item.get("isOnline", False) and trades >= 10,
                        "deep_link": f"https://www.gate.io/ru/p2p/detail?id={ad_id}" if ad_id else "https://www.gate.io/ru/p2p",
                        "web_link": "https://www.gate.io/ru/p2p"
                    })
                except (ValueError, TypeError):
                    continue
            
            merchants.sort(key=lambda m: score_merchant(m, amount))
            print(f"  ✅ Gate.io ranked: {len(merchants)}")
            return merchants[:15]
            
        except Exception as e:
            print(f"❌ Gate.io exception: {e}")
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
        fetch_bitget_merchants(crypto, fiat, amount, methods),
        fetch_htx_merchants(crypto, fiat, amount, methods),
        fetch_gateio_merchants(crypto, fiat, amount, methods),
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_merchants = []
    for result in results:
        if not isinstance(result, Exception):
            all_merchants.extend(result)
    
    if not all_merchants:
        print("❌ No liquidity from any exchange")
        result = {
            "error": "No liquidity available",
            "merchants": [],
            "stats": {},
            "best_rate": 0,
            "best_exchange": "none",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        CACHE[cache_key] = (time.time(), result)
        return result
    
    all_merchants.sort(key=lambda m: score_merchant(m, amount))
    filtered = [m for m in all_merchants if amount >= m["min_amount"]][:40]
    
    best_rate = filtered[0]["price"] if filtered else 0
    best_exchange = filtered[0]["exchange"] if filtered else "none"
    
    stats: Dict[str, Any] = {}
    for m in filtered:
        ex = m["exchange"]
        if ex not in stats:
            stats[ex] = {"exchange": ex, "prices": [], "merchant_count": 0}
        stats[ex]["prices"].append(m["price"])
    
    for ex, data in stats.items():
        prices = data.pop("prices")
        data["buy_price"] = min(prices)
        data["merchant_count"] = len(prices)
        data["avg_price"] = round(sum(prices) / len(prices), 2)
    
    result = {
        "merchants": filtered,
        "stats": stats,
        "best_rate": best_rate,
        "best_exchange": best_exchange,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    
    CACHE[cache_key] = (time.time(), result)
    
    print(f"🎯 Returning {len(filtered)} merchants from {len(stats)} exchanges, best=₽{best_rate} on {best_exchange}")
    return result

@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return {"name": "P2P Aggregator API", "version": "2.0.0", "exchanges": ["bybit", "bitget", "htx", "gateio"]}
