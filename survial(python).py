import os, json, time, hmac, hashlib, random, sqlite3, asyncio, logging, math
from urllib.parse import parse_qsl
from aiohttp import web, ClientSession, ClientTimeout

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
DB_PATH = os.environ.get("DB_PATH", "game.db")
PORT = int(os.environ.get("PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bob")

db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
db.executescript("PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS players("
                 "id INTEGER PRIMARY KEY, name TEXT, state TEXT NOT NULL, score INTEGER DEFAULT 0, updated REAL);")

class GameError(Exception):
    pass

# ---------------- игровые данные ----------------
RESL = [("wood", "🪵", "Дерево", "Стройка"), ("stone", "🪨", "Камень", "Стройка"),
        ("scrap", "🔩", "Металлолом", "Стройка, оружие"), ("food", "🍞", "Еда", "Лечение: 5🍞 = +40❤️"),
        ("water", "💧", "Вода", "Полив: 1 на посадку"), ("seeds", "🌱", "Семена", "Посадка"),
        ("gold", "🪙", "Золото", "Покупки"), ("energy", "⚡", "Энергия", "Лаборатория, лазер"),
        ("crystal", "💎", "Кристаллы", "Питомцы, лазер")]
RK = [r[0] for r in RESL]
OBJ = {"tree": ("🌳", "wood"), "rock": ("🪨", "stone"), "junk": ("🗑️", "scrap"),
       "well": ("💧", "water"), "bush": ("🍅", "food"), "seed": ("🌱", "seeds")}
MSG = {"tree": ["Ты ударил дерево. Дерево запомнило.", "Дерево обиделось, но дало дров.", "Дерево упало молча. Из уважения к тебе."],
       "rock": ["Ты добыл камень. Археологи уже заинтересовались.", "Камень оказался тяжелее, чем выглядел.", "Камень пожаловался в профсоюз булыжников."],
       "junk": ["Найден металлолом. Почти новый. Всего 17 лет лежал в болоте.", "Ржавая штука. Возможно, раньше была важной.", "Ты вытащил из кучи нечто. Оно гудит. Это нормально."],
       "well": ["Вода. Почти без мутантов.", "Ведро вернулось с водой и жизненным опытом.", "В воде плавал пельмень. Ты сделал вид, что не заметил."],
       "bush": ["Куст пожертвовал помидоры науке.", "Ты собрал еду. Она смотрела на тебя.", "Помидоры созрели и сразу начали торговаться."],
       "seed": ["Семечко. Скоро будет картошка-тиран.", "Семена нашлись в подозрительной грязи.", "Семечко подмигнуло. Или показалось."]}
LINES = ["Геннадий повысил себя до генерального кабачка.", "Твой хомяк потребовал трудовой договор.",
         "Крыса открыла ИП прямо в твоём инвентаре.", "Ты нашёл древний артефакт. Это чек из супермаркета.",
         "Бухгалтер Апокалипсиса требует отчёт за морковку.", "Табуретка посмотрела на тебя с подозрением.",
         "Картошка подала заявление на отпуск.", "Помидор в углу репетирует речь.",
         "Курица пересчитала яйца и осталась недовольна.", "Остров гудит: где-то рождается мем.",
         "Огурец утверждает, что он на самом деле кабачок.", "Колодец спросил, не пора ли ему в отпуск.",
         "Чайка украла твой план на жизнь. План был плохой.", "Грядка требует повышения арендной платы.",
         "Морковь светится. Это нормально. Наверное.", "Кот сидит на сокровище и делает вид, что это просто кот.",
         "Лопата ушла в самоволку.", "Пельмень на горизонте принял стойку.",
         "Верстак открыл курсы по повышению квалификации молотков.", "Туман пахнет борщом. Подозрительно.",
         "Камень у костра неожиданно заговорил. Сказал: «Ой».", "Тыква начала вести дневник.",
         "Башня защиты потребовала охранника для себя.", "Мухи организовали профсоюз.",
         "Бочка воды утверждает, что она на самом деле океан.", "Картофельная пушка чихнула. Рядом скончался кустик.",
         "Кабачок Геннадий опубликовал мемуары: «Я и грядки».", "Ржавая гайка нашла своего болта. Свадьба в субботу.",
         "Ночью кто-то переставил все табуретки. Зачем — неизвестно.", "На острове объявлен день тишины. Все молчат, кроме курицы."]
# имя, эмодзи, цена 1 уровня, ресурс, выработка/ч за уровень, нужный уровень игрока, описание
BLD = {
    "campfire": ("Костёр", "🔥", {"wood": 10}, None, 0, 1, "+1 час офлайн-накопления за уровень"),
    "bed": ("Грядка", "🥕", {"wood": 30, "gold": 20}, None, 0, 1, "+1 клетка на ферме за уровень"),
    "sawmill": ("Лесопилка", "🪚", {"gold": 30}, "wood", 60, 1, "Дерево 60/ч за уровень"),
    "well": ("Колодец", "🪣", {"gold": 25}, "water", 80, 1, "Вода 80/ч, автополив: рост быстрее на 4%/ур."),
    "crusher": ("Камнедробилка", "⛏️", {"gold": 40, "wood": 20}, "stone", 50, 2, "Камень 50/ч за уровень"),
    "press": ("Металлоломный пресс", "🗜️", {"gold": 60, "stone": 30}, "scrap", 40, 3, "Металлолом 40/ч за уровень"),
    "bench": ("Верстак", "🔨", {"wood": 50, "scrap": 10}, None, 0, 3, "+2 урона за уровень"),
    "generator": ("Генератор на картошке", "🥔", {"gold": 80, "scrap": 20}, "energy", 30, 4, "Энергия 30/ч за уровень"),
    "tower": ("Башня защиты", "🗼", {"stone": 80, "scrap": 40}, None, 0, 4, "+25 макс. HP, враги бьют слабее"),
    "autofarm": ("Автоферма", "🤖", {"gold": 150, "scrap": 40}, "food", 70, 5, "Еда 70/ч за уровень"),
    "junk": ("Завод бесполезных вещей", "🗑️", {"gold": 200, "scrap": 60}, "gold", 40, 6, "Золото 40/ч за уровень"),
    "lab": ("Лаборатория мутаций", "🧪", {"gold": 300, "energy": 50}, "crystal", 2, 7, "Кристаллы 2/ч, +2% мутаций за уровень"),
}
# имя, эмодзи, семян, секунд, ресурс, количество, уровень
CROP = {"potato": ("Картофель", "🥔", 1, 30, "food", 5, 1), "tomato": ("Помидор", "🍅", 2, 90, "food", 14, 2),
        "cucumber": ("Огурец", "🥒", 3, 180, "gold", 25, 3), "moneytree": ("Денежное дерево", "💸", 5, 600, "gold", 150, 4),
        "carrot": ("Радиоактивная морковь", "☢️", 8, 900, "crystal", 2, 6),
        "pumpkin": ("Золотая тыква", "🎃", 20, 1800, "gold", 700, 9)}
# имя, эмодзи, hp, урон, золото, опыт, уровень, шанс кристалла
ENEMY = {"tomato": ("Злой помидор", "🍅", 30, 4, 15, 12, 1, 0), "potato": ("Картофельный террорист", "🥔", 60, 7, 30, 25, 2, 0),
         "cuke": ("Огурец-беглец", "🥒", 100, 10, 50, 40, 4, 0), "chicken": ("Курица-берсерк", "🐔", 200, 14, 90, 70, 5, 0.05),
         "gena": ("Кабачок Геннадий", "🥬", 400, 18, 200, 150, 7, 0.1),
         "acc": ("Бухгалтер Апокалипсиса", "🧾", 900, 24, 500, 350, 10, 0.2),
         "dumpling": ("Великий Пельмень", "🥟", 1500, 40, 1500, 900, 14, 0.5)}
WEAP = {"stick": ("Деревянная палка", "🪵", 4), "pan": ("Сковородка", "🍳", 8), "cuke": ("Огуречный меч", "🥒", 16),
        "cannon": ("Картофельная пушка", "🥔", 30), "laser": ("Лазерный прополщик", "🔫", 60)}
# имя, эмодзи, цена, тип, значение, уровень
SHOP = {
    "seeds5": ("Пачка семян ×5", "🌱", {"gold": 10}, "res", {"seeds": 5}, 1),
    "seeds30": ("Мешок семян ×30", "🌾", {"gold": 50}, "res", {"seeds": 30}, 2),
    "water30": ("Бочка воды ×30", "🛢️", {"gold": 15}, "res", {"water": 30}, 1),
    "fert": ("Удобрение (рост ×2 быстрее)", "💩", {"gold": 30}, "fert", 0, 2),
    "medkit": ("Аптечка +60❤️", "🩹", {"gold": 25}, "heal", 60, 1),
    "eat": ("Съесть паёк +40❤️", "🍞", {"food": 5}, "heal", 40, 1),
    "sell_wood": ("Продать 20 дерева", "🪵", {"wood": 20}, "res", {"gold": 6}, 1),
    "sell_stone": ("Продать 20 камня", "🪨", {"stone": 20}, "res", {"gold": 8}, 1),
    "sell_scrap": ("Продать 20 металлолома", "🔩", {"scrap": 20}, "res", {"gold": 12}, 1),
    "sell_food": ("Продать 20 еды", "🍞", {"food": 20}, "res", {"gold": 6}, 1),
    "useless": ("Бесполезная вещь", "🧦", {"gold": 5}, "useless", 0, 1),
    "dubious": ("Сомнительное улучшение (только у торговца)", "🕶️", {"gold": 150}, "dealer", 0, 1),
    "w_pan": ("Сковородка", "🍳", {"gold": 60}, "weapon", "pan", 2),
    "w_cuke": ("Огуречный меч", "🥒", {"gold": 200, "wood": 40}, "weapon", "cuke", 4),
    "w_cannon": ("Картофельная пушка", "🥔", {"gold": 600, "scrap": 80}, "weapon", "cannon", 7),
    "w_laser": ("Лазерный прополщик", "🔫", {"gold": 2000, "crystal": 10, "energy": 100}, "weapon", "laser", 10),
}
PET = {"chicken": ("Курица-налоговик", "🐔", {"gold": 300}, 3, "+10% пассивного дохода за ур.", "Обычный"),
       "rat": ("Крыса-бизнесмен", "🐀", {"gold": 500}, 4, "+3% удачи за ур. (крит, мутации)", "Редкий"),
       "hamster": ("Хомяк-электрик", "🐹", {"gold": 600, "scrap": 30}, 5, "+15% энергии за ур.", "Редкий"),
       "cat": ("Кот-вор", "🐈", {"gold": 800}, 6, "Приносит ресурсы при добыче, но тырит еду", "Эпический"),
       "zuc": ("Мини-кабачок", "🥒", {"gold": 1000, "crystal": 2}, 8, "+2 урона за ур.", "Легендарный")}
# биомы: имя, эмодзи, цена в золоте, уровень, множители добычи по ресурсам, доп. шанс кристалла
BIO = {"swamp": ("Болото", "🐸", 0, 1, {"scrap": 1.5}, 0),
       "forest": ("Тёмный лес", "🌲", 500, 4, {"wood": 2}, 0),
       "cave": ("Пещера", "🕳️", 1500, 7, {"stone": 2}, .02),
       "waste": ("Радиоактивная пустошь", "☢️", 5000, 10, {"scrap": 2, "energy": 1}, .04),
       "volcano": ("Вулкан бесполезных вещей", "🌋", 15000, 14, {"stone": 2, "scrap": 2, "wood": 2}, .06)}
PRESTIGE_LVL = 15
# ежедневные задания: id, текст, счётчик, цель, золото, опыт
DQ = [("dq1", "Собери 3 урожая", "harvest", 3, 60, 40), ("dq2", "Добудь 50 раз", "collect", 50, 80, 50),
      ("dq3", "Победи 3 врагов", "kills", 3, 90, 60), ("dq4", "Построй или улучши 2 здания", "build", 2, 100, 60),
      ("dq5", "Добудь 100 раз", "collect", 100, 150, 90), ("dq6", "Собери 6 урожаев", "harvest", 6, 120, 80)]
Q = [("q1", "Первая картошка: собери урожай", "harvest", 1, 30, 20), ("q2", "Фермер-стахановец: 25 урожаев", "harvest", 25, 200, 100),
     ("q3", "Лесоруб года: 100 добыч", "collect", 100, 100, 60), ("q4", "Мозоли на пальце: 1000 добыч", "collect", 1000, 800, 400),
     ("q5", "Прораб: 5 построек", "build", 5, 150, 80), ("q6", "Ты зачем построил 47 курятников?", "build", 47, 3000, 1500),
     ("q7", "Первая кровь (томатная)", "kills", 1, 50, 40), ("q8", "Уничтожитель овощей: 25 побед", "kills", 25, 600, 300),
     ("q9", "Миллионер на грядке: 10000 золота", "gold", 10000, 2000, 1000), ("q10", "Налоговая тебя боится: уровень 10", "lvl", 10, 1000, 500)]
# достижения: id, название, ключ прогресса, цель, золото, опыт
ACH = [("a1", "🥔 Первая картошка", "harvest", 1, 20, 20), ("a2", "🪓 Лесоруб года", "collect", 500, 300, 150),
       ("a3", "💰 Миллионер на грядке", "gold", 100000, 5000, 2000), ("a4", "🧾 Налоговая тебя боится", "lvl", 20, 3000, 1500),
       ("a5", "🥬 Уничтожитель Геннадия", "kc_gena", 10, 1000, 500), ("a6", "🥟 Пельменный погром", "kc_dumpling", 5, 3000, 1500),
       ("a7", "🐔 Ты зачем построил 47 курятников?", "build", 100, 6000, 3000), ("a8", "🐾 Зоопарк: 5 питомцев", "petcount", 5, 1500, 700),
       ("a9", "🔄 Первое перерождение", "prestige", 1, 2000, 1000), ("a10", "🌍 Путешественник: 3 биома", "biocount", 3, 800, 400)]
EV = [("dealer", "🕶️ Продавец сомнительных улучшений", "В магазине появился подозрительный товар!", 300),
      ("rain", "🍅 Помидорный дождь", "Урожай растёт вдвое быстрее!", 300), ("tax", "🧾 Налоговая проверка", "С тебя 8% золота.", 0),
      ("riot", "🪑 Восстание табуреток", "+50% золота за победы.", 300), ("alien", "👽 Пришельцы", "Украли холодильник с 10% еды.", 0),
      ("potato", "🥔 Картофельный кризис", "Урожай картошки ×2.", 300), ("junk", "🗑️ День бесплатного мусора", "Металлолом в подарок.", 0),
      ("gold", "🐔 Золотая курица", "Курица что-то снесла!", 0), ("trader", "🧳 Странный торговец", "Всучил подарок.", 0),
      ("fog", "☢️ Радиоактивный туман", "Мутации чаще.", 300)]

META = {"RES": {k: [e, n, p] for k, e, n, p in RESL}, "OBJ": {k: list(v) for k, v in OBJ.items()},
        "BLD": {i: {"n": b[0], "e": b[1], "d": b[6], "u": b[5]} for i, b in BLD.items()},
        "CROP": CROP, "ENEMY": ENEMY, "WEAP": WEAP, "Q": Q, "ACH": ACH, "DQ": DQ, "PLVL": PRESTIGE_LVL,
        "BIO": {i: {"n": b[0], "e": b[1], "c": b[2], "u": b[3], "b": b[4], "x": b[5]} for i, b in BIO.items()},
        "PET": {i: {"n": p[0], "e": p[1], "u": p[3], "d": p[4], "r": p[5]} for i, p in PET.items()},
        "SHOP": {i: {"n": s[0], "e": s[1], "c": s[2], "k": s[3], "v": s[4], "u": s[5]} for i, s in SHOP.items()}}

# ---------------- состояние ----------------
def new_state():
    t = time.time()
    return {"res": {"wood": 20, "stone": 0, "scrap": 0, "food": 10, "water": 10, "seeds": 5, "gold": 50, "energy": 0, "crystal": 0},
            "lvl": 1, "xp": 0, "hp": 120.0, "pts": 0, "sk": {"str": 0, "spd": 0, "luck": 0, "cap": 0},
            "bld": {}, "plots": [], "weapon": "stick", "weapons": ["stick"], "pets": {}, "acc": {},
            "last": t, "hpt": t, "cnt": {"collect": 0, "harvest": 0, "kills": 0, "build": 0}, "claimed": [],
            "daily": {"day": "", "streak": 0}, "event": None, "evt_t": t, "log": ["Добро пожаловать на остров, Боб!"], "name": "Боб",
            "kc": {}, "prestige": 0, "biome": "swamp", "biomes": ["swamp"], "dq": {"day": "", "base": {}, "claimed": []}}

def migrate(s):
    # старые сохранения без новых полей
    for k, v in (("kc", {}), ("prestige", 0), ("biome", "swamp"), ("biomes", ["swamp"]), ("dq", {"day": "", "base": {}, "claimed": []})):
        s.setdefault(k, v)
    s["cnt"].setdefault("build", 0)
    return s

def pm(s): return 1 + .1 * s["prestige"]
def today_of(now): return time.strftime("%Y-%m-%d", time.gmtime(now))

def progress(s):
    return {**s["cnt"], "lvl": s["lvl"], "gold": s["res"]["gold"], "prestige": s["prestige"], "petcount": sum(1 for v in s["pets"].values() if v),
            "biocount": len(s["biomes"]), **{"kc_" + k: v for k, v in s["kc"].items()}}

def ensure_daily(s, now):
    d = s["dq"]; t = today_of(now)
    if d["day"] != t:
        d["day"] = t; d["claimed"] = []; d["base"] = {k: s["cnt"].get(k, 0) for k in ("collect", "harvest", "kills", "build")}

def daily_list(s, now):
    rnd = random.Random(today_of(now)); ids = rnd.sample(range(len(DQ)), 3)
    return [DQ[i] for i in sorted(ids)]

def need(l): return 30 * l * l + 20
def cap(s): return 500 + 250 * s["lvl"] + 300 * s["sk"]["cap"]
def maxhp(s): return 100 + 20 * s["lvl"] + 25 * s["bld"].get("tower", 0)
def luck(s): return min(.6, .05 + .02 * s["sk"]["luck"] + .03 * s["pets"].get("rat", 0))
def dmg(s): return WEAP[s["weapon"]][2] + 2 * s["sk"]["str"] + 2 * s["bld"].get("bench", 0) + 2 * s["pets"].get("zuc", 0)
def bcost(s, i): return {k: math.ceil(v * 1.55 ** s["bld"].get(i, 0)) for k, v in BLD[i][2].items()}
def pcost(i, l): return {k: math.ceil(v * (l + 1) ** 1.5) for k, v in PET[i][2].items()}
def say(s, m): s["log"] = (s["log"] + [m])[-15:]
def active(s, i, now): e = s["event"]; return bool(e and e["id"] == i and e["until"] > now)

def add(s, k, n):
    n = int(n); b = s["res"][k]
    s["res"][k] = b + n if k in ("gold", "crystal") else min(max(cap(s), b), b + n)
    return s["res"][k] - b

def pay(s, c):
    if any(s["res"][k] < v for k, v in c.items()): raise GameError("Не хватает ресурсов")
    for k, v in c.items(): s["res"][k] -= v

def gain_xp(s, n):
    s["xp"] += n
    while s["xp"] >= need(s["lvl"]):
        s["xp"] -= need(s["lvl"]); s["lvl"] += 1; s["pts"] += 1; s["res"]["gold"] += 20 * s["lvl"]; s["hp"] = maxhp(s)
        say(s, f"🎉 Уровень {s['lvl']}! Награда: {20 * s['lvl']}🪙 и очко навыка")

def rates(s):
    m = (1 + .1 * s["pets"].get("chicken", 0)) * pm(s); r = {}
    for i, l in s["bld"].items():
        b = BLD[i]
        if b[3]:
            r[b[3]] = r.get(b[3], 0) + b[4] * l * m * ((1 + .15 * s["pets"].get("hamster", 0)) if b[3] == "energy" else 1)
    return r

def tick(s, now):
    dt = max(0, now - s["last"]); dt = min(dt, (8 + s["bld"].get("campfire", 0)) * 3600); s["last"] = now
    gains = {}
    for k, v in rates(s).items():
        a = s["acc"].get(k, 0) + v * dt / 3600; w = int(a); s["acc"][k] = a - w
        if w: gains[k] = add(s, k, w)
    s["hp"] = min(maxhp(s), s["hp"] + (now - s["hpt"]) / 6); s["hpt"] = now
    while len(s["plots"]) < 2 + s["bld"].get("bed", 0): s["plots"].append({"c": None, "t": 0})
    if s["event"] and s["event"]["until"] <= now: s["event"] = None
    ensure_daily(s, now)
    return {"secs": int(dt), "gain": {k: v for k, v in gains.items() if v}} if dt >= 120 and gains else None

def view(s, now):
    e = s["event"]
    d = s["daily"]; today = time.strftime("%Y-%m-%d", time.gmtime(now))
    return {"name": s["name"], "res": s["res"], "cap": cap(s), "lvl": s["lvl"], "xp": s["xp"], "need": need(s["lvl"]),
            "hp": int(s["hp"]), "maxhp": maxhp(s), "pts": s["pts"], "sk": s["sk"], "dmg": dmg(s), "luck": int(luck(s) * 100),
            "bld": s["bld"], "next": {i: bcost(s, i) for i in BLD}, "plots": s["plots"], "weapon": s["weapon"],
            "weapons": s["weapons"], "pets": s["pets"], "pnext": {i: pcost(i, s["pets"].get(i, 0)) for i in PET},
            "rates": {k: round(v) for k, v in rates(s).items()}, "claimed": s["claimed"],
            "prog": progress(s), "streak": d["streak"], "dclaim": d["day"] != today,
            "dq": [{"id": q[0], "t": q[1], "k": q[2], "g": q[3], "gold": q[4], "xp": q[5],
                    "p": min(q[3], s["cnt"].get(q[2], 0) - s["dq"]["base"].get(q[2], 0)), "done": q[0] in s["dq"]["claimed"]}
                   for q in daily_list(s, now)],
            "prestige": s["prestige"], "pbonus": int((pm(s) - 1) * 100), "biome": s["biome"], "biomes": s["biomes"],
            "event": {"id": e["id"], "title": e["title"], "text": e["text"], "left": int(e["until"] - now)} if e else None,
            "log": s["log"], "now": now, "offcap": 8 + s["bld"].get("campfire", 0)}

# ---------------- действия ----------------
def a_bootstrap(s, b, now): return {"meta": META}
def a_state(s, b, now): return {}

def a_collect(s, b, now):
    k = b.get("obj"); o = OBJ.get(k)
    if not o: raise GameError("Нет такого объекта")
    crit = random.random() < luck(s)
    bio = BIO[s["biome"]]
    base = (1 + s["sk"]["spd"] + s["lvl"] // 3) * (3 if crit else 1) * bio[4].get(o[1], 1) * pm(s)
    g = {o[1]: add(s, o[1], base)}
    if random.random() < .03: g["gold"] = add(s, "gold", random.randint(3, 10) * s["lvl"])
    if random.random() < .01 + .005 * s["sk"]["luck"] + bio[5]: g["crystal"] = add(s, "crystal", 1)
    extra = ""
    if s["pets"].get("cat"):
        if random.random() < .10:
            r = random.choice(RK[:6]); g[r] = g.get(r, 0) + add(s, r, s["pets"]["cat"] * 2); extra = " 🐈 Кот притащил добычу."
        elif random.random() < .05 and s["res"]["food"] >= 2:
            s["res"]["food"] -= 2; extra = " 🐈 Кот украл 2🍞."
    s["cnt"]["collect"] += 1; gain_xp(s, 2)
    if random.random() < .08: say(s, random.choice(LINES))
    return {"gain": g, "crit": crit, "msg": random.choice(MSG[k]) + (" КРИТ!" if crit else "") + extra}

def plot_of(s, b):
    i = b.get("plot")
    if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(s["plots"]): raise GameError("Нет такой грядки")
    return s["plots"][i]

def a_plant(s, b, now):
    c = CROP.get(b.get("crop")); p = plot_of(s, b)
    if not c: raise GameError("Нет такого растения")
    if p["c"]: raise GameError("Грядка занята")
    if s["lvl"] < c[6]: raise GameError(f"Нужен уровень {c[6]}")
    pay(s, {"seeds": c[2], "water": 1})
    t = c[3] * (1 - min(.5, .04 * s["bld"].get("well", 0))) * (.5 if active(s, "rain", now) else 1)
    p["c"] = b["crop"]; p["t"] = now + t
    return {"msg": f"{c[1]} посажено"}

def a_harvest(s, b, now):
    p = plot_of(s, b)
    if not p["c"]: raise GameError("Тут ничего не растёт")
    if now < p["t"] - .5: raise GameError("Ещё не созрело")
    k = p["c"]; c = CROP[k]; n = c[5]
    mc = .05 + .02 * s["bld"].get("lab", 0) + luck(s) / 2 + (.25 if active(s, "fog", now) else 0)
    mut = random.random() < mc
    if mut: n *= 3
    if k == "potato" and active(s, "potato", now): n *= 2
    n = int(n * pm(s))
    got = add(s, c[4], n); p["c"] = None; p["t"] = 0
    s["cnt"]["harvest"] += 1; gain_xp(s, c[2] * 3 + 5)
    return {"gain": {c[4]: got}, "msg": ("☢️ МУТАЦИЯ! Урожай ×3! " if mut else "") + f"Собрано: {c[1]} +{got}"}

def a_build(s, b, now):
    i = b.get("id")
    if i not in BLD: raise GameError("Нет такого здания")
    l = s["bld"].get(i, 0)
    if s["lvl"] < BLD[i][5]: raise GameError(f"Нужен уровень {BLD[i][5]}")
    if l >= 25: raise GameError("Максимальный уровень")
    pay(s, bcost(s, i)); s["bld"][i] = l + 1; s["cnt"]["build"] += 1; gain_xp(s, 10 + 5 * l)
    return {"msg": f"{BLD[i][1]} {BLD[i][0]}: уровень {l + 1}"}

def a_buy(s, b, now):
    it = SHOP.get(b.get("id"))
    if not it: raise GameError("Нет такого товара")
    n, e, c, k, v, u = it
    if s["lvl"] < u: raise GameError(f"Нужен уровень {u}")
    if k == "weapon" and v in s["weapons"]: raise GameError("Уже куплено")
    if k == "heal" and s["hp"] >= maxhp(s): raise GameError("Ты и так здоров")
    if k == "dealer" and not active(s, "dealer", now): raise GameError("Торговец уже ушёл")
    pay(s, c); msg = f"Куплено: {n}"
    if k == "res":
        for r, a in v.items(): add(s, r, a)
    elif k == "heal": s["hp"] = min(maxhp(s), s["hp"] + v)
    elif k == "fert":
        for p in s["plots"]:
            if p["c"]: p["t"] = now + max(0, p["t"] - now) * .5
    elif k == "weapon":
        s["weapons"].append(v); s["weapon"] = max(s["weapons"], key=lambda w: WEAP[w][2]); msg = f"Экипировано: {WEAP[v][0]}"
    elif k == "dealer":
        if random.random() < .6: s["pts"] += 1; msg = "Торговец подмигнул: +1 очко навыка!"
        else: gain_xp(s, 30); msg = "Это оказалась пыль в баночке. Зато опыт: +30 XP."
    else:
        gain_xp(s, 5); msg = random.choice(["Ты купил носок. Он без пары, зато с характером.", "Бесполезная вещь. Зато твоя."])
    return {"msg": msg}

def a_attack(s, b, now):
    e = ENEMY.get(b.get("id"))
    if not e: raise GameError("Нет такого врага")
    if s["lvl"] < e[6]: raise GameError(f"Нужен уровень {e[6]}")
    if s["hp"] < 10: raise GameError("Слишком мало здоровья — подлечись")
    ph, eh, d, rounds = s["hp"], e[2], dmg(s), []
    for _ in range(25):
        c = random.random() < luck(s); pd = int(d * random.uniform(.8, 1.2) * (2 if c else 1)); eh -= pd; ed = 0
        if eh > 0:
            ed = max(1, int(e[3] * random.uniform(.7, 1.3) * .97 ** s["bld"].get("tower", 0))); ph -= ed
        rounds.append([pd, ed, max(0, int(ph)), max(0, eh), int(c)])
        if eh <= 0 or ph <= 0: break
    f = {"e": b["id"], "rounds": rounds, "gold": 0, "xp": 0}
    if eh <= 0:
        g = int(e[4] * (1.5 if active(s, "riot", now) else 1)); s["hp"] = max(1, ph)
        add(s, "gold", g); gain_xp(s, e[5]); s["cnt"]["kills"] += 1; s["kc"][b["id"]] = s["kc"].get(b["id"], 0) + 1; f.update(res="win", gold=g, xp=e[5])
        if random.random() < e[7]: add(s, "crystal", 1); f["crystal"] = 1
        say(s, f"⚔️ {e[1]} {e[0]} побеждён! +{g}🪙")
    elif ph <= 0:
        s["hp"] = 1; f["res"] = "lose"; say(s, f"💀 {e[0]} победил тебя. Позор на весь огород.")
    else:
        s["hp"] = max(1, ph); f["res"] = "draw"; say(s, f"🏃 {e[0]} сбежал.")
    return {"fight": f}

def a_skill(s, b, now):
    k = b.get("stat")
    if k not in s["sk"]: raise GameError("Нет такого навыка")
    if s["pts"] < 1: raise GameError("Нет очков навыков")
    s["pts"] -= 1; s["sk"][k] += 1; return {"msg": "Навык улучшен"}

def a_pet(s, b, now):
    i = b.get("id"); p = PET.get(i)
    if not p: raise GameError("Нет такого питомца")
    l = s["pets"].get(i, 0)
    if s["lvl"] < p[3]: raise GameError(f"Нужен уровень {p[3]}")
    if l >= 10: raise GameError("Максимальный уровень")
    pay(s, pcost(i, l)); s["pets"][i] = l + 1
    return {"msg": f"{p[1]} {p[0]}: уровень {l + 1}"}

def a_claim(s, b, now):
    qid = b.get("id")
    dq = next((x for x in daily_list(s, now) if x[0] == qid), None)
    if dq:
        if qid in s["dq"]["claimed"]: raise GameError("Награда уже получена")
        if s["cnt"].get(dq[2], 0) - s["dq"]["base"].get(dq[2], 0) < dq[3]: raise GameError("Задание не выполнено")
        s["dq"]["claimed"].append(qid); add(s, "gold", dq[4]); gain_xp(s, dq[5])
        return {"msg": f"Ежедневное задание: +{dq[4]}🪙 +{dq[5]} XP"}
    q = next((x for x in Q + ACH if x[0] == qid), None)
    if not q: raise GameError("Нет такого задания")
    if q[0] in s["claimed"]: raise GameError("Награда уже получена")
    if progress(s).get(q[2], 0) < q[3]: raise GameError("Задание не выполнено")
    s["claimed"].append(q[0]); add(s, "gold", q[4]); gain_xp(s, q[5])
    return {"msg": f"Награда: +{q[4]}🪙 +{q[5]} XP"}

def a_biome(s, b, now):
    i = b.get("id"); bio = BIO.get(i)
    if not bio: raise GameError("Нет такого биома")
    if i in s["biomes"]:
        s["biome"] = i; return {"msg": f"Ты переехал: {bio[1]} {bio[0]}"}
    if s["lvl"] < bio[3]: raise GameError(f"Нужен уровень {bio[3]}")
    pay(s, {"gold": bio[2]}); s["biomes"].append(i); s["biome"] = i
    say(s, f"🗺️ Открыт биом: {bio[1]} {bio[0]}")
    return {"msg": f"Открыт новый биом: {bio[1]} {bio[0]}!"}

def a_prestige(s, b, now):
    if s["lvl"] < PRESTIGE_LVL: raise GameError(f"Перерождение с уровня {PRESTIGE_LVL}")
    if b.get("confirm") is not True: raise GameError("Нужно подтверждение")
    n = new_state(); n["name"] = s["name"]
    for k in ("pets", "claimed", "cnt", "kc", "daily", "dq", "biomes", "biome", "weapons", "weapon", "log"):
        n[k] = s[k]
    n["prestige"] = s["prestige"] + 1; n["res"]["gold"] += 200 * n["prestige"]; n["res"]["crystal"] += 3 * n["prestige"]
    n["pts"] = 2 * n["prestige"]
    s.clear(); s.update(n)
    say(s, f"🔄 Перерождение №{s['prestige']}! Доход навсегда +{int((pm(s) - 1) * 100)}%")
    return {"msg": f"🔄 Перерождение! Постоянный бонус к доходу +{int((pm(s) - 1) * 100)}%"}

def a_daily(s, b, now):
    today = time.strftime("%Y-%m-%d", time.gmtime(now)); y = time.strftime("%Y-%m-%d", time.gmtime(now - 86400)); d = s["daily"]
    if d["day"] == today: raise GameError("Сегодня награда уже получена")
    d["streak"] = d["streak"] + 1 if d["day"] == y else 1; d["day"] = today
    g = 30 + 15 * min(d["streak"], 7); add(s, "gold", g); add(s, "seeds", 3)
    return {"msg": f"Серия {d['streak']} дн.: +{g}🪙 +3🌱"}

def a_event(s, b, now):
    if s["event"] or now - s["evt_t"] < 180 or random.random() > .5: return {}
    s["evt_t"] = now; i, t, x, d = random.choice(EV); r = s["res"]
    if i == "tax": r["gold"] -= int(r["gold"] * .08)
    elif i == "alien": r["food"] -= int(r["food"] * .1)
    elif i == "junk": add(s, "scrap", 10 + 5 * s["lvl"])
    elif i == "gold":
        if random.random() < .25: add(s, "crystal", 1)
        else: add(s, "gold", 50 * s["lvl"])
    elif i == "trader": add(s, random.choice(["wood", "stone", "seeds"]), 20 + 5 * s["lvl"])
    if d: s["event"] = {"id": i, "title": t, "text": x, "until": now + d}
    say(s, t + ": " + x)
    return {"msg": t + " — " + x}

ACTS = {"bootstrap": a_bootstrap, "state": a_state, "collect": a_collect, "plant": a_plant, "harvest": a_harvest,
        "build": a_build, "buy": a_buy, "attack": a_attack, "skill": a_skill, "pet": a_pet, "claim": a_claim,
        "daily": a_daily, "event": a_event, "biome": a_biome, "prestige": a_prestige}

# ---------------- авторизация, БД, HTTP ----------------
def verify(init):
    if not BOT_TOKEN or not init: raise ValueError("Открой игру через Telegram-бота")
    d = dict(parse_qsl(init, keep_blank_values=True)); h = d.pop("hash", None)
    if not h or "user" not in d: raise ValueError("Неверные данные Telegram")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h): raise ValueError("Подпись Telegram неверна")
    if time.time() - int(d.get("auth_date", "0")) > 86400: raise ValueError("Сессия устарела — открой игру заново")
    return json.loads(d["user"])

locks, RL = {}, {}

def load(uid, name):
    r = db.execute("SELECT state FROM players WHERE id=?", (uid,)).fetchone()
    s = migrate(json.loads(r[0]) if r else new_state()); s["name"] = name
    return s

def save(uid, s):
    score = s["prestige"] * 20000 + s["lvl"] * 1000 + sum(s["bld"].values()) * 50 + int(s["res"]["gold"] // 10)
    db.execute("INSERT INTO players(id,name,state,score,updated) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
               "name=excluded.name,state=excluded.state,score=excluded.score,updated=excluded.updated",
               (uid, s["name"], json.dumps(s, ensure_ascii=False), score, time.time()))

def endpoint(fn):
    async def h(req):
        try: u = verify(req.headers.get("X-Init-Data", ""))
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=401)
        uid = int(u["id"]); now = time.time()
        if now - RL.get(uid, 0) < .08: return web.json_response({"ok": False, "error": "Не так быстро!"}, status=429)
        RL[uid] = now
        try: body = await req.json()
        except Exception: body = {}
        if not isinstance(body, dict): body = {}
        async with locks.setdefault(uid, asyncio.Lock()):
            s = load(uid, (u.get("first_name") or "Боб")[:30]); off = tick(s, now); snap = json.dumps(s)
            try: out = fn(s, body, now) or {}
            except GameError as e:
                s = json.loads(snap); save(uid, s)
                return web.json_response({"ok": False, "error": str(e), "state": view(s, now)}, status=400)
            except Exception:
                log.exception("action failed"); return web.json_response({"ok": False, "error": "Ошибка сервера"}, status=500)
            save(uid, s)
        out.update(ok=True, state=view(s, now))
        if off: out["offline"] = off
        return web.json_response(out)
    return h

async def top(req):
    rows = db.execute("SELECT name,score FROM players ORDER BY score DESC LIMIT 10").fetchall()
    return web.json_response({"ok": True, "top": [{"name": n, "score": c} for n, c in rows]})

async def health(req): return web.json_response({"ok": True, "time": time.time()})
async def index(req): return web.FileResponse(os.path.join(HERE, "index.html"))

@web.middleware
async def cors(req, handler):
    try: r = web.Response() if req.method == "OPTIONS" else await handler(req)
    except web.HTTPException as e: r = e
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r

# ---------------- Telegram-бот (long polling) ----------------
HELP = ("🥔 «Жирный Боб: Последний огород»\nДобывай ресурсы, строй базу, выращивай овощи, бей мутантов и накапливай золото. "
        "Производство идёт даже когда ты спишь (до 8 часов).\n/profile — профиль\n/top — рейтинг\n/help — помощь")

async def tg(sess, method, **kw):
    async with sess.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=kw) as r: return await r.json()

