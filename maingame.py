import os
import json
import time
import random
import hmac
import hashlib
from urllib.parse import parse_qsl
from aiohttp import web, ClientSession

# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.environ.get("8307112310:AAFneoMo4ACr6SKTloHNQ9hVZIw1mf-apGQ", "")
WEBAPP_URL = "https://proman890k-wq.github.io/Kojsanislux/"
RENDER_URL = "https://game-2gla.onrender.com"
API = f"https://api.telegram.org/bot{TOKEN}"
TOKEN_TO_USD_RATE = 1_000_000
DB_FILE = "database.json"

# =========================
# РАБОТА С БАЗОЙ ДАННЫХ
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
    {
        "id": "cooler",
        "name": "Кулер",
        "rarity": "Обычный",
        "price": 10,
        "chance": 60.0
    },
    {
        "id": "ram",
        "name": "Памятка RAM",
        "rarity": "Редкий",
        "price": 50,
        "chance": 25.0
    },
    {
        "id": "gpu",
        "name": "Видеокарта RTX",
        "rarity": "Сверхредкий",
        "price": 200,
        "chance": 10.0
    },
    {
        "id": "core",
        "name": "Квантовое ядро",
        "rarity": "Эпический",
        "price": 800,
        "chance": 4.9999
    },
    {
        "id": "neural_matrix",
        "name": "Нейроматрица ИИ",
        "rarity": "Ультраредкий",
        "price": 5000,
        "chance": 0.0001
    },
]

# =========================
# РЫНОК
# =========================

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

        MARKET["lots"] = [
            l for l in MARKET["lots"]
            if l.get("owner")
        ] + new_lots


# =========================
# TELEGRAM API & CORS
# =========================

async def call(session, method, **params):
    try:
        async with session.post(
            f"{API}/{method}",
            json=params
        ) as response:
            return await response.json()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


@web.middleware
async def cors(request, handler):

    if request.method == "OPTIONS":
        response = web.Response()

    else:
        try:
            response = await handler(request)

        except Exception as e:
            print("SERVER ERROR:", repr(e))

            response = web.json_response(
                {"error": "Internal server error"},
                status=500
            )

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

    return response


# =========================
# TELEGRAM AUTH
# =========================

def tg_user_id(data):

    init = data.get("initData") or ""

    if not init or not TOKEN:
        return None

    try:
        pairs = dict(
            parse_qsl(
                init,
                keep_blank_values=True
            )
        )

        recv = pairs.pop("hash", "")

        check = "\n".join(
            f"{k}={v}"
            for k, v in sorted(pairs.items())
        )

        secret = hmac.new(
            b"WebAppData",
            TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calc = hmac.new(
            secret,
            check.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calc, recv):
            return None

        return json.loads(
            pairs["user"]
        )["id"]

    except Exception:
        return None


def get_user_login(data):

    uid = tg_user_id(data)

    if uid is not None:

        login = f"tg_{uid}"

        if login not in USERS:
            USERS[login] = new_user()
            save_db(USERS)

        return login

    return str(
        data.get("login", "")
    ).strip()


# =========================
# ОСНОВНЫЕ ROUTES
# =========================

async def handle_index(request):

    return web.Response(
        text="Bot Webhook Server is running!"
    )


# =========================
# HEALTH CHECK
# =========================

async def handle_health(request):

    return web.json_response({
        "status": "ok",
        "service": "neurofarm",
        "time": int(time.time())
    })


# =========================
# РЕГИСТРАЦИЯ
# =========================

async def handle_register(request):

    try:

        data = await request.json()

        login = data.get(
            "login",
            ""
        ).strip()

        password = data.get(
            "password",
            ""
        ).strip()

        if not login or not password:

            return web.json_response(
                {
                    "error": "Заполните логин и пароль"
                },
                status=400
            )

        if login in USERS or login.startswith("tg_"):

            return web.json_response(
                {
                    "error": "Логин уже занят"
                },
                status=400
            )

        USERS[login] = new_user(password)

        save_db(USERS)

        return web.json_response({
            "status": "ok",
            "login": login
        })

    except Exception as e:

        return web.json_response(
            {"error": str(e)},
            status=500
        )


# =========================
# ВХОД
# =========================

async def handle_login(request):

    try:

        data = await request.json()

        login = data.get(
            "login",
            ""
        ).strip()

        password = data.get(
            "password",
            ""
        ).strip()

        if (
            login not in USERS
            or USERS[login].get("password") != password
        ):

            return web.json_response(
                {
                    "error": "Неверный логин или пароль"
                },
                status=400
            )

        user = USERS[login]

        response_state = user.get(
            "game_state",
            {}
        )

        if not response_state:

            response_state = {
                "coins": user.get(
                    "coins",
                    100
                ),
                "tokens": user.get(
                    "tokens",
                    5
                ),
                "inv": user.get(
                    "inv",
                    {}
                ),
                "next": user.get(
                    "next_box",
                    0
                ),
                "mine": user.get(
                    "mine",
                    []
                )
            }

        return web.json_response({
            "status": "ok",
            "login": login,
            "state": response_state
        })

    except Exception as e:

        return web.json_response(
            {"error": str(e)},
            status=500
        )


# =========================
# TELEGRAM WEBHOOK
# =========================

async def handle_webhook(request):

    try:

        data = await request.json()

        session = request.app["s"]

        message = data.get(
            "message",
            {}
        )

        if message.get("successful_payment"):

            await grant_purchase(
                session,
                message
            )

            return web.Response(
                text="OK"
            )

        if "pre_checkout_query" in data:

            pcq = data[
                "pre_checkout_query"
            ]

            ok = (
                pcq.get("invoice_payload")
                in ITEMS
            )

            extra = {}

            if not ok:
                extra = {
                    "error_message":
                        "Неизвестный товар"
                }

            await call(
                session,
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=ok,
                **extra
            )

            return web.Response(
                text="OK"
            )

        if message.get(
            "text",
            ""
        ).startswith("/start"):

            chat_id = message[
                "chat"
            ]["id"]

            await call(
                session,
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "🌱 Добро пожаловать "
                    "в Нейроферму!\n\n"
                    "Запускай игру 👇"
                ),
                reply_markup={
                    "inline_keyboard": [[
                        {
                            "text": "🎮 Играть",
                            "web_app": {
                                "url": WEBAPP_URL
                            }
                        }
                    ]]
                }
            )

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            repr(e)
        )

    return web.Response(
        text="OK"
    )


