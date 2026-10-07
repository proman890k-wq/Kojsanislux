import os
import json
import time
import random
import hmac
import hashlib
import asyncio
from urllib.parse import parse_qsl

from aiohttp import web, ClientSession, ClientTimeout


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.environ.get("8307112310:AAFneoMo4ACr6SKTloHNQ9hVZIw1mf-apGQ", "")
WEBAPP_URL = "https://proman890k-wq.github.io/Kojsanislux/"
RENDER_URL = "https://game-2gla.onrender.com"
API = f"https://api.telegram.org/bot{TOKEN}"
DB_FILE = "database.json"

TOKEN_TO_USD_RATE = 1_000_000

# Сохраняем БД максимум раз в этот интервал.
# Это сильно уменьшает количество операций записи на Render.
SAVE_INTERVAL = 1.0


# =========================
# БАЗА ДАННЫХ
# =========================

def load_db():
    if not os.path.exists(DB_FILE):
        return {}

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


USERS = load_db()
DB_DIRTY = False
DB_SAVE_LOCK = asyncio.Lock()
DB_SAVE_TASK = None


def _write_db_sync(snapshot):
    tmp_file = DB_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        # Без indent=4 файл заметно быстрее записывается.
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))

    os.replace(tmp_file, DB_FILE)


async def save_db(force=False):
    global DB_DIRTY

    if not DB_DIRTY and not force:
        return

    async with DB_SAVE_LOCK:
        if not DB_DIRTY and not force:
            return

        # Копия позволяет не держать event loop на записи.
        snapshot = USERS.copy()

        await asyncio.to_thread(_write_db_sync, snapshot)
        DB_DIRTY = False


def mark_db_dirty():
    global DB_DIRTY, DB_SAVE_TASK

    DB_DIRTY = True

    if DB_SAVE_TASK is None or DB_SAVE_TASK.done():
        DB_SAVE_TASK = asyncio.create_task(delayed_save())


async def delayed_save():
    await asyncio.sleep(SAVE_INTERVAL)
    await save_db()


async def periodic_db_save(app):
    try:
        while True:
            await asyncio.sleep(SAVE_INTERVAL)
            await save_db()
    except asyncio.CancelledError:
        await save_db(force=True)


# =========================
# БАЗА ПРЕДМЕТОВ
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

ITEM_BY_ID = {item["id"]: item for item in ITEM_DATABASE}


# =========================
# РЫНОК
# =========================

MARKET = {
    "lots": [],
    "last_update": 0,
    "id_counter": 100,
}


def choose_random_item():
    value = random.uniform(0, 100)
    cumulative = 0

    for item in ITEM_DATABASE:
        cumulative += item["chance"]
        if value <= cumulative:
            return item

    return ITEM_DATABASE[-1]


def refresh_market_if_needed():
    now = time.time()

    if now - MARKET["last_update"] <= 300 and MARKET["lots"]:
        return

    MARKET["last_update"] = now

    new_lots = []

    for _ in range(random.randint(4, 6)):
        chosen_item = choose_random_item()

        MARKET["id_counter"] += 1

        new_lots.append({
            "id": MARKET["id_counter"],
            "item_id": chosen_item["id"],
            "name": chosen_item["name"],
            "rarity": chosen_item["rarity"],
            "price": max(1, chosen_item["price"] + random.randint(-2, 5)),
            "mine": 0,
        })

    # Лоты игроков сохраняются.
    MARKET["lots"] = [
        lot for lot in MARKET["lots"] if lot.get("owner")
    ] + new_lots


def lots_for(login):
    return [
        {**lot, "mine": 1 if lot.get("owner") == login else 0}
        for lot in MARKET["lots"]
    ]


# =========================
# TELEGRAM API
# =========================

async def call(session, method, **params):
    try:
        async with session.post(
            f"{API}/{method}",
            json=params,
        ) as response:
            return await response.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =========================
# CORS
# =========================

