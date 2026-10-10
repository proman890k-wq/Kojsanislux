import os
import re
import math
import json
import time
import random
import hmac
import hashlib
import secrets
import asyncio
import unicodedata
from urllib.parse import parse_qsl
from aiohttp import web, ClientSession, ClientTimeout

try:
    import asyncpg
except ImportError:
    asyncpg = None

# =========================
# НАСТРОЙКИ (секреты — через переменные окружения Render)
# =========================
TOKEN = os.environ.get("BOT_TOKEN", "")  # ОБЯЗАТЕЛЬНО через переменную окружения
PROMO_URL = os.environ.get("PROMO_URL", "https://raw.githubusercontent.com/proman890k-wq/Kojsanislux/refs/heads/main/promo.txt")
WEBAPP_URL = "https://proman890k-wq.github.io/Kojsanislux/"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://game-2gla.onrender.com")
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # Postgres: Neon / Supabase / Render
WEBHOOK_SECRET = hashlib.sha256(("wh:" + TOKEN).encode()).hexdigest()[:48] if TOKEN else ""
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
USERS = {}
COMPANIES = {}   # id -> объект компании (тот же объект лежит в USERS["co_<id>"])
BOT_NAME = ""
_dirty = set()
_deleted = set()
_pool = None

def touch(login):
    """Пометить пользователя изменённым — фоновая задача запишет его в БД."""
    _dirty.add(login)

async def flush():
    global _dirty, _deleted
    if not _dirty and not _deleted:
        return
    batch, _dirty = _dirty, set()
    gone, _deleted = _deleted, set()
    try:
        if _pool:
            rows = [(l, json.dumps(USERS[l], ensure_ascii=False)) for l in batch if l in USERS]
            async with _pool.acquire() as c:
                if gone:
                    await c.execute("DELETE FROM users WHERE login = ANY($1::text[])", list(gone))
                if rows:
                    await c.executemany(
                        "INSERT INTO users (login, data) VALUES ($1, $2::jsonb) "
                        "ON CONFLICT (login) DO UPDATE SET data = EXCLUDED.data", rows)
        else:
            tmp = DB_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(USERS, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DB_FILE)
    except Exception as e:
        print("DB WRITE ERROR:", repr(e))
        _dirty |= batch
        _deleted |= gone

async def writer():
    while True:
        await asyncio.sleep(1)
        await flush()

def read_file_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

async def db_init():
    global _pool
    if DATABASE_URL:
        if asyncpg is None:
            raise RuntimeError("Добавь asyncpg в requirements.txt")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3,
                                          statement_cache_size=0)
        async with _pool.acquire() as c:
            await c.execute("CREATE TABLE IF NOT EXISTS users "
                            "(login TEXT PRIMARY KEY, data JSONB NOT NULL)")
            rows = await c.fetch("SELECT login, data FROM users")
        for r in rows:
            USERS[r["login"]] = json.loads(r["data"])
        if not rows:  # разовый перенос из старого database.json
            USERS.update(read_file_db())
            _dirty.update(USERS)
        print(f"DB: Postgres, пользователей: {len(USERS)}")
    else:
        USERS.update(read_file_db())
        print("WARNING: DATABASE_URL не задан — данные в файле и пропадут при перезапуске на Render")
    for l, u in USERS.items():  # компании лежат в той же таблице
        if l.startswith("co_") and isinstance(u, dict) and "members" in u and "cores" in u:
            COMPANIES[u["id"]] = u
    migrate_coins()
    print(f"Компаний: {len(COMPANIES)}")

def new_user():
    return {"inv": {"cooler": 1}, "next_box": 0, "perks": {}, "purchases": [],
            "promos": [], "created": time.time(),
            "stats": {"tot": 0, "usd": 0}, "name": "", "avatar": "",
            "daily": {"last": "", "streak": 0}, "bank": {"amt": 0.0, "t": 0},
            "loan": {"debt": 0.0, "principal": 0.0, "t": 0, "since": 0, "last_remind": 0, "remind_n": 0},
            "co": {"id": None, "hold": {}, "cost": {}, "last": 0, "pending": 0.0},
            "ref": {"by": None, "paid": True, "count": 0, "earned": 0.0, "names": []}}

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

ITEMS["company_license"] = ("Лицензия на компанию", "Открыть свою компанию без $1 000 000", 250)

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
    name = None
    start = ""
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
                ud = json.loads(pairs.get("user", "{}"))
                uid = ud.get("id")
                start = pairs.get("start_param", "")
                name = ("@" + ud["username"]) if ud.get("username") else ud.get("first_name")
        except Exception:
            pass
    if uid is None and DEV_MODE and data.get("user_id") is not None:
        uid = data["user_id"]
    if uid is None:
        return None
    login = f"tg_{uid}"
    if login not in USERS:
        USERS[login] = new_user()
        inv = ref_login(start)
        if inv and inv != login and inv in USERS:
            USERS[login]["ref"].update(by=inv, paid=False)
        touch(login)
    norm(USERS[login])
    if name and USERS[login].get("name") != name:
        USERS[login]["name"] = name
        touch(login)
    if USERS[login]["ref"].get("by") and not USERS[login]["ref"].get("paid"):
        pay_referral(login)
    return login

def promo_list(user):
    """Все промокоды пользователя: код, время, награда (старые записи — только код)."""
    return [p if isinstance(p, dict) else {"code": p} for p in user["promos"]]

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
        "game": user.get("game"),
        "perks": user["perks"],
        "profile": {"id": login[3:], "created": user["created"],
                    "promos": len(user["promos"]), "promo_list": promo_list(user),
                    "vip": is_vip(user), "avatar": user.get("avatar", "")},
        "daily": daily_info(user),
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
    touch(login)
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
    touch(login)
    return web.json_response({"state": state_of(user)})

