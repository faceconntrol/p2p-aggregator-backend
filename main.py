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

# Флаг для использования мок-данных (пока Bybit API не работает)
USE_MOCK_DATA = True

PAYMENT_MAPPING = {
    "Sberbank": "Sberbank",
    "Tinkoff": "TinkoffNew",
    "VTB": "VTB",
    "AlfaBank": "AlfaBank",
    "Raiffeisen": "RaiffeisenBank",
    "SBP": "SBP",
}

async def fetch_bybit_merchants(crypto: str, fiat: str, amount: float, payment_methods: List[str]) -> List[Dict]:
    if USE_MOCK_DATA:
        return []
    
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
            print(f"📦 Bybit raw response keys: {data.keys()}")
            print(f"📦 Bybit raw response: {json.dumps(data)[:1000]}")
            
            # Пробуем разные форматы ответа
            items = None
            if "result" in data and data["result"]:
                items = data["result"].get("items", [])
            elif "data" in data and data["data"]:
                items = data["data"].get("list", [])
            
            if not items:
                print(f"❌ Bybit: no items found in response")
                return []
            
            print(f"✅ Bybit found {len(items)} items")
            
            merchants = []
            for item in items:
                try:
                    price = float(item.get("price", 0))
                    quantity = float(item.get("quantity", item.get("amount", 0)))
                    min_amount = float(item.get("minAmount", item.get("minTradeAmount", 0)))
                    max_amount = float(item.get("maxAmount", item.get("maxTradeAmount", 0)))
                    completed_rate = float(item.get("completedRate", item.get("finishRate", "0.95")))
                    completed_count = item.get("completedCount", item.get("orderCount", 0))
                    
                    if completed_rate < 0.90 or quantity < amount:
                        continue
                    
                    adv_no = item.get("advNo") or item.get("id", "")
                    
                    merchants.append({
                        "id": str(adv_no),
                        "exchange": "bybit",
                        "merchant_name": item.get("nickname", item.get("userName", "Unknown")),
                        "price": price,
                        "available_amount": quantity,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": round(completed_rate * 100, 1),
                        "completed_trades": completed_count,
                        "payment_methods": item.get("payments", item.get("payMethods", [])),
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
    if USE_MOCK_DATA:
        return []
    
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
            if not items:
                return []
            
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
                        "id": str(ad_id),
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

def get_mock_merchants(amount: float) -> List[Dict]:
    """Возвращает тестовые данные для демонстрации"""
    merchants = [
        {
            "id": "bybit_1",
            "exchange": "bybit",
            "merchant_name": "CryptoPro",
            "price": 96.85,
            "available_amount": 150000,
            "min_amount": 5000,
            "max_amount": 500000,
            "success_rate": 98.5,
            "completed_trades": 2340,
            "payment_methods": ["Tinkoff", "Sberbank"],
            "is_verified": True,
            "deep_link": "bybit://fiat/otc/detail?advNo=1",
            "web_link": "https://www.bybit.com/fiat/trade/otc"
        },
        {
            "id": "bybit_2",
            "exchange": "bybit",
            "merchant_name": "FastMoney",
            "price": 97.10,
            "available_amount": 80000,
            "min_amount": 3000,
            "max_amount": 300000,
            "success_rate": 96.8,
            "completed_trades": 890,
            "payment_methods": ["Tinkoff", "Raiffeisen"],
            "is_verified": True,
            "deep_link": "bybit://fiat/otc/detail?advNo=2",
            "web_link": "https://www.bybit.com/fiat/trade/otc"
        },
        {
            "id": "bybit_3",
            "exchange": "bybit",
            "merchant_name": "RubleExchange",
            "price": 96.50,
            "available_amount": 250000,
            "min_amount": 10000,
            "max_amount": 1000000,
            "success_rate": 99.1,
            "completed_trades": 5600,
            "payment_methods": ["Sberbank", "VTB"],
            "is_verified": True,
            "deep_link": "bybit://fiat/otc/detail?advNo=3",
            "web_link": "https://www.bybit.com/fiat/trade/otc"
        },
        {
            "id": "htx_1",
            "exchange": "htx",
            "merchant_name": "HuobiTrader",
            "price": 97.45,
            "available_amount": 120000,
            "min_amount": 5000,
            "max_amount": 400000,
            "success_rate": 97.2,
            "completed_trades": 1200,
            "payment_methods": ["Tinkoff", "AlfaBank"],
            "is_verified": True,
            "deep_link": "htx://otc/detail?id=1",
            "web_link": "https://www.htx.com/ru-ru/fiat-crypto/trade"
        },
        {
            "id": "htx_2",
            "exchange": "htx",
            "merchant_name": "CryptoKing",
            "price": 98.00,
            "available_amount": 60000,
            "min_amount": 2000,
            "max_amount": 200000,
            "success_rate": 95.5,
            "completed_trades": 450,
            "payment_methods": ["Sberbank"],
            "is_verified": True,
            "deep_link": "htx://otc/detail?id=2",
            "web_link": "https://www.htx.com/ru-ru/fiat-crypto/trade"
        },
        {
            "id": "bitget_1",
            "exchange": "bitget",
            "merchant_name": "BitgetPro",
            "price": 96.95,
            "available_amount": 90000,
            "min_amount": 3000,
            "max_amount": 350000,
            "success_rate": 96.0,
            "completed_trades": 780,
            "payment_methods": ["Tinkoff", "Raiffeisen"],
            "is_verified": True,
            "deep_link": "bitget://p2p/detail?advNo=1",
            "web_link": "https://www.bitget.com/ru/p2p"
        }
    ]
    
    # Сортируем по цене
    merchants.sort(key=lambda x: x["price"])
    return merchants

@app.get("/api/p2p/merchants")
async def get_p2p_merchants(
    crypto: str = Query("USDT"),
    fiat: str = Query("RUB"),
    amount: float = Query(10000),
    payment_methods: str = Query("Tinkoff")
):
    print(f"🚀 Request: crypto={crypto}, fiat={fiat}, amount={amount}, methods={payment_methods}")
    
    methods = [m.strip() for m in payment_methods.split(",")]
    
    # Получаем данные
    if USE_MOCK_DATA:
        all_merchants = get_mock_merchants(amount)
    else:
        bybit_merchants = await fetch_bybit_merchants(crypto, fiat, amount, methods)
        htx_merchants = await fetch_htx_merchants(crypto, fiat, amount, methods)
        all_merchants = bybit_merchants + htx_merchants
        all_merchants.sort(key=lambda x: x["price"])
    
    # Фильтруем по доступной сумме
    filtered = [m for m in all_merchants if m["available_amount"] >= amount and amount >= m["min_amount"]][:30]
    
    best_rate = filtered[0]["price"] if filtered else 0
    best_exchange = filtered[0]["exchange"] if filtered else "bybit"
    
    # Статистика по биржам
    stats = {}
    for m in filtered:
        ex = m["exchange"]
        if ex not in stats:
            stats[ex] = {
                "exchange": ex,
                "prices": [],
                "merchant_count": 0
            }
        stats[ex]["prices"].append(m["price"])
    
    for ex, data in stats.items():
        prices = data.pop("prices")
        data["buy_price"] = min(prices)
        data["merchant_count"] = len(prices)
        data["avg_price"] = round(sum(prices) / len(prices), 2)
    
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
    return {"name": "P2P Aggregator API", "version": "1.0.0", "mock_data": USE_MOCK_DATA}
