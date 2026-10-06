"""Улучшенный бэкенд для Нейрофермы: поддержка биржи, предметов, курса долларов и Stars.
pip install aiohttp
"""
import asyncio, os, time, random
from aiohttp import web, ClientSession

TOKEN = "8307112310:AAFneoMo4ACr6SKTl0HNQ9hVZIW1mf-apGQ"
WEBAPP_URL = "https://proman80k-wq.github.io/Kojsanislux/"
API = f"https://api.telegram.org/bot{TOKEN}"

# Товары за Stars (включая покупку монет для биржи)
ITEMS = {
    "boost2":  ("Разгон ×2 на 1 час", "Весь доход ×2", 15),
    "boost5":  ("Разгон ×5 на 1 час", "Весь доход ×5", 50),
    "auto":    ("Авто-тап", "3 тапа в секунду навсегда", 100),
    "offline": ("Работа во сне", "Офлайн-доход до 8 часов", 75),
    "perm2":   ("Вечный ×2", "Постоянный множитель к доходу", 250),
    "coins1":  ("500 монет биржи 🪙", "Для покупок у других игроков", 50),
    "coins2":  ("3000 монет биржи 🪙", "Выгоднее на 20%", 250),
}

# База данных в памяти (для продакшна лучше заменить на SQLite / PostgreSQL)
USERS = {}
# Общий рынок (биржа) и банк лотов
MARKET = {
    "lots": [
        {"id": 1, "item": "cooler", "price": 15, "mine": 0},
        {"id": 2, "item": "ram", "price": 50, "mine": 0},
        {"id": 3, "item": "gpu", "price": 180, "mine": 0}
    ],
    "id_counter": 10
}
DEF = {
    "cooler": {"npc": 10},
    "ram": {"npc": 40},
    "gpu": {"npc": 150},
    "core": {"npc": 500}
}
W8 = {"cooler": 55, "ram": 28, "gpu": 14, "core": 3}

# Искусственный курс: 1 доллар = X токенов (например, 1,000,000 токенов = 1$)
TOKEN_TO_USD_RATE = 1_000_000 

async def call(s, method, **params):
    async with s.post(f"{API}/{method}", json=params) as r:
        return await r.json()

@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

def get_user_id(request_data):
    return str(request_data.get("user_id", "test_user"))

# --- API Эндпоинты игры ---

async def handle_me(request):
    data = await request.json() if request.can_read_body else {}
    uid = get_user_id(data)
    if uid not in USERS:
        USERS[uid] = {
            "coins": 100, "inv": {"cooler": 1}, "next_box": 0, "mine": [],
            "usd_rate": TOKEN_TO_USD_RATE
        }
    u = USERS[uid]
    return web.json_response({
        "state": {
            "coins": u["coins"],
            "inv": u["inv"],
            "next": u["next_box"],
            "mine": u["mine"],
            "rate": u["usd_rate"]
        }
    })

async def handle_drop(request):
    data = await request.json()
    uid = get_user_id(data)
    u = USERS.get(uid)
    now = time.time() * 1000
    if not u or now < u["next_box"]:
        return web.json_response({"error": "Ящик ещё не готов"}, status=400)
    
    # Розыгрыш предмета
    r_val = random.uniform(0, 100)
    chosen = "cooler"
    for k, w in W8.items():
        r_val -= w
        if r_val < 0:
            chosen = k
            break
    
    u["inv"][chosen] = u["inv"].get(chosen, 0) + 1
    u["next_box"] = now + 60000 # 1 минута
    
    return web.json_response({
        "item": chosen,
        "state": {"coins": u["coins"], "inv": u["inv"], "next": u["next_box"], "mine": u["mine"]}
    })

async def handle_market(request):
    data = await request.json() if request.can_read_body else {}
    uid = get_user_id(data)
    u = USERS.get(uid, {"coins": 0, "inv": {}, "next_box": 0, "mine": []})
    return web.json_response({
        "state": {"coins": u["coins"], "inv": u["inv"], "next": u["next_box"], "mine": u["mine"]},
        "lots": MARKET["lots"]
    })

