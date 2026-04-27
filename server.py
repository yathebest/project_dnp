import argparse
import asyncio
import json
import logging
import random
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed


TCP_HOST = "0.0.0.0"
TCP_PORT = 5555
WS_HOST = "0.0.0.0"
WS_PORT = 8765
GRACE_SECONDS = 15
MAX_PLAYERS = 2
MAX_ROOM_CODE_LEN = 16
MAX_PASSWORD_LEN = 64
MAX_CHAT_TEXT_LEN = 500
MAX_CHAT_HISTORY = 50

ERRORS = {
    "room_exists": "Room with this code already exists.",
    "room_not_found": "Room not found.",
    "room_full": f"Room is full ({MAX_PLAYERS} players already).",
    "wrong_password": "Wrong password.",
    "bad_room_code": f"Room code must be 1-{MAX_ROOM_CODE_LEN} alphanumeric characters.",
    "bad_password": f"Password too long (max {MAX_PASSWORD_LEN}).",
    "no_active_game": "No active game.",
    "game_over": "Game is over.",
    "not_your_turn": "Not your turn.",
    "out_of_bounds": "Move is out of bounds.",
    "cell_occupied": "Cell is already occupied.",
    "round_running": "Round is still running.",
    "bad_move": "Invalid move format.",
    "unknown_type": "Unknown message type.",
    "invalid_json": "Invalid JSON.",
}


def now_ms():
    return int(time.time() * 1000)


def gen_code():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


def check_winner(b):
    lines = []
    for i in range(3):
        lines.append([(i, 0), (i, 1), (i, 2)])
        lines.append([(0, i), (1, i), (2, i)])
    lines.append([(0, 0), (1, 1), (2, 2)])
    lines.append([(0, 2), (1, 1), (2, 0)])
    for line in lines:
        (r1, c1), (r2, c2), (r3, c3) = line
        v = b[r1][c1]
        if v and v == b[r2][c2] == b[r3][c3]:
            return v, [[r, c] for (r, c) in line]
    if all(c is not None for row in b for c in row):
        return "draw", None
    return None, None


def normalize_code(code):
    if not code:
        return None
    c = code.strip().upper()
    if not c or len(c) > MAX_ROOM_CODE_LEN:
        return None
    if not all(ch.isalnum() or ch in "-_" for ch in c):
        return None
    return c


def clean_name(name, pid):
    n = (name or "").strip()
    return n[:32] if n else "player-" + pid[:4]


@dataclass(eq=False)
class Player:
    pid: str
    name: str
    transport: str
    send: object
    peer: str = ""
    mark: Optional[str] = None
    room_code: Optional[str] = None
    wants_new_round: bool = False
    connected: bool = True


@dataclass
class Room:
    code: str
    password: Optional[str] = None
    players: list = field(default_factory=list)
    board: list = field(default_factory=lambda: [[None] * 3 for _ in range(3)])
    turn: str = "X"
    scores: dict = field(default_factory=lambda: {"X": 0, "O": 0, "draws": 0})
    round_no: int = 1
    winner: Optional[str] = None
    win_line: Optional[list] = None
    grace_task: Optional[asyncio.Task] = None
    pending_name: Optional[str] = None
    rejoin_code: Optional[str] = None
    chat: list = field(default_factory=list)


rooms: dict = {}
observers: set = set()
log = logging.getLogger("ttt")


def rooms_snapshot():
    out = []
    for r in rooms.values():
        played = any(c is not None for row in r.board for c in row)
        in_grace = r.grace_task is not None
        out.append({
            "code": r.code,
            "players": len(r.players),
            "max": MAX_PLAYERS,
            "has_password": r.password is not None,
            "in_progress": r.round_no > 1 or played,
            "joinable": len(r.players) < MAX_PLAYERS and not in_grace,
            "in_grace": in_grace,
        })
    out.sort(key=lambda d: (not (d["joinable"] or d["in_grace"]), d["code"]))
    return out


def gen_rejoin_code():
    return "%04d" % random.randint(0, 9999)


def opponent_of(room, player):
    for q in room.players:
        if q is not player:
            return q
    return None