# =========================
# ПРОМОКОДЫ (TXT на GitHub)
# Формат строк:  КОД = 500 $      КОД = 5000000 токенов      # комментарий
# Без единицы число считается токенами.
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
    if any(p["code"] == code for p in promo_list(user)):  # проверка и отметка идут без await — двойное использование невозможно
        return web.json_response({"error": "Ты уже использовал этот промокод"}, status=400)
    user["promos"].append({"code": code, "ts": time.time(), **reward})
    touch(login)
    return web.json_response({**reward, "code": code, "promos": len(user["promos"])})

# =========================
# СТАТИСТИКА И ЛИДЕРБОРД
# (прогресс хранится на устройстве, поэтому значения приходят от клиента)
# =========================
def store_game(login, g):
    """Сохранить прогресс игрока. Возвращает "ok", "stale" или "Bad data"."""
    if not isinstance(g, dict) or len(json.dumps(g)) > 50_000:
        return "Bad data"
    try:
        tot, usd, last, xp = (float(g.get(k, 0)) for k in ("tot", "usd", "last", "xp"))
    except (TypeError, ValueError):
        return "Bad data"
    if not all(math.isfinite(x) for x in (tot, usd, last, xp)) or tot < 0 or usd < 0 or xp < 0:
        return "Bad data"
    user = USERS[login]
    if last < float((user.get("game") or {}).get("last", 0)):
        return "stale"  # более старая копия не затирает новую
    user["game"] = g
    user["stats"] = {"tot": max(user["stats"].get("tot", 0), tot), "usd": round(usd, 2),
                     "xp": max(user["stats"].get("xp", 0), xp)}
    touch(login)
    return "ok"

async def handle_save(request):
    """Облачное сохранение: весь прогресс игрока лежит на сервере."""
    data = await read_json(request)
    login = get_login(data)
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    res = store_game(login, data.get("state"))
    if res == "Bad data":
        return web.json_response({"error": "Bad data"}, status=400)
    return web.json_response({"status": res})

def board(key, login, n=20):
    rows = [(l, u) for l, u in USERS.items()
            if not l.startswith("co_") and isinstance(u.get("stats"), dict) and u["stats"].get(key, 0) > 0]
    rows.sort(key=lambda r: r[1]["stats"][key], reverse=True)
    top = [{"name": u.get("name") or "Игрок", "a": u.get("avatar", ""), "v": u["stats"][key], "me": l == login}
           for l, u in rows[:n]]
    rank = next((i + 1 for i, (l, _) in enumerate(rows) if l == login), 0)
    return top, rank

async def handle_top(request):
    login = get_login(await read_json(request))
    if not login:
        return web.json_response({"error": "Unauthorized"}, status=401)
    tokens, rt = board("tot", login)
    usd, ru = board("usd", login)
    xp, rx = board("xp", login)
    return web.json_response({"tokens": tokens, "usd": usd, "xp": xp, "me_tokens": rt, "me_usd": ru, "me_xp": rx})

# =========================
# ДОНАТЫ (Telegram Stars)
# =========================
async def handle_invoice(request):
    data = await read_json(request)
    if not get_login(data):
        return web.json_response({"error": "Unauthorized"}, status=401)
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
    elif item == "company_license":
        perks["company_license"] = perks.get("company_license", 0) + 1
    else:  # perm2, offline24, vip
        perks[item] = 1

async def grant_purchase(session, message):
    sp = message["successful_payment"]
    item = sp.get("invoice_payload")
    uid = message.get("from", {}).get("id")
    charge = sp.get("telegram_payment_charge_id")
    if item not in ITEMS or uid is None:
        return
    login = f"tg_{uid}"
    user = norm(USERS.setdefault(login, new_user()))
    if any(p.get("charge") == charge for p in user["purchases"]):
        return
    user["purchases"].append({"item": item, "charge": charge, "ts": time.time()})
    apply_perk(user["perks"], item)
    touch(login)
    await flush()  # платёж фиксируем в БД сразу
    await call(session, "sendMessage", chat_id=message["chat"]["id"],
               text=f"🎁 Покупка получена: {ITEMS[item][0]}. Спасибо!")

async def handle_webhook(request):
    if WEBHOOK_SECRET and not hmac.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET):
        return web.Response(status=403, text="forbidden")  # иначе кто угодно мог бы подделать оплату
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
            parts = message["text"].split(maxsplit=1)
            param = parts[1].strip() if len(parts) > 1 else ""
            url, text = WEBAPP_URL, "🌱 Добро пожаловать в Нейроферму!\n\nЗапускай игру 👇"
            if param.startswith("co_"):
                code = re.sub(r"[^A-Za-z0-9]", "", param[3:])[:12].upper()
                c = next((x for x in COMPANIES.values() if x["code"] == code), None)
                if c:
                    url = f"{WEBAPP_URL}?join={code}"
                    text = f"🏢 Тебя пригласили в компанию «{c['name']}» ({c['ticker']})!\n\nЖми и вступай 👇"
            elif param.startswith("ref_"):
                inv, me_id = ref_login(param), message.get("from", {}).get("id")
                login = f"tg_{me_id}"
                if inv and me_id and inv != login and inv in USERS and login not in USERS:
                    USERS[login] = new_user()
                    USERS[login]["ref"].update(by=inv, paid=False)
                    touch(login)
                    text = "🎁 Тебя пригласил друг!\n\nЗапускай игру 👇"
            await call(session, "sendMessage", chat_id=message["chat"]["id"], text=text,
                       reply_markup={"inline_keyboard": [[{"text": "🎮 Играть", "web_app": {"url": url}}]]})
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))
    return web.Response(text="OK")

# =========================
# КОМПАНИИ, СЕРВЕРЫ, УЛУЧШЕНИЯ, БАНК, ЕЖЕДНЕВКА
# Компания лежит в той же таблице, что и игроки (ключ "co_<id>").
# Цена монеты — показатель компании (не торгуется): чем она выше, тем сильнее аппетит сервера. Всё покупается за доллары.
# =========================
ICONS = ["🏢", "🚀", "🧠", "🐉", "🦄", "🔥", "🌐", "⚡", "🛰️", "🐸", "💎", "🧬"]
CREATE_USD = 1_000_000      # цена открытия компании
CORE_SLOTS = 3              # максимум ядер на сервере
TICK_SEC = 10               # шаг случайных событий
NO_SAVE = "Нет сохранения — подожди пару секунд и повтори"