@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except Exception as e:
            print("REQUEST ERROR:", repr(e))
            response = web.json_response(
                {"error": "Internal server error"},
                status=500,
            )

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

    return response


# =========================
# АВТОРИЗАЦИЯ TELEGRAM
# =========================

def tg_user_id(data):
    init = data.get("initData") or ""

    if not init or not TOKEN:
        return None

    try:
        pairs = dict(parse_qsl(init, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")

        check = "\n".join(
            f"{key}={value}"
            for key, value in sorted(pairs.items())
        )

        secret = hmac.new(
            b"WebAppData",
            TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret,
            check.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        return json.loads(pairs["user"])["id"]

    except Exception:
        return None


def get_user_login(data):
    uid = tg_user_id(data)

    if uid is not None:
        login = f"tg_{uid}"

        if login not in USERS:
            USERS[login] = new_user()
            mark_db_dirty()

        return login

    return str(data.get("login", "")).strip()


# =========================
# ПОЛЬЗОВАТЕЛЬ
# =========================

def new_user(password=""):
    return {
        "password": password,
        "coins": 100,
        "tokens": 5,
        "inv": {"cooler": 1},
        "next_box": 0,
        "mine": [],
        "game_state": {},
        "perks": {},
        "purchases": [],
    }


def sync(user):
    game_state = user.get("game_state")

    if game_state:
        game_state["coins"] = user["coins"]
        game_state["tokens"] = user.get("tokens", 5)
        game_state["inv"] = user["inv"]
        game_state["next"] = user["next_box"]
        game_state["mine"] = user.get("mine", [])


def state_of(user):
    return user.get("game_state") or {
        "coins": user.get("coins", 100),
        "tokens": user.get("tokens", 5),
        "inv": user.get("inv", {}),
        "next": user.get("next_box", 0),
        "mine": user.get("mine", []),
    }


# =========================
# ROUTES
# =========================

async def handle_index(request):
    return web.Response(text="Bot Webhook Server is running!")


async def handle_register(request):
    try:
        data = await request.json()

        login = str(data.get("login", "")).strip()
        password = str(data.get("password", "")).strip()

        if not login or not password:
            return web.json_response(
                {"error": "Заполните логин и пароль"},
                status=400,
            )

        if login in USERS or login.startswith("tg_"):
            return web.json_response(
                {"error": "Логин уже занят"},
                status=400,
            )

        USERS[login] = new_user(password)
        mark_db_dirty()

        return web.json_response({
            "status": "ok",
            "login": login,
        })

    except Exception as e:
        return web.json_response(
            {"error": str(e)},
            status=500,
        )


async def handle_login(request):
    try:
        data = await request.json()

        login = str(data.get("login", "")).strip()
        password = str(data.get("password", "")).strip()

        user = USERS.get(login)

        if not user or user.get("password") != password:
            return web.json_response(
                {"error": "Неверный логин или пароль"},
                status=400,
            )

        return web.json_response({
            "status": "ok",
            "login": login,
            "state": state_of(user),
        })

    except Exception as e:
        return web.json_response(
            {"error": str(e)},
            status=500,
        )


async def handle_webhook(request):
    try:
        data = await request.json()
        session = request.app["s"]

        message = data.get("message", {})

        if message.get("successful_payment"):
            await grant_purchase(session, message)
            return web.Response(text="OK")

        if "pre_checkout_query" in data:
            pcq = data["pre_checkout_query"]
            ok = pcq.get("invoice_payload") in ITEMS

            extra = {} if ok else {
                "error_message": "Неизвестный товар"
            }

            await call(
                session,
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=ok,
                **extra,
            )

            return web.Response(text="OK")

        if message.get("text", "").startswith("/start"):
            chat_id = message["chat"]["id"]

            await call(
                session,
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "🌱 Добро пожаловать в Нейроферму!\n\n"
                    "Запускай игру 👇"
                ),
                reply_markup={
                    "inline_keyboard": [[
                        {
                            "text": "🎮 Играть",
                            "web_app": {"url": WEBAPP_URL},
                        }
                    ]]
                },
            )

    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))

    return web.Response(text="OK")