# =========================
# ПРОФИЛЬ
# =========================

async def handle_me(request):

    try:
        data = await request.json()

    except:
        data = {}

    login = get_user_login(data)

    if not login or login not in USERS:

        return web.json_response(
            {"error": "Unauthorized"},
            status=401
        )

    user = USERS[login]

    response_state = user.get(
        "game_state",
        {}
    )

    if not response_state:

        response_state = {
            "coins": user.get(
                "coins",
                100
            ),
            "tokens": user.get(
                "tokens",
                5
            ),
            "inv": user.get(
                "inv",
                {}
            ),
            "next": user.get(
                "next_box",
                0
            ),
            "mine": user.get(
                "mine",
                []
            )
        }

    return web.json_response({
        "state": response_state
    })


# =========================
# СОХРАНЕНИЕ
# =========================

async def handle_save(request):

    try:
        data = await request.json()

    except:
        data = {}

    login = get_user_login(data)

    user_state = data.get(
        "state"
    )

    if not login or login not in USERS:

        return web.json_response(
            {"error": "Unauthorized"},
            status=401
        )

    if (
        user_state
        and isinstance(user_state, dict)
    ):

        pwd = USERS[login].get(
            "password"
        )

        USERS[login]["game_state"] = user_state

        if "coins" in user_state:
            USERS[login]["coins"] = user_state["coins"]

        if "tokens" in user_state:
            USERS[login]["tokens"] = user_state["tokens"]

        if "inv" in user_state:
            USERS[login]["inv"] = user_state["inv"]

        if "next" in user_state:
            USERS[login]["next_box"] = user_state["next"]

        if "mine" in user_state:
            USERS[login]["mine"] = user_state["mine"]

        USERS[login]["password"] = pwd

        save_db(USERS)

    return web.json_response({
        "status": "ok"
    })


# =========================
# DROP
# =========================

async def handle_drop(request):

    data = await request.json()

    login = get_user_login(data)

    user = USERS.get(login)

    now = time.time() * 1000

    if not user:

        return web.json_response(
            {
                "error":
                    "Пользователь не найден"
            },
            status=400
        )

    if now < user["next_box"]:

        return web.json_response(
            {
                "error":
                    "Ящик ещё не готов"
            },
            status=400
        )

    value = random.uniform(
        0,
        100
    )

    chosen_item = ITEM_DATABASE[0]["id"]

    cumulative = 0

    for item in ITEM_DATABASE:

        cumulative += item["chance"]

        if value <= cumulative:

            chosen_item = item["id"]

            break

    user["inv"][chosen_item] = (
        user["inv"].get(
            chosen_item,
            0
        ) + 1
    )

    user["next_box"] = (
        now + 60000
    )

    if user.get("game_state"):

        user["game_state"]["inv"] = (
            user["inv"]
        )

        user["game_state"]["next"] = (
            user["next_box"]
        )

    save_db(USERS)

    return web.json_response({
        "item": chosen_item,
        "state": user.get(
            "game_state",
            {
                "coins":
                    user["coins"],
                "tokens":
                    user["tokens"],
                "inv":
                    user["inv"],
                "next":
                    user["next_box"]
            }
        )
    })


