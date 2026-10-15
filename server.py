"""UNO Online — авторитетный игровой сервер для Telegram Mini App.

FastAPI + WebSocket. Состояние комнат живёт в памяти процесса и снимками
сохраняется в SQLite (восстанавливается после перезапуска). Сервер рассчитан
на ОДИН экземпляр процесса (см. README, раздел «Масштабирование»).

Запуск:  python server.py   (или  uvicorn server:app --host 0.0.0.0 --port $PORT)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlparse

try:  # .env необязателен
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8000"))
DEV_MODE = env_bool("DEV_MODE", False)
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
DB_PATH = os.getenv("DB_PATH", "uno.db")
AUTH_MAX_AGE = int(os.getenv("AUTH_MAX_AGE", "86400"))  # срок годности initData, сек
RECONNECT_GRACE = int(os.getenv("RECONNECT_GRACE", "90"))  # сколько ждём вернувшегося игрока, сек
EMPTY_ROOM_TTL = int(os.getenv("EMPTY_ROOM_TTL", "300"))  # комната без подключённых людей живёт, сек
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "90"))  # нет сообщений (даже ping) — соединение закрывается
UNO_WINDOW = float(os.getenv("UNO_WINDOW", "4.0"))  # окно, в которое можно поймать «забывшего UNO»
MAX_MSG_BYTES = 4096
RATE_CAPACITY = 15.0
RATE_REFILL = 5.0  # токенов в секунду

HAND_SIZE = 7
UNO_PENALTY = 2
AFK_LIMIT = 3  # столько таймаутов подряд — игрок переводится на автопилот
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 5
TARGETS = (100, 200, 300, 500)
TURN_TIMES = (20, 30, 45, 60)
DIFFICULTIES = ("easy", "normal", "hard")
BOT_NAMES = ["Бот Макс", "Бот Лина", "Бот Артём", "Бот Зоя", "Бот Тимур", "Бот Ника"]
BOT_UNO_RATE = {"easy": 0.6, "normal": 0.85, "hard": 0.98}
BOT_CATCH_RATE = {"easy": 0.2, "normal": 0.5, "hard": 0.85}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("uno")


class GameError(Exception):
    """Ошибка, понятная игроку (код + текст на русском)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthError(Exception):
    pass


# ----------------------------------------------------------------------------
# Telegram: проверка initData
# ----------------------------------------------------------------------------


def validate_init_data(init_data: str, bot_token: str, max_age: int = AUTH_MAX_AGE, now: Optional[float] = None) -> dict:
    """Проверка подписи Telegram Web App по официальному алгоритму.

    secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash   = HMAC_SHA256(key=secret, msg=data_check_string)
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > 4096:
        raise AuthError("Некорректные данные авторизации")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise AuthError("Нет подписи Telegram")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise AuthError("Подпись Telegram не совпала")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise AuthError("Некорректная дата авторизации")
    now = time.time() if now is None else now
    if auth_date <= 0 or now - auth_date > max_age:
        raise AuthError("Данные авторизации устарели — перезапустите приложение")
    if auth_date - now > 300:
        raise AuthError("Некорректная дата авторизации")
    try:
        user = json.loads(pairs.get("user", ""))
        uid = int(user["id"])
    except Exception:
        raise AuthError("В данных нет пользователя")
    user["id"] = uid
    return user


def clean_text(value: Any, maxlen: int) -> str:
    if not isinstance(value, str):
        return ""
    out = "".join(ch for ch in value if ch.isprintable())
    return out.strip()[:maxlen]


def clean_photo(url: Any) -> Optional[str]:
    """Принимаем только аватары с доменов Telegram."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    try:
        u = urlparse(url)
    except Exception:
        return None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        return None
    if host in ("t.me", "telegram.org") or host.endswith(".t.me") or host.endswith(".telegram.org"):
        return url
    return None


def user_from_telegram(tg: dict) -> dict:
    name = clean_text(f"{tg.get('first_name', '')} {tg.get('last_name', '')}", 24)
    username = clean_text(tg.get("username", ""), 32)
    if not name:
        name = username or f"Игрок {tg['id']}"
    return {"id": int(tg["id"]), "name": name, "username": username, "photo": clean_photo(tg.get("photo_url"))}


# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------


