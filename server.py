"""Автотесты UNO Online: правила, авторизация, статистика, протокол.

Запуск из папки проекта:  pip install -r requirements-dev.txt && pytest -q
"""
import asyncio
import hashlib
import hmac
import json
import random
import time
from urllib.parse import parse_qsl, urlencode

import pytest

import server
from server import GameError, Player, Room

NOW = 1000.0


# ----------------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------------


def C(cid, color, value):
    return {"id": cid, "color": color, "value": value}


def make_room(n=2, **kw):
    r = Room("TEST1", "test", "p0", max_players=4, **kw)
    for i in range(n):
        r.add_player(Player(f"p{i}", f"P{i}"))
    r.start_series(NOW)
    return r


def setup(r, hands, top, color=None, turn=0):
    """Подменяет руки и верхнюю карту, чтобы проверять конкретные ситуации."""
    for p, h in zip(r.players, hands):
        p.hand = h
        p.uno_called = False
    r.discard = [top]
    r.color = color or top["color"]
    r.turn = turn
    r.direction = 1
    r.pending, r.pending_kind = 0, None
    r.drawn = None
    r.uno = None


def code_of(exc_info):
    return exc_info.value.code


def sign_init_data(user, token, auth_date=None):
    auth_date = int(time.time()) if auth_date is None else auth_date
    pairs = {"auth_date": str(auth_date), "query_id": "AAH", "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


# ----------------------------------------------------------------------------
# Колода и очки
# ----------------------------------------------------------------------------


def test_deck_is_standard_108():
    d = server.build_deck()
    assert len(d) == 108
    assert len({c["id"] for c in d}) == 108
    assert sum(c["color"] == "W" for c in d) == 8
    assert sum(c["value"] == "wild4" for c in d) == 4
    assert sum(c["value"] == "0" for c in d) == 4
    assert sum(c["value"] == "7" for c in d) == 8


def test_card_points():
    assert server.card_points(C(1, "R", "7")) == 7
    assert server.card_points(C(2, "R", "skip")) == 20
    assert server.card_points(C(3, "B", "draw2")) == 20
    assert server.card_points(C(4, "W", "wild")) == 50
    assert server.hand_points([C(1, "R", "7"), C(2, "W", "wild4")]) == 57


@pytest.mark.parametrize("n", [2, 3, 4])
def test_deal(n):
    r = make_room(n)
    assert r.status == "playing"
    assert all(len(p.hand) == 7 for p in r.players)
    assert r.discard[-1]["value"].isdigit()
    assert len(r.deck) == 108 - 7 * n - 1


def test_need_two_players():
    r = Room("T", "t", "p0")
    r.add_player(Player("p0", "P0"))
    with pytest.raises(GameError) as e:
        r.start_series(NOW)
    assert code_of(e) == "need_players"


def test_room_full():
    r = Room("T", "t", "p0", max_players=2)
    r.add_player(Player("p0", "P0"))
    r.add_player(Player("p1", "P1"))
    with pytest.raises(GameError) as e:
        r.add_player(Player("p2", "P2"))
    assert code_of(e) == "room_full"


# ----------------------------------------------------------------------------
# Ходы и правила
# ----------------------------------------------------------------------------


def test_not_your_turn():
    r = make_room(2)
    setup(r, [[C(1, "R", "5"), C(2, "R", "6")], [C(3, "R", "1"), C(4, "R", "2")]], C(100, "R", "3"))
    with pytest.raises(GameError) as e:
        r.play_card("p1", 3, None, NOW)
    assert code_of(e) == "not_your_turn"


def test_illegal_card_rejected():
    r = make_room(2)
    setup(r, [[C(1, "G", "5"), C(2, "R", "1")], [C(3, "R", "1")]], C(100, "R", "3"))
    with pytest.raises(GameError) as e:
        r.play_card("p0", 1, None, NOW)
    assert code_of(e) == "illegal_move"
    assert len(r.players[0].hand) == 2  # ничего не изменилось


def test_same_value_other_color_is_legal():
    r = make_room(2)
    setup(r, [[C(1, "G", "3"), C(2, "R", "1")], [C(3, "R", "1")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.color == "G"
    assert r.turn == 1


def test_wild_requires_color():
    r = make_room(2)
    setup(r, [[C(1, "W", "wild"), C(2, "R", "1")], [C(3, "R", "1")]], C(100, "R", "3"))
    with pytest.raises(GameError) as e:
        r.play_card("p0", 1, None, NOW)
    assert code_of(e) == "color_required"
    r.play_card("p0", 1, "B", NOW)
    assert r.color == "B"


def test_wild4_only_without_current_color():
    r = make_room(2)
    setup(r, [[C(1, "W", "wild4"), C(2, "R", "5")], [C(3, "G", "1")]], C(100, "R", "3"))
    with pytest.raises(GameError) as e:
        r.play_card("p0", 1, "B", NOW)
    assert code_of(e) == "illegal_move"
    setup(r, [[C(1, "W", "wild4"), C(2, "B", "5")], [C(3, "G", "1")]], C(100, "R", "3"))
    r.play_card("p0", 1, "G", NOW)
    assert r.color == "G"
    assert len(r.players[1].hand) == 5
    assert r.turn == 0  # при двух игроках следующий ход снова у первого


def test_skip_three_players():
    r = make_room(3)
    setup(r, [[C(1, "R", "skip"), C(2, "R", "5")], [C(3, "R", "1")], [C(4, "R", "2")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.turn == 2


def test_reverse_three_players():
    r = make_room(3)
    setup(r, [[C(1, "R", "reverse"), C(2, "R", "5")], [C(3, "R", "1")], [C(4, "R", "2")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.direction == -1
    assert r.turn == 2


def test_reverse_two_players_acts_as_skip():
    r = make_room(2)
    setup(r, [[C(1, "R", "reverse"), C(2, "R", "5")], [C(3, "R", "1")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.turn == 0


def test_draw2_without_stacking():
    r = make_room(3)
    setup(r, [[C(1, "R", "draw2"), C(2, "R", "5")], [C(3, "R", "9")], [C(4, "R", "2")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert len(r.players[1].hand) == 3
    assert r.turn == 2  # жертва пропускает ход


def test_draw2_stacking_chain():
    r = make_room(3, stacking=True)
    setup(r, [[C(1, "R", "draw2"), C(2, "R", "5")], [C(3, "G", "draw2"), C(5, "G", "1")], [C(4, "R", "7")]],
          C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.pending == 2 and r.turn == 1
    assert r.playable_ids("p1") == [3]  # можно ответить только такой же «+2»
    r.play_card("p1", 3, None, NOW)
    assert r.pending == 4 and r.turn == 2
    r.draw_card("p2", NOW)
    assert len(r.players[2].hand) == 5
    assert r.pending == 0 and r.turn == 0


def test_draw_playable_card_then_play_or_pass():
    r = make_room(2)
    setup(r, [[C(1, "B", "1")], [C(3, "R", "1")]], C(100, "R", "3"))
    r.deck = [C(50, "B", "2"), C(51, "R", "9")]
    r.draw_card("p0", NOW)
    assert r.drawn == 51
    assert r.players[0].hand[-1]["id"] == 51
    assert r.view("p0")["game"]["can_pass"] is True
    with pytest.raises(GameError) as e:
        r.play_card("p0", 1, None, NOW)  # другая карта — нельзя
    assert code_of(e) == "must_play_drawn"
    with pytest.raises(GameError) as e:
        r.draw_card("p0", NOW)
    assert code_of(e) == "already_drawn"
    r.pass_turn("p0", NOW)
    assert r.turn == 1 and r.drawn is None


def test_draw_unplayable_card_ends_turn():
    r = make_room(2)
    setup(r, [[C(1, "B", "1")], [C(3, "R", "1")]], C(100, "R", "3"))
    r.deck = [C(51, "R", "9"), C(50, "B", "2")]
    r.draw_card("p0", NOW)
    assert r.drawn is None
    assert r.turn == 1
    assert len(r.players[0].hand) == 2


def test_cannot_pass_without_drawing():
    r = make_room(2)
    with pytest.raises(GameError) as e:
        r.pass_turn(r.current().pid, NOW)
    assert code_of(e) == "cannot_pass"


def test_reshuffle_when_deck_empty():
    r = make_room(2)
    r.deck = []
    r.discard = [C(i, "R", "1") for i in range(10)]
    top = r.discard[-1]
    got = r.draw_cards(1)
    assert len(got) == 1
    assert r.discard == [top]
    assert len(r.deck) == 8


# ----------------------------------------------------------------------------
# UNO
# ----------------------------------------------------------------------------


def test_uno_can_be_caught():
    r = make_room(2)
    setup(r, [[C(1, "R", "5"), C(2, "R", "6")], [C(3, "R", "1"), C(4, "R", "2")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.uno and r.uno["pid"] == "p0"
    r.catch_uno("p1", "p0", NOW + 1)
    assert len(r.players[0].hand) == 3
    assert r.uno is None


def test_uno_called_in_time_cannot_be_caught():
    r = make_room(2)
    setup(r, [[C(1, "R", "5"), C(2, "R", "6")], [C(3, "R", "1"), C(4, "R", "2")]], C(100, "R", "3"))
    r.call_uno("p0")  # две карты в свой ход — можно объявить заранее
    r.play_card("p0", 1, None, NOW)
    assert r.uno is None
    with pytest.raises(GameError):
        r.catch_uno("p1", "p0", NOW + 1)


def test_uno_window_expires():
    r = make_room(2)
    setup(r, [[C(1, "R", "5"), C(2, "R", "6")], [C(3, "R", "1"), C(4, "R", "2")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    with pytest.raises(GameError) as e:
        r.catch_uno("p1", "p0", NOW + server.UNO_WINDOW + 1)
    assert code_of(e) == "nothing_to_catch"
    assert len(r.players[0].hand) == 1


def test_uno_not_allowed_with_many_cards():
    r = make_room(2)
    with pytest.raises(GameError) as e:
        r.call_uno("p0")
    assert code_of(e) == "uno_not_allowed"


# ----------------------------------------------------------------------------
# Конец раунда, очки, выход игроков
# ----------------------------------------------------------------------------


def test_round_end_scoring_and_stats_events():
    r = make_room(2)
    setup(r, [[C(1, "R", "5")], [C(2, "G", "7"), C(3, "W", "wild")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.status == "round_over"
    assert r.result["winner"] == "p0"
    assert r.result["points"] == 57
    assert r.scores["p0"] == 57
    rounds = [s for s in r.pending_stats if s["kind"] == "round"]
    assert {(s["pid"], s["won"]) for s in rounds} == {("p0", True), ("p1", False)}
    assert len({s["event_id"] for s in r.pending_stats}) == len(r.pending_stats)


def test_match_over_when_target_reached():
    r = make_room(2, target_score=50)
    setup(r, [[C(1, "R", "5")], [C(2, "G", "7"), C(3, "W", "wild")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    assert r.status == "match_over"
    assert r.result["match_winner"] == "p0"
    assert {s["kind"] for s in r.pending_stats} == {"round", "match"}


def test_next_round_after_round_over():
    r = make_room(2)
    setup(r, [[C(1, "R", "5")], [C(2, "G", "7")]], C(100, "R", "3"))
    r.play_card("p0", 1, None, NOW)
    r.next_round(NOW)
    assert r.status == "playing" and r.round_no == 2
    assert all(len(p.hand) == 7 for p in r.players)
    assert r.scores["p0"] == 7  # очки сохранились


def test_leaving_two_player_match_forfeits():
    r = make_room(2)
    closed = r.remove_player("p1", NOW)
    assert closed is False
    assert r.status == "match_over"
    assert r.result["match_winner"] == "p0"
    matches = {(s["pid"], s["won"]) for s in r.pending_stats if s["kind"] == "match"}
    assert matches == {("p1", False), ("p0", True)}
    assert not any(s["kind"] == "round" for s in r.pending_stats)


def test_owner_transfers_when_owner_leaves_lobby():
    r = Room("T", "t", "p0")
    for i in range(3):
        r.add_player(Player(f"p{i}", f"P{i}"))
    assert r.remove_player("p0", NOW) is False
    assert r.owner == "p1"


def test_last_human_leaving_closes_room():
    r = Room("T", "t", "p0")
    r.add_player(Player("p0", "P0"))
    r.add_bot()
    assert r.remove_player("p0", NOW) is True


# ----------------------------------------------------------------------------
# Приватность состояния и сериализация
# ----------------------------------------------------------------------------


def test_view_hides_opponent_cards():
    r = make_room(3)
    v = r.view("p0")
    assert len(v["game"]["hand"]) == 7
    mine = {c["id"] for c in v["game"]["hand"]}
    assert mine == {c["id"] for c in r.players[0].hand}
    for p in v["players"]:
        assert "hand" not in p
        assert isinstance(p["cards"], int)
    blob = json.dumps(v)
    for other in r.players[1:]:
        for c in other.hand:
            assert f'"id":{c["id"]},' not in blob.replace(" ", "") or c["id"] in mine


def test_view_never_contains_password():
    r = make_room(2)
    r.set_password("secret123")
    blob = json.dumps(r.view("p0"))
    assert "secret123" not in blob and r.pw_hash not in blob
    assert r.view("p0")["has_password"] is True


def test_password_check():
    r = Room("T", "t", "p0")
    r.set_password("abc")
    assert r.check_password("abc")
    assert not r.check_password("abd")
    assert not r.check_password("")


def test_room_roundtrip():
    r = make_room(3)
    r.set_password("abc")
    d = json.loads(json.dumps(r.to_dict()))
    r2 = Room.from_dict(d)
    assert r2.code == r.code and r2.turn == r.turn and r2.color == r.color
    assert r2.discard == r.discard and r2.deck == r.deck
    assert [p.hand for p in r2.players] == [p.hand for p in r.players]
    assert r2.check_password("abc")


# ----------------------------------------------------------------------------
# Боты: сохранение карт и завершение партии
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("n", [2, 3, 4])
@pytest.mark.parametrize("stacking", [False, True])
def test_bot_game_keeps_all_108_cards(n, stacking):
    random.seed(n * 10 + int(stacking))
    r = make_room(n, stacking=stacking)
    for p in r.players:
        p.auto = True
    for _ in range(5000):
        if r.status != "playing":
            break
        r.bot_move(NOW)
        total = sum(len(p.hand) for p in r.players) + len(r.deck) + len(r.discard)
        assert total == 108
    if r.status != "playing":
        winner = next(p for p in r.players if p.pid == r.result["winner"])
        assert winner.hand == []


# ----------------------------------------------------------------------------
# Telegram-авторизация
# ----------------------------------------------------------------------------

TOKEN = "123456:TEST-TOKEN"


def test_valid_init_data():
    init = sign_init_data({"id": 42, "first_name": "Ann", "last_name": "B"}, TOKEN)
    user = server.validate_init_data(init, TOKEN)
    assert user["id"] == 42
    assert server.user_from_telegram(user)["name"] == "Ann B"


def test_init_data_wrong_token():
    init = sign_init_data({"id": 42, "first_name": "Ann"}, "999:OTHER")
    with pytest.raises(server.AuthError):
        server.validate_init_data(init, TOKEN)


def test_init_data_forged_user_id():
    init = sign_init_data({"id": 42, "first_name": "Ann"}, TOKEN)
    pairs = dict(parse_qsl(init))
    pairs["user"] = json.dumps({"id": 7, "first_name": "Ann"})
    with pytest.raises(server.AuthError):
        server.validate_init_data(urlencode(pairs), TOKEN)


def test_init_data_expired_and_missing_hash():
    old = sign_init_data({"id": 42, "first_name": "Ann"}, TOKEN, auth_date=int(time.time()) - 10 * 86400)
    with pytest.raises(server.AuthError):
        server.validate_init_data(old, TOKEN)
    with pytest.raises(server.AuthError):
        server.validate_init_data("auth_date=1&user=%7B%7D", TOKEN)


def test_dev_login_disabled_in_production(monkeypatch):
    monkeypatch.setattr(server, "DEV_MODE", False)
    with pytest.raises(server.AuthError):
        server.authenticate({"dev_user": {"id": 5, "name": "Hacker"}})
    monkeypatch.setattr(server, "DEV_MODE", True)
    assert server.authenticate({"dev_user": {"id": 5, "name": "Dev"}})["id"] == 5


def test_authenticate_uses_signature(monkeypatch):
    monkeypatch.setattr(server, "BOT_TOKEN", TOKEN)
    init = sign_init_data({"id": 42, "first_name": "Ann"}, TOKEN)
    assert server.authenticate({"init_data": init})["id"] == 42


def test_photo_only_from_telegram():
    assert server.clean_photo("https://t.me/i/userpic/320/x.jpg")
    assert server.clean_photo("https://evil.com/x.jpg") is None
    assert server.clean_photo("http://t.me/x.jpg") is None


def test_origin_policy(monkeypatch):
    monkeypatch.setattr(server, "DEV_MODE", False)
    monkeypatch.setattr(server, "ALLOWED_ORIGINS", ["https://a.github.io"])
    assert server.origin_allowed("https://a.github.io", "srv.example.com")
    assert server.origin_allowed("https://srv.example.com", "srv.example.com")
    assert not server.origin_allowed("https://evil.com", "srv.example.com")
    assert server.origin_allowed(None, "srv.example.com")


# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------


def test_stats_are_idempotent(tmp_path):
    db = server.Database(str(tmp_path / "s.db"))
    db.upsert_player(1, "A", "a", None)
    assert db.record_round("e1", 1, True, 50) is True
    assert db.record_round("e1", 1, True, 50) is False
    assert db.record_match("m1", 1, True, 250) is True
    assert db.record_match("m1", 1, True, 250) is False
    p = db.get_profile(1)
    assert (p["rounds"], p["points"], p["wins"], p["streak"], p["best_score"]) == (1, 50, 1, 1, 250)
    db.record_match("m2", 1, False, 100)
    p = db.get_profile(1)
    assert (p["losses"], p["streak"], p["best_streak"], p["win_rate"]) == (1, 0, 1, 50)
    assert db.leaderboard()[0]["tg_id"] == 1
    db.close()


def test_room_snapshot_storage(tmp_path):
    db = server.Database(str(tmp_path / "r.db"))
    db.save_room("ABC12", "{}")
    assert db.load_rooms() == [("ABC12", "{}")]
    db.delete_room("ABC12")
    assert db.load_rooms() == []
    db.close()


# ----------------------------------------------------------------------------
# Протокол (через process, без реальной сети)
# ----------------------------------------------------------------------------


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        pass


@pytest.fixture
def net(tmp_path):
    server.db = server.Database(str(tmp_path / "net.db"))
    for d in (server.rooms, server.conns, server.player_room, server.pw_fails):
        d.clear()
    yield
    server.db.close()


def make_conn(uid):
    c = server.Conn(FakeWS())
    c.pid = str(uid)
    c.user = {"id": uid, "name": f"U{uid}", "photo": None}
    server.conns[c.pid] = c
    return c


def reply(conn, req):
    return next(m for m in reversed(conn.ws.sent) if m.get("req_id") == req)


def test_duplicate_request_is_not_executed_twice(net):
    async def scenario():
        c = make_conn(1)
        msg = {"type": "create_room", "req_id": "abc", "name": "X"}
        await server.process(c, msg)
        await server.process(c, msg)
        return c

    c = asyncio.run(scenario())
    assert len(server.rooms) == 1
    replies = [m for m in c.ws.sent if m.get("req_id") == "abc"]
    assert len(replies) == 2 and all(m["type"] == "ack" for m in replies)


def test_full_protocol_flow(net):
    async def scenario():
        c1, c2, c3 = make_conn(1), make_conn(2), make_conn(3)
        await server.process(c1, {"type": "create_room", "req_id": "a", "max_players": 2})
        assert reply(c1, "a")["type"] == "ack"
        code = next(iter(server.rooms))

        await server.process(c2, {"type": "join_room", "req_id": "b", "code": code})
        assert reply(c2, "b")["type"] == "ack"

        await server.process(c3, {"type": "join_room", "req_id": "c", "code": code})
        assert reply(c3, "c")["code"] == "room_full"

        await server.process(c2, {"type": "start_game", "req_id": "d"})
        assert reply(c2, "d")["code"] == "not_owner"

        await server.process(c1, {"type": "start_game", "req_id": "e"})
        assert reply(c1, "e")["type"] == "ack"
        room = server.rooms[code]
        assert room.status == "playing"

        state = [m for m in c2.ws.sent if m["type"] == "room_state"][-1]["room"]
        assert len(state["game"]["hand"]) == 7
        assert all("hand" not in p for p in state["players"])

        other = c2 if room.current().pid == "1" else c1
        cid = room.get(other.pid).hand[0]["id"]
        await server.process(other, {"type": "play_card", "req_id": "f", "card_id": cid})
        assert reply(other, "f")["code"] == "not_your_turn"

        await server.process(c3, {"type": "play_card", "req_id": "g", "card_id": 1})
        assert reply(c3, "g")["code"] == "no_room"

        # обрыв связи и возвращение в ту же комнату с теми же картами
        await server.on_disconnect(c2)
        assert room.get("2").connected is False
        c2b = server.Conn(FakeWS())
        back = await server.attach(c2b, {"id": 2, "name": "U2", "username": "", "photo": None})
        assert back is room
        assert len(room.get("2").hand) == 7

    asyncio.run(scenario())


def test_cannot_join_two_rooms(net):
    async def scenario():
        c1 = make_conn(1)
        await server.process(c1, {"type": "create_room", "req_id": "a"})
        await server.process(c1, {"type": "create_room", "req_id": "b"})
        assert reply(c1, "b")["code"] == "already_in_room"

    asyncio.run(scenario())
    assert len(server.rooms) == 1


def test_training_room_starts_immediately(net):
    async def scenario():
        c1 = make_conn(1)
        await server.process(c1, {"type": "create_room", "req_id": "a", "training": True, "bot_count": 2})
        assert reply(c1, "a")["type"] == "ack"

    asyncio.run(scenario())
    room = next(iter(server.rooms.values()))
    assert room.status == "playing" and len(room.players) == 3 and room.ranked is False
