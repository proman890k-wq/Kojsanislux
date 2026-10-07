import os
import json
import time
import random
from aiohttp import web, ClientSession

# =========================
# НАСТРОЙКИ
# =========================

TOKEN = "8307112310:AAFneoMo4ACr6SKTloHNQ9hVZIw1mf-apGQ"
WEBAPP_URL = "https://proman890k-wq.github.io/Kojsanislux/"
RENDER_URL = "https://game-2gla.onrender.com"
API = f"https://api.telegram.org/bot{TOKEN}"
TOKEN_TO_USD_RATE = 1_000_000
DB_FILE = "database.json"

# =========================
# РАБОТА С БАЗОЙ ДАННЫХ (JSON)
# =========================

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

USERS = load_db()

# =========================
# БАЗА ПРЕДМЕТОВ И УЛУЧШЕНИЙ
# =========================

ITEMS = {
    "boost2": ("Разгон ×2 на 1 час", "Весь доход ×2", 15),
    "boost5": ("Разгон ×5 на 1 час", "Весь доход ×5", 50),
    "auto": ("Авто-тап", "3 тапа в секунду навсегда", 100),
    "offline": ("Работа во сне", "Офлайн-доход до 8 часов", 75),
    "perm2": ("Вечный ×2", "Постоянный множитель к доходу", 250),
    "coins1": ("500 монет биржи 🪙", "Для покупок", 50),
    "coins2": ("3000 монет биржи 🪙", "Выгоднее на 20%", 250),
}

ITEM_DATABASE = [
    {"id": "cooler", "name": "Кулер", "rarity": "Обычный", "price": 10, "chance": 60.0},
    {"id": "ram", "name": "Памятка RAM", "rarity": "Редкий", "price": 50, "chance": 25.0},
    {"id": "gpu", "name": "Видеокарта RTX", "rarity": "Сверхредкий", "price": 200, "chance": 10.0},
    {"id": "core", "name": "Квантовое ядро", "rarity": "Эпический", "price": 800, "chance": 4.9999},
    {"id": "neural_matrix", "name": "Нейроматрица ИИ", "rarity": "Ультраредкий", "price": 5000, "chance": 0.0001},
]

# Рынок с авто-обновлением каждые 5 минут (300 секунд)
MARKET = {
    "lots": [],
    "last_update": 0,
    "id_counter": 100
}

def refresh_market_if_needed():
    now = time.time()
    if now - MARKET["last_update"] > 300 or not MARKET["lots"]:
        MARKET["last_update"] = now
        new_lots = []
        for i in range(random.randint(4, 6)):
            rnd = random.uniform(0, 100)
            chosen_item = ITEM_DATABASE[0]
            cumulative = 0
            for item in ITEM_DATABASE:
                cumulative += item["chance"]
                if rnd <= cumulative:
                    chosen_item = item
                    break
            
            MARKET["id_counter"] += 1
            new_lots.append({
                "id": MARKET["id_counter"],
                "item_id": chosen_item["id"],
                "name": chosen_item["name"],
                "rarity": chosen_item["rarity"],
                "price": chosen_item["price"] + random.randint(-2, 5),
                "mine": 0
            })
        MARKET["lots"] = new_lots


# =========================
# TELEGRAM API & CORS
# =========================

async def call(session, method, **params):
    try:
        async with session.post(f"{API}/{method}", json=params) as response:
            return await response.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except Exception as e:
            response = web.json_response({"error": "Internal server error"}, status=500)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

def get_user_login(data):
    return str(data.get("login", "")).strip()


# =========================
# РОУТЫ АВТОРИЗАЦИИ И ИГРЫ
# =========================

async def handle_index(request):
    return web.Response(text="Bot Webhook Server is running!")