# ключ: (название, описание, базовая цена $, максимум уровней). Покупает владелец из казны.
UPGRADES = {
    "eff": ("Охлаждение", "−8% аппетита сервера за уровень", 2000, 5),
    "boost": ("Оверклок", "+5% к баффу дохода за уровень", 5000, 10),
    "slots": ("Офис", "+3 места в компании за уровень", 3000, 8),
}

EVENTS = [  # (вид, текст)
    ("heat", "🔥 Сервер «{n}» перегрелся — аппетит ×1.8 на 5 минут"),
    ("grant", "🏛 Грант для «{n}»: +${v} в казну"),
    ("surge", "🚀 Инвесторы заметили «{n}» — бафф дохода +20% на 5 минут"),
]

def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)

def clean(text, n):
    text = re.sub(r"[\x00-\x1f<>]", "", str(text))
    return re.sub(r"\s+", " ", text).strip()[:n]

def valid_emoji(s):
    """Любой эмодзи (в т.ч. составные: флаги, семьи, с оттенками кожи). Буквы/цифры/теги запрещены."""
    s = str(s or "").strip()
    if not s or len(s) > 16:
        return False
    ok_cats = {"So", "Sk", "Sm", "Cf", "Mn", "Me"}
    if any(unicodedata.category(ch) not in ok_cats for ch in s):
        return False
    return any(unicodedata.category(ch) in ("So", "Sk") for ch in s)

def mid(login):
    return hashlib.sha1(login.encode()).hexdigest()[:8]

def uname(login):
    return (USERS.get(login) or {}).get("name") or "Игрок"

def uava(login):
    return (USERS.get(login) or {}).get("avatar") or ""

def level_for_xp(xp):
    return int(math.sqrt(max(0.0, xp) / 50)) + 1

def level_of(user):
    try:
        return level_for_xp(float((user.get("game") or {}).get("xp", 0)))
    except (TypeError, ValueError):
        return 1

def usd_of(user):
    return float(user["game"].get("usd", 0))

def add_usd(user, d, dxp=0.0):
    """Изменить доллары/опыт в серверной копии сохранения (клиент применяет ту же дельту)."""
    g = user["game"]
    g["usd"] = round(max(0.0, float(g.get("usd", 0)) + d), 2)
    g["xp"] = float(g.get("xp", 0)) + dxp
    g["last"] = float(g.get("last", 0)) + 1  # чтобы старая копия клиента не затёрла изменение
    st = user.setdefault("stats", {})
    st["usd"] = g["usd"]
    st["xp"] = max(st.get("xp", 0), g["xp"])

def co_of_user(user):
    cid = user["co"].get("id")
    c = COMPANIES.get(cid) if cid else None
    if cid and not c:
        user["co"]["id"] = None
    return c

def upg(c, key):
    try:
        return int((c.get("up") or {}).get(key, 0))
    except (TypeError, ValueError):
        return 0

def up_cost(key, lvl):
    return int(UPGRADES[key][2] * 1.8 ** lvl)

def co_level(c):
    return 1 + int(math.sqrt(max(0.0, c.get("cxp", 0)) / 400))

def co_max(c):
    return min(60, 10 + 2 * (co_level(c) - 1) + 3 * upg(c, "slots"))

def core_n(c):
    return sum(1 for x in c["cores"] if x)

def co_buff(c, now=None):
    now = now or time.time()
    b = 0.10 * co_level(c) + 0.10 * core_n(c) + 0.05 * upg(c, "boost")
    if c.get("ad_until", 0) > now:
        b += 0.15
    if c.get("surge_until", 0) > now:
        b += 0.20
    return 1 + min(2.0, b)

def co_log(c, text):
    c["feed"].insert(0, {"ts": int(time.time()), "t": text})
    del c["feed"][30:]

def appetite(c, now=None):
    """Доля токенов игрока, которую сервер съедает за минуту."""
    now = now or time.time()
    n = len(c["members"])
    # чем дороже монета компании, тем больше токенов ест сервер (но не наоборот)
    price = float(c["px"]["price"])
    r = 0.002 * (1 + 0.12 * (n - 1)) * min(4.0, max(0.7, price ** 0.3))
    if c.get("heat_until", 0) > now:
        r *= 1.8
    r *= max(0.25, 1 - 0.2 * core_n(c))
    r *= max(0.3, 1 - 0.08 * upg(c, "eff"))
    return min(r, 0.05)

def fair_price(c, now):
    """«Справедливая» цена: растёт с участниками, скормленными токенами, ядрами, уровнем и улучшениями."""
    n = len(c["members"])
    f = (1 + 0.2 * (n - 1)) * (1 + 0.8 * math.log10(1 + c.get("fed", 0) / 2e5)) \
        * (1 + 0.22 * core_n(c)) * (1 + 0.01 * co_level(c)) * (1 + 0.04 * upg(c, "boost"))
    if c.get("ad_until", 0) > now:
        f *= 1.3
    if c.get("surge_until", 0) > now:
        f *= 1.15
    return f

def tick_price(c, now):
    """Как на реальном рынке: случайное блуждание + импульс + медленный возврат к справедливой цене."""
    px = c["px"]
    p = px["price"]
    mom = px.get("mom", 0.0) * 0.9 + random.gauss(0, 0.0035)
    px["mom"] = mom
    p *= math.exp(0.004 * math.log(fair_price(c, now) / p) + mom + random.gauss(0, 0.012))
    px["price"] = round(min(1e9, max(0.01, p)), 6)
    px["h"].append(round(px["price"], 4))
    del px["h"][:-180]
    px["n"] = px.get("n", 0) + 1
    if px["n"] % 30 == 0:
        px["d"].append(round(px["price"], 4))
        del px["d"][:-288]