# =========================
# РЫНОК
# =========================

async def handle_market(request):

    refresh_market_if_needed()

    try:
        data = await request.json()

    except:
        data = {}

    login = get_user_login(data)

    user = USERS.get(
        login,
        {
            "coins": 100,
            "tokens": 5,
            "inv": {},
            "next_box": 0,
            "mine": []
        }
    )

    state_data = user.get(
        "game_state",
        {
            "coins":
                user.get(
                    "coins",
                    100
                ),
            "tokens":
                user.get(
                    "tokens",
                    5
                ),
            "inv":
                user.get(
                    "inv",
                    {}
                ),
            "next":
                user.get(
                    "next_box",
                    0
                ),
            "mine":
                user.get(
                    "mine",
                    []
                )
        }
    )

    time_left = max(
        0,
        300 - int(
            time.time()
            - MARKET["last_update"]
        )
    )

    return web.json_response({
        "state": state_data,
        "lots": lots_for(login),
        "ttl": time_left
    })


# =========================
# ПОКУПКА ЛОТА
# =========================

async def handle_buy_lot(request):

    data = await request.json()

    login = get_user_login(data)

    user = USERS.get(login)

    lot_id = data.get("id")

    if not user:

        return web.json_response(
            {
                "error":
                    "Пользователь не найден"
            },
            status=400
        )

    lot = next(
        (
            l for l in MARKET["lots"]
            if l["id"] == lot_id
        ),
        None
    )

    if not lot:

        return web.json_response(
            {
                "error":
                    "Лот уже продан или обновлен"
            },
            status=400
        )

    if lot.get("owner") == login:

        return web.json_response(
            {
                "error":
                    "Это ваш лот"
            },
            status=400
        )

    if user["coins"] < lot["price"]:

        return web.json_response(
            {
                "error":
                    "Не хватает монет"
            },
            status=400
        )

    user["coins"] -= lot["price"]

    seller = USERS.get(
        lot.get("owner")
    )

    if seller:

        seller["coins"] = (
            seller.get(
                "coins",
                0
            )
            + int(
                lot["price"] * 0.95
            )
        )

        sync(seller)

    user["inv"][
        lot["item_id"]
    ] = (
        user["inv"].get(
            lot["item_id"],
            0
        ) + 1
    )

    MARKET["lots"].remove(
        lot
    )

    sync(user)

    save_db(USERS)

    return web.json_response({
        "state": state_of(user),
        "lots": lots_for(login)
    })


# =========================
# ВЫСТАВЛЕНИЕ ЛОТА
# =========================

async def handle_list_lot(request):

    data = await request.json()

    login = get_user_login(data)

    user = USERS.get(login)

    item = data.get("item")

    price = int(
        data.get(
            "price",
            0
        )
    )

    if (
        not user
        or price <= 0
        or user["inv"].get(
            item,
            0
        ) <= 0
    ):

        return web.json_response(
            {
                "error":
                    "Неверные данные или нет предмета"
            },
            status=400
        )

    user["inv"][item] -= 1

    if user["inv"][item] <= 0:

        del user["inv"][item]

    MARKET["id_counter"] += 1

    item_info = next(
        (
            i for i in ITEM_DATABASE
            if i["id"] == item
        ),
        {
            "name": item,
            "rarity": "Обычный"
        }
    )

    new_lot = {
        "id":
            MARKET["id_counter"],
        "item_id":
            item,
        "name":
            item_info["name"],
        "rarity":
            item_info["rarity"],
        "price":
            price,
        "mine":
            1,
        "owner":
            login
    }

    MARKET["lots"].append(
        new_lot
    )

    if user.get("game_state"):

        user["game_state"]["inv"] = (
            user["inv"]
        )

    save_db(USERS)

    return web.json_response({
        "state":
            user.get(
                "game_state"
            ),
        "lots":
            lots_for(login)
    })


# =========================
# МОНЕТЫ
# =========================

COINS = {
    "coins1": 500,
    "coins2": 3000
}


# =========================
# СОЗДАНИЕ ПОЛЬЗОВАТЕЛЯ
# =========================

def new_user(password=""):

    return {
        "password": password,
        "coins": 100,
        "tokens": 5,
        "inv": {
            "cooler": 1
        },
        "next_box": 0,
        "mine": [],
        "game_state": {},
        "perks": {},
        "purchases": []
    }


# =========================
# СИНХРОНИЗАЦИЯ
# =========================

def sync(user):

    gs = user.get(
        "game_state"
    )

    if gs:

        gs["coins"] = user["coins"]

        gs["inv"] = user["inv"]

        gs["next"] = user["next_box"]