async def handle_register(request):
    try:
        data = await request.json()
        login = data.get("login", "").strip()
        password = data.get("password", "").strip()

        if not login or not password:
            return web.json_response({"error": "Заполните логин и пароль"}, status=400)

        if login in USERS:
            return web.json_response({"error": "Логин уже занят"}, status=400)

        USERS[login] = {
            "password": password,
            "coins": 100,
            "tokens": 5,
            "inv": {"cooler": 1},
            "next_box": 0,
            "mine": [],
            "game_state": {}
        }
        save_db(USERS)
        return web.json_response({"status": "ok", "login": login})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_login(request):
    try:
        data = await request.json()
        login = data.get("login", "").strip()
        password = data.get("password", "").strip()

        if login not in USERS or USERS[login].get("password") != password:
            return web.json_response({"error": "Неверный логин или пароль"}, status=400)

        user = USERS[login]
        response_state = user.get("game_state", {})
        if not response_state:
            response_state = {
                "coins": user.get("coins", 100),
                "tokens": user.get("tokens", 5),
                "inv": user.get("inv", {}),
                "next": user.get("next_box", 0),
                "mine": user.get("mine", [])
            }

        return web.json_response({"status": "ok", "login": login, "state": response_state})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_webhook(request):
    try:
        data = await request.json()
        session = request.app["s"]

        if "pre_checkout_query" in data:
            pcq = data["pre_checkout_query"]
            await call(session, "answerPreCheckoutQuery", pre_checkout_query_id=pcq["id"], ok=True)
            return web.Response(text="OK")

        message = data.get("message", {})
        if message.get("text", "").startswith("/start"):
            chat_id = message["chat"]["id"]
            await call(
                session, "sendMessage",
                chat_id=chat_id,
                text="🌱 Добро пожаловать в Нейроферму!\n\nЗапускай игру 👇",
                reply_markup={"inline_keyboard": [[{"text": "🎮 Играть", "web_app": {"url": WEBAPP_URL}}]]}
            )
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))

    return web.Response(text="OK")


async def handle_me(request):
    try:
        data = await request.json()
    except:
        data = {}

    login = get_user_login(data)
    if not login or login not in USERS:
        return web.json_response({"error": "Unauthorized"}, status=401)

    user = USERS[login]
    response_state = user.get("game_state", {})
    if not response_state:
        response_state = {
            "coins": user.get("coins", 100),
            "tokens": user.get("tokens", 5),
            "inv": user.get("inv", {}),
            "next": user.get("next_box", 0),
            "mine": user.get("mine", [])
        }

    return web.json_response({"state": response_state})


async def handle_save(request):
    try:
        data = await request.json()
    except:
        data = {}

    login = get_user_login(data)
    user_state = data.get("state")

    if not login or login not in USERS:
        return web.json_response({"error": "Unauthorized"}, status=401)

    if user_state and isinstance(user_state, dict):
        pwd = USERS[login].get("password")
        USERS[login]["game_state"] = user_state
        if "coins" in user_state: USERS[login]["coins"] = user_state["coins"]
        if "tokens" in user_state: USERS[login]["tokens"] = user_state["tokens"]
        if "inv" in user_state: USERS[login]["inv"] = user_state["inv"]
        if "next" in user_state: USERS[login]["next_box"] = user_state["next"]
        if "mine" in user_state: USERS[login]["mine"] = user_state["mine"]
        USERS[login]["password"] = pwd
        save_db(USERS)

    return web.json_response({"status": "ok"})


async def handle_drop(request):
    data = await request.json()
    login = get_user_login(data)
    user = USERS.get(login)
    now = time.time() * 1000

    if not user:
        return web.json_response({"error": "Пользователь не найден"}, status=400)

    if now < user["next_box"]:
        return web.json_response({"error": "Ящик ещё не готов"}, status=400)

    value = random.uniform(0, 100)
    chosen_item = ITEM_DATABASE[0]["id"]
    cumulative = 0
    for item in ITEM_DATABASE:
        cumulative += item["chance"]
        if value <= cumulative:
            chosen_item = item["id"]
            break

    user["inv"][chosen_item] = user["inv"].get(chosen_item, 0) + 1
    user["next_box"] = now + 60000

    if user.get("game_state"):
        user["game_state"]["inv"] = user["inv"]
        user["game_state"]["next"] = user["next_box"]
    
    save_db(USERS)

    return web.json_response({
        "item": chosen_item,
        "state": user.get("game_state", {"coins": user["coins"], "tokens": user["tokens"], "inv": user["inv"], "next": user["next_box"]})
    })