async def market_loop():
    """Цена монет и случайные события компаний (перегрев, гранты, всплески)."""
    while True:
        await asyncio.sleep(TICK_SEC)
        try:
            now = time.time()
            for c in list(COMPANIES.values()):
                steps = max(1, min(60, int((now - c.get("tick", now)) // TICK_SEC)))
                c["tick"] = now
                for _ in range(steps):
                    tick_price(c, now)
                if random.random() < 0.006 * steps:
                    kind, text = random.choice(EVENTS)
                    v = 0
                    if kind == "heat":
                        c["heat_until"] = now + 300
                        c["px"]["price"] = round(max(0.01, c["px"]["price"] * 0.94), 6)
                    elif kind == "surge":
                        c["surge_until"] = now + 300
                        c["px"]["price"] = round(c["px"]["price"] * 1.08, 6)
                    else:
                        v = 50 * co_level(c) * max(1, len(c["members"]))
                        c["treasury"] = round(c["treasury"] + v, 2)
                        c["px"]["price"] = round(c["px"]["price"] * 1.03, 6)
                    co_log(c, text.format(n=c["name"], v=f"{v:,}"))
                touch("co_" + c["id"])
        except Exception as e:
            print("MARKET ERROR:", repr(e))

def migrate_coins():
    """Разовый перенос: монеты на руках превращаются в доллары (к выплате); у компаний цена хранится в c["px"]."""
    prices = {cid: float((c.get("coin") or c.get("px") or {}).get("price", 1.0)) for cid, c in COMPANIES.items()}
    for l, u in USERS.items():
        if l.startswith("co_") or not isinstance(u.get("co"), dict):
            continue
        hold = u["co"].get("hold") or {}
        if hold:
            total = sum(min(250_000.0, float(h) * prices.get(cid, 0.0)) for cid, h in hold.items())
            if total > 0:
                u["co"]["pending"] = round(u["co"].get("pending", 0) + total, 2)
            u["co"]["hold"], u["co"]["cost"] = {}, {}
            touch(l)
    for c in COMPANIES.values():
        if "coin" in c:
            c["px"] = c.pop("coin")
        px = c.setdefault("px", {"price": 1.0, "h": [1.0], "d": [1.0], "mom": 0.0, "n": 0})
        px.setdefault("h", [px["price"]]); px.setdefault("d", [px["price"]])
        c.setdefault("up", {"eff": 0, "boost": 0, "slots": 0})
        c.setdefault("surge_until", 0)
        touch("co_" + c["id"])

def co_row(c):
    base = (c["px"]["d"] or c["px"]["h"] or [c["px"]["price"]])[0]
    return {"id": c["id"], "name": c["name"], "ticker": c["ticker"], "icon": c["icon"],
            "members": len(c["members"]), "max": co_max(c), "lvl": co_level(c),
            "min_level": c["min_level"], "private": c["private"], "motto": c.get("motto", ""),
            "price": c["px"]["price"], "chg": round((c["px"]["price"] / base - 1) * 100, 2) if base else 0,
            "score": int(c.get("cxp", 0)), "owner": uname(c["owner"]), "cores": core_n(c),
            "buff": round(co_buff(c), 2)}

def co_view(c, login):
    now = time.time()
    v = co_row(c)
    mem = [{"id": mid(l), "name": uname(l), "a": uava(l), "lvl": level_of(USERS[l]) if l in USERS else 1,
            "fed": m.get("fed", 0), "don": m.get("don", 0), "owner": l == c["owner"], "me": l == login}
           for l, m in c["members"].items()]
    mem.sort(key=lambda m: (not m["owner"], -m["fed"]))
    cl = co_level(c)
    ups = [{"k": k, "n": u[0], "d": u[1], "lvl": upg(c, k), "max": u[3],
            "cost": up_cost(k, upg(c, k)) if upg(c, k) < u[3] else 0} for k, u in UPGRADES.items()]
    v.update({
        "is_owner": login == c["owner"], "code": c["code"], "treasury": round(c["treasury"], 2),
        "cxp": int(c["cxp"]), "cxp_cur": 400 * (cl - 1) ** 2, "cxp_next": 400 * cl * cl,
        "fed": c["fed"], "appetite": round(appetite(c, now) * 100, 3),
        "heat": max(0, int(c.get("heat_until", 0) - now)), "ad": max(0, int(c.get("ad_until", 0) - now)),
        "surge": max(0, int(c.get("surge_until", 0) - now)), "h": c["px"]["h"], "d": c["px"]["d"],
        "ad_cost": 200 * cl, "mem": mem, "ups": ups,
        "core_slots": [({"owner": uname(x["owner"]), "mine": x["owner"] == login} if x else None) for x in c["cores"]],
        "feed": c["feed"][:15]})
    return v

def ok(user, login, d_usd=0.0, d_xp=0.0, **extra):
    c = co_of_user(user)
    return web.json_response({"ok": True, "d_usd": round(d_usd, 2), "d_xp": round(d_xp, 2),
                              "inv": user["inv"], "co": co_view(c, login) if c else None, **extra})

async def co_ctx(request):
    """Читает запрос, при наличии state сохраняет прогресс (нужно для проверки баланса)."""
    data = await read_json(request)
    login = get_login(data)
    if not login:
        return data, None, None
    g = data.get("state")
    if isinstance(g, dict):
        store_game(login, g)
    return data, login, USERS[login]

def new_code():
    while True:
        code = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if not any(c["code"] == code for c in COMPANIES.values()):
            return code

def give_core(login):
    u = USERS.get(login)
    if u:
        u["inv"]["core"] = u["inv"].get("core", 0) + 1
        touch(login)

def remove_member(c, login, text):
    for i, x in enumerate(c["cores"]):
        if x and x["owner"] == login:
            give_core(login)
            c["cores"][i] = None
    c["members"].pop(login, None)
    u = USERS.get(login)
    if u:
        u["co"]["id"] = None
        touch(login)
    co_log(c, text)
    if c["owner"] == login and c["members"]:
        c["owner"] = min(c["members"], key=lambda l: c["members"][l]["joined"])
        co_log(c, f"👑 Новый владелец: {uname(c['owner'])}")
    touch("co_" + c["id"])

def disband(c):
    """Закрыть компанию: ядра возвращаются, 50% казны делится между участниками."""
    for x in c["cores"]:
        if x:
            give_core(x["owner"])
    members = list(c["members"])
    share = round(c["treasury"] * 0.5 / len(members), 2) if members else 0.0
    for l in members:
        u = USERS.get(l)
        if u and isinstance(u.get("co"), dict):
            u["co"]["id"] = None
            if share > 0:
                u["co"]["pending"] = round(u["co"].get("pending", 0) + share, 2)
            touch(l)
    key = "co_" + c["id"]
    USERS.pop(key, None)
    COMPANIES.pop(c["id"], None)
    _dirty.discard(key)
    _deleted.add(key)

# ---------- запросы ----------
async def handle_co_sync(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    co, now = user["co"], time.time()
    c = co_of_user(user)
    if c and login not in c["members"]:  # рассинхрон — отвязываем игрока
        co["id"] = None
        c = None
    drain = 0.0
    if c:
        try:
            t = float(data.get("t", 0))
        except (TypeError, ValueError):
            t = 0.0
        if not math.isfinite(t) or t < 0:
            t = 0.0
        dt = min(900.0, max(0.0, now - (co.get("last") or now)))
        drain = t * (1 - math.exp(-appetite(c, now) / 60 * dt))
        if drain > 0:
            c["fed"] += drain
            c["members"][login]["fed"] = c["members"][login].get("fed", 0) + drain
            touch("co_" + c["id"])
    co["last"] = now
    pend = round(co.get("pending", 0), 2)
    if pend > 0:
        co["pending"] = 0
    touch(login)
    return web.json_response({"drain": drain, "pending": pend, "lic": user["perks"].get("company_license", 0),
                              "co": co_view(c, login) if c else None, "bot": BOT_NAME,
                              "lvl": level_of(user)})

async def handle_co_list(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    mine = user["co"].get("id")
    rows = []
    for c in COMPANIES.values():
        if c["private"] and c["id"] != mine:
            continue
        r = co_row(c)
        r["mine"] = c["id"] == mine
        rows.append(r)
    rows.sort(key=lambda r: -r["score"])
    return web.json_response({"rows": rows[:60], "lvl": level_of(user)})

async def handle_co_find(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    code = str(data.get("code", "")).strip().upper()[:12]
    c = next((x for x in COMPANIES.values() if x["code"] == code), None)
    if not c:
        return err("Код не найден")
    return web.json_response({"row": co_row(c)})

async def handle_co_create(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    if co_of_user(user):
        return err("Ты уже в компании")
    name = clean(data.get("name", ""), 20)
    ticker = re.sub(r"[^A-Za-z]", "", str(data.get("ticker", ""))).upper()[:5]
    if len(name) < 3:
        return err("Название — от 3 символов")
    if len(ticker) < 3:
        return err("Тикер — от 3 букв (A–Z)")
    if any(c["ticker"] == ticker or c["name"].lower() == name.lower() for c in COMPANIES.values()):
        return err("Такое название или тикер уже заняты")
    raw_icon = str(data.get("icon") or "").strip()
    if raw_icon and not valid_emoji(raw_icon):
        return err("Иконка должна быть эмодзи")
    icon = raw_icon or ICONS[0]
    try:
        min_level = max(1, min(999, int(data.get("min_level", 1))))
    except (TypeError, ValueError):
        min_level = 1
    if data.get("pay") == "stars":
        if user["perks"].get("company_license", 0) < 1:
            return err("Нет лицензии — купи её за 250 ⭐")
        user["perks"]["company_license"] -= 1
        d_usd = 0.0
    else:
        if not isinstance(user.get("game"), dict):
            return err(NO_SAVE)
        if usd_of(user) < CREATE_USD:
            return err("Нужно $1 000 000")
        add_usd(user, -CREATE_USD, 500)
        d_usd = -float(CREATE_USD)
    cid, now = secrets.token_hex(3), time.time()
    while cid in COMPANIES:
        cid = secrets.token_hex(3)
    c = {"id": cid, "name": name, "ticker": ticker, "icon": icon, "owner": login, "created": now,
         "private": bool(data.get("private")), "min_level": min_level, "code": new_code(), "motto": "",
         "members": {login: {"joined": now, "fed": 0.0, "don": 0.0}}, "cores": [None] * CORE_SLOTS,
         "treasury": 0.0, "cxp": 0.0, "fed": 0.0, "heat_until": 0, "ad_until": 0, "surge_until": 0,
         "up": {"eff": 0, "boost": 0, "slots": 0}, "feed": [], "tick": now,
         "px": {"price": 1.0, "h": [1.0], "d": [1.0], "mom": 0.0, "n": 0}}
    USERS["co_" + cid] = c
    COMPANIES[cid] = c
    user["co"].update(id=cid, last=now)
    co_log(c, f"🎉 Компания «{name}» ({ticker}) основана")
    touch(login)
    touch("co_" + cid)
    return ok(user, login, d_usd=d_usd, d_xp=500 if d_usd else 200)

async def handle_co_join(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    if co_of_user(user):
        return err("Ты уже в компании. Сначала выйди из неё")
    code = str(data.get("code", "")).strip().upper()[:12]
    if code:
        c = next((x for x in COMPANIES.values() if x["code"] == code), None)
    else:
        c = COMPANIES.get(data.get("id"))
        if c and c["private"]:
            return err("Компания закрытая — нужен код приглашения")
    if not c:
        return err("Компания не найдена")
    lvl = level_of(user)
    if lvl < c["min_level"]:
        return err(f"Нужен уровень {c['min_level']} (у тебя {lvl})")
    if len(c["members"]) >= co_max(c):
        return err("В компании нет мест")
    c["members"][login] = {"joined": time.time(), "fed": 0.0, "don": 0.0}
    c["cxp"] += 30
    user["co"].update(id=c["id"], last=time.time())
    co_log(c, f"👋 {uname(login)} вступил в компанию")
    touch(login)
    touch("co_" + c["id"])
    return ok(user, login, d_xp=0)

async def handle_co_leave(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c:
        return err("Ты не в компании")
    if len(c["members"]) <= 1:
        disband(c)
    else:
        remove_member(c, login, f"🚪 {uname(login)} покинул компанию")
    return ok(user, login)

async def handle_co_disband(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец может распустить компанию")
    disband(c)
    return ok(user, login)

async def handle_co_settings(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец меняет настройки")
    if "private" in data:
        c["private"] = bool(data["private"])
    if "min_level" in data:
        try:
            c["min_level"] = max(1, min(999, int(data["min_level"])))
        except (TypeError, ValueError):
            pass
    if "motto" in data:
        c["motto"] = clean(data["motto"], 60)
    if "icon" in data:
        if not valid_emoji(data["icon"]):
            return err("Иконка должна быть эмодзи")
        c["icon"] = str(data["icon"]).strip()
    if data.get("regen"):
        c["code"] = new_code()
    touch("co_" + c["id"])
    return ok(user, login)

async def handle_co_kick(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец может исключать")
    target = next((l for l in c["members"] if mid(l) == data.get("uid")), None)
    if not target or target == login:
        return err("Участник не найден")
    remove_member(c, target, f"✖ {uname(target)} исключён из компании")
    return ok(user, login)

async def handle_co_core(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c:
        return err("Ты не в компании")
    if data.get("act") == "put":
        if user["inv"].get("core", 0) < 1:
            return err("Нужно Ядро сингулярности — оно выпадает из ящиков")
        free = next((i for i, x in enumerate(c["cores"]) if x is None), None)
        if free is None:
            return err("Все 3 слота сервера заняты")
        user["inv"]["core"] -= 1
        if user["inv"]["core"] <= 0:
            del user["inv"]["core"]
        c["cores"][free] = {"owner": login}
        c["cxp"] += 100
        co_log(c, f"🌀 {uname(login)} подключил ядро к серверу")
    else:
        try:
            i = int(data.get("slot"))
            x = c["cores"][i]
        except (TypeError, ValueError, IndexError):
            return err("Слот не найден")
        if not x or (x["owner"] != login and c["owner"] != login):
            return err("Это не твоё ядро")
        c["cores"][i] = None
        give_core(x["owner"])
        co_log(c, f"⏏ Ядро игрока {uname(x['owner'])} отключено")
    touch(login)
    touch("co_" + c["id"])
    return ok(user, login)

async def handle_co_donate(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c:
        return err("Ты не в компании")
    if not isinstance(user.get("game"), dict):
        return err(NO_SAVE)
    try:
        x = math.floor(float(data.get("usd", 0)) * 100 + 1e-6) / 100
    except (TypeError, ValueError, OverflowError):
        x = 0
    if not (x >= 1) or not math.isfinite(x):
        return err("Минимальный вклад — $1")
    if x > usd_of(user) + 0.001:
        return err("Не хватает долларов")
    dxp = round(min(x * 0.2, 2000), 2)
    add_usd(user, -x, dxp)
    c["treasury"] = round(c["treasury"] + x, 2)
    c["cxp"] += x * 0.5
    c["members"][login]["don"] = c["members"][login].get("don", 0) + x
    co_log(c, f"💰 {uname(login)} вложил ${x:,.2f} в казну")
    touch("co_" + c["id"])
    return ok(user, login, d_usd=-x, d_xp=dxp)

async def handle_co_dividends(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец выплачивает дивиденды")
    n = len(c["members"])
    share = math.floor(c["treasury"] / n * 100) / 100
    if share < 1:
        return err("В казне слишком мало — нужно хотя бы $1 на каждого")
    for l in c["members"]:
        u = USERS[l]
        u["co"]["pending"] = round(u["co"].get("pending", 0) + share, 2)
        touch(l)
    c["treasury"] = round(c["treasury"] - share * n, 2)
    co_log(c, f"💸 Дивиденды: по ${share:,.2f} каждому из {n} участников")
    touch("co_" + c["id"])
    return ok(user, login)

async def handle_co_ad(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец запускает рекламу")
    now, cost = time.time(), 200 * co_level(c)
    if c.get("ad_until", 0) > now:
        return err("Реклама уже идёт")
    if c["treasury"] < cost:
        return err(f"В казне нужно ${cost}")
    c["treasury"] = round(c["treasury"] - cost, 2)
    c["ad_until"] = now + 600
    co_log(c, f"📣 Запущена реклама за ${cost}: бафф дохода +15% на 10 минут")
    touch("co_" + c["id"])
    return ok(user, login)

async def handle_co_upgrade(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    c = co_of_user(user)
    if not c or c["owner"] != login:
        return err("Только владелец покупает улучшения")
    key = data.get("k")
    if key not in UPGRADES:
        return err("Неизвестное улучшение")
    lvl = upg(c, key)
    if lvl >= UPGRADES[key][3]:
        return err("Максимальный уровень")
    cost = up_cost(key, lvl)
    if c["treasury"] < cost:
        return err(f"В казне нужно ${cost:,}")
    c["treasury"] = round(c["treasury"] - cost, 2)
    c.setdefault("up", {})[key] = lvl + 1
    c["cxp"] += cost * 0.1
    co_log(c, f"🛠 Улучшение «{UPGRADES[key][0]}» → ур. {lvl + 1}")
    touch("co_" + c["id"])
    return ok(user, login)

# ---------- аватар (любой эмодзи) ----------
async def handle_avatar(request):
    data = await read_json(request)
    login = get_login(data)
    if not login:
        return err("Unauthorized", 401)
    a = str(data.get("avatar") or "").strip()
    if a and not valid_emoji(a):
        return err("Нужен эмодзи")
    USERS[login]["avatar"] = a
    touch(login)
    return web.json_response({"avatar": a})

# ---------- ежедневная награда ----------
def day_str(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))

def daily_info(user):
    d = user.setdefault("daily", {"last": "", "streak": 0})
    now = time.time()
    today, yest = day_str(now), day_str(now - 86400)
    alive = d.get("last") in (today, yest)
    return {"can": d.get("last") != today, "streak": d.get("streak", 0) if alive else 0}

async def handle_daily(request):
    login = get_login(await read_json(request))
    if not login:
        return err("Unauthorized", 401)
    user = USERS[login]
    d = user.setdefault("daily", {"last": "", "streak": 0})
    now = time.time()
    today, yest = day_str(now), day_str(now - 86400)
    if d.get("last") == today:
        return err("Награда уже получена — приходи завтра")
    d["streak"] = d.get("streak", 0) + 1 if d.get("last") == yest else 1
    d["last"] = today
    streak = d["streak"]
    usd = 100 * min(streak, 14)
    user["co"]["pending"] = round(user["co"].get("pending", 0) + usd, 2)
    item = None
    if streak % 7 == 0:
        user["inv"]["core"] = user["inv"].get("core", 0) + 1
        item = "core"
    touch(login)
    return web.json_response({"usd": usd, "streak": streak, "item": item, "state": state_of(user)})

# ---------- банк: 1% в час, до 24 часов накопления ----------
BANK_RATE = 0.01

def bank_cap(user):
    return 5000 * level_of(user)

def bank_accrue(user):
    b = user.setdefault("bank", {"amt": 0.0, "t": time.time()})
    now = time.time()
    if b.get("amt", 0) > 0:
        hrs = min(24.0, max(0.0, (now - b.get("t", now)) / 3600))
        b["amt"] = round(b["amt"] * (1 + BANK_RATE * hrs), 2)
    b["t"] = now
    return b

# ---------- кредит: берёшь в долг, растут проценты, бот напоминает ----------
LOAN_RATE = 0.015           # 1.5% в час
LOAN_GRACE = 24 * 3600      # через сутки долг считается просроченным, бот начинает напоминать
REMIND_EVERY = 12 * 3600    # напоминания не чаще раза в 12 часов
REMIND_MAX = 6

def loan_cap(user):
    return 2000 * level_of(user)

def loan_of(user):
    return user.setdefault("loan", {"debt": 0.0, "principal": 0.0, "t": 0, "since": 0, "last_remind": 0, "remind_n": 0})

def loan_accrue(user):
    ln, now = loan_of(user), time.time()
    if ln.get("debt", 0) > 0:
        hrs = max(0.0, (now - ln.get("t", now)) / 3600)
        ln["debt"] = round(min(ln.get("principal", 0) * 5, ln["debt"] * (1 + LOAN_RATE * hrs)), 2)
    ln["t"] = now
    return ln

def loan_clear(ln):
    ln.update(debt=0.0, principal=0.0, since=0, last_remind=0, remind_n=0)

def bank_json(user):
    b, ln, now = user["bank"], loan_accrue(user), time.time()
    held = ln["debt"] > 0
    cap = loan_cap(user)
    return {"amt": b["amt"], "cap": bank_cap(user), "rate": BANK_RATE * 100,
            "loan": {"debt": ln["debt"], "cap": cap, "left": max(0, round(cap - ln["debt"], 2)),
                     "rate": LOAN_RATE * 100, "age_h": round((now - ln["since"]) / 3600, 1) if held else 0,
                     "overdue": bool(held and now - ln["since"] >= LOAN_GRACE), "grace_h": LOAN_GRACE // 3600}}

def money_in(v):
    try:
        x = math.floor(float(v) * 100 + 1e-6) / 100
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return x if math.isfinite(x) else 0.0

async def handle_bank(request):
    data, login, user = await co_ctx(request)
    if not login:
        return err("Unauthorized", 401)
    b = bank_accrue(user)
    act, d_usd, d_xp, now = data.get("act", "info"), 0.0, 0.0, time.time()
    if act in ("dep", "wd", "borrow", "repay") and not isinstance(user.get("game"), dict):
        return err(NO_SAVE)
    if act == "dep":
        x = money_in(data.get("usd", 0))
        if x < 1:
            return err("Минимальный вклад — $1")
        if x > usd_of(user) + 0.001:
            return err("Не хватает долларов")
        if b["amt"] + x > bank_cap(user) + 0.001:
            return err(f"Лимит вклада — ${bank_cap(user):,} (растёт с уровнем)")
        b["amt"] = round(b["amt"] + x, 2)
        add_usd(user, -x, 1)
        d_usd, d_xp = -x, 1.0
    elif act == "wd":
        x = b["amt"]
        if x < 0.01:
            return err("На вкладе пусто")
        b["amt"] = 0.0
        add_usd(user, x, 0)
        d_usd = x
    elif act == "borrow":
        ln = loan_accrue(user)
        if ln["debt"] > 0 and now - ln["since"] >= LOAN_GRACE:
            return err("Сначала верни просроченный долг")
        x = money_in(data.get("usd", 0))
        if x < 1:
            return err("Минимальная сумма кредита — $1")
        if ln["debt"] + x > loan_cap(user) + 0.001:
            return err(f"Лимит кредита — ${loan_cap(user):,} (растёт с уровнем)")
        if ln["debt"] <= 0:
            ln.update(since=now, principal=0.0, last_remind=0, remind_n=0)
        ln["debt"] = round(ln["debt"] + x, 2)
        ln["principal"] = round(ln["principal"] + x, 2)
        add_usd(user, x, 0)
        d_usd = x
    elif act == "repay":
        ln = loan_accrue(user)
        if ln["debt"] < 0.01:
            return err("Долгов нет")
        want = ln["debt"] if data.get("all") else money_in(data.get("usd", 0))
        x = min(want, ln["debt"], math.floor(usd_of(user) * 100) / 100)
        if x < 0.01:
            return err("Не хватает долларов")
        ln["debt"] = round(ln["debt"] - x, 2)
        if ln["debt"] < 0.01:
            loan_clear(ln)
        add_usd(user, -x, 3)
        d_usd, d_xp = -x, 3.0
    touch(login)
    return web.json_response({"ok": True, "d_usd": round(d_usd, 2), "d_xp": d_xp, "bank": bank_json(user)})

async def debt_loop(app):
    """Раз в 10 минут: если долг висит дольше суток — бот напоминает в личку (не чаще раза в 12 часов)."""
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            for l, u in list(USERS.items()):
                ln = u.get("loan") if not l.startswith("co_") else None
                if not isinstance(ln, dict) or ln.get("debt", 0) < 0.01:
                    continue
                if now - ln.get("since", now) < LOAN_GRACE or now - ln.get("last_remind", 0) < REMIND_EVERY \
                        or ln.get("remind_n", 0) >= REMIND_MAX:
                    continue
                try:
                    chat = int(l[3:])
                except ValueError:
                    continue
                loan_accrue(u)
                ln["last_remind"], ln["remind_n"] = now, ln.get("remind_n", 0) + 1
                touch(l)
                days = max(1, int((now - ln["since"]) // 86400))
                text = (f"⚠️ Долг в Нейробанке: ${ln['debt']:,.2f}\n"
                        f"Он висит уже {days} дн., проценты капают — {LOAN_RATE * 100:g}% в час. "
                        f"Верни долг, пока он не вырос 👇")
                await call(app["s"], "sendMessage", chat_id=chat, text=text,
                           reply_markup={"inline_keyboard": [[{"text": "🏦 Открыть банк", "web_app": {"url": WEBAPP_URL}}]]})
        except Exception as e:
            print("DEBT LOOP ERROR:", repr(e))

CO_ROUTES = [("/api/co/sync", handle_co_sync), ("/api/co/list", handle_co_list), ("/api/co/find", handle_co_find),
             ("/api/co/create", handle_co_create), ("/api/co/join", handle_co_join),
             ("/api/co/leave", handle_co_leave), ("/api/co/disband", handle_co_disband),
             ("/api/co/settings", handle_co_settings), ("/api/co/kick", handle_co_kick),
             ("/api/co/core", handle_co_core), ("/api/co/donate", handle_co_donate),
             ("/api/co/dividends", handle_co_dividends), ("/api/co/ad", handle_co_ad),
             ("/api/co/upgrade", handle_co_upgrade), ("/api/avatar", handle_avatar),
             ("/api/daily", handle_daily), ("/api/bank", handle_bank)]

# =========================
# РЕФЕРАЛКА: $500 за каждого нового игрока по твоей ссылке
# Ссылка: https://t.me/<бот>?start=ref_<твой Telegram ID>
# Приглашённого определяет сервер (по /start или подписанному start_param), клиент ничего не подделает.
# =========================
REF_USD = 500

def ref_login(param):
    m = re.fullmatch(r"ref_(\d{1,15})", param or "")
    return f"tg_{m.group(1)}" if m else None

def pay_referral(login):
    """Игрок реально зашёл в игру — начисляем награду пригласившему (один раз)."""
    u = USERS[login]
    r = u["ref"]
    if not r.get("by") or r.get("paid"):
        return
    r["paid"] = True
    inviter = USERS.get(r["by"])
    if inviter and r["by"] != login:
        inviter["co"]["pending"] = round(inviter["co"].get("pending", 0) + REF_USD, 2)
        ir = inviter["ref"]
        ir["count"] += 1
        ir["earned"] = round(ir["earned"] + REF_USD, 2)
        ir["names"].insert(0, {"name": u.get("name") or "Игрок", "ts": int(time.time())})
        del ir["names"][30:]
        touch(r["by"])
    touch(login)

async def handle_ref(request):
    login = get_login(await read_json(request))
    if not login:
        return err("Unauthorized", 401)
    r = USERS[login]["ref"]
    link = f"https://t.me/{BOT_NAME}?start=ref_{login[3:]}" if BOT_NAME else ""
    return web.json_response({"link": link, "count": r["count"], "earned": r["earned"],
                              "reward": REF_USD, "list": r["names"][:20]})

CO_ROUTES.append(("/api/ref", handle_ref))

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
    await db_init()
    app["writer_task"] = asyncio.create_task(writer())
    app["market_task"] = asyncio.create_task(market_loop())
    app["s"] = ClientSession()
    app["debt_task"] = asyncio.create_task(debt_loop(app))
    if TOKEN:
        await call(app["s"], "setWebhook", url=f"{RENDER_URL}/webhook", secret_token=WEBHOOK_SECRET,
                   allowed_updates=["message", "pre_checkout_query"])
        me = await call(app["s"], "getMe")
        global BOT_NAME
        BOT_NAME = (me.get("result") or {}).get("username", "")
    app["keep_alive_task"] = asyncio.create_task(keep_alive(app))

async def on_cleanup(app):
    if "writer_task" in app:
        app["writer_task"].cancel()
    if "market_task" in app:
        app["market_task"].cancel()
    if "debt_task" in app:
        app["debt_task"].cancel()
    await flush()
    if _pool:
        await _pool.close()
    if "keep_alive_task" in app:
        app["keep_alive_task"].cancel()
    if "s" in app:
        await app["s"].close()

app = web.Application(middlewares=[cors])
app.router.add_get("/", handle_index)
app.router.add_get("/health", handle_health)
app.router.add_post("/webhook", handle_webhook)
for path, h in [("/api/me", handle_me), ("/api/drop", handle_drop), ("/api/use", handle_use),
                ("/api/promo", handle_promo), ("/api/save", handle_save), ("/api/top", handle_top), ("/api/invoice", handle_invoice)]:
    app.router.add_route("*", path, h)
for path, h in CO_ROUTES:
    app.router.add_route("*", path, h)
app.on_startup.append(on_start)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
