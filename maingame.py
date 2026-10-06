```python
import os
import time
import random
from aiohttp import web, ClientSession


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = "ВСТАВЬ_СЮДА_НОВЫЙ_ТОКЕН_БОТА"

WEBAPP_URL = "https://proman80k-wq.github.io/Kojsanislux/"
RENDER_URL = "https://game-2gla.onrender.com"

API = f"https://api.telegram.org/bot{TOKEN}"

TOKEN_TO_USD_RATE = 1_000_000


# =========================
# ПРЕДМЕТЫ
# =========================

ITEMS = {
    "boost2": (
        "Разгон ×2 на 1 час",
        "Весь доход ×2",
        15
    ),
    "boost5": (
        "Разгон ×5 на 1 час",
        "Весь доход ×5",
        50
    ),
    "auto": (
        "Авто-тап",
        "3 тапа в секунду навсегда",
        100
    ),
    "offline": (
        "Работа во сне",
        "Офлайн-доход до 8 часов",
        75
    ),
    "perm2": (
        "Вечный ×2",
        "Постоянный множитель к доходу",
        250
    ),
    "coins1": (
        "500 монет биржи 🪙",
        "Для покупок у других игроков",
        50
    ),
    "coins2": (
        "3000 монет биржи 🪙",
        "Выгоднее на 20%",
        250
    ),
}


# =========================
# ПОЛЬЗОВАТЕЛИ
# =========================

USERS = {}


# =========================
# РЫНОК
# =========================

MARKET = {
    "lots": [
        {
            "id": 1,
            "item": "cooler",
            "price": 15,
            "mine": 0
        },
        {
            "id": 2,
            "item": "ram",
            "price": 50,
            "mine": 0
        },
        {
            "id": 3,
            "item": "gpu",
            "price": 180,
            "mine": 0
        }
    ],
    "id_counter": 10
}


# =========================
# ШАНСЫ ДРОПА
# =========================

W8 = {
    "cooler": 55,
    "ram": 28,
    "gpu": 14,
    "core": 3
}


# =========================
# TELEGRAM API
# =========================

async def call(session, method, **params):

    try:

        async with session.post(
            f"{API}/{method}",
            json=params
        ) as response:

            result = await response.json()

            print(
                f"Telegram API: {method} -> "
                f"{result.get('ok')}"
            )

            if not result.get("ok"):

                print(
                    "Telegram API error:",
                    result
                )

            return result

    except Exception as e:

        print(
            f"Telegram API exception ({method}):",
            repr(e)
        )

        return {
            "ok": False,
            "error": str(e)
        }


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

            print(
                "Request error:",
                repr(e)
            )

            response = web.json_response(
                {
                    "error":
                        "Internal server error"
                },
                status=500
            )


    response.headers[
        "Access-Control-Allow-Origin"
    ] = "*"

    response.headers[
        "Access-Control-Allow-Headers"
    ] = "Content-Type"

    response.headers[
        "Access-Control-Allow-Methods"
    ] = "GET, POST, OPTIONS"

    return response


# =========================
# USER ID
# =========================

def get_user_id(data):

    return str(
        data.get(
            "user_id",
            "test_user"
        )
    )


# =========================
# ГЛАВНАЯ
# =========================

async def handle_index(request):

    return web.Response(
        text="Bot Webhook Server is running!"
    )


# =========================
# WEBHOOK
# =========================

async def handle_webhook(request):

    try:

        data = await request.json()

        print("")
        print("==============================")
        print("TELEGRAM UPDATE:")
        print(data)
        print("==============================")
        print("")

        session = request.app["s"]


        # =========================
        # PRE CHECKOUT
        # =========================

        if "pre_checkout_query" in data:

            pcq = data[
                "pre_checkout_query"
            ]

            print(
                "PRE CHECKOUT:",
                pcq
            )

            await call(
                session,
                "answerPreCheckoutQuery",

                pre_checkout_query_id=
                    pcq["id"],

                ok=True
            )

            return web.Response(
                text="OK"
            )


        # =========================
        # MESSAGE
        # =========================

        message = data.get(
            "message",
            {}
        )


        # =========================
        # ОПЛАТА
        # =========================

        if "successful_payment" in message:

            payment = message[
                "successful_payment"
            ]

            uid = str(
                message["from"]["id"]
            )

            payload = payment[
                "invoice_payload"
            ]

            print(
                "PAID:",
                uid,
                payload
            )


            if uid not in USERS:

                USERS[uid] = {
                    "coins": 0,
                    "inv": {},
                    "next_box": 0,
                    "mine": [],
                    "usd_rate":
                        TOKEN_TO_USD_RATE
                }


            if payload == "coins1":

                USERS[uid]["coins"] += 500


            elif payload == "coins2":

                USERS[uid]["coins"] += 3000


            print(
                "USER AFTER PAYMENT:",
                USERS[uid]
            )


        # =========================
        # /START
        # =========================

        elif message.get(
            "text",
            ""
        ).startswith("/start"):

            chat_id = message[
                "chat"
            ]["id"]

            user = message.get(
                "from",
                {}
            )


            print("")
            print("==============================")
            print("START COMMAND")
            print("Chat ID:", chat_id)
            print("User:", user)
            print("==============================")
            print("")


            uid = str(
                user.get(
                    "id",
                    chat_id
                )
            )


            # Создаём пользователя

            if uid not in USERS:

                USERS[uid] = {
                    "coins": 100,

                    "inv": {
                        "cooler": 1
                    },

                    "next_box": 0,

                    "mine": [],

                    "usd_rate":
                        TOKEN_TO_USD_RATE
                }

                print(
                    "Created new user:",
                    uid
                )


            # Отправляем сообщение

            result = await call(

                session,

                "sendMessage",

                chat_id=chat_id,

                text=(
                    "🌱 Добро пожаловать "
                    "в Нейроферму!\n\n"
                    "Запускай игру 👇"
                ),

                reply_markup={

                    "inline_keyboard": [

                        [

                            {

                                "text":
                                    "🎮 Играть",

                                "web_app": {

                                    "url":
                                        WEBAPP_URL
                                }
                            }
                        ]
                    ]
                }
            )


            print(
                "START RESPONSE:",
                result
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
# /API/ME
# =========================

async def handle_me(request):

    try:

        data = await request.json()

    except:

        data = {}


    uid = get_user_id(
        data
    )


    if uid not in USERS:

        USERS[uid] = {

            "coins":
                100,

            "inv": {
                "cooler": 1
            },

            "next_box":
                0,

            "mine":
                [],

            "usd_rate":
                TOKEN_TO_USD_RATE
        }


    user = USERS[uid]


    return web.json_response({

        "state": {

            "coins":
                user["coins"],

            "inv":
                user["inv"],

            "next":
                user["next_box"],

            "mine":
                user["mine"],

            "rate":
                user["usd_rate"]
        }
    })


# =========================
# DROP
# =========================

async def handle_drop(request):

    data = await request.json()

    uid = get_user_id(
        data
    )

    user = USERS.get(
        uid
    )

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


    chosen = "cooler"


    for item, weight in W8.items():

        value -= weight

        if value < 0:

            chosen = item

            break


    user["inv"][chosen] = (
        user["inv"].get(
            chosen,
            0
        ) + 1
    )


    user["next_box"] = (
        now + 60000
    )


    return web.json_response({

        "item":
            chosen,

        "state": {

            "coins":
                user["coins"],

            "inv":
                user["inv"],

            "next":
                user["next_box"],

            "mine":
                user["mine"]
        }
    })


# =========================
# MARKET
# =========================

async def handle_market(request):

    try:

        data = await request.json()

    except:

        data = {}


    uid = get_user_id(
        data
    )


    user = USERS.get(

        uid,

        {
            "coins": 0,
            "inv": {},
            "next_box": 0,
            "mine": []
        }
    )


    return web.json_response({

        "state": {

            "coins":
                user["coins"],

            "inv":
                user["inv"],

            "next":
                user["next_box"],

            "mine":
                user["mine"]
        },

        "lots":
            MARKET["lots"]
    })


# =========================
# BUY
# =========================

async def handle_buy_lot(request):

    data = await request.json()

    uid = get_user_id(
        data
    )

    user = USERS.get(
        uid
    )

    lot_id = data.get(
        "id"
    )


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
            lot

            for lot in MARKET["lots"]

            if lot["id"] == lot_id
        ),

        None
    )


    if not lot:

        return web.json_response(

            {
                "error":
                    "Лот уже продан"
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


    user["inv"][lot["item"]] = (

        user["inv"].get(
            lot["item"],
            0
        ) + 1
    )


    MARKET["lots"].remove(
        lot
    )


    if (
        lot.get("mine")
        and
        str(
            lot.get("owner")
        ) != uid
    ):

        owner = USERS.get(
            str(
                lot.get(
                    "owner"
                )
            )
        )


        if owner:

            owner["coins"] += int(
                lot["price"] * 0.95
            )


    return web.json_response({

        "state": {

            "coins":
                user["coins"],

            "inv":
                user["inv"],

            "next":
                user["next_box"],

            "mine":
                user["mine"]
        },

        "lots":
            MARKET["lots"]
    })


# =========================
# LIST LOT
# =========================

async def handle_list_lot(request):

    data = await request.json()

    uid = get_user_id(
        data
    )

    user = USERS.get(
        uid
    )

    item = data.get(
        "item"
    )

    price = int(
        data.get(
            "price",
            0
        )
    )


    if not user:

        return web.json_response(

            {
                "error":
                    "Пользователь не найден"
            },

            status=400
        )


    if (
        price <= 0
        or
        not user["inv"].get(
            item,
            0
        ) > 0
    ):

        return web.json_response(

            {
                "error":
                    "Неверные данные "
                    "или нет предмета"
            },

            status=400
        )


    user["inv"][item] -= 1


    if user["inv"][item] <= 0:

        del user["inv"][item]


    MARKET["id_counter"] += 1


    new_lot = {

        "id":
            MARKET["id_counter"],

        "item":
            item,

        "price":
            price,

        "mine":
            1,

        "owner":
            uid
    }


    MARKET["lots"].append(
        new_lot
    )


    user["mine"].append(
        new_lot
    )


    return web.json_response({

        "state": {

            "coins":
                user["coins"],

            "inv":
                user["inv"],

            "next":
                user["next_box"],

            "mine":
                user["mine"]
        },

        "lots":
            MARKET["lots"]
    })


# =========================
# INVOICE
# =========================

async def invoice(request):

    data = await request.json()

    item_key = data.get(
        "item"
    )


    item = ITEMS.get(
        item_key
    )


    if not item:

        return web.json_response(

            {
                "error":
                    "bad item"
            },

            status=400
        )


    result = await call(

        request.app["s"],

        "createInvoiceLink",

        title=item[0],

        description=item[1],

        payload=item_key,

        currency="XTR",

        prices=[

            {
                "label":
                    item[0],

                "amount":
                    item[2]
            }
        ]
    )


    if not result.get("ok"):

        return web.json_response(

            {
                "error":
                    result.get(
                        "description",
                        "Telegram error"
                    )
            },

            status=500
        )


    return web.json_response({

        "link":
            result["result"]
    })


# =========================
# STARTUP
# =========================

async def on_start(app):

    print("")
    print("==============================")
    print("STARTING BOT SERVER")
    print("==============================")
    print("")


    if not TOKEN:

        print(
            "ERROR: TOKEN пустой!"
        )

        return


    app["s"] = ClientSession()


    # Проверяем бота

    bot_info = await call(
        app["s"],
        "getMe"
    )


    print(
        "BOT INFO:",
        bot_info
    )


    if not bot_info.get("ok"):

        print(
            "ОШИБКА ТОКЕНА!"
        )

        return


    # =========================
    # WEBHOOK
    # =========================

    webhook_url = (
        f"{RENDER_URL}/webhook"
    )


    print(
        "Устанавливаю webhook:",
        webhook_url
    )


    webhook_result = await call(

        app["s"],

        "setWebhook",

        url=webhook_url,

        allowed_updates=[

            "message",

            "pre_checkout_query"
        ]
    )


    print(
        "SET WEBHOOK:",
        webhook_result
    )


    # Проверяем webhook

    webhook_info = await call(

        app["s"],

        "getWebhookInfo"
    )


    print(
        "WEBHOOK INFO:",
        webhook_info
    )


# =========================
# CLEANUP
# =========================

async def on_cleanup(app):

    if "s" in app:

        await app["s"].close()


# =========================
# APPLICATION
# =========================

app = web.Application(

    middlewares=[
        cors
    ]
)


app.router.add_get(
    "/",
    handle_index
)

app.router.add_post(
    "/webhook",
    handle_webhook
)

app.router.add_route(
    "*",
    "/api/invoice",
    invoice
)

app.router.add_route(
    "*",
    "/api/me",
    handle_me
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


app.on_startup.append(
    on_start
)

app.on_cleanup.append(
    on_cleanup
)


# =========================
# ЗАПУСК
# =========================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )


    print(
        f"SERVER PORT: {port}"
    )


    web.run_app(

        app,

        host="0.0.0.0",

        port=port
    )
