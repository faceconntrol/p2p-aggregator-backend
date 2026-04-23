from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any
from datetime import datetime, timezone
import json

app = FastAPI(title="P2P Aggregator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PAYMENT_MAPPING = {
    "Sberbank": "Sberbank",
    "Tinkoff": "TinkoffNew",
    "VTB": "VTB",
    "AlfaBank": "AlfaBank",
    "Raiffeisen": "RaiffeisenBank",
    "SBP": "SBP",
}

async def fetch_bybit_merchants(crypto: str, fiat: str, amount: float, payment_methods: List[str]) -> List[Dict]:
    url = "https://api2.bybit.com/fiat/otc/item/online"
    bybit_payments = [PAYMENT_MAPPING.get(m, m) for m in payment_methods]
    
    payload = {
        "tokenId": crypto,
        "currencyId": fiat,
        "payment": bybit_payments,
        "side": "0",
        "size": "30",
        "page": "1",
        "amount": str(int(amount)),
        "authMaker": False,
        "canTrade": True
    }
    
    print(f"🔍 Bybit request payload: {json.dumps(payload)}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://www.bybit.com",
        "Referer": "https://www.bybit.com/fiat/trade/otc",
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        try:
            response = await client.post(url, json=payload)
            print(f"📡 Bybit status: {response.status_code}")
            
            if response.status_code != 200:
                print(f"❌ Bybit error response: {response.text[:500]}")
                return []
            
            data = response.json()
            print(f"📦 Bybit retCode: {data.get('retCode')}, retMsg: {data.get('retMsg')}")
            
            if data.get("retCode") != 0:
                return []
            
            items = data.get("result", {}).get("items", [])
            print(f"✅ Bybit found {len(items)} items")
            
            merchants = []
            for item in items:
                try:
                    price = float(item.get("price", 0))
                    quantity = float(item.get("quantity", 0))
                    min_amount = float(item.get("minAmount", 0))
                    max_amount = float(item.get("maxAmount", 0))
                    completed_rate = float(item.get("completedRate", "0.95"))
                    completed_count = item.get("completedCount", 0)
                    
                    if completed_rate < 0.90 or quantity < amount:
                        continue
                    
                    adv_no = item.get("advNo", "")
                    
                    merchants.append({
                        "id": adv_no,
                        "exchange": "bybit",
                        "merchant_name": item.get("nickname", "Unknown"),
                        "price": price,
                        "available_amount": quantity,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": round(completed_rate * 100, 1),
                        "completed_trades": completed_count,
                        "payment_methods": item.get("payments", []),
                        "is_verified": completed_count >= 10 and completed_rate >= 0.90,
                        "deep_link": f"bybit://fiat/otc/detail?advNo={adv_no}",
                        "web_link": f"https://www.bybit.com/fiat/trade/otc/detail?advNo={adv_no}"
                    })
                except (ValueError, TypeError) as e:
                    print(f"⚠️ Parse error: {e}")
                    continue
            
            print(f"✅ Bybit filtered: {len(merchants)} merchants")
            merchants.sort(key=lambda x: x["price"])
            return merchants[:20]
            
        except Exception as e:
            print(f"❌ Bybit exception: {e}")
            return []

async def fetch_htx_merchants(crypto: str, fiat: str, amount: float, payment_methods: List[str]) -> List[Dict]:
    # HTX часто блокирует запросы, пробуем упрощенный вариант
    coin_map = {"USDT": "2", "BTC": "1", "ETH": "3"}
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
    
    print(f"🔍 HTX request: {params}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://www.htx.com",
        "Referer": "https://www.htx.com/ru-ru/fiat-crypto/trade"
    }
    
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        try:
            response = await client.get(url, params=params)
            print(f"📡 HTX status: {response.status_code}")
            
            if response.status_code != 200:
                return []
            
            data = response.json()
            items = data.get("data", [])
            print(f"✅ HTX found {len(items)} items")
            
            merchants = []
            for item in items[:20]:
                try:
                    price = float(item.get("price", 0))
                    min_amount = float(item.get("minTradeLimit", 0))
                    max_amount = float(item.get("maxTradeLimit", 0))
                    finish_rate = item.get("monthFinishRate", 0.95)
                    order_count = item.get("monthOrderCount", 0)
                    
                    if finish_rate < 0.90 or max_amount < amount:
                        continue
                    
                    ad_id = item.get("adId", "")
                    pay_methods = [p.get("name", "") for p in item.get("payMethods", [])]
                    
                    merchants.append({
                        "id": ad_id,
                        "exchange": "htx",
                        "merchant_name": item.get("userName", "Unknown"),
                        "price": price,
                        "available_amount": max_amount,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": round(finish_rate * 100, 1),
                        "completed_trades": order_count,
                        "payment_methods": pay_methods,
                        "is_verified": item.get("isOnline", False) and order_count >= 10,
                        "deep_link": f"htx://otc/detail?id={ad_id}",
                        "web_link": f"https://www.htx.com/ru-ru/fiat-crypto/trade/detail?adId={ad_id}"
                    })
                except (ValueError, TypeError):
                    continue
            
            merchants.sort(key=lambda x: x["price"])
            return merchants[:20]
            
        except Exception as e:
            print(f"❌ HTX exception: {e}")
            return []

@app.get("/api/p2p/merchants")
async def get_p2p_merchants(
    crypto: str = Query("USDT"),
    fiat: str = Query("RUB"),
    amount: float = Query(10000),
    payment_methods: str = Query("Tinkoff")
):
    print(f"🚀 Request: crypto={crypto}, fiat={fiat}, amount={amount}, methods={payment_methods}")
    
    methods = [m.strip() for m in payment_methods.split(",")]
    
    # Запрашиваем обе биржи параллельно
    bybit_task = fetch_bybit_merchants(crypto, fiat, amount, methods)
    htx_task = fetch_htx_merchants(crypto, fiat, amount, methods)
    
    bybit_merchants, htx_merchants = await asyncio.gather(bybit_task, htx_task, return_exceptions=True)
    
    if isinstance(bybit_merchants, Exception):
        print(f"Bybit failed: {bybit_merchants}")
        bybit_merchants = []
    if isinstance(htx_merchants, Exception):
        print(f"HTX failed: {htx_merchants}")
        htx_merchants = []
    
    all_merchants = bybit_merchants + htx_merchants
    all_merchants.sort(key=lambda x: x["price"])
    
    filtered = [m for m in all_merchants if m["available_amount"] >= amount and amount >= m["min_amount"]][:30]
    
    best_rate = filtered[0]["price"] if filtered else 0
    best_exchange = filtered[0]["exchange"] if filtered else "bybit"
    
    stats = {}
    if bybit_merchants:
        prices = [m["price"] for m in bybit_merchants]
        if prices:
            stats["bybit"] = {
                "exchange": "bybit",
                "buy_price": min(prices),
                "merchant_count": len(bybit_merchants),
                "avg_price": round(sum(prices) / len(prices), 2)
            }
    
    if htx_merchants:
        prices = [m["price"] for m in htx_merchants]
        if prices:
            stats["htx"] = {
                "exchange": "htx",
                "buy_price": min(prices),
                "merchant_count": len(htx_merchants),
                "avg_price": round(sum(prices) / len(prices), 2)
            }
    
    print(f"🎯 Returning {len(filtered)} merchants, best_rate={best_rate}")
    
    return {
        "merchants": filtered,
        "stats": stats,
        "best_rate": best_rate,
        "best_exchange": best_exchange,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/")
async def root():
    return {"name": "P2P Aggregator API", "version": "1.0.0"}