def reset_room(room):
    room.board = [[None] * 3 for _ in range(3)]
    room.turn = "X"
    room.scores = {"X": 0, "O": 0, "draws": 0}
    room.round_no = 1
    room.winner = None
    room.win_line = None
    room.chat = []
    room.rejoin_code = None
    for p in room.players:
        p.wants_new_round = False


async def send_error(p, code):
    await p.send({"type": "error", "code": code, "message": ERRORS.get(code, code)})


async def broadcast_rooms():
    if not observers:
        return
    msg = {"type": "rooms", "rooms": rooms_snapshot()}
    await asyncio.gather(
        *(p.send(msg) for p in list(observers)),
        return_exceptions=True,
    )


async def broadcast_to_room(room, msg):
    targets = [p for p in room.players if p.connected]
    if targets:
        await asyncio.gather(*(p.send(msg) for p in targets),
                             return_exceptions=True)


async def broadcast_state(room, last_move=None):
    t0 = time.perf_counter()
    targets = [p for p in room.players if p.connected]
    if not targets:
        return
    for p in targets:
        opp = opponent_of(room, p)
        await p.send({
            "type": "state",
            "room": room.code,
            "round": room.round_no,
            "board": room.board,
            "turn": room.turn,
            "you": p.mark,
            "your_name": p.name,
            "opponent": opp.name if opp else None,
            "opponent_connected": opp.connected if opp else False,
            "scores": room.scores,
            "winner": room.winner,
            "line": room.win_line,
            "rejoin_code": room.rejoin_code,
            "server_ts": now_ms(),
        })
    fanout_ms = (time.perf_counter() - t0) * 1000.0
    move_str = "(%d,%d)" % last_move if last_move else "-"
    log.info("SYNC room=%s round=%d turn=%s move=%s recipients=%d fanout_ms=%.2f",
             room.code, room.round_no, room.turn, move_str, len(targets), fanout_ms)


def create_room(code, password):
    code = normalize_code(code)
    if code is None:
        return None, "bad_room_code"
    if code in rooms:
        return None, "room_exists"
    if password is not None and not isinstance(password, str):
        return None, "bad_password"
    if password and len(password) > MAX_PASSWORD_LEN:
        return None, "bad_password"
    pw = password if password else None
    r = Room(code=code, password=pw)
    rooms[code] = r
    log.info("ROOM CREATED room=%s password=%s", code, "yes" if pw else "no")
    return r, None


def find_room(code, password, name, rejoin_code):
    code = normalize_code(code)
    if code is None:
        return None, "bad_room_code"
    r = rooms.get(code)
    if r is None:
        return None, "room_not_found"
    in_grace = r.grace_task is not None
    rejoin = False
    if in_grace and rejoin_code and r.rejoin_code and rejoin_code == r.rejoin_code:
        rejoin = True
    elif in_grace and name and r.pending_name == name and not r.rejoin_code:
        rejoin = True
    if not rejoin and (in_grace or len(r.players) >= MAX_PLAYERS):
        return None, "room_full"
    if not rejoin and r.password is not None and (password or "") != r.password:
        return None, "wrong_password"
    return r, None


def auto_room():
    for r in rooms.values():
        if r.password is None and r.grace_task is None and len(r.players) < MAX_PLAYERS:
            return r
    code = gen_code()
    while code in rooms:
        code = gen_code()
    r = Room(code=code)
    rooms[code] = r
    log.info("ROOM CREATED room=%s password=no reason=auto", code)
    return r


