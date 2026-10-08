import os
import re
import json
import time
import random
import hmac
import hashlib
import asyncio
from urllib.parse import parse_qsl
from aiohttp import web, ClientSession, ClientTimeout

# =========================
# НАСТРОЙКИ (секреты — через переменные окружения Render)
# =========================
TOKEN = os.environ.get("BOT_TOKEN", "8307112310:AAFneoMo4ACr6SKTloHNQ9hVZIw1mf-apGQ")
PROMO_URL = os.environ.get("PROMO_URL", "https://raw.githubusercontent.com/proman890k-wq/Kojsanislux/refs/heads/main/promo.txt")
WEBAPP_URL = "https://proman890k-wq.github.io/Kojsanislux/"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://game-2gla.onrender.com")
DEV_MODE = os.environ.get("DEV_MODE") == "1"  # только для локальных тестов без Telegram
API = f"https://api.telegram.org/bot{TOKEN}"
DB_FILE = "database.json"
BOX_COOLDOWN_MS = 60_000
VIP_BOX_COOLDOWN_MS = 30_000
HOUR_MS = 3_600_000

if not TOKEN:
    print("WARNING: BOT_TOKEN не задан — авторизация и платежи работать не будут")

# =========================
# БАЗА ДАННЫХ
# =========================
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_db(data):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)

USERS = load_db()

def new_user():
    return {"inv": {"cooler": 1}, "next_box": 0, "perks": {}, "purchases": [],
            "promos": [], "created": time.time()}

def norm(user):
    for k, v in new_user().items():
        user.setdefault(k, v)
    return user

# =========================
# ПРЕДМЕТЫ И ДОНАТЫ
# =========================
ITEM_DATABASE = [
    {"id": "cooler", "chance": 60.0},
    {"id": "ram", "chance": 25.0},
    {"id": "gpu", "chance": 10.0},
    {"id": "core", "chance": 4.9999},
    {"id": "neural_matrix", "chance": 0.0001},
]

# id: (название, описание, цена в звёздах)
ITEMS = {
    "boost2": ("Разгон x2 на 1 час", "Весь доход x2. Время суммируется", 15),
    "boost5": ("Разгон x5 на 1 час", "Весь доход x5. Время суммируется", 50),
    "perm2": ("Вечный x2", "Постоянный множитель к доходу", 250),
    "auto2h": ("Авто-тап на 2 часа", "3 тапа в секунду 2 часа. Время суммируется", 350),
    "offline24": ("Работа во сне 24 ч", "Офлайн-доход копится до 24 часов", 500),
    "vip": ("VIP-статус", "Доход x1.5, ящики вдвое чаще, больше золотых звёзд, бейдж", 1000),
}

# =========================
# TELEGRAM API, CORS, АВТОРИЗАЦИЯ
# =========================
async def call(session, method, **params):
    try:
        async with session.post(f"{API}/{method}", json=params) as r:
            return await r.json()
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
            print("SERVER ERROR:", repr(e))
            response = web.json_response({"error": "Internal server error"}, status=500)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def get_login(data):
    """Логин = Telegram ID, проверенный по подписи initData."""
    uid = None
    init = data.get("initData") or ""
    if init and TOKEN:
        try:
            pairs = dict(parse_qsl(init, keep_blank_values=True))
            recv = pairs.pop("hash", "")
            check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
            secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
            calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            fresh = time.time() - int(pairs.get("auth_date", 0)) < 7 * 86400
            if hmac.compare_digest(calc, recv) and fresh:
                uid = json.loads(pairs.get("user", "{}")).get("id")
        except Exception:
            pass
    if uid is None and DEV_MODE and data.get("user_id") is not None:
        uid = data["user_id"]
    if uid is None:
        return None
    login = f"tg_{uid}"
    if login not in USERS:
        USERS[login] = new_user()
        save_db(USERS)
    norm(USERS[login])
    return login

def is_vip(user):
    return bool(user["perks"].get("vip"))

def state_of(user):
    return {"inv": user["inv"], "next": user["next_box"]}

