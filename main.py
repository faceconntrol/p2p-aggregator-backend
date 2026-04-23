from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime

app = FastAPI(title="P2P Aggregator API", version="1.0.0")

# CORS для мобильного приложения
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HTTP клиент
async def get_http_client():
    return httpx.AsyncClient(
        timeout=15.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://www.bybit.com",
            "Referer": "https://www.bybit.com/"
        }
    )

# Маппинг способов оплаты для Bybit
PAYMENT_MAPPING = {
    "Sberbank": "Sberbank",
    "Tinkoff": "TinkoffNew",
    "VTB": "VTB",
    "AlfaBank": "AlfaBank",
    "Raiffeisen": "RaiffeisenBank",
    "SBP": "SBP",
    "QIWI": "QIWI",
    "YooMoney": "YooMoney"
}

async def fetch_bybit_merchants(
    crypto: str = "USDT",
    fiat: str = "RUB",
    amount: float = 10000,
    payment_methods: List[str] = ["Tinkoff"]
) -> List[Dict[str, Any]]:
    """Запрос к Bybit P2P API"""
    url = "https://api2.bybit.com/fiat/otc/item/online"
    
    # Конвертируем способы оплаты
    bybit_payments = [PAYMENT_MAPPING.get(m, m) for m in payment_methods]
    
    payload = {
        "tokenId": crypto,
        "currencyId": fiat,
        "payment": bybit_payments,
        "side": "0",  # покупка крипты
        "size": "30",
        "page": "1",
        "amount": str(int(amount)),
        "authMaker": False,
        "canTrade": True
    }
    
    async with await get_http_client() as client:
        try:
            response = await client.post(url, json=payload)
            
            if response.status_code != 200:
                print(f"Bybit error: {response.status_code}")
                return []
            
            data = response.json()
            
            if data.get("retCode") != 0:
                print(f"Bybit API error: {data.get('retMsg')}")
                return []
            
            items = data.get("result", {}).get("items", [])
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
                    
                    adv_no = item.get("advNo") or item.get("id", "")
                    
                    merchants.append({
                        "id": adv_no,
                        "exchange": "bybit",
                        "merchant_name": item.get("nickname", "Unknown"),
                        "merchant_id": item.get("userId", ""),
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
                except (ValueError, TypeError):
                    continue
            
            merchants.sort(key=lambda x: x["price"])
            return merchants[:20]  # Топ-20
            
        except Exception as e:
            print(f"Bybit request error: {e}")
            return []

async def fetch_htx_merchants(
    crypto: str = "USDT",
    fiat: str = "RUB",
    amount: float = 10000,
    payment_methods: List[str] = ["Tinkoff"]
) -> List[Dict[str, Any]]:
    """Запрос к HTX P2P API"""
    coin_map = {"USDT": "2", "BTC": "1", "ETH": "3"}
    currency_map = {"RUB": "11", "USD": "1", "EUR": "2"}
    
    url = "https://www.htx.com/-/x/otc/v1/data/trade-market"
    params = {
        "coinId": coin_map.get(crypto, "2"),
        "currency": currency_map.get(fiat, "11"),
        "tradeType": "1",
        "currPage": "1",
        "payMethod": "0",
        "country": "19",
        "online": "1",
        "amount": str(int(amount))
    }
    
    async with await get_http_client() as client:
        try:
            response = await client.get(url, params=params)
            
            if response.status_code != 200:
                return []
            
            data = response.json()
            
            if data.get("code") != 200:
                return []
            
            items = data.get("data", [])
            merchants = []
            
            for item in items:
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
                        "merchant_id": item.get("uid", ""),
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
            print(f"HTX request error: {e}")
            return []

@app.get("/api/p2p/merchants")
async def get_p2p_merchants(
    crypto: str = Query("USDT", description="Криптовалюта"),
    fiat: str = Query("RUB", description="Фиатная валюта"),
    amount: float = Query(10000, description="Сумма"),
    payment_methods: str = Query("Tinkoff", description="Способы оплаты через запятую")
):
    """Агрегация P2P-предложений"""
    
    methods = [m.strip() for m in payment_methods.split(",")]
    
    # Параллельные запросы к биржам
    tasks = [
        fetch_bybit_merchants(crypto, fiat, amount, methods),
        fetch_htx_merchants(crypto, fiat, amount, methods),
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_merchants = []
    stats = {}
    
    # Bybit
    if not isinstance(results[0], Exception) and results[0]:
        bybit_merchants = results[0]
        all_merchants.extend(bybit_merchants)
        prices = [m["price"] for m in bybit_merchants if m["price"] > 0]
        if prices:
            stats["bybit"] = {
                "exchange": "bybit",
                "buy_price": min(prices),
                "sell_price": round(min(prices) * 1.005, 2),
                "spread": round(min(prices) * 0.005, 2),
                "spread_percent": 0.5,
                "merchant_count": len(bybit_merchants),
                "min_price": min(prices),
                "max_price": max(prices),
                "avg_price": round(sum(prices) / len(prices), 2),
                "total_liquidity": round(sum(m["available_amount"] for m in bybit_merchants), 2)
            }
    
    # HTX
    if not isinstance(results[1], Exception) and results[1]:
        htx_merchants = results[1]
        all_merchants.extend(htx_merchants)
        prices = [m["price"] for m in htx_merchants if m["price"] > 0]
        if prices:
            stats["htx"] = {
                "exchange": "htx",
                "buy_price": min(prices),
                "sell_price": round(min(prices) * 1.005, 2),
                "spread": round(min(prices) * 0.005, 2),
                "spread_percent": 0.5,
                "merchant_count": len(htx_merchants),
                "min_price": min(prices),
                "max_price": max(prices),
                "avg_price": round(sum(prices) / len(prices), 2),
                "total_liquidity": round(sum(m["available_amount"] for m in htx_merchants), 2)
            }
    
    # Сортировка всех мерчантов по цене
    all_merchants.sort(key=lambda x: x["price"])
    
    # Фильтрация по доступной сумме
    filtered_merchants = [
        m for m in all_merchants
        if m["available_amount"] >= amount
        and amount >= m["min_amount"]
    ][:30]
    
    best_rate = filtered_merchants[0]["price"] if filtered_merchants else 0
    best_exchange = filtered_merchants[0]["exchange"] if filtered_merchants else "bybit"
    
    return {
        "merchants": filtered_merchants,
        "stats": stats,
        "best_rate": best_rate,
        "best_exchange": best_exchange,
        "updated_at": datetime.utcnow().isoformat()
    }

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/")
async def root():
    return {
        "name": "P2P Aggregator API",
        "version": "1.0.0",
        "endpoints": ["/api/p2p/merchants", "/api/health"]
    }