def ensure(uid, name):
    if not db.execute("SELECT 1 FROM players WHERE id=?", (uid,)).fetchone():
        s = new_state(); s["name"] = name; save(uid, s)

async def handle(sess, u):
    cb = u.get("callback_query"); m = u.get("message")
    if cb:
        await tg(sess, "answerCallbackQuery", callback_query_id=cb["id"])
        uid, chat, text, name = cb["from"]["id"], cb["message"]["chat"]["id"], "/profile" if cb["data"] == "p" else "/help", cb["from"].get("first_name", "Боб")
    elif m and m.get("text"):
        uid, chat, text, name = m["from"]["id"], m["chat"]["id"], m["text"].split()[0].split("@")[0], m["from"].get("first_name", "Боб")
    else: return
    kb = {"inline_keyboard": [[{"text": "🎮 Играть", "web_app": {"url": WEBAPP_URL}}],
                              [{"text": "📊 Профиль", "callback_data": "p"}, {"text": "❓ Помощь", "callback_data": "h"}]]}
    if text == "/start":
        ensure(uid, name)
        await tg(sess, "sendMessage", chat_id=chat, text=f"Привет, {name}! 🥔\n\n{HELP}", reply_markup=kb)
    elif text == "/help": await tg(sess, "sendMessage", chat_id=chat, text=HELP, reply_markup=kb)
    elif text == "/profile":
        r = db.execute("SELECT state FROM players WHERE id=?", (uid,)).fetchone()
        if not r: await tg(sess, "sendMessage", chat_id=chat, text="Профиля нет. Нажми /start")
        else:
            s = migrate(json.loads(r[0]))
            await tg(sess, "sendMessage", chat_id=chat, reply_markup=kb, text=f"🧔 {s['name']}\n⭐ Уровень: {s['lvl']}\n🪙 Золото: {s['res']['gold']}\n🏠 Уровней построек: {sum(s['bld'].values())}\n⚔️ Побед: {s['cnt']['kills']}\n🔄 Перерождений: {s['prestige']}")
    elif text == "/top":
        rows = db.execute("SELECT name,score FROM players ORDER BY score DESC LIMIT 10").fetchall()
        await tg(sess, "sendMessage", chat_id=chat, text="🏆 Топ огородников:\n" + "\n".join(f"{i + 1}. {n} — {c}" for i, (n, c) in enumerate(rows)))

async def bot_loop(sess):
    off = 0
    try: await tg(sess, "deleteWebhook")
    except Exception: log.exception("deleteWebhook")
    while True:
        try:
            r = await tg(sess, "getUpdates", offset=off, timeout=25, allowed_updates=["message", "callback_query"])
            for u in r.get("result", []):
                off = u["update_id"] + 1
                try: await handle(sess, u)
                except Exception: log.exception("update failed")
        except asyncio.CancelledError: raise
        except Exception:
            log.exception("polling"); await asyncio.sleep(3)

async def on_start(app):
    app["sess"] = ClientSession(timeout=ClientTimeout(total=40))
    if BOT_TOKEN: app["bot"] = asyncio.create_task(bot_loop(app["sess"]))
    else: log.warning("BOT_TOKEN не задан: авторизация и бот не работают")

async def on_stop(app):
    if "bot" in app: app["bot"].cancel()
    await app["sess"].close(); db.close()

def make_app():
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", index); app.router.add_get("/health", health); app.router.add_get("/api/top", top)
    for name, fn in ACTS.items(): app.router.add_post("/api/" + name, endpoint(fn))
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda r: web.Response())
    app.on_startup.append(on_start); app.on_cleanup.append(on_stop)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