def state_of(user):

    return user.get(
        "game_state"
    ) or {
        "coins":
            user.get(
                "coins",
                100
            ),
        "tokens":
            user.get(
                "tokens",
                5
            ),
        "inv":
            user.get(
                "inv",
                {}
            ),
        "next":
            user.get(
                "next_box",
                0
            ),
        "mine":
            user.get(
                "mine",
                []
            )
    }


def lots_for(login):

    return [
        {
            **l,
            "mine":
                1
                if l.get("owner") == login
                else 0
        }
        for l in MARKET["lots"]
    ]


# =========================
# ПОКУПКА
# =========================

async def grant_purchase(
    session,
    message
):

    sp = message[
        "successful_payment"
    ]

    item = sp.get(
        "invoice_payload"
    )

    uid = message.get(
        "from",
        {}
    ).get("id")

    cid = sp.get(
        "telegram_payment_charge_id"
    )

    if item not in ITEMS or uid is None:
        return

    login = f"tg_{uid}"

    user = USERS.setdefault(
        login,
        new_user()
    )

    purchases = user.setdefault(
        "purchases",
        []
    )

    if any(
        p.get("charge") == cid
        for p in purchases
    ):
        return

    purchases.append({
        "item": item,
        "charge": cid,
        "ts": time.time()
    })

    if item in COINS:

        user["coins"] = (
            user.get(
                "coins",
                0
            )
            + COINS[item]
        )

        sync(user)

    else:

        perks = user.setdefault(
            "perks",
            {}
        )

        perks[item] = (
            perks.get(
                item,
                0
            ) + 1
        )

    save_db(USERS)

    await call(
        session,
        "sendMessage",
        chat_id=message["chat"]["id"],
        text=(
            f"🎁 Покупка получена: "
            f"{ITEMS[item][0]}. "
            f"Спасибо!"
        )
    )


# =========================
# INVOICE
# =========================

async def handle_invoice(request):

    data = await request.json()

    item = data.get(
        "item"
    )

    if item not in ITEMS:

        return web.json_response(
            {
                "error":
                    "Неизвестный товар"
            },
            status=400
        )

    name, desc, price = ITEMS[item]

    r = await call(
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
                "amount": price
            }
        ]
    )

    if not r.get("ok"):

        return web.json_response(
            {
                "error":
                    "Не удалось создать счёт"
            },
            status=500
        )

    return web.json_response({
        "link":
            r["result"]
    })


# =========================
# ИСПОЛЬЗОВАНИЕ ПРЕДМЕТА
# =========================

async def handle_use(request):

    data = await request.json()

    user = USERS.get(
        get_user_login(data)
    )

    item = data.get(
        "item"
    )

    if (
        not user
        or user["inv"].get(
            item,
            0
        ) <= 0
    ):

        return web.json_response(
            {
                "error":
                    "Нет предмета"
            },
            status=400
        )

    user["inv"][item] -= 1

    if user["inv"][item] <= 0:

        del user["inv"][item]

    sync(user)

    save_db(USERS)

    return web.json_response({
        "state":
            state_of(user)
    })


# =========================
# ЗАПУСК СЕРВЕРА
# =========================

async def on_start(app):

    app["s"] = ClientSession()

    await call(
        app["s"],
        "setWebhook",
        url=f"{RENDER_URL}/webhook",
        allowed_updates=[
            "message",
            "pre_checkout_query"
        ]
    )


async def on_cleanup(app):

    if "s" in app:

        await app["s"].close()


# =========================
# СОЗДАНИЕ APPLICATION
# =========================

app = web.Application(
    middlewares=[cors]
)

# Главная
app.router.add_get(
    "/",
    handle_index
)

# Health check для Render
app.router.add_get(
    "/health",
    handle_health
)

# Telegram webhook
app.router.add_post(
    "/webhook",
    handle_webhook
)

# API
app.router.add_post(
    "/api/register",
    handle_register
)

app.router.add_post(
    "/api/login",
    handle_login
)

app.router.add_route(
    "*",
    "/api/me",
    handle_me
)

app.router.add_route(
    "*",
    "/api/save",
    handle_save
)

app.router.add_route(
    "*",
    "/api/drop",
    handle_drop
)

app.router.add_route(
    "*",
    "/api/market",
    handle_market
)

app.router.add_route(
    "*",
    "/api/buy",
    handle_buy_lot
)

app.router.add_route(
    "*",
    "/api/list",
    handle_list_lot
)

app.router.add_route(
    "*",
    "/api/invoice",
    handle_invoice
)

app.router.add_route(
    "*",
    "/api/use",
    handle_use
)

# Startup / Cleanup
app.on_startup.append(
    on_start
)

app.on_cleanup.append(
    on_cleanup
)


# =========================
# START
# =========================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=port
    )