async def handle_buy_lot(request):
    data = await request.json()
    uid = get_user_id(data)
    u = USERS.get(uid)
    lot_id = data.get("id")
    
    lot = next((l for l in MARKET["lots"] if l["id"] == lot_id), None)
    if not lot:
        return web.json_response({"error": "Лот уже продан"}, status=400)
    if u["coins"] < lot["price"]:
        return web.json_response({"error": "Не хватает монет"}, status=400)
    
    u["coins"] -= lot["price"]
    u["inv"][lot["item"]] = u["inv"].get(lot["item"], 0) + 1
    MARKET["lots"].remove(lot)
    
    if lot.get("mine") and str(lot.get("owner")) != uid:
        owner = USERS.get(str(lot.get("owner")))
        if owner:
            owner["coins"] += int(lot["price"] * 0.95) # комиссия 5%

    return web.json_response({
        "state": {"coins": u["coins"], "inv": u["inv"], "next": u["next_box"], "mine": u["mine"]},
        "lots": MARKET["lots"]
    })

async def handle_list_lot(request):
    data = await request.json()
    uid = get_user_id(data)
    u = USERS.get(uid)
    item = data.get("item")
    price = int(data.get("price", 0))
    
    if price <= 0 or not u["inv"].get(item, 0) > 0:
        return web.json_response({"error": "Неверные данные или нет предмета"}, status=400)
    
    u["inv"][item] -= 1
    if u["inv"][item] <= 0:
        del u["inv"][item]
        
    MARKET["id_counter"] += 1
    new_lot = {"id": MARKET["id_counter"], "item": item, "price": price, "mine": 1, "owner": uid}
    MARKET["lots"].append(new_lot)
    u["mine"].append(new_lot)
    
    return web.json_response({
        "state": {"coins": u["coins"], "inv": u["inv"], "next": u["next_box"], "mine": u["mine"]},
        "lots": MARKET["lots"]
    })

async def invoice(request):
    data = await request.json()
    item_key = data.get("item")
    item = ITEMS.get(item_key)
    if not item:
        return web.json_response({"error": "bad item"}, status=400)
    r = await call(request.app["s"], "createInvoiceLink",
                   title=item[0], description=item[1], payload=item_key,
                   currency="XTR", prices=[{"label": item[0], "amount": item[2]}])
    return web.json_response({"link": r["result"]})

async def poll(app):
    s, offset = app["s"], 0
    while True:
        try:
            r = await call(s, "getUpdates", offset=offset, timeout=25,
                           allowed_updates=["message", "pre_checkout_query"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "pre_checkout_query" in u:
                    await call(s, "answerPreCheckoutQuery",
                               pre_checkout_query_id=u["pre_checkout_query"]["id"], ok=True)
                m = u.get("message", {})
                if "successful_payment" in m:
                    p = m["successful_payment"]
                    uid = str(m["from"]["id"])
                    payload = p["invoice_payload"]
                    print("PAID", uid, payload)
                    if uid not in USERS:
                        USERS[uid] = {"coins": 0, "inv": {}, "next_box": 0, "mine": [], "usd_rate": TOKEN_TO_USD_RATE}
                    
                    if payload == "coins1":
                        USERS[uid]["coins"] += 500
                    elif payload == "coins2":
                        USERS[uid]["coins"] += 3000
                elif m.get("text", "").startswith("/start"):
                    await call(s, "sendMessage", chat_id=m["chat"]["id"], text="Запускай нейроферму 👇",
                               reply_markup={"inline_keyboard": [[{"text": "Играть",
                                                                  "web_app": {"url": WEBAPP_URL}}]]})
        except Exception as e:
            print("Poll error:", e)
        await asyncio.sleep(1)

async def on_start(app):
    app["s"] = ClientSession()
    app["task"] = asyncio.create_task(poll(app))

async def on_cleanup(app):
    await app["s"].close()

app = web.Application(middlewares=[cors])
app.router.add_route("*", "/api/invoice", invoice)
app.router.add_route("*", "/api/me", handle_me)
app.router.add_route("*", "/api/drop", handle_drop)
app.router.add_route("*", "/api/market", handle_market)
app.router.add_route("*", "/api/buy", handle_buy_lot)
app.router.add_route("*", "/api/list", handle_list_lot)

app.on_startup.append(on_start)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, port=8080)