async def handle_me(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login)

    if not user:
        return web.json_response(
            {"error": "Unauthorized"},
            status=401,
        )

    return web.json_response({
        "state": state_of(user)
    })


async def handle_save(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user_state = data.get("state")

    if not login or login not in USERS:
        return web.json_response(
            {"error": "Unauthorized"},
            status=401,
        )

    if isinstance(user_state, dict):
        user = USERS[login]

        user["game_state"] = user_state

        if "coins" in user_state:
            user["coins"] = user_state["coins"]

        if "tokens" in user_state:
            user["tokens"] = user_state["tokens"]

        if "inv" in user_state:
            user["inv"] = user_state["inv"]

        if "next" in user_state:
            user["next_box"] = user_state["next"]

        if "mine" in user_state:
            user["mine"] = user_state["mine"]

        sync(user)
        mark_db_dirty()

    return web.json_response({"status": "ok"})


async def handle_drop(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login)

    if not user:
        return web.json_response(
            {"error": "Пользователь не найден"},
            status=400,
        )

    now = time.time() * 1000

    if now < user.get("next_box", 0):
        return web.json_response(
            {"error": "Ящик ещё не готов"},
            status=400,
        )

    chosen_item = choose_random_item()["id"]

    inventory = user.setdefault("inv", {})
    inventory[chosen_item] = inventory.get(chosen_item, 0) + 1

    user["next_box"] = now + 60000

    sync(user)
    mark_db_dirty()

    return web.json_response({
        "item": chosen_item,
        "state": state_of(user),
    })


async def handle_market(request):
    refresh_market_if_needed()

    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login)

    if user:
        state_data = state_of(user)
    else:
        state_data = {
            "coins": 100,
            "tokens": 5,
            "inv": {},
            "next": 0,
            "mine": [],
        }

    time_left = max(
        0,
        300 - int(time.time() - MARKET["last_update"]),
    )

    return web.json_response({
        "state": state_data,
        "lots": lots_for(login),
        "ttl": time_left,
    })


async def handle_buy_lot(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login)
    lot_id = data.get("id")

    if not user:
        return web.json_response(
            {"error": "Пользователь не найден"},
            status=400,
        )

    lot = next(
        (lot for lot in MARKET["lots"] if lot["id"] == lot_id),
        None,
    )

    if not lot:
        return web.json_response(
            {"error": "Лот уже продан или обновлен"},
            status=400,
        )

    if lot.get("owner") == login:
        return web.json_response(
            {"error": "Это ваш лот"},
            status=400,
        )

    price = lot["price"]

    if user.get("coins", 0) < price:
        return web.json_response(
            {"error": "Не хватает монет"},
            status=400,
        )

    user["coins"] -= price

    seller = USERS.get(lot.get("owner"))

    if seller:
        seller["coins"] = seller.get("coins", 0) + int(price * 0.95)
        sync(seller)

    inventory = user.setdefault("inv", {})
    item_id = lot["item_id"]
    inventory[item_id] = inventory.get(item_id, 0) + 1

    MARKET["lots"].remove(lot)

    sync(user)
    mark_db_dirty()

    return web.json_response({
        "state": state_of(user),
        "lots": lots_for(login),
    })