# =========================
# ROUTES
# =========================
async def handle_index(request):
    return web.Response(text="Bot Webhook Server is running!")

async def handle_health(request):
    return web.json_response({"status": "ok", "time": int(time.time())})

async def handle_me(request):
    login = get_login(await read_json(request))
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    user = USERS[login]
    return web.json_response({
        "state": state_of(user),
        "perks": user["perks"],
        "profile": {"id": login[3:], "created": user["created"],
                    "promos": len(user["promos"]), "vip": is_vip(user)},
    })

async def handle_drop(request):
    login = get_login(await read_json(request))
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    user = USERS[login]
    now = time.time() * 1000
    if now < user["next_box"]:
        return web.json_response({"error": "Ящик ещё не готов"}, status=400)
    value, chosen, acc = random.uniform(0, 100), ITEM_DATABASE[0]["id"], 0
    for it in ITEM_DATABASE:
        acc += it["chance"]
        if value <= acc:
            chosen = it["id"]
            break
    user["inv"][chosen] = user["inv"].get(chosen, 0) + 1
    user["next_box"] = now + (VIP_BOX_COOLDOWN_MS if is_vip(user) else BOX_COOLDOWN_MS)
    save_db(USERS)
    return web.json_response({"item": chosen, "state": state_of(user)})

async def handle_use(request):
    data = await read_json(request)
    login = get_login(data)
    item = data.get("item")
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    user = USERS[login]
    if user["inv"].get(item, 0) <= 0:
        return web.json_response({"error": "Нет предмета"}, status=400)
    user["inv"][item] -= 1
    if user["inv"][item] <= 0:
        del user["inv"][item]
    save_db(USERS)
    return web.json_response({"state": state_of(user)})

# =========================
# ПРОМОКОДЫ (TXT на GitHub)
# =========================
_promo_cache = {"t": 0.0, "data": {}}
_attempts = {}

def raw_url(url):
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url

def parse_promos(text):
    out = {}
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        code, val = line.split("=", 1)
        m = re.match(r"([\d_,]+(?:\.\d+)?)\s*(.*)", val.strip())
        if not m or not code.strip():
            continue
        num = float(m.group(1).replace("_", "").replace(",", ""))
        unit = m.group(2).lower().strip()
        if unit.startswith(("$", "usd", "dollar", "дол")):
            out[code.strip().upper()] = {"usd": round(num, 2)}
        else:
            out[code.strip().upper()] = {"tokens": int(num)}
    return out

async def load_promos(session):
    if time.time() - _promo_cache["t"] < 60 and _promo_cache["data"]:
        return _promo_cache["data"]
    url = raw_url(PROMO_URL.strip())
    if not url.startswith("http"):
        return None
    try:
        async with session.get(url, timeout=ClientTimeout(total=10)) as r:
            if r.status == 200:
                _promo_cache["data"] = parse_promos(await r.text())
                _promo_cache["t"] = time.time()
    except Exception as e:
        print("PROMO FETCH ERROR:", repr(e))
    return _promo_cache["data"] or None

async def handle_promo(request):
    data = await read_json(request)
    login = get_login(data)
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    user = USERS[login]
    now = time.time()
    tries = [t for t in _attempts.get(login, []) if now - t < 60]
    if len(tries) >= 10:
        return web.json_response({"error": "Слишком много попыток, подожди минуту"}, status=429)
    _attempts[login] = tries + [now]

    code = str(data.get("code", "")).strip().upper()[:40]
    promos = await load_promos(request.app["s"])
    if promos is None:
        return web.json_response({"error": "Промокоды сейчас недоступны"}, status=503)
    reward = promos.get(code)
    if not code or not reward:
        return web.json_response({"error": "Неверный промокод"}, status=400)
    if code in user["promos"]:
        return web.json_response({"error": "Ты уже использовал этот промокод"}, status=400)
    user["promos"].append(code)
    save_db(USERS)
    return web.json_response({**reward, "promos": len(user["promos"])})