async def seat_player(room, p):
    observers.discard(p)
    used = {q.mark for q in room.players}
    p.mark = "X" if "X" not in used else "O"
    p.room_code = room.code
    room.players.append(p)
    log.info("JOIN room=%s name=%s mark=%s transport=%s peer=%s",
             room.code, p.name, p.mark, p.transport, p.peer)
    opp = opponent_of(room, p)
    await p.send({
        "type": "joined",
        "room": room.code,
        "you": p.mark,
        "opponent": opp.name if opp else None,
        "rejoined": False,
        "has_password": room.password is not None,
    })
    if room.chat:
        await p.send({"type": "chat_history", "messages": list(room.chat)})
    if len(room.players) == MAX_PLAYERS:
        if room.rejoin_code is None:
            room.rejoin_code = gen_rejoin_code()
        log.info("ROUND %d STARTED room=%s rejoin_code=%s",
                 room.round_no, room.code, room.rejoin_code)
        await broadcast_to_room(room, {
            "type": "log", "level": "info",
            "message": "Round %d started - %s (X) vs %s (O)" % (
                room.round_no, room.players[0].name, room.players[1].name),
        })
        await broadcast_state(room)
    else:
        await p.send({"type": "log", "level": "info",
                      "message": "Waiting for opponent..."})
    await broadcast_rooms()


async def seat_reconnect(room, p):
    observers.discard(p)
    used = {q.mark for q in room.players}
    p.mark = "O" if "X" in used else "X"
    p.room_code = room.code
    room.players.append(p)
    if room.grace_task:
        room.grace_task.cancel()
        room.grace_task = None
    room.pending_name = None
    log.info("RECONNECT room=%s name=%s mark=%s", room.code, p.name, p.mark)
    opp = opponent_of(room, p)
    await p.send({
        "type": "joined",
        "room": room.code,
        "you": p.mark,
        "opponent": opp.name if opp else None,
        "rejoined": True,
        "has_password": room.password is not None,
    })
    if room.chat:
        await p.send({"type": "chat_history", "messages": list(room.chat)})
    if opp:
        await opp.send({"type": "opponent_rejoined"})
    await broadcast_state(room)
    await broadcast_rooms()


async def cmd_create(p, name, code, password):
    p.name = clean_name(name, p.pid)
    r, err = create_room(code, password)
    if err:
        await send_error(p, err)
        return
    await seat_player(r, p)


async def cmd_join(p, name, code, password, rejoin_code):
    p.name = clean_name(name, p.pid)
    r, err = find_room(code, password, p.name, rejoin_code)
    if err:
        await send_error(p, err)
        return
    in_grace = r.grace_task is not None
    is_rejoin = False
    if in_grace and rejoin_code and r.rejoin_code and rejoin_code == r.rejoin_code:
        is_rejoin = True
    elif in_grace and r.pending_name == p.name and not r.rejoin_code:
        is_rejoin = True
    if is_rejoin:
        await seat_reconnect(r, p)
    else:
        await seat_player(r, p)


async def cmd_auto(p, name):
    p.name = clean_name(name, p.pid)
    await seat_player(auto_room(), p)


async def cmd_move(p, row, col):
    r = rooms.get(p.room_code or "")
    if not r or len(r.players) < 2:
        await send_error(p, "no_active_game")
        return
    if r.winner is not None:
        await send_error(p, "game_over")
        return
    if r.turn != p.mark:
        await send_error(p, "not_your_turn")
        return
    if not (0 <= row < 3 and 0 <= col < 3):
        await send_error(p, "out_of_bounds")
        return
    if r.board[row][col] is not None:
        await send_error(p, "cell_occupied")
        return

    t_recv = time.perf_counter()
    r.board[row][col] = p.mark
    winner, line = check_winner(r.board)
    if winner == "draw":
        r.winner = "draw"
        r.scores["draws"] += 1
        log.info("ROUND %d FINISHED room=%s result=draw", r.round_no, r.code)
    elif winner is not None:
        r.winner = winner
        r.win_line = line
        r.scores[winner] += 1
        log.info("ROUND %d FINISHED room=%s winner=%s line=%s",
                 r.round_no, r.code, winner, line)
    else:
        r.turn = "O" if r.turn == "X" else "X"

    await broadcast_state(r, last_move=(row, col))
    ms = (time.perf_counter() - t_recv) * 1000.0
    log.info("MOVE room=%s round=%d by=%s cell=(%d,%d) move_to_broadcast_ms=%.2f",
             r.code, r.round_no, p.mark, row, col, ms)