async def handle_list_lot(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    login = get_user_login(data)
    user = USERS.get(login)

    item = data.get("item")

    try:
        price = int(data.get("price", 0))
    except (TypeError, ValueError):
        price = 0

    inventory = user.get("inv", {}) if user else {}

    if not user or price <= 0 or inventory.get(item, 0) <= 0:
        return web.json_response(
            {"error": "Неверные данные или нет предмета"},
            status=400,
        )

    inventory[item] -= 1

    if inventory[item] <= 0:
        del inventory[item]

    MARKET["id_counter"] += 1

    item_info = ITEM_BY_ID.get(
        item,
        {
            "name": item,
            "rarity": "Обычный",
        },
    )

    MARKET["lots"].append({
        "id": MARKET["id_counter"],
        "item_id": item,
        "name": item_info["name"],
        "rarity": item_info["rarity"],
        "price": price,
        "mine": 1,
        "owner": login,
    })

    sync(user)
    mark_db_dirty()

    return web.json_response({
        "state": state_of(user),
        "lots": lots_for(login),
    })


# =========================
# ПОКУПКИ TELEGRAM
# =========================

COINS = {
    "coins1": 500,
    "coins2": 3000,
}


async def grant_purchase(session, message):
    successful_payment = message["successful_payment"]

    item = successful_payment.get("invoice_payload")
    uid = message.get("from", {}).get("id")
    charge_id = successful_payment.get(
        "telegram_payment_charge_id"
    )

    if item not in ITEMS or uid is None:
        return

    login = f"tg_{uid}"
    user = USERS.setdefault(login, new_user())

    purchases = user.setdefault("purchases", [])

    if any(
        purchase.get("charge") == charge_id
        for purchase in purchases
    ):
        return

    purchases.append({
        "item": item,
        "charge": charge_id,
        "ts": time.time(),
    })

    if item in COINS:
        user["coins"] = user.get("coins", 0) + COINS[item]
        sync(user)
    else:
        perks = user.setdefault("perks", {})
        perks[item] = perks.get(item, 0) + 1

    mark_db_dirty()

    await call(
        session,
        "sendMessage",
        chat_id=message["chat"]["id"],
        text=f"🎁 Покупка получена: {ITEMS[item][0]}. Спасибо!",
    )


async def handle_invoice(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    item = data.get("item")

    if item not in ITEMS:
        return web.json_response(
            {"error": "Неизвестный товар"},
            status=400,
        )

    name, desc, price = ITEMS[item]

    result = await call(
        request.app["s"],
        "createInvoiceLink",
        title=name,
        description=desc,
        payload=item,
        provider_token="",
        currency="XTR",
        prices=[
            {
                "label": name,
                "amount": price,
            }
        ],
    )

    if not result.get("ok"):
        return web.json_response(
            {"error": "Не удалось создать счёт"},
            status=500,
        )

    return web.json_response({
        "link": result["result"]
    })


async def handle_use(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    user = USERS.get(get_user_login(data))
    item = data.get("item")

    if not user:
        return web.json_response(
            {"error": "Пользователь не найден"},
            status=400,
        )

    inventory = user.setdefault("inv", {})

    if inventory.get(item, 0) <= 0:
        return web.json_response(
            {"error": "Нет предмета"},
            status=400,
        )

    inventory[item] -= 1

    if inventory[item] <= 0:
        del inventory[item]

    sync(user)
    mark_db_dirty()

    return web.json_response({
        "state": state_of(user)
    })


# =========================
# ЗАПУСК
# =========================

async def on_start(app):
    timeout = ClientTimeout(total=15)

    app["s"] = ClientSession(timeout=timeout)

    app["db_task"] = asyncio.create_task(
        periodic_db_save(app)
    )

    if TOKEN:
        result = await call(
            app["s"],
            "setWebhook",
            url=f"{RENDER_URL}/webhook",
            allowed_updates=[
                "message",
                "pre_checkout_query",
            ],
        )

        if not result.get("ok"):
            print("WEBHOOK ERROR:", result)


async def on_cleanup(app):
    task = app.get("db_task")

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    await save_db(force=True)

    session = app.get("s")

    if session:
        await session.close()


app = web.Application(
    middlewares=[cors],
    client_max_size=2 * 1024 * 1024,
)

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
app.router.add_route("*", "/api/invoice", handle_invoice)
app.router.add_route("*", "/api/use", handle_use)

app.on_startup.append(on_start)
app.on_cleanup.append(on_cleanup)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(
        app,
        host="0.0.0.0",
        port=port,
    )