class Database:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS players (
                    tg_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT,
                    photo TEXT,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    rounds INTEGER NOT NULL DEFAULT 0,
                    round_wins INTEGER NOT NULL DEFAULT 0,
                    points INTEGER NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    best_score INTEGER NOT NULL DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    code TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def upsert_player(self, tg_id: int, name: str, username: str, photo: Optional[str]) -> None:
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO players (tg_id, name, username, photo, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tg_id) DO UPDATE SET
                     name=excluded.name, username=excluded.username,
                     photo=excluded.photo, updated_at=excluded.updated_at""",
                (tg_id, name, username, photo, now, now),
            )

    def get_profile(self, tg_id: int) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM players WHERE tg_id=?", (tg_id,)).fetchone()
        if row is None:
            return {"name": "", "photo": None, "wins": 0, "losses": 0, "rounds": 0, "round_wins": 0,
                    "points": 0, "win_rate": 0, "streak": 0, "best_streak": 0, "best_score": 0}
        matches = row["wins"] + row["losses"]
        return {
            "name": row["name"], "photo": row["photo"], "wins": row["wins"], "losses": row["losses"],
            "rounds": row["rounds"], "round_wins": row["round_wins"], "points": row["points"],
            "win_rate": round(100 * row["wins"] / matches) if matches else 0,
            "streak": row["streak"], "best_streak": row["best_streak"], "best_score": row["best_score"],
        }

    def record_round(self, event_id: str, tg_id: int, won: bool, points: int) -> bool:
        """Идемпотентно: повторный event_id игнорируется."""
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO events (event_id, created_at) VALUES (?, ?)", (event_id, time.time()))
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "UPDATE players SET rounds=rounds+1, round_wins=round_wins+?, points=points+?, updated_at=? WHERE tg_id=?",
                (1 if won else 0, int(points), time.time(), tg_id),
            )
            return True

    def record_match(self, event_id: str, tg_id: int, won: bool, score: int) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO events (event_id, created_at) VALUES (?, ?)", (event_id, time.time()))
            if cur.rowcount != 1:
                return False
            if won:
                self.conn.execute(
                    """UPDATE players SET wins=wins+1, streak=streak+1,
                       best_streak=MAX(best_streak, streak+1), best_score=MAX(best_score, ?), updated_at=? WHERE tg_id=?""",
                    (int(score), time.time(), tg_id),
                )
            else:
                self.conn.execute(
                    "UPDATE players SET losses=losses+1, streak=0, best_score=MAX(best_score, ?), updated_at=? WHERE tg_id=?",
                    (int(score), time.time(), tg_id),
                )
            return True

    def leaderboard(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT tg_id, name, photo, wins, losses, points, rounds FROM players
                   WHERE wins > 0 OR rounds > 0
                   ORDER BY wins DESC, points DESC, rounds ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_events(self, older_than_sec: float) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM events WHERE created_at < ?", (time.time() - older_than_sec,))

    def save_room(self, code: str, data: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO rooms (code, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (code, data, time.time()),
            )

    def delete_room(self, code: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM rooms WHERE code=?", (code,))

    def load_rooms(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute("SELECT code, data FROM rooms").fetchall()
        return [(r["code"], r["data"]) for r in rows]


# ----------------------------------------------------------------------------
# Карты и колода
# ----------------------------------------------------------------------------

COLORS = ["R", "B", "G", "Y"]
COLOR_ORDER = {"R": 0, "Y": 1, "G": 2, "B": 3, "W": 4}
VALUE_ORDER = {**{str(i): i for i in range(10)}, "skip": 10, "reverse": 11, "draw2": 12, "wild": 13, "wild4": 14}
ACTION_VALUES = ("skip", "reverse", "draw2")


def build_deck() -> list[dict]:
    """Стандартная колода UNO: 108 карт."""
    cards: list[dict] = []
    cid = 0
    for color in COLORS:
        cards.append({"id": cid, "color": color, "value": "0"})
        cid += 1
        for value in [str(i) for i in range(1, 10)] + list(ACTION_VALUES):
            for _ in range(2):
                cards.append({"id": cid, "color": color, "value": value})
                cid += 1
    for value in ("wild", "wild4"):
        for _ in range(4):
            cards.append({"id": cid, "color": "W", "value": value})
            cid += 1
    return cards


def card_points(card: dict) -> int:
    v = card["value"]
    if v.isdigit():
        return int(v)
    if v in ACTION_VALUES:
        return 20
    return 50


def hand_points(hand: list[dict]) -> int:
    return sum(card_points(c) for c in hand)


# ----------------------------------------------------------------------------
# Игрок и комната (вся игровая логика — чистый Python без сети)
# ----------------------------------------------------------------------------


class Player:
    __slots__ = ("pid", "name", "photo", "bot", "difficulty", "hand", "connected", "disc_at", "auto",
                 "uno_called", "timeouts")

    def __init__(self, pid: str, name: str, photo: Optional[str] = None, bot: bool = False, difficulty: str = "normal"):
        self.pid = pid
        self.name = name
        self.photo = photo
        self.bot = bot
        self.difficulty = difficulty
        self.hand: list[dict] = []
        self.connected = True
        self.disc_at: Optional[float] = None
        self.auto = False
        self.uno_called = False
        self.timeouts = 0

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        p = cls(d["pid"], d["name"], d.get("photo"), d.get("bot", False), d.get("difficulty", "normal"))
        for s in cls.__slots__:
            if s in d:
                setattr(p, s, d[s])
        return p


PERSIST_FIELDS = (
    "uid", "pw_salt", "pw_hash", "status", "round_no", "series", "ranked", "scores", "deck", "discard", "color",
    "direction", "turn", "drawn", "pending", "pending_kind", "turn_deadline", "bot_at", "uno", "forfeit_points",
    "result", "created", "event_seq",
)


class Room:
    def __init__(self, code: str, name: str, owner: str, max_players: int = 4, public: bool = True,
                 allow_bots: bool = True, target_score: int = 200, stacking: bool = False, turn_time: int = 45):
        self.code = code
        self.uid = uuid.uuid4().hex[:12]
        self.name = name
        self.owner = owner
        self.max_players = max_players
        self.public = public
        self.allow_bots = allow_bots
        self.target_score = target_score
        self.stacking = stacking
        self.turn_time = turn_time
        self.pw_salt = ""
        self.pw_hash = ""
        self.players: list[Player] = []
        self.banned: set[str] = set()
        self.status = "lobby"  # lobby | playing | round_over | match_over
        self.round_no = 0
        self.series = 0
        self.ranked = False
        self.scores: dict[str, int] = {}
        # состояние раунда
        self.deck: list[dict] = []
        self.discard: list[dict] = []
        self.color = "R"
        self.direction = 1
        self.turn = 0
        self.drawn: Optional[int] = None
        self.pending = 0
        self.pending_kind: Optional[str] = None
        self.turn_deadline: Optional[float] = None
        self.bot_at: Optional[float] = None
        self.uno: Optional[dict] = None
        self.forfeit_points = 0
        self.result: Optional[dict] = None
        # служебное (не сохраняется)
        self.created = time.time()
        self.empty_since: Optional[float] = None
        self.event_seq = 0
        self.events: list[dict] = []
        self.pending_stats: list[dict] = []
        self.lock = asyncio.Lock()

    # ---- пароль ----------------------------------------------------------
    def set_password(self, password: str) -> None:
        self.pw_salt = secrets.token_hex(8)
        self.pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), self.pw_salt.encode(), 20000).hex()

    def check_password(self, password: str) -> bool:
        if not self.pw_hash:
            return True
        calc = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), self.pw_salt.encode(), 20000).hex()
        return hmac.compare_digest(calc, self.pw_hash)

    # ---- утилиты -----------------------------------------------------------
    def get(self, pid: str) -> Optional[Player]:
        for p in self.players:
            if p.pid == pid:
                return p
        return None

    def index_of(self, pid: str) -> int:
        for i, p in enumerate(self.players):
            if p.pid == pid:
                return i
        return -1

    def current(self) -> Player:
        return self.players[self.turn]

    def _ev(self, t: str, **kw: Any) -> None:
        self.event_seq += 1
        self.events.append({"eid": self.event_seq, "t": t, **kw})

    def _eid(self, kind: str, pid: str) -> str:
        if kind == "r":
            return f"{self.uid}:s{self.series}:r{self.round_no}:{pid}"
        return f"{self.uid}:s{self.series}:m:{pid}"

    # ---- лобби -----------------------------------------------------------
    def add_player(self, p: Player) -> None:
        if self.status != "lobby":
            raise GameError("in_progress", "Матч уже начался")
        if len(self.players) >= self.max_players:
            raise GameError("room_full", "Комната заполнена")
        self.players.append(p)
        self.scores.setdefault(p.pid, 0)

    def add_bot(self, difficulty: str = "normal") -> Player:
        if difficulty not in DIFFICULTIES:
            difficulty = "normal"
        used = {p.name for p in self.players}
        name = next((n for n in BOT_NAMES if n not in used), f"Бот {len(self.players) + 1}")
        bot = Player("bot:" + secrets.token_hex(3), name, None, True, difficulty)
        self.add_player(bot)
        return bot

    def reset_to_lobby(self) -> None:
        self.status = "lobby"
        self.round_no = 0
        self.scores = {p.pid: 0 for p in self.players}
        self.deck, self.discard = [], []
        self.pending, self.pending_kind = 0, None
        self.drawn = None
        self.uno = None
        self.result = None
        self.turn_deadline = None
        self.bot_at = None
        self.forfeit_points = 0
        for p in self.players:
            p.hand = []
            p.uno_called = False
            p.timeouts = 0

    # ---- старт ---------------------------------------------------------------
    def start_series(self, now: float) -> None:
        if len(self.players) < 2:
            raise GameError("need_players", "Нужно минимум 2 игрока — пригласите друга или добавьте бота")
        self.series += 1
        self.round_no = 0
        self.scores = {p.pid: 0 for p in self.players}
        self.ranked = sum(1 for p in self.players if not p.bot) >= 2
        self._start_round(now)

    def next_round(self, now: float) -> None:
        if self.status == "round_over":
            self._start_round(now)
        elif self.status == "match_over":
            self.start_series(now)  # реванш: новая серия
        else:
            raise GameError("bad_state", "Сейчас нельзя начать следующий раунд")

    def _start_round(self, now: float) -> None:
        self.round_no += 1
        self.deck = build_deck()
        random.shuffle(self.deck)
        for p in self.players:
            p.hand = []
            p.uno_called = False
            p.timeouts = 0
            if p.connected:
                p.auto = False
        for _ in range(HAND_SIZE):
            for p in self.players:
                p.hand.append(self.deck.pop())
        # стартовая карта — всегда числовая (упрощение, описано в README)
        while True:
            c = self.deck.pop()
            if c["value"].isdigit():
                break
            self.deck.insert(0, c)
        self.discard = [c]
        self.color = c["color"]
        self.direction = 1
        self.pending, self.pending_kind = 0, None
        self.forfeit_points = 0
        self.result = None
        self.uno = None
        self.status = "playing"
        self.turn = (self.round_no - 1) % len(self.players)
        self._ev("deal", round=self.round_no)
        self._begin_turn(now)

    # ---- ход ---------------------------------------------------------------
    def _begin_turn(self, now: float) -> None:
        self.drawn = None
        self.turn_deadline = now + self.turn_time if self.turn_time > 0 else None
        self._schedule_bot(now)

    def _schedule_bot(self, now: float) -> None:
        p = self.current()
        self.bot_at = now + random.uniform(0.9, 1.8) if (p.bot or p.auto) else None

    def _advance(self, steps: int, now: float) -> None:
        self.turn = (self.turn + self.direction * steps) % len(self.players)
        self._begin_turn(now)

    def _next_index(self) -> int:
        return (self.turn + self.direction) % len(self.players)

    def _check_turn(self, pid: str) -> None:
        if self.status != "playing":
            raise GameError("not_playing", "Раунд сейчас не идёт")
        if self.players[self.turn].pid != pid:
            raise GameError("not_your_turn", "Сейчас не ваш ход")

    def draw_cards(self, n: int) -> list[dict]:
        out: list[dict] = []
        for _ in range(n):
            if not self.deck:
                if len(self.discard) > 1:
                    top = self.discard.pop()
                    self.deck = self.discard[:]
                    random.shuffle(self.deck)
                    self.discard = [top]
                    self._ev("reshuffle")
                else:
                    break
            out.append(self.deck.pop())
        return out

    def _give(self, p: Player, n: int) -> int:
        cards = self.draw_cards(n)
        p.hand.extend(cards)
        p.uno_called = False
        return len(cards)

    def is_playable(self, p: Player, c: dict) -> bool:
        if self.pending:  # режим «наложения»: можно только ответить такой же картой
            return c["value"] == self.pending_kind
        if c["color"] == "W":
            if c["value"] == "wild4":
                # официальное правило: только если нет карты текущего цвета
                return not any(x["color"] == self.color for x in p.hand)
            return True
        return c["color"] == self.color or c["value"] == self.discard[-1]["value"]

    def playable_ids(self, pid: str) -> list[int]:
        if self.status != "playing" or self.players[self.turn].pid != pid:
            return []
        p = self.players[self.turn]
        ids = []
        for c in p.hand:
            if self.drawn is not None and c["id"] != self.drawn:
                continue
            if self.is_playable(p, c):
                ids.append(c["id"])
        return ids

    def _illegal_reason(self, c: dict) -> str:
        if self.pending:
            return "Нужно ответить такой же картой «+» или взять штрафные карты"
        if c["value"] == "wild4":
            return "«Взять четыре» можно сыграть, только если нет карты текущего цвета"
        return "Эту карту нельзя сыграть: нужен тот же цвет, значение или символ"

    def play_card(self, pid: str, card_id: int, color: Optional[str], now: float) -> None:
        self._check_turn(pid)
        p = self.current()
        card = next((c for c in p.hand if c["id"] == card_id), None)
        if card is None:
            raise GameError("no_card", "У вас нет такой карты")
        if self.drawn is not None and card_id != self.drawn:
            raise GameError("must_play_drawn", "После добора можно сыграть только взятую карту или пропустить ход")
        if not self.is_playable(p, card):
            raise GameError("illegal_move", self._illegal_reason(card))
        if card["color"] == "W" and color not in COLORS:
            raise GameError("color_required", "Выберите цвет")
        self._do_play(p, card, color, now)

    def _do_play(self, p: Player, card: dict, color: Optional[str], now: float) -> None:
        p.hand.remove(card)
        self.discard.append(card)
        self.color = color if card["color"] == "W" else card["color"]
        p.timeouts = 0
        self.uno = None
        self.drawn = None
        self._ev("play", pid=p.pid, card=card, color=self.color)
        if card["color"] == "W":
            self._ev("color", pid=p.pid, color=self.color)
        left = len(p.hand)
        v = card["value"]
        n = len(self.players)
        if left == 0:
            # штраф последней карты («+2»/«+4») применяется до подсчёта очков
            if v in ("draw2", "wild4"):
                victim = self.players[self._next_index()]
                cnt = self._give(victim, 2 if v == "draw2" else 4)
                self._ev("penalty", pid=victim.pid, count=cnt)
            self._end_round(p, now)
            return
        if left == 1:
            if (p.bot or p.auto) and random.random() < BOT_UNO_RATE.get(p.difficulty, 0.85):
                p.uno_called = True
                self._ev("uno", pid=p.pid)
            if not p.uno_called:
                self._open_uno_window(p, now)
        elif left >= 3:
            p.uno_called = False
        # эффекты карты
        if v == "reverse":
            self.direction *= -1
            self._ev("reverse", direction=self.direction)
            if n == 2:  # при двух игроках «Смена направления» работает как «Пропуск»
                self._ev("skip", pid=self.players[self._next_index()].pid)
                self._advance(0, now)
            else:
                self._advance(1, now)
        elif v == "skip":
            self._ev("skip", pid=self.players[self._next_index()].pid)
            self._advance(2, now)
        elif v in ("draw2", "wild4"):
            amount = 2 if v == "draw2" else 4
            if self.stacking:
                self.pending += amount
                self.pending_kind = v
                self._advance(1, now)
            else:
                victim = self.players[self._next_index()]
                cnt = self._give(victim, amount)
                self._ev("penalty", pid=victim.pid, count=cnt)
                self._advance(2, now)
        else:
            self._advance(1, now)

    def _open_uno_window(self, p: Player, now: float) -> None:
        catcher = None
        bots = [b for b in self.players if b.bot and b.pid != p.pid]
        if bots and not p.bot:
            b = random.choice(bots)
            if random.random() < BOT_CATCH_RATE.get(b.difficulty, 0.5):
                catcher = b.pid
        self.uno = {"pid": p.pid, "until": now + UNO_WINDOW, "catcher": catcher,
                    "catch_at": now + random.uniform(1.0, 2.5)}

    def draw_card(self, pid: str, now: float) -> None:
        self._check_turn(pid)
        p = self.current()
        if self.drawn is not None:
            raise GameError("already_drawn", "Вы уже взяли карту: сыграйте её или пропустите ход")
        self.uno = None
        p.timeouts = 0
        if self.pending:
            n, self.pending, self.pending_kind = self.pending, 0, None
            cnt = self._give(p, n)
            self._ev("penalty", pid=pid, count=cnt)
            self._advance(1, now)
            return
        cards = self.draw_cards(1)
        if not cards:  # колода и сброс исчерпаны
            self._ev("empty")
            self._advance(1, now)
            return
        c = cards[0]
        p.hand.append(c)
        p.uno_called = False
        self._ev("draw", pid=pid, count=1)
        if self.is_playable(p, c):
            self.drawn = c["id"]
            self._schedule_bot(now)
            if self.turn_deadline is not None:
                self.turn_deadline = max(self.turn_deadline, now + 10)
        else:
            self._advance(1, now)

    def pass_turn(self, pid: str, now: float) -> None:
        self._check_turn(pid)
        if self.drawn is None:
            raise GameError("cannot_pass", "Сначала возьмите карту из колоды")
        self.uno = None
        self._ev("pass", pid=pid)
        self._advance(1, now)

    def call_uno(self, pid: str) -> None:
        p = self.get(pid)
        if p is None or self.status != "playing":
            raise GameError("uno_not_allowed", "Сейчас нельзя объявить UNO")
        n = len(p.hand)
        is_turn = self.players[self.turn].pid == pid
        if not (n == 1 or (n == 2 and is_turn)):
            raise GameError("uno_not_allowed", "UNO можно объявить, когда осталась одна карта (или две — перед ходом)")
        p.uno_called = True
        if self.uno and self.uno["pid"] == pid:
            self.uno = None
        self._ev("uno", pid=pid)

    def catch_uno(self, by: str, target: Optional[str], now: float) -> None:
        w = self.uno
        if self.status != "playing" or not w or now > w["until"]:
            raise GameError("nothing_to_catch", "Поймать некого: время вышло")
        target = target or w["pid"]
        t = self.get(target)
        if t is None or target != w["pid"] or by == target:
            raise GameError("nothing_to_catch", "Поймать некого")
        if len(t.hand) != 1 or t.uno_called:
            raise GameError("nothing_to_catch", "Игрок уже объявил UNO")
        self.uno = None
        cnt = self._give(t, UNO_PENALTY)
        self._ev("caught", pid=by, target=target)
        self._ev("penalty", pid=target, count=cnt)

    def timeout_action(self, now: float) -> None:
        p = self.current()
        prev = p.timeouts
        self._ev("timeout", pid=p.pid)
        if self.drawn is None:
            self.draw_card(p.pid, now)
        if self.status == "playing" and self.current() is p and self.drawn is not None:
            self.pass_turn(p.pid, now)
        p.timeouts = prev + 1
        if p.timeouts >= AFK_LIMIT and not p.bot and not p.auto:
            p.auto = True
            self._ev("auto", pid=p.pid)

    # ---- боты --------------------------------------------------------------
    def bot_move(self, now: float) -> None:
        p = self.current()
        diff = p.difficulty if p.bot else "normal"
        ids = set(self.playable_ids(p.pid))
        cards = [c for c in p.hand if c["id"] in ids]
        if not cards:
            if self.drawn is None:
                self.draw_card(p.pid, now)
            else:
                self.pass_turn(p.pid, now)
            return
        best = max(cards, key=lambda c: self._bot_score(p, c, diff))
        color = self._bot_color(p, best, diff) if best["color"] == "W" else None
        self._do_play(p, best, color, now)

    def _bot_score(self, p: Player, c: dict, diff: str) -> float:
        s = random.random() * (3.0 if diff == "easy" else 0.5)
        if diff == "easy":
            return s
        nxt = self.players[self._next_index()]
        threat = len(nxt.hand) <= 2
        v = c["value"]
        if c["color"] == "W":
            s -= 4
            if len(p.hand) <= 2:
                s += 6
            if threat and v == "wild4":
                s += 8
        elif v in ACTION_VALUES:
            s += 3 if threat else 1.5
        else:
            s += int(v) * 0.1
        if diff == "hard":
            s += 0.4 * sum(1 for x in p.hand if x["color"] == c["color"] and x is not c)
        return s

    def _bot_color(self, p: Player, c: dict, diff: str) -> str:
        if diff == "easy":
            return random.choice(COLORS)
        counts = {k: 0 for k in COLORS}
        for x in p.hand:
            if x is not c and x["color"] in counts:
                counts[x["color"]] += 1
        top = max(counts.values())
        return random.choice([k for k, n in counts.items() if n == top])

    # ---- конец раунда ------------------------------------------------------
    def _end_round(self, winner: Player, now: float, forced: bool = False) -> None:
        pts = self.forfeit_points + sum(hand_points(x.hand) for x in self.players if x is not winner)
        self.scores[winner.pid] = self.scores.get(winner.pid, 0) + pts
        match_over = forced or self.scores[winner.pid] >= self.target_score
        self.status = "match_over" if match_over else "round_over"
        self.turn_deadline = None
        self.bot_at = None
        self.uno = None
        self.drawn = None
        self.pending, self.pending_kind = 0, None
        self.result = {
            "winner": winner.pid,
            "winner_name": winner.name,
            "points": pts,
            "match_over": match_over,
            "match_winner": winner.pid if match_over else None,
            "target": self.target_score,
            "players": [
                {"id": x.pid, "name": x.name, "photo": x.photo, "bot": x.bot, "cards": len(x.hand),
                 "hand_points": hand_points(x.hand), "score": self.scores.get(x.pid, 0)}
                for x in self.players
            ],
        }
        self._ev("round_end", winner=winner.pid, points=pts, match_over=match_over)
        if self.ranked:
            for x in self.players:
                if x.bot:
                    continue
                if not forced:
                    self.pending_stats.append({"kind": "round", "event_id": self._eid("r", x.pid), "pid": x.pid,
                                               "won": x is winner, "points": pts if x is winner else 0})
                if match_over:
                    self.pending_stats.append({"kind": "match", "event_id": self._eid("m", x.pid), "pid": x.pid,
                                               "won": x is winner, "score": self.scores.get(x.pid, 0)})

    # ---- выход игрока ------------------------------------------------------
    def remove_player(self, pid: str, now: float) -> bool:
        """Убирает игрока из комнаты. Возвращает True, если комнату нужно закрыть."""
        idx = self.index_of(pid)
        if idx < 0:
            return False
        p = self.players[idx]
        if self.ranked and not p.bot and self.status in ("playing", "round_over"):
            self.pending_stats.append({"kind": "match", "event_id": self._eid("m", pid), "pid": pid,
                                       "won": False, "score": self.scores.get(pid, 0)})
        was_current = self.status == "playing" and idx == self.turn
        if self.status == "playing":
            self.forfeit_points += hand_points(p.hand)
            self.deck.extend(p.hand)
            random.shuffle(self.deck)
            if self.uno and self.uno["pid"] == pid:
                self.uno = None
        self.players.pop(idx)
        self.scores.pop(pid, None)
        self._ev("left", pid=pid, name=p.name)
        humans = [x for x in self.players if not x.bot]
        if not humans:
            return True
        if self.owner == pid:
            self.owner = next((x for x in humans if x.connected), humans[0]).pid
        n = len(self.players)
        if self.status == "playing":
            if n < 2:
                self._end_round(self.players[0], now, forced=True)
            else:
                if idx < self.turn:
                    self.turn -= 1
                elif was_current:
                    self.turn = idx % n if self.direction == 1 else (idx - 1) % n
                    self._begin_turn(now)
                self.turn %= n
        elif self.status in ("round_over", "match_over") and n < 2:
            self.reset_to_lobby()
        return False

    # ---- представление для клиента -------------------------------------------
    def _pview(self, p: Player) -> dict:
        return {
            "id": p.pid, "name": p.name, "photo": p.photo, "bot": p.bot,
            "difficulty": p.difficulty if p.bot else None,
            "connected": p.connected, "auto": p.auto,
            "cards": len(p.hand) if self.status != "lobby" else 0,
            "score": self.scores.get(p.pid, 0),
            "uno": bool(p.uno_called and len(p.hand) == 1),
            "owner": p.pid == self.owner,
        }

    def view(self, pid: str, with_events: bool = True) -> dict:
        """Персональное состояние: чужие карты НИКОГДА не попадают в ответ."""
        d: dict[str, Any] = {
            "code": self.code, "name": self.name, "owner": self.owner, "public": self.public,
            "has_password": bool(self.pw_hash), "max_players": self.max_players,
            "allow_bots": self.allow_bots, "target_score": self.target_score,
            "stacking": self.stacking, "turn_time": self.turn_time,
            "status": self.status, "round_no": self.round_no, "ranked": self.ranked,
            "players": [self._pview(p) for p in self.players],
            "you": pid, "server_time": time.time(),
            "events": list(self.events) if with_events else [],
        }
        me = self.get(pid)
        if self.status != "lobby" and self.discard and me is not None:
            playing = self.status == "playing"
            my_turn = playing and self.players[self.turn].pid == pid
            u = self.uno
            hand = sorted(me.hand, key=lambda c: (COLOR_ORDER[c["color"]], VALUE_ORDER[c["value"]], c["id"]))
            d["game"] = {
                "top": self.discard[-1], "color": self.color, "direction": self.direction,
                "turn": self.players[self.turn].pid if playing else None,
                "deck": len(self.deck), "pending": self.pending, "pending_kind": self.pending_kind,
                "turn_deadline": self.turn_deadline if playing else None,
                "uno": {"pid": u["pid"], "until": u["until"]} if (u and playing) else None,
                "hand": hand,
                "playable": self.playable_ids(pid),
                "can_draw": bool(my_turn and self.drawn is None),
                "can_pass": bool(my_turn and self.drawn is not None),
                "drawn": self.drawn if my_turn else None,
                "result": self.result,
            }
        return d

    def public_info(self) -> dict:
        owner = self.get(self.owner)
        return {"code": self.code, "name": self.name, "players": len(self.players), "max_players": self.max_players,
                "status": self.status, "has_password": bool(self.pw_hash), "allow_bots": self.allow_bots,
                "target_score": self.target_score, "owner_name": owner.name if owner else ""}

    # ---- сериализация ------------------------------------------------------
    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in PERSIST_FIELDS}
        d.update({"code": self.code, "name": self.name, "owner": self.owner, "max_players": self.max_players,
                  "public": self.public, "allow_bots": self.allow_bots, "target_score": self.target_score,
                  "stacking": self.stacking, "turn_time": self.turn_time,
                  "players": [p.to_dict() for p in self.players], "banned": sorted(self.banned)})
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Room":
        r = cls(d["code"], d["name"], d["owner"], d["max_players"], d["public"], d["allow_bots"],
                d["target_score"], d["stacking"], d["turn_time"])
        for k in PERSIST_FIELDS:
            if k in d:
                setattr(r, k, d[k])
        r.players = [Player.from_dict(x) for x in d["players"]]
        r.banned = set(d.get("banned", []))
        return r


# ----------------------------------------------------------------------------
# Сетевой слой
# ----------------------------------------------------------------------------

db: Optional[Database] = None
rooms: dict[str, Room] = {}
conns: dict[str, "Conn"] = {}
player_room: dict[str, str] = {}
pw_fails: dict[tuple[str, str], tuple[int, float]] = {}


class Conn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.pid: Optional[str] = None
        self.user: Optional[dict] = None
        self.tokens = RATE_CAPACITY
        self.last = time.monotonic()
        self.seen: "OrderedDict[str, dict]" = OrderedDict()
        self.strikes = 0
        self.closed = False

    def take_token(self) -> bool:
        now = time.monotonic()
        self.tokens = min(RATE_CAPACITY, self.tokens + (now - self.last) * RATE_REFILL)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


async def send(conn: Conn, obj: dict) -> None:
    if conn.closed:
        return
    try:
        await conn.ws.send_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        conn.closed = True


async def flush_stats(room: Room) -> None:
    items, room.pending_stats = room.pending_stats, []
    touched: set[str] = set()
    for it in items:
        try:
            tg = int(it["pid"])
        except ValueError:
            continue
        if it["kind"] == "round":
            ok = db.record_round(it["event_id"], tg, it["won"], it["points"])
        else:
            ok = db.record_match(it["event_id"], tg, it["won"], it["score"])
        if ok:
            touched.add(it["pid"])
    for pid in touched:
        c = conns.get(pid)
        if c:
            await send(c, {"type": "profile", "profile": db.get_profile(int(pid))})


async def commit(room: Room) -> None:
    """Сохранить статистику, разослать состояние, записать снимок комнаты."""
    await flush_stats(room)
    for p in list(room.players):
        if p.bot or not p.connected:
            continue
        c = conns.get(p.pid)
        if c:
            await send(c, {"type": "room_state", "room": room.view(p.pid)})
    room.events.clear()
    try:
        db.save_room(room.code, json.dumps(room.to_dict(), ensure_ascii=False))
    except Exception:
        log.exception("save_room failed")


async def close_room(room: Room, reason: str) -> None:
    rooms.pop(room.code, None)
    for p in room.players:
        if p.bot:
            continue
        if player_room.get(p.pid) == room.code:
            player_room.pop(p.pid, None)
        c = conns.get(p.pid)
        if c:
            await send(c, {"type": "left_room", "reason": reason})
    try:
        db.delete_room(room.code)
    except Exception:
        log.exception("delete_room failed")


async def remove_from_room(room: Room, pid: str, reason: str) -> None:
    """Убрать игрока (выход, кик, тайм-аут). Вызывать под room.lock."""
    now = time.time()
    closed = room.remove_player(pid, now)
    if player_room.get(pid) == room.code:
        player_room.pop(pid, None)
    c = conns.get(pid)
    if c:
        await send(c, {"type": "left_room", "reason": reason})
    if closed:
        await close_room(room, "closed")
    else:
        await commit(room)


def new_code() -> str:
    for _ in range(200):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in rooms:
            return code
    raise GameError("busy", "Не удалось создать комнату, попробуйте ещё раз")


def my_room(conn: Conn) -> Room:
    room = rooms.get(player_room.get(conn.pid or "", ""))
    if room is None:
        raise GameError("no_room", "Вы не находитесь в комнате")
    return room


def ensure_not_in_room(pid: str) -> None:
    code = player_room.get(pid)
    if code and code in rooms:
        raise GameError("already_in_room", f"Вы уже в комнате {code}. Сначала выйдите из неё.")
    player_room.pop(pid, None)


# --- разбор полей входящих сообщений ---------------------------------------


def get_str(msg: dict, key: str, maxlen: int, default: str = "") -> str:
    v = msg.get(key, default)
    if v is None:
        return default
    if not isinstance(v, str):
        raise GameError("bad_request", f"Поле «{key}» должно быть строкой")
    v = v.strip()
    if len(v) > maxlen:
        raise GameError("bad_request", f"Поле «{key}» слишком длинное")
    return v


def get_int(msg: dict, key: str, default: Optional[int], lo: int, hi: int) -> Optional[int]:
    v = msg.get(key, default)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise GameError("bad_request", f"Поле «{key}» должно быть числом")
    if v < lo or v > hi:
        raise GameError("bad_request", f"Поле «{key}» вне допустимых значений")
    return v


def get_bool(msg: dict, key: str, default: bool) -> bool:
    v = msg.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise GameError("bad_request", f"Поле «{key}» должно быть true/false")
    return v


def require_owner(room: Room, pid: str) -> None:
    if room.owner != pid:
        raise GameError("not_owner", "Это может сделать только владелец комнаты")


# --- обработчики -----------------------------------------------------------


async def h_ping(conn: Conn, msg: dict) -> None:
    await send(conn, {"type": "pong", "server_time": time.time()})


async def h_get_rooms(conn: Conn, msg: dict) -> None:
    lst = [r.public_info() for r in rooms.values() if r.public]
    lst.sort(key=lambda r: (r["status"] != "lobby", -r["players"]))
    await send(conn, {"type": "rooms", "rooms": lst[:50]})


async def h_get_stats(conn: Conn, msg: dict) -> None:
    board = [{"rank": i + 1, "name": r["name"], "photo": r["photo"], "wins": r["wins"], "losses": r["losses"],
              "points": r["points"], "rounds": r["rounds"], "you": str(r["tg_id"]) == conn.pid}
             for i, r in enumerate(db.leaderboard(20))]
    await send(conn, {"type": "stats", "profile": db.get_profile(int(conn.pid)), "leaderboard": board})


async def h_create_room(conn: Conn, msg: dict) -> None:
    pid = conn.pid
    ensure_not_in_room(pid)
    training = get_bool(msg, "training", False)
    name = get_str(msg, "name", 24)
    max_players = get_int(msg, "max_players", 4, 2, 4)
    public = get_bool(msg, "public", True)
    password = get_str(msg, "password", 16)
    allow_bots = get_bool(msg, "allow_bots", True)
    target = get_int(msg, "target_score", 200, 100, 500)
    stacking = get_bool(msg, "stacking", False)
    turn_time = get_int(msg, "turn_time", 45, 20, 60)
    difficulty = get_str(msg, "difficulty", 12, "normal")
    if target not in TARGETS:
        raise GameError("bad_request", "Недопустимое количество очков для победы")
    if turn_time not in TURN_TIMES:
        raise GameError("bad_request", "Недопустимое время хода")
    if difficulty not in DIFFICULTIES:
        raise GameError("bad_request", "Недопустимая сложность ботов")
    if password and not public and len(password) < 3:
        raise GameError("bad_request", "Пароль должен быть не короче 3 символов")
    bot_count = 0
    if training:
        bot_count = get_int(msg, "bot_count", 2, 1, 3)
        max_players, public, allow_bots, password = bot_count + 1, False, True, ""
    code = new_code()
    room = Room(code, name or f"Комната {code}", pid, max_players, public, allow_bots, target, stacking, turn_time)
    if password:
        room.set_password(password)
    owner = Player(pid, conn.user["name"], conn.user.get("photo"))
    async with room.lock:
        room.add_player(owner)
        rooms[code] = room
        player_room[pid] = code
        if training:
            for _ in range(bot_count):
                room.add_bot(difficulty)
            room.start_series(time.time())
        await commit(room)


def check_pw_attempts(pid: str, code: str) -> None:
    n, until = pw_fails.get((pid, code), (0, 0.0))
    if n >= 5 and time.time() < until:
        raise GameError("too_many_attempts", "Слишком много неверных паролей. Попробуйте через минуту.")


async def join_room_impl(conn: Conn, code: str, password: str) -> None:
    pid = conn.pid
    code = code.strip().upper()
    room = rooms.get(code)
    if room is None:
        raise GameError("room_not_found", "Комната с таким кодом не найдена")
    async with room.lock:
        if rooms.get(code) is not room:
            raise GameError("room_not_found", "Комната закрыта")
        p = room.get(pid)
        if p is not None:  # возвращение в свою комнату
            p.connected, p.disc_at = True, None
            await commit(room)
            return
        if pid in room.banned:
            raise GameError("banned", "Владелец исключил вас из этой комнаты")
        ensure_not_in_room(pid)
        if room.status != "lobby":
            raise GameError("in_progress", "Матч уже идёт — присоединиться нельзя")
        if len(room.players) >= room.max_players:
            raise GameError("room_full", "Комната заполнена")
        check_pw_attempts(pid, code)
        if not room.check_password(password):
            n, _ = pw_fails.get((pid, code), (0, 0.0))
            pw_fails[(pid, code)] = (n + 1, time.time() + 60)
            raise GameError("bad_password", "Неверный пароль")
        pw_fails.pop((pid, code), None)
        room.add_player(Player(pid, conn.user["name"], conn.user.get("photo")))
        player_room[pid] = code
        room._ev("joined", pid=pid, name=conn.user["name"])
        await commit(room)


async def h_join_room(conn: Conn, msg: dict) -> None:
    code = get_str(msg, "code", 8)
    if not code:
        raise GameError("bad_request", "Введите код комнаты")
    await join_room_impl(conn, code, get_str(msg, "password", 16))


async def h_quick_play(conn: Conn, msg: dict) -> None:
    ensure_not_in_room(conn.pid)
    cands = [r for r in rooms.values()
             if r.public and r.status == "lobby" and not r.pw_hash
             and len(r.players) < r.max_players and conn.pid not in r.banned]
    if not cands:
        await send(conn, {"type": "quick_none"})
        return
    room = max(cands, key=lambda r: len(r.players))
    await join_room_impl(conn, room.code, "")


async def h_leave_room(conn: Conn, msg: dict) -> None:
    room = my_room(conn)
    async with room.lock:
        if rooms.get(room.code) is not room or room.get(conn.pid) is None:
            raise GameError("no_room", "Вы не находитесь в комнате")
        await remove_from_room(room, conn.pid, "left")


async def h_request_state(conn: Conn, msg: dict) -> None:
    room = rooms.get(player_room.get(conn.pid, ""))
    if room is None or room.get(conn.pid) is None:
        await send(conn, {"type": "left_room", "reason": "none"})
        return
    await send(conn, {"type": "room_state", "room": room.view(conn.pid, with_events=False)})


async def room_action(conn: Conn, fn: Callable[[Room, float], None], owner_only: bool = False,
                      human_action: bool = False) -> None:
    room = my_room(conn)
    async with room.lock:
        if rooms.get(room.code) is not room or room.get(conn.pid) is None:
            raise GameError("no_room", "Вы не находитесь в комнате")
        if owner_only:
            require_owner(room, conn.pid)
        me = room.get(conn.pid)
        if human_action and me.auto:
            me.auto = False  # игрок вернулся к управлению
        fn(room, time.time())
        await commit(room)


async def h_start_game(conn: Conn, msg: dict) -> None:
    def fn(room: Room, now: float) -> None:
        if room.status != "lobby":
            raise GameError("bad_state", "Матч уже запущен")
        room.start_series(now)
    await room_action(conn, fn, owner_only=True)


async def h_play_card(conn: Conn, msg: dict) -> None:
    cid = get_int(msg, "card_id", None, 0, 107)
    if cid is None:
        raise GameError("bad_request", "Не указана карта")
    color = msg.get("color")
    if color is not None and color not in COLORS:
        raise GameError("bad_request", "Недопустимый цвет")
    await room_action(conn, lambda room, now: room.play_card(conn.pid, cid, color, now), human_action=True)


async def h_draw_card(conn: Conn, msg: dict) -> None:
    await room_action(conn, lambda room, now: room.draw_card(conn.pid, now), human_action=True)


async def h_pass_turn(conn: Conn, msg: dict) -> None:
    await room_action(conn, lambda room, now: room.pass_turn(conn.pid, now), human_action=True)


async def h_call_uno(conn: Conn, msg: dict) -> None:
    await room_action(conn, lambda room, now: room.call_uno(conn.pid), human_action=True)


async def h_catch_uno(conn: Conn, msg: dict) -> None:
    target = get_str(msg, "target", 40) or None
    await room_action(conn, lambda room, now: room.catch_uno(conn.pid, target, now), human_action=True)


async def h_next_round(conn: Conn, msg: dict) -> None:
    await room_action(conn, lambda room, now: room.next_round(now), owner_only=True)


async def h_return_to_lobby(conn: Conn, msg: dict) -> None:
    def fn(room: Room, now: float) -> None:
        if room.status not in ("round_over", "match_over"):
            raise GameError("bad_state", "Вернуться в лобби можно после окончания раунда")
        room.reset_to_lobby()
    await room_action(conn, fn, owner_only=True)


async def h_add_bot(conn: Conn, msg: dict) -> None:
    diff = get_str(msg, "difficulty", 12, "normal")
    if diff not in DIFFICULTIES:
        raise GameError("bad_request", "Недопустимая сложность бота")

    def fn(room: Room, now: float) -> None:
        if not room.allow_bots:
            raise GameError("bots_disabled", "В этой комнате боты запрещены")
        room.add_bot(diff)
    await room_action(conn, fn, owner_only=True)


async def h_kick(conn: Conn, msg: dict) -> None:
    target = get_str(msg, "target", 40)
    room = my_room(conn)
    async with room.lock:
        if rooms.get(room.code) is not room:
            raise GameError("no_room", "Комната закрыта")
        require_owner(room, conn.pid)
        if room.status != "lobby":
            raise GameError("bad_state", "Исключать можно только до начала матча")
        t = room.get(target)
        if t is None or target == conn.pid:
            raise GameError("bad_request", "Нельзя исключить этого игрока")
        if not t.bot:
            room.banned.add(target)
        await remove_from_room(room, target, "kicked")


async def h_transfer_owner(conn: Conn, msg: dict) -> None:
    target = get_str(msg, "target", 40)

    def fn(room: Room, now: float) -> None:
        t = room.get(target)
        if t is None or t.bot or target == conn.pid:
            raise GameError("bad_request", "Нельзя передать управление этому игроку")
        room.owner = target
        room._ev("owner", pid=target)
    await room_action(conn, fn, owner_only=True)


async def h_close_room(conn: Conn, msg: dict) -> None:
    room = my_room(conn)
    async with room.lock:
        if rooms.get(room.code) is not room:
            raise GameError("no_room", "Комната уже закрыта")
        require_owner(room, conn.pid)
        await close_room(room, "closed")


HANDLERS: dict[str, Callable[[Conn, dict], Any]] = {
    "ping": h_ping, "get_rooms": h_get_rooms, "get_stats": h_get_stats, "create_room": h_create_room,
    "join_room": h_join_room, "quick_play": h_quick_play, "leave_room": h_leave_room,
    "request_state": h_request_state, "start_game": h_start_game, "play_card": h_play_card,
    "draw_card": h_draw_card, "pass_turn": h_pass_turn, "call_uno": h_call_uno, "catch_uno": h_catch_uno,
    "next_round": h_next_round, "return_to_lobby": h_return_to_lobby, "add_bot": h_add_bot,
    "kick": h_kick, "transfer_owner": h_transfer_owner, "close_room": h_close_room,
}


async def process(conn: Conn, msg: dict) -> None:
    req = msg.get("req_id")
    if req is not None and (not isinstance(req, str) or len(req) > 64):
        req = None
    if req and req in conn.seen:  # повтор того же запроса — не выполняем второй раз
        await send(conn, conn.seen[req])
        return
    handler = HANDLERS.get(msg.get("type"))
    if handler is None:
        reply = {"type": "error", "code": "unknown_type", "message": "Неизвестная команда", "req_id": req}
    else:
        try:
            await handler(conn, msg)
            reply = {"type": "ack", "req_id": req}
        except GameError as e:
            reply = {"type": "error", "code": e.code, "message": e.message, "req_id": req}
        except Exception:
            log.exception("handler %s failed", msg.get("type"))
            reply = {"type": "error", "code": "internal", "message": "Внутренняя ошибка сервера", "req_id": req}
    if req:
        conn.seen[req] = reply
        while len(conn.seen) > 200:
            conn.seen.popitem(last=False)
    await send(conn, reply)


# --- авторизация и жизненный цикл соединения --------------------------------


def authenticate(msg: dict) -> dict:
    init = msg.get("init_data")
    if isinstance(init, str) and init:
        if not BOT_TOKEN:
            raise AuthError("Сервер не настроен: не задан BOT_TOKEN")
        return user_from_telegram(validate_init_data(init, BOT_TOKEN))
    dev = msg.get("dev_user")
    if DEV_MODE and isinstance(dev, dict):
        uid = dev.get("id")
        if isinstance(uid, bool) or not isinstance(uid, int) or not (0 < uid < 2 ** 52):
            raise AuthError("Некорректный тестовый пользователь")
        name = clean_text(dev.get("name"), 24) or f"Игрок {uid}"
        return {"id": uid, "name": name, "username": "", "photo": None}
    raise AuthError("Откройте игру через Telegram")


def origin_allowed(origin: Optional[str], host: Optional[str]) -> bool:
    if not origin:
        return True  # не браузерный клиент; права всё равно даёт только подпись initData
    o = origin.rstrip("/")
    if host and o.endswith("://" + host):
        return True  # страница отдана этим же сервером
    if "*" in ALLOWED_ORIGINS:
        return True
    if not ALLOWED_ORIGINS:
        return DEV_MODE
    return o in ALLOWED_ORIGINS


async def attach(conn: Conn, user: dict) -> Optional[Room]:
    pid = str(user["id"])
    db.upsert_player(user["id"], user["name"], user.get("username", ""), user.get("photo"))
    old = conns.get(pid)
    if old is not None and old is not conn:
        await send(old, {"type": "replaced"})
        old.closed = True
        try:
            await old.ws.close(code=4001)
        except Exception:
            pass
    conn.pid, conn.user = pid, user
    conns[pid] = conn
    room = rooms.get(player_room.get(pid, ""))
    if room is not None and room.get(pid) is None:
        player_room.pop(pid, None)
        room = None
    return room


async def on_disconnect(conn: Conn) -> None:
    conn.closed = True
    pid = conn.pid
    if not pid or conns.get(pid) is not conn:
        return
    del conns[pid]
    room = rooms.get(player_room.get(pid, ""))
    if room is None:
        return
    async with room.lock:
        p = room.get(pid)
        if p is not None and rooms.get(room.code) is room:
            p.connected, p.disc_at = False, time.time()
            room._ev("disconnected", pid=pid)
            await commit(room)


# --- фоновые задачи ----------------------------------------------------------


async def tick_room(room: Room, now: float) -> bool:
    changed = False
    for p in list(room.players):
        if p.bot or p.connected or p.disc_at is None or now - p.disc_at <= RECONNECT_GRACE:
            continue
        if room.status in ("lobby", "match_over") or not room.allow_bots:
            # ожидание истекло: игрок покидает комнату (во время матча это поражение)
            closed = room.remove_player(p.pid, now)
            if player_room.get(p.pid) == room.code:
                player_room.pop(p.pid, None)
            if closed:
                await close_room(room, "closed")
                return False
            changed = True
        elif not p.auto:
            p.auto = True  # за отключившегося ходит бот
            room._ev("auto", pid=p.pid)
            changed = True
    if room.status == "playing" and room.players:
        cur = room.current()
        if cur.bot or cur.auto:
            if room.bot_at is None:
                room.bot_at = now + 1.0
            elif now >= room.bot_at:
                room.bot_move(now)
                changed = True
        elif room.turn_deadline is not None and now >= room.turn_deadline:
            room.timeout_action(now)
            changed = True
        u = room.uno
        if u:
            if now > u["until"]:
                room.uno = None
            elif u.get("catcher") and now >= u["catch_at"]:
                catcher, u["catcher"] = u["catcher"], None
                try:
                    room.catch_uno(catcher, u["pid"], now)
                    changed = True
                except GameError:
                    pass
    if any(not p.bot and p.connected for p in room.players):
        room.empty_since = None
    elif room.empty_since is None:
        room.empty_since = now
    elif now - room.empty_since > EMPTY_ROOM_TTL:
        await close_room(room, "idle")
        return False
    return changed


async def ticker() -> None:
    while True:
        await asyncio.sleep(0.4)
        now = time.time()
        for room in list(rooms.values()):
            try:
                async with room.lock:
                    if rooms.get(room.code) is not room:
                        continue
                    if await tick_room(room, now):
                        await commit(room)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick failed for room %s", room.code)
        if int(now) % 60 == 0:
            for k in [k for k, (_, until) in pw_fails.items() if until < now]:
                pw_fails.pop(k, None)


def restore_rooms() -> None:
    now = time.time()
    for code, data in db.load_rooms():
        try:
            room = Room.from_dict(json.loads(data))
        except Exception:
            log.exception("cannot restore room %s", code)
            db.delete_room(code)
            continue
        humans = [p for p in room.players if not p.bot]
        if not humans:
            db.delete_room(code)
            continue
        for p in humans:
            p.connected, p.disc_at = False, now
            player_room[p.pid] = code
        if room.status == "playing":
            room.turn_deadline = now + room.turn_time + 15
            room.bot_at = None
        rooms[code] = room
    log.info("restored %d room(s)", len(rooms))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = Database(DB_PATH)
    db.prune_events(30 * 86400)
    restore_rooms()
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN не задан: авторизация Telegram недоступна%s",
                    " (DEV_MODE: разрешён тестовый вход)" if DEV_MODE else "")
    if not ALLOWED_ORIGINS and not DEV_MODE:
        log.warning("ALLOWED_ORIGINS не задан: внешние страницы не смогут подключаться к WebSocket")
    task = asyncio.create_task(ticker())
    log.info("UNO server started (dev=%s)", DEV_MODE)
    try:
        yield
    finally:
        task.cancel()
        for c in list(conns.values()):
            try:
                await c.ws.close(code=1001)
            except Exception:
                pass
        conns.clear()
        db.close()
        log.info("UNO server stopped")


app = FastAPI(title="UNO Online", lifespan=lifespan, docs_url="/docs" if DEV_MODE else None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or (["*"] if DEV_MODE else []),
    allow_methods=["GET"],
    allow_headers=["*"],
)

INDEX_PATH = Path(__file__).with_name("index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "rooms": len(rooms), "online": len(conns), "time": time.time()}


@app.get("/")
async def index():
    if INDEX_PATH.exists():
        return FileResponse(INDEX_PATH, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"service": "UNO Online", "ws": "/ws", "health": "/health"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host")):
        log.warning("rejected websocket from origin %s", ws.headers.get("origin"))
        await ws.accept()  # сначала принимаем, чтобы клиент увидел код 4003 и показал понятную ошибку
        await ws.close(code=4003)
        return
    await ws.accept()
    conn = Conn(ws)
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
        msg = json.loads(raw) if len(raw) <= MAX_MSG_BYTES else None
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            await send(conn, {"type": "auth_error", "message": "Ожидалась авторизация"})
            await ws.close(code=4000)
            return
        try:
            user = authenticate(msg)
        except AuthError as e:
            await send(conn, {"type": "auth_error", "message": str(e)})
            await ws.close(code=4002)
            return
        room = await attach(conn, user)
        await send(conn, {
            "type": "auth_ok",
            "user": {"id": str(user["id"]), "name": user["name"], "photo": user.get("photo")},
            "profile": db.get_profile(user["id"]),
            "server_time": time.time(),
            "config": {"unoWindow": UNO_WINDOW, "grace": RECONNECT_GRACE, "targets": list(TARGETS),
                       "turnTimes": list(TURN_TIMES)},
            "room": room.view(conn.pid, with_events=False) if room else None,
        })
        if room is not None:
            async with room.lock:
                p = room.get(conn.pid)
                if p is not None and rooms.get(room.code) is room:
                    p.connected, p.disc_at = True, None
                    room._ev("reconnected", pid=conn.pid)
                    await commit(room)
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=IDLE_TIMEOUT)
            if len(raw) > MAX_MSG_BYTES:
                await ws.close(code=1009)
                return
            if not conn.take_token():
                conn.strikes += 1
                if conn.strikes > 60:
                    await ws.close(code=4008)
                    return
                await send(conn, {"type": "error", "code": "rate_limited", "message": "Слишком часто. Подождите секунду."})
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                await send(conn, {"type": "error", "code": "bad_json", "message": "Некорректное сообщение"})
                continue
            if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                await send(conn, {"type": "error", "code": "bad_request", "message": "Некорректное сообщение"})
                continue
            await process(conn, msg)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if "disconnect" not in repr(e).lower():
            log.exception("websocket error")
    finally:
        await on_disconnect(conn)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, ws_max_size=8192, log_level="info")