async def cmd_new_round(p):
    r = rooms.get(p.room_code or "")
    if not r or len(r.players) < 2:
        return
    if r.winner is None:
        await send_error(p, "round_running")
        return
    p.wants_new_round = True
    if all(q.wants_new_round for q in r.players):
        r.round_no += 1
        r.board = [[None] * 3 for _ in range(3)]
        r.turn = "X"
        r.winner = None
        r.win_line = None
        for q in r.players:
            q.wants_new_round = False
        log.info("ROUND %d STARTED room=%s", r.round_no, r.code)
        await broadcast_to_room(r, {
            "type": "log", "level": "info",
            "message": "Round %d started" % r.round_no,
        })
        await broadcast_state(r)
    else:
        await broadcast_to_room(r, {
            "type": "log", "level": "info",
            "message": "%s wants to play another round" % p.name,
        })


async def cmd_chat(p, text):
    r = rooms.get(p.room_code or "")
    if not r or p not in r.players:
        await send_error(p, "no_active_game")
        return
    text = (text or "").strip()
    if not text:
        return
    if len(text) > MAX_CHAT_TEXT_LEN:
        text = text[:MAX_CHAT_TEXT_LEN]
    nick = p.name or "?"
    msg = {"type": "chat", "nickname": nick, "text": text, "ts": now_ms()}
    r.chat.append(msg)
    if len(r.chat) > MAX_CHAT_HISTORY:
        r.chat = r.chat[-MAX_CHAT_HISTORY:]
    log.info("CHAT room=%s nickname=%s text=%s", r.code, nick, text[:60])
    await broadcast_to_room(r, msg)


async def cmd_leave(p):
    r = rooms.get(p.room_code or "")
    if r is None or p not in r.players:
        observers.add(p)
        return
    log.info("LEAVE name=%s room=%s mark=%s", p.name, r.code, p.mark)
    if r.grace_task:
        r.grace_task.cancel()
        r.grace_task = None
        r.pending_name = None
    r.players.remove(p)
    p.room_code = None
    p.mark = None
    p.name = ""
    observers.add(p)
    if not r.players:
        rooms.pop(r.code, None)
        log.info("ROOM CLOSED room=%s reason=empty", r.code)
    else:
        survivor = r.players[0]
        reset_room(r)
        log.info("ROOM RESET room=%s reason=opponent_left survivor=%s",
                 r.code, survivor.name)
        await survivor.send({
            "type": "log", "level": "info",
            "message": "Opponent left. Waiting for a new opponent...",
        })
        await survivor.send({
            "type": "joined",
            "room": r.code,
            "you": survivor.mark,
            "opponent": None,
            "rejoined": False,
            "has_password": r.password is not None,
        })
    await p.send({"type": "rooms", "rooms": rooms_snapshot()})
    await broadcast_rooms()


async def grace_timer(r):
    try:
        await asyncio.sleep(GRACE_SECONDS)
    except asyncio.CancelledError:
        return
    r.grace_task = None
    r.pending_name = None
    if not r.players:
        rooms.pop(r.code, None)
        log.info("ROOM CLOSED room=%s reason=both_left", r.code)
        await broadcast_rooms()
        return
    survivor = r.players[0]
    reset_room(r)
    log.info("ROOM RESET room=%s reason=grace_expired survivor=%s",
             r.code, survivor.name)
    await survivor.send({
        "type": "log", "level": "info",
        "message": "Opponent didn't return. Waiting for a new opponent...",
    })
    await survivor.send({
        "type": "joined",
        "room": r.code,
        "you": survivor.mark,
        "opponent": None,
        "rejoined": False,
        "has_password": r.password is not None,
    })
    await broadcast_rooms()


async def on_connect(p):
    observers.add(p)
    await p.send({"type": "rooms", "rooms": rooms_snapshot()})


