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
            
            if response.status_code != 200:
                print(f"❌ Bybit status: {response.status_code}")
                return []
            
            data = response.json()
            
            # Логируем сырой ответ
            print(f"📦 Bybit raw keys: {data.keys()}")
            print(f"📦 Bybit raw (first 1000 chars): {json.dumps(data)[:1000]}")
            
            merchants = []
            
            # Пробуем разные пути к items
            items = None
            
            # Формат 1: {"result": {"items": [...]}}
            if "result" in data and data["result"] and "items" in data["result"]:
                items = data["result"]["items"]
                print(f"✅ Found items in result.items")
            
            # Формат 2: {"data": {"list": [...]}}
            elif "data" in data and data["data"] and "list" in data["data"]:
                items = data["data"]["list"]
                print(f"✅ Found items in data.list")
            
            # Формат 3: {"data": [...]}
            elif "data" in data and isinstance(data["data"], list):
                items = data["data"]
                print(f"✅ Found items directly in data")
            
            # Формат 4: Прямой массив
            elif isinstance(data, list):
                items = data
                print(f"✅ Response is a direct array")
            
            if not items:
                print(f"❌ Could not find items. Available keys: {data.keys()}")
                return []
            
            print(f"📊 Processing {len(items)} items")
            
            for item in items:
                try:
                    # Пробуем разные названия полей
                    price = float(item.get("price", 0))
                    quantity = float(item.get("quantity", item.get("amount", item.get("maxAmount", 0))))
                    min_amount = float(item.get("minAmount", item.get("minTradeLimit", 0)))
                    max_amount = float(item.get("maxAmount", item.get("maxTradeLimit", 0)))
                    
                    # Разные названия для рейтинга
                    completed_rate = None
                    for key in ["completedRate", "finishRate", "completionRate"]:
                        if key in item:
                            completed_rate = float(item[key])
                            break
                    if completed_rate is None:
                        completed_rate = 0.95
                    
                    # Количество сделок
                    completed_count = 0
                    for key in ["completedCount", "orderCount", "monthOrderCount", "tradeCount"]:
                        if key in item:
                            completed_count = int(item[key])
                            break
                    
                    if completed_rate < 0.90:
                        continue
                    
                    # ID объявления
                    adv_no = item.get("advNo") or item.get("id") or item.get("adId") or ""
                    
                    # Имя мерчанта
                    nickname = item.get("nickname") or item.get("userName") or item.get("merchantName") or "Unknown"
                    
                    # Способ оплаты
                    payments = item.get("payments") or item.get("payMethods") or []
                    if isinstance(payments, str):
                        payments = [payments]
                    
                    merchants.append({
                        "id": str(adv_no),
                        "exchange": "bybit",
                        "merchant_name": nickname,
                        "price": price,
                        "available_amount": quantity,
                        "min_amount": min_amount,
                        "max_amount": max_amount,
                        "success_rate": round(completed_rate * 100, 1),
                        "completed_trades": completed_count,
                        "payment_methods": payments,
                        "is_verified": completed_count >= 10,
                        "deep_link": f"bybit://fiat/otc/detail?advNo={adv_no}",
                        "web_link": f"https://www.bybit.com/fiat/trade/otc/detail?advNo={adv_no}"
                    })
                    
                except (ValueError, TypeError) as e:
                    print(f"⚠️ Parse error for item: {e}")
                    continue
            
            merchants.sort(key=lambda x: x["price"])
            print(f"✅ Bybit filtered: {len(merchants)} merchants")
            return merchants[:20]
            
        except Exception as e:
            print(f"❌ Bybit exception: {e}")
            return []