async def handle_market(request):
    refresh_market_if_needed()
    try:
        data = await request.json()
    except:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login, {"coins": 100, "tokens": 5, "inv": {}, "next_box": 0, "mine": []})

    state_data = user.get("game_state", {
        "coins": user.get("coins", 100),
        "tokens": user.get("tokens", 5),
        "inv": user.get("inv", {}),
        "next": user.get("next_box", 0),
        "mine": user.get("mine", [])
    })

    time_left = max(0, 300 - int(time.time() - MARKET["last_update"]))

    return web.json_response({
        "state": state_data,
        "lots": MARKET["lots"],
        "ttl": time_left
    })


async def handle_buy_lot(request):
    data = await request.json()
    login = get_user_login(data)
    user = USERS.get(login)
    lot_id = data.get("id")

    if not user:
        return web.json_response({"error": "Пользователь не найден"}, status=400)

    lot = next((l for l in MARKET["lots"] if l["id"] == lot_id), None)
    if not lot:
        return web.json_response({"error": "Лот уже продан или обновлен"}, status=400)

    if user["coins"] < lot["price"]:
        return web.json_response({"error": "Не хватает монет"}, status=400)

    user["coins"] -= lot["price"]
    item_key = lot["item_id"]
    user["inv"][item_key] = user["inv"].get(item_key, 0) + 1
    MARKET["lots"].remove(lot)

    if user.get("game_state"):
        user["game_state"]["coins"] = user["coins"]
        user["game_state"]["inv"] = user["inv"]

    save_db(USERS)

    return web.json_response({
        "state": user.get("game_state"),
        "lots": MARKET["lots"]
    })


async def handle_list_lot(request):
    data = await request.json()
    login = get_user_login(data)
    user = USERS.get(login)
    item = data.get("item")
    price = int(data.get("price", 0))

    if not user or price <= 0 or user["inv"].get(item, 0) <= 0:
        return web.json_response({"error": "Неверные данные или нет предмета"}, status=400)

    user["inv"][item] -= 1
    if user["inv"][item] <= 0:
        del user["inv"][item]

    MARKET["id_counter"] += 1
    item_info = next((i for i in ITEM_DATABASE if i["id"] == item), {"name": item, "rarity": "Обычный"})

    new_lot = {
        "id": MARKET["id_counter"],
        "item_id": item,
        "name": item_info["name"],
        "rarity": item_info["rarity"],
        "price": price,
        "mine": 1,
        "owner": login
    }
    MARKET["lots"].append(new_lot)

    if user.get("game_state"):
        user["game_state"]["inv"] = user["inv"]

    save_db(USERS)

    return web.json_response({
        "state": user.get("game_state"),
        "lots": MARKET["lots"]
    })


# =========================
# ЗАПУСК СЕРВЕРА
# =========================

async def on_start(app):
    app["s"] = ClientSession()
    await call(app["s"], "setWebhook", url=f"{RENDER_URL}/webhook", allowed_updates=["message", "pre_checkout_query"])

async def on_cleanup(app):
    if "s" in app: await app["s"].close()

app = web.Application(middlewares=[cors])
app.router.add_get("/", handle_index)
app.router.add_post("/webhook", handle_webhook)
app.router.add_post("/api/register", handle_register)
app.router.add_post("/api/login", handle_login)
app.router.add_route("*", "/api/me", handle_me)
app.router.add_route("*", "/api/save", handle_save)
app.router.add_route("*", "/api/drop", handle_drop)
app.router.add_route("*", "/api/market", handle_market)
app.router.add_route("*", "/api/buy", handle_buy_lot)
app.router.add_route("*", "/api/list", handle_list_lot)

app.on_startup.append(on_start)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)