# =========================
# ДОНАТЫ (Telegram Stars)
# =========================
async def handle_invoice(request):
    data = await read_json(request)
    item = data.get("item")
    if item not in ITEMS:
        return web.json_response({"error": "Неизвестный товар"}, status=400)
    name, desc, price = ITEMS[item]
    r = await call(request.app["s"], "createInvoiceLink", title=name, description=desc,
                    payload=item, provider_token="", currency="XTR",
                    prices=[{"label": name, "amount": price}])
    if not r.get("ok"):
        return web.json_response({"error": "Не удалось создать счёт"}, status=500)
    return web.json_response({"link": r["result"]})

def apply_perk(perks, item):
    now = time.time() * 1000
    if item in ("boost2", "boost5"):
        mult = 2 if item == "boost2" else 5
        active = perks.get("boost_until", 0) > now
        perks["boost_mult"] = max(perks.get("boost_mult", 0), mult) if active else mult
        perks["boost_until"] = (perks["boost_until"] if active else now) + HOUR_MS
    elif item == "auto2h":
        perks["auto_until"] = max(perks.get("auto_until", 0), now) + 2 * HOUR_MS
    else:  # perm2, offline24, vip
        perks[item] = 1

async def grant_purchase(session, message):
    sp = message["successful_payment"]
    item = sp.get("invoice_payload")
    uid = message.get("from", {}).get("id")
    charge = sp.get("telegram_payment_charge_id")
    if item not in ITEMS or uid is None:
        return
    user = norm(USERS.setdefault(f"tg_{uid}", new_user()))
    if any(p.get("charge") == charge for p in user["purchases"]):
        return
    user["purchases"].append({"item": item, "charge": charge, "ts": time.time()})
    apply_perk(user["perks"], item)
    save_db(USERS)
    await call(session, "sendMessage", chat_id=message["chat"]["id"],
                text=f"🎁 Покупка получена: {ITEMS[item][0]}. Спасибо!")

async def handle_webhook(request):
    try:
        data = await request.json()
        session = request.app["s"]
        message = data.get("message", {})
        if message.get("successful_payment"):
            await grant_purchase(session, message)
        elif "pre_checkout_query" in data:
            pcq = data["pre_checkout_query"]
            ok = pcq.get("invoice_payload") in ITEMS
            extra = {} if ok else {"error_message": "Неизвестный товар"}
            await call(session, "answerPreCheckoutQuery",
                       pre_checkout_query_id=pcq["id"], ok=ok, **extra)
        elif message.get("text", "").startswith("/start"):
            uid = message.get("from", {}).get("id")
            if uid:
                login = f"tg_{uid}"
                if login not in USERS:
                    USERS[login] = new_user()
                    save_db(USERS)
                norm(USERS[login])
            await call(session, "sendMessage", chat_id=message["chat"]["id"],
                       text="🌱 Добро пожаловать в Нейроферму!\n\nЗапускай игру 👇",
                       reply_markup={"inline_keyboard": [[
                           {"text": "🎮 Играть", "web_app": {"url": WEBAPP_URL}}]]})
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))
    return web.Response(text="OK")

# =========================
# ЗАПУСК
# =========================
async def keep_alive(app):
    await asyncio.sleep(10)
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(f"{RENDER_URL}/health"):
                    pass
            except Exception:
                pass
            await asyncio.sleep(240)

async def on_start(app):
    app["s"] = ClientSession()
    if TOKEN:
        await call(app["s"], "setWebhook", url=f"{RENDER_URL}/webhook",
                    allowed_updates=["message", "pre_checkout_query"])
    app["keep_alive_task"] = asyncio.create_task(keep_alive(app))

async def on_cleanup(app):
    if "keep_alive_task" in app:
        app["keep_alive_task"].cancel()
    if "s" in app:
        await app["s"].close()

app = web.Application(middlewares=[cors])
app.router.add_get("/", handle_index)
app.router.add_get("/health", handle_health)
app.router.add_post("/webhook", handle_webhook)
for path, h in [("/api/me", handle_me), ("/api/drop", handle_drop), ("/api/use", handle_use),
                ("/api/promo", handle_promo), ("/api/invoice", handle_invoice)]:
    app.router.add_route("*", path, h)
app.on_startup.append(on_start)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