async def on_disconnect(p):
    observers.discard(p)
    if not p.connected:
        return
    p.connected = False
    r = rooms.get(p.room_code or "")
    log.info("DISCONNECT name=%s room=%s mark=%s",
             p.name, r.code if r else "-", p.mark)
    if not r or p not in r.players:
        await broadcast_rooms()
        return
    if len(r.players) == 1:
        r.players.remove(p)
        if r.grace_task:
            r.grace_task.cancel()
            r.grace_task = None
        rooms.pop(r.code, None)
        log.info("ROOM CLOSED room=%s reason=empty", r.code)
        await broadcast_rooms()
        return
    opp = opponent_of(r, p)
    r.players.remove(p)
    r.pending_name = p.name
    if opp:
        await opp.send({"type": "opponent_left",
                        "grace_seconds": GRACE_SECONDS, "name": p.name})
    if r.grace_task:
        r.grace_task.cancel()
    r.grace_task = asyncio.create_task(grace_timer(r))
    await broadcast_rooms()


async def dispatch(p, msg):
    t = msg.get("type")
    if t == "create":
        await cmd_create(p, msg.get("name", ""), msg.get("room"), msg.get("password"))
    elif t == "join":
        code = msg.get("room")
        if not code or (isinstance(code, str) and not code.strip()):
            await cmd_auto(p, msg.get("name", ""))
        else:
            await cmd_join(p, msg.get("name", ""), code,
                           msg.get("password"), msg.get("rejoin_code"))
    elif t == "auto":
        await cmd_auto(p, msg.get("name", ""))
    elif t == "move":
        try:
            row = int(msg["row"])
            col = int(msg["col"])
        except (KeyError, TypeError, ValueError):
            await send_error(p, "bad_move")
            return
        await cmd_move(p, row, col)
    elif t == "new_round":
        await cmd_new_round(p)
    elif t == "ping":
        await p.send({"type": "pong", "ts": msg.get("ts"), "server_ts": now_ms()})
    elif t == "leave":
        await cmd_leave(p)
    elif t == "list_rooms":
        await p.send({"type": "rooms", "rooms": rooms_snapshot()})
    elif t == "chat":
        await cmd_chat(p, msg.get("text", ""))
    else:
        await send_error(p, "unknown_type")


async def handle_tcp(reader, writer):
    peer_t = writer.get_extra_info("peername")
    peer = "%s:%d" % peer_t if peer_t else "?"
    pid = uuid.uuid4().hex

    async def send(msg):
        try:
            writer.write((json.dumps(msg) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    p = Player(pid=pid, name="", transport="tcp", send=send, peer=peer)
    log.info("CONNECT transport=tcp peer=%s pid=%s", peer, pid[:8])
    await on_connect(p)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                await send({"type": "error", "code": "invalid_json",
                            "message": ERRORS["invalid_json"]})
                continue
            await dispatch(p, msg)
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        await on_disconnect(p)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_ws(websocket):
    peer_t = websocket.remote_address
    peer = "%s:%d" % peer_t if peer_t else "?"
    pid = uuid.uuid4().hex

    async def send(msg):
        try:
            await websocket.send(json.dumps(msg))
        except ConnectionClosed:
            pass

    p = Player(pid=pid, name="", transport="ws", send=send, peer=peer)
    log.info("CONNECT transport=ws peer=%s pid=%s", peer, pid[:8])
    await on_connect(p)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                await send({"type": "error", "code": "invalid_json",
                            "message": ERRORS["invalid_json"]})
                continue
            await dispatch(p, msg)
    except ConnectionClosed:
        pass
    finally:
        await on_disconnect(p)


async def main(args):
    tcp = await asyncio.start_server(handle_tcp, args.tcp_host, args.tcp_port)
    log.info("TCP listening on %s:%d", args.tcp_host, args.tcp_port)
    ws = None
    if not args.no_ws:
        ws = await websockets.serve(handle_ws, args.ws_host, args.ws_port,
                                    ping_interval=20, ping_timeout=20)
        log.info("WS  listening on %s:%d", args.ws_host, args.ws_port)
    async with tcp:
        try:
            await asyncio.Future()
        finally:
            if ws:
                ws.close()
                await ws.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tcp-host", default=TCP_HOST)
    parser.add_argument("--tcp-port", type=int, default=TCP_PORT)
    parser.add_argument("--ws-host", default=WS_HOST)
    parser.add_argument("--ws-port", type=int, default=WS_PORT)
    parser.add_argument("--no-ws", action="store_true")
    parser.add_argument("--log-file", default="server.log")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        log.info("shutting down")
