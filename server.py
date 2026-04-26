"""
Multiplayer Tic-Tac-Toe game server.

Runs two listeners in a single asyncio event loop sharing one in-memory
RoomManager:
  * Raw TCP on port 5555 (newline-delimited JSON) - used by CLI clients
    and the load-test script. This is the path that satisfies the
    "Python sockets" project requirement.
  * WebSocket on port 8765 - used by the browser frontend. Same JSON
    schema, no protocol translation.

Game logic:
  * Two players per room (marks X and O).
  * Server is the source of truth for the board, turn, and scores.
  * Win/draw detection after every move; winning line is broadcast.
  * Best-of-N rounds: when a game ends, both players send "new_round"
    to start a fresh board (scores persist).

Resilience:
  * Disconnect detected via empty readline / ConnectionClosed.
  * Mid-game disconnect: opponent gets "opponent_left" + 15s grace
    timer. If the player rejoins (same name+room) inside the window,
    the room resumes. Otherwise the survivor wins by forfeit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore
    ConnectionClosed = Exception  # type: ignore


TCP_HOST = "0.0.0.0"
TCP_PORT = 5555
WS_HOST = "0.0.0.0"
WS_PORT = 8765
GRACE_SECONDS = 15

LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


# ---------------------------------------------------------------------------
# Player & Room
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Player:
    """One connected client. Transport-agnostic via the `send` callback.

    `eq=False` keeps the default object-identity __hash__/__eq__ so Player
    instances can live inside sets (we use one for lobby observers)."""

    pid: str
    name: str
    transport: str  # "tcp" or "ws"
    send: Callable[[dict], Awaitable[None]]
    close: Callable[[], Awaitable[None]]
    mark: Optional[str] = None  # "X" or "O", set when joined a room
    room_code: Optional[str] = None
    wants_new_round: bool = False
    connected: bool = True
    peer: str = ""  # remote address for logging


MAX_PLAYERS = 2
MAX_ROOM_CODE_LEN = 16
MAX_PASSWORD_LEN = 64
MAX_NICKNAME_LEN = 32
MAX_CHAT_TEXT_LEN = 500
MAX_CHAT_HISTORY = 50


ERROR_MESSAGES: dict[str, str] = {
    "room_exists":     "Room with this code already exists. Try a different code or use Join.",
    "room_not_found":  "Room not found. Make sure the code is correct, or use Create.",
    "room_full":       f"Room is full ({MAX_PLAYERS} players already).",
    "wrong_password":  "Wrong password.",
    "bad_room_code":   f"Room code must be 1-{MAX_ROOM_CODE_LEN} alphanumeric characters.",
    "bad_password":    f"Password too long (max {MAX_PASSWORD_LEN}).",
    "no_active_game":  "No active game.",
    "game_over":       "Game is over.",
    "not_your_turn":   "Not your turn.",
    "out_of_bounds":   "Move is out of bounds.",
    "cell_occupied":   "Cell is already occupied.",
    "round_running":   "Round is still running.",
    "bad_move":        "Invalid move format.",
    "unknown_type":    "Unknown message type.",
    "invalid_json":    "Invalid JSON.",
}


@dataclass
class Room:
    code: str
    password: Optional[str] = None  # None → no password
    players: list[Player] = field(default_factory=list)
    board: list[list[Optional[str]]] = field(
        default_factory=lambda: [[None] * 3 for _ in range(3)]
    )
    turn: str = "X"
    scores: dict[str, int] = field(default_factory=lambda: {"X": 0, "O": 0, "draws": 0})
    round_no: int = 1
    winner: Optional[str] = None  # "X" / "O" / "draw" / None
    win_line: Optional[list[list[int]]] = None
    grace_task: Optional[asyncio.Task] = None
    pending_name: Optional[str] = None  # name we expect to rejoin during grace
    chat: list[dict] = field(default_factory=list)  # per-room chat history

    def opponent_of(self, player: Player) -> Optional[Player]:
        for p in self.players:
            if p is not player:
                return p
        return None

    def reset_board(self) -> None:
        self.board = [[None] * 3 for _ in range(3)]
        self.turn = "X"
        self.winner = None
        self.win_line = None
        for p in self.players:
            p.wants_new_round = False


# ---------------------------------------------------------------------------
# RoomManager - core game logic
# ---------------------------------------------------------------------------


class RoomManager:
    def __init__(self, log: logging.Logger) -> None:
        self.rooms: dict[str, Room] = {}
        self.log = log
        self.lobby_observers: set[Player] = set()

    # -- lobby observation -------------------------------------------------

    def _rooms_snapshot(self) -> list[dict]:
        out: list[dict] = []
        for r in self.rooms.values():
            board_started = any(c is not None for row in r.board for c in row)
            out.append(
                {
                    "code": r.code,
                    "players": len(r.players),
                    "max": MAX_PLAYERS,
                    "has_password": r.password is not None,
                    "in_progress": (r.round_no > 1) or board_started,
                    "joinable": (
                        len(r.players) < MAX_PLAYERS and r.grace_task is None
                    ),
                }
            )
        # Open rooms first, then full ones; alphabetically inside each group.
        out.sort(key=lambda d: (not d["joinable"], d["code"]))
        return out

    async def _broadcast_rooms(self) -> None:
        if not self.lobby_observers:
            return
        msg = {"type": "rooms", "rooms": self._rooms_snapshot()}
        await asyncio.gather(
            *(p.send(msg) for p in list(self.lobby_observers)),
            return_exceptions=True,
        )

    async def on_connect(self, player: Player) -> None:
        """Called by transport handlers right after the Player object is built."""
        self.lobby_observers.add(player)
        await player.send({"type": "rooms", "rooms": self._rooms_snapshot()})

    async def list_rooms(self, player: Player) -> None:
        await player.send({"type": "rooms", "rooms": self._rooms_snapshot()})

    async def chat(self, player: Player, text: str) -> None:
        """Per-room chat: only valid when seated in a room. Messages are
        broadcast to every player currently in that room (and only there)."""
        room = self.rooms.get(player.room_code or "")
        if not room or player not in room.players:
            await self._send_error(player, "no_active_game")
            return
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_CHAT_TEXT_LEN:
            text = text[:MAX_CHAT_TEXT_LEN]
        nickname = player.name or "?"
        msg = {
            "type": "chat",
            "nickname": nickname,
            "text": text,
            "ts": _now_ms(),
        }
        room.chat.append(msg)
        if len(room.chat) > MAX_CHAT_HISTORY:
            room.chat = room.chat[-MAX_CHAT_HISTORY:]
        self.log.info(
            "CHAT room=%s nickname=%s text=%s",
            room.code,
            nickname,
            (text[:60] + "...") if len(text) > 60 else text,
        )
        if room.players:
            await asyncio.gather(
                *(p.send(msg) for p in room.players if p.connected),
                return_exceptions=True,
            )

    async def _send_lobby_state(self, player: Player) -> None:
        """Send a freshly-arrived/returned observer the current rooms list."""
        await player.send({"type": "rooms", "rooms": self._rooms_snapshot()})

    def _reset_room_session(self, room: Room) -> None:
        """Wipe the playing state of a room so a new opponent starts fresh.

        Used when one player leaves (gracefully or after grace expiry) and the
        survivor stays in the room waiting for a new opponent."""
        room.board = [[None] * 3 for _ in range(3)]
        room.turn = "X"
        room.scores = {"X": 0, "O": 0, "draws": 0}
        room.round_no = 1
        room.winner = None
        room.win_line = None
        room.chat = []
        for p in room.players:
            p.wants_new_round = False

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _normalize_code(code: Optional[str]) -> Optional[str]:
        if code is None:
            return None
        c = code.strip().upper()
        if not c or len(c) > MAX_ROOM_CODE_LEN:
            return None
        # Allow letters, digits, dash, underscore.
        if not all(ch.isalnum() or ch in "-_" for ch in c):
            return None
        return c

    @staticmethod
    def _normalize_password(pw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Returns (cleaned_password_or_None, error_code_or_None)."""
        if pw is None or pw == "":
            return None, None
        if not isinstance(pw, str):
            return None, "bad_password"
        if len(pw) > MAX_PASSWORD_LEN:
            return None, "bad_password"
        return pw, None

    def _create_room(
        self, code: Optional[str], password: Optional[str]
    ) -> tuple[Optional[Room], Optional[str]]:
        norm = self._normalize_code(code)
        if norm is None:
            return None, "bad_room_code"
        if norm in self.rooms:
            return None, "room_exists"
        pw, err = self._normalize_password(password)
        if err is not None:
            return None, err
        room = Room(code=norm, password=pw)
        self.rooms[norm] = room
        self.log.info(
            "ROOM CREATED room=%s password=%s reason=create",
            norm,
            "yes" if pw else "no",
        )
        return room, None

    def _find_room(
        self, code: Optional[str], password: Optional[str], expected_name: Optional[str]
    ) -> tuple[Optional[Room], Optional[str]]:
        """Look up an existing room for joining. Honors grace-period reconnect:
        a room with grace_task set has 1 active player and 1 pending slot — the
        player whose name matches `pending_name` may take the open slot, anyone
        else is rejected with `room_full`."""
        norm = self._normalize_code(code)
        if norm is None:
            return None, "bad_room_code"
        room = self.rooms.get(norm)
        if room is None:
            return None, "room_not_found"
        in_grace = room.grace_task is not None
        rejoin_ok = in_grace and expected_name is not None and room.pending_name == expected_name
        if not rejoin_ok and len(room.players) >= MAX_PLAYERS:
            return None, "room_full"
        if room.password is not None and (password or "") != room.password:
            return None, "wrong_password"
        return room, None

    def _auto_match(self) -> Room:
        """Find any open, non-password room, or create a new one."""
        for r in self.rooms.values():
            if r.password is None and r.grace_task is None and len(r.players) < MAX_PLAYERS:
                return r
        code = _gen_room_code()
        while code in self.rooms:
            code = _gen_room_code()
        room = Room(code=code)
        self.rooms[code] = room
        self.log.info("ROOM CREATED room=%s password=no reason=auto", code)
        return room

    async def _broadcast(self, room: Room, msg: dict) -> float:
        """Send `msg` to every connected player in the room. Returns fan-out ms."""
        t0 = time.perf_counter()
        recipients = [p for p in room.players if p.connected]
        if recipients:
            await asyncio.gather(
                *(p.send(msg) for p in recipients), return_exceptions=True
            )
        return (time.perf_counter() - t0) * 1000.0

    def _state_payload(self, room: Room, you: Player) -> dict:
        opp = room.opponent_of(you)
        return {
            "type": "state",
            "room": room.code,
            "round": room.round_no,
            "board": room.board,
            "turn": room.turn,
            "you": you.mark,
            "your_name": you.name,
            "opponent": opp.name if opp else None,
            "opponent_connected": opp.connected if opp else False,
            "scores": room.scores,
            "winner": room.winner,
            "line": room.win_line,
            "server_ts": _now_ms(),
        }

    async def _broadcast_state(
        self, room: Room, *, last_move: Optional[tuple[int, int]] = None
    ) -> None:
        t0 = time.perf_counter()
        recipients = [p for p in room.players if p.connected]
        if not recipients:
            return
        # Build a personalized state per player (they each see "you" differently).
        await asyncio.gather(
            *(p.send(self._state_payload(room, p)) for p in recipients),
            return_exceptions=True,
        )
        fanout_ms = (time.perf_counter() - t0) * 1000.0
        move_str = f"({last_move[0]},{last_move[1]})" if last_move else "-"
        self.log.info(
            "SYNC room=%s round=%d turn=%s move=%s recipients=%d fanout_ms=%.2f",
            room.code,
            room.round_no,
            room.turn,
            move_str,
            len(recipients),
            fanout_ms,
        )

    @staticmethod
    def _check_winner(
        board: list[list[Optional[str]]],
    ) -> tuple[Optional[str], Optional[list[list[int]]]]:
        lines: list[list[tuple[int, int]]] = []
        for i in range(3):
            lines.append([(i, 0), (i, 1), (i, 2)])
            lines.append([(0, i), (1, i), (2, i)])
        lines.append([(0, 0), (1, 1), (2, 2)])
        lines.append([(0, 2), (1, 1), (2, 0)])
        for line in lines:
            (r1, c1), (r2, c2), (r3, c3) = line
            v = board[r1][c1]
            if v and v == board[r2][c2] == board[r3][c3]:
                return v, [[r, c] for (r, c) in line]
        if all(cell is not None for row in board for cell in row):
            return "draw", None
        return None, None

    # -- lifecycle --------------------------------------------------------

    @staticmethod
    def _clean_name(name: str, fallback_pid: str) -> str:
        n = (name or "").strip()
        if not n:
            return f"player-{fallback_pid[:4]}"
        return n[:32]

    async def _send_error(self, player: Player, code: str, extra: Optional[dict] = None) -> None:
        msg = {
            "type": "error",
            "code": code,
            "message": ERROR_MESSAGES.get(code, code),
        }
        if extra:
            msg.update(extra)
        await player.send(msg)

    async def create(
        self,
        player: Player,
        name: str,
        room_code: Optional[str],
        password: Optional[str],
    ) -> None:
        name = self._clean_name(name, player.pid)
        player.name = name
        room, err = self._create_room(room_code, password)
        if err is not None:
            await self._send_error(player, err)
            return
        assert room is not None
        await self._seat_player(room, player, rejoined=False)

    async def join(
        self,
        player: Player,
        name: str,
        room_code: Optional[str],
        password: Optional[str],
    ) -> None:
        name = self._clean_name(name, player.pid)
        player.name = name
        room, err = self._find_room(room_code, password, expected_name=name)
        if err is not None:
            await self._send_error(player, err)
            return
        assert room is not None
        # Reconnect path: name matches the grace-window pending name.
        if room.grace_task is not None and room.pending_name == name:
            await self._seat_reconnect(room, player)
            return
        await self._seat_player(room, player, rejoined=False)

    async def auto_match(self, player: Player, name: str) -> None:
        name = self._clean_name(name, player.pid)
        player.name = name
        room = self._auto_match()
        await self._seat_player(room, player, rejoined=False)

    async def _seat_reconnect(self, room: Room, player: Player) -> None:
        assert len(room.players) == 1
        self.lobby_observers.discard(player)
        existing_marks = {p.mark for p in room.players}
        free_mark = "O" if "X" in existing_marks else "X"
        player.mark = free_mark
        player.room_code = room.code
        room.players.append(player)
        if room.grace_task is not None:
            room.grace_task.cancel()
            room.grace_task = None
        room.pending_name = None
        self.log.info(
            "RECONNECT room=%s name=%s mark=%s",
            room.code,
            player.name,
            free_mark,
        )
        opp = room.opponent_of(player)
        await player.send(
            {
                "type": "joined",
                "room": room.code,
                "you": free_mark,
                "opponent": opp.name if opp else None,
                "rejoined": True,
                "has_password": room.password is not None,
            }
        )
        if room.chat:
            await player.send(
                {"type": "chat_history", "messages": list(room.chat)}
            )
        if opp:
            await opp.send({"type": "opponent_rejoined"})
        await self._broadcast_state(room)
        await self._broadcast_rooms()

    async def _seat_player(
        self, room: Room, player: Player, *, rejoined: bool
    ) -> None:
        self.lobby_observers.discard(player)
        used = {p.mark for p in room.players}
        player.mark = "X" if "X" not in used else "O"
        player.room_code = room.code
        room.players.append(player)
        self.log.info(
            "JOIN room=%s name=%s mark=%s transport=%s peer=%s",
            room.code,
            player.name,
            player.mark,
            player.transport,
            player.peer,
        )
        opp = room.opponent_of(player)
        await player.send(
            {
                "type": "joined",
                "room": room.code,
                "you": player.mark,
                "opponent": opp.name if opp else None,
                "rejoined": rejoined,
                "has_password": room.password is not None,
            }
        )
        if room.chat:
            await player.send(
                {"type": "chat_history", "messages": list(room.chat)}
            )
        if len(room.players) == MAX_PLAYERS:
            self.log.info("ROUND %d STARTED room=%s", room.round_no, room.code)
            await self._broadcast(
                room,
                {
                    "type": "log",
                    "level": "info",
                    "message": f"Round {room.round_no} started - {room.players[0].name} (X) vs {room.players[1].name} (O)",
                },
            )
            await self._broadcast_state(room)
        else:
            await player.send(
                {
                    "type": "log",
                    "level": "info",
                    "message": "Waiting for opponent...",
                }
            )
        await self._broadcast_rooms()

    async def move(self, player: Player, row: int, col: int) -> None:
        room = self.rooms.get(player.room_code or "")
        if not room or len(room.players) < 2:
            await self._send_error(player, "no_active_game")
            return
        if room.winner is not None:
            await self._send_error(player, "game_over")
            return
        if room.turn != player.mark:
            await self._send_error(player, "not_your_turn")
            return
        if not (0 <= row < 3 and 0 <= col < 3):
            await self._send_error(player, "out_of_bounds")
            return
        if room.board[row][col] is not None:
            await self._send_error(player, "cell_occupied")
            return

        t_recv = time.perf_counter()
        room.board[row][col] = player.mark
        winner, line = self._check_winner(room.board)
        if winner == "draw":
            room.winner = "draw"
            room.scores["draws"] += 1
            self.log.info(
                "ROUND %d FINISHED room=%s result=draw",
                room.round_no,
                room.code,
            )
        elif winner is not None:
            room.winner = winner
            room.win_line = line
            room.scores[winner] += 1
            self.log.info(
                "ROUND %d FINISHED room=%s winner=%s line=%s",
                room.round_no,
                room.code,
                winner,
                line,
            )
        else:
            room.turn = "O" if room.turn == "X" else "X"

        await self._broadcast_state(room, last_move=(row, col))
        move_to_broadcast_ms = (time.perf_counter() - t_recv) * 1000.0
        self.log.info(
            "MOVE room=%s round=%d by=%s cell=(%d,%d) move_to_broadcast_ms=%.2f",
            room.code,
            room.round_no,
            player.mark,
            row,
            col,
            move_to_broadcast_ms,
        )

    async def new_round(self, player: Player) -> None:
        room = self.rooms.get(player.room_code or "")
        if not room or len(room.players) < 2:
            return
        if room.winner is None:
            await self._send_error(player, "round_running")
            return
        player.wants_new_round = True
        if all(p.wants_new_round for p in room.players):
            room.round_no += 1
            room.reset_board()
            self.log.info("ROUND %d STARTED room=%s", room.round_no, room.code)
            await self._broadcast(
                room,
                {
                    "type": "log",
                    "level": "info",
                    "message": f"Round {room.round_no} started",
                },
            )
            await self._broadcast_state(room)
        else:
            await self._broadcast(
                room,
                {
                    "type": "log",
                    "level": "info",
                    "message": f"{player.name} wants to play another round",
                },
            )

    async def ping(self, player: Player, client_ts: Any) -> None:
        await player.send(
            {"type": "pong", "ts": client_ts, "server_ts": _now_ms()}
        )

    async def leave(self, player: Player) -> None:
        """Graceful leave initiated by the client.

        The leaving player is returned to lobby-observer status with their
        connection still open. The room itself stays alive; if a survivor
        remains they are bumped back to the "waiting for opponent" screen
        with a fresh game state, and the room re-appears as `1/2` in the
        lobby list, ready for a new opponent to join."""
        room = self.rooms.get(player.room_code or "")
        if room is None or player not in room.players:
            self.lobby_observers.add(player)
            return

        self.log.info(
            "LEAVE name=%s room=%s mark=%s",
            player.name,
            room.code,
            player.mark,
        )

        # Cancel a stale grace timer (e.g. the opponent had DC'd and this
        # player is now also bowing out before the timer expired).
        if room.grace_task is not None:
            room.grace_task.cancel()
            room.grace_task = None
            room.pending_name = None

        room.players.remove(player)
        player.room_code = None
        player.mark = None
        player.name = ""
        self.lobby_observers.add(player)

        if not room.players:
            # Last person out — clean up the empty room.
            self.rooms.pop(room.code, None)
            self.log.info("ROOM CLOSED room=%s reason=empty", room.code)
        else:
            # Survivor stays. Reset the room so the next joiner walks into
            # a fresh game.
            survivor = room.players[0]
            self._reset_room_session(room)
            self.log.info(
                "ROOM RESET room=%s reason=opponent_left survivor=%s",
                room.code,
                survivor.name,
            )
            await survivor.send(
                {
                    "type": "log",
                    "level": "info",
                    "message": "Opponent left. Waiting for a new opponent…",
                }
            )
            # A fresh `joined` with opponent=None bumps the survivor's UI
            # back to the waiting screen.
            await survivor.send(
                {
                    "type": "joined",
                    "room": room.code,
                    "you": survivor.mark,
                    "opponent": None,
                    "rejoined": False,
                    "has_password": room.password is not None,
                }
            )

        await self._send_lobby_state(player)
        await self._broadcast_rooms()

    async def on_connection_lost(self, player: Player) -> None:
        """Called when the underlying socket is gone (unexpected or terminal)."""
        self.lobby_observers.discard(player)
        if not player.connected:
            return
        player.connected = False
        room = self.rooms.get(player.room_code or "")
        self.log.info(
            "DISCONNECT name=%s room=%s mark=%s",
            player.name,
            room.code if room else "-",
            player.mark,
        )
        if not room or player not in room.players:
            await self._broadcast_rooms()
            return

        # If still in lobby (only one player) just drop the room.
        if len(room.players) == 1:
            room.players.remove(player)
            if room.grace_task is not None:
                room.grace_task.cancel()
                room.grace_task = None
            self.rooms.pop(room.code, None)
            self.log.info("ROOM CLOSED room=%s reason=empty", room.code)
            await self._broadcast_rooms()
            return

        # Two players -> opponent stays, start grace timer.
        opponent = room.opponent_of(player)
        room.players.remove(player)
        room.pending_name = player.name
        if opponent:
            await opponent.send(
                {
                    "type": "opponent_left",
                    "grace_seconds": GRACE_SECONDS,
                    "name": player.name,
                }
            )
        if room.grace_task is not None:
            room.grace_task.cancel()
        room.grace_task = asyncio.create_task(self._grace_timer(room))
        await self._broadcast_rooms()

    async def _grace_timer(self, room: Room) -> None:
        try:
            await asyncio.sleep(GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        # Grace expired without the disconnected player coming back. The
        # room itself lives on: any remaining player is bumped to a fresh
        # "waiting" state so a new opponent can take the empty seat.
        room.grace_task = None
        room.pending_name = None
        if not room.players:
            if room.code in self.rooms:
                del self.rooms[room.code]
            self.log.info("ROOM CLOSED room=%s reason=both_left", room.code)
            await self._broadcast_rooms()
            return
        survivor = room.players[0]
        self._reset_room_session(room)
        self.log.info(
            "ROOM RESET room=%s reason=grace_expired survivor=%s",
            room.code,
            survivor.name,
        )
        await survivor.send(
            {
                "type": "log",
                "level": "info",
                "message": "Opponent didn't return. Waiting for a new opponent…",
            }
        )
        await survivor.send(
            {
                "type": "joined",
                "room": room.code,
                "you": survivor.mark,
                "opponent": None,
                "rejoined": False,
                "has_password": room.password is not None,
            }
        )
        await self._broadcast_rooms()


# ---------------------------------------------------------------------------
# TCP and WebSocket adapters
# ---------------------------------------------------------------------------


async def handle_tcp(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    manager: RoomManager,
    log: logging.Logger,
) -> None:
    peer_t = writer.get_extra_info("peername")
    peer = f"{peer_t[0]}:{peer_t[1]}" if peer_t else "?"
    pid = uuid.uuid4().hex

    async def send(msg: dict) -> None:
        try:
            writer.write((json.dumps(msg) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def close() -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    player = Player(
        pid=pid, name="", transport="tcp", send=send, close=close, peer=peer
    )
    log.info("CONNECT transport=tcp peer=%s pid=%s", peer, pid[:8])
    await manager.on_connect(player)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                await send({"type": "error", "code": "invalid_json", "message": ERROR_MESSAGES["invalid_json"]})
                continue
            await dispatch(manager, player, msg)
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    except Exception as exc:  # pragma: no cover
        log.exception("tcp handler crashed: %s", exc)
    finally:
        await manager.on_connection_lost(player)
        await close()


async def handle_ws(
    websocket: Any,
    manager: RoomManager,
    log: logging.Logger,
) -> None:
    peer_t = websocket.remote_address
    peer = f"{peer_t[0]}:{peer_t[1]}" if peer_t else "?"
    pid = uuid.uuid4().hex

    async def send(msg: dict) -> None:
        try:
            await websocket.send(json.dumps(msg))
        except ConnectionClosed:
            pass

    async def close() -> None:
        try:
            await websocket.close()
        except Exception:
            pass

    player = Player(
        pid=pid, name="", transport="ws", send=send, close=close, peer=peer
    )
    log.info("CONNECT transport=ws peer=%s pid=%s", peer, pid[:8])
    await manager.on_connect(player)

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                await send({"type": "error", "code": "invalid_json", "message": ERROR_MESSAGES["invalid_json"]})
                continue
            await dispatch(manager, player, msg)
    except ConnectionClosed:
        pass
    except Exception as exc:  # pragma: no cover
        log.exception("ws handler crashed: %s", exc)
    finally:
        await manager.on_connection_lost(player)


async def dispatch(manager: RoomManager, player: Player, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "create":
        await manager.create(
            player,
            msg.get("name", ""),
            msg.get("room"),
            msg.get("password"),
        )
    elif mtype == "join":
        # Backward-compatible: empty/null room → auto-match.
        room_code = msg.get("room")
        if room_code is None or (isinstance(room_code, str) and not room_code.strip()):
            await manager.auto_match(player, msg.get("name", ""))
        else:
            await manager.join(
                player,
                msg.get("name", ""),
                room_code,
                msg.get("password"),
            )
    elif mtype == "auto":
        await manager.auto_match(player, msg.get("name", ""))
    elif mtype == "move":
        try:
            row = int(msg["row"])
            col = int(msg["col"])
        except (KeyError, TypeError, ValueError):
            await manager._send_error(player, "bad_move")
            return
        await manager.move(player, row, col)
    elif mtype == "new_round":
        await manager.new_round(player)
    elif mtype == "ping":
        await manager.ping(player, msg.get("ts"))
    elif mtype == "leave":
        await manager.leave(player)
    elif mtype == "list_rooms":
        await manager.list_rooms(player)
    elif mtype == "chat":
        # Server uses player.name as the authoritative nickname.
        await manager.chat(player, msg.get("text", ""))
    else:
        await manager._send_error(player, "unknown_type", {"received": str(mtype)[:32]})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    log = logging.getLogger("ttt")
    manager = RoomManager(log)

    tcp_server = await asyncio.start_server(
        lambda r, w: handle_tcp(r, w, manager, log), args.tcp_host, args.tcp_port
    )
    log.info("TCP listening on %s:%d", args.tcp_host, args.tcp_port)

    ws_server = None
    if not args.no_ws:
        if websockets is None:
            log.warning("websockets package not installed; WS disabled")
        else:
            ws_server = await websockets.serve(
                lambda ws: handle_ws(ws, manager, log),
                args.ws_host,
                args.ws_port,
                ping_interval=20,
                ping_timeout=20,
            )
            log.info("WS  listening on %s:%d", args.ws_host, args.ws_port)

    async with tcp_server:
        try:
            await asyncio.Future()  # run forever
        finally:
            if ws_server is not None:
                ws_server.close()
                await ws_server.wait_closed()


def main() -> None:
    p = argparse.ArgumentParser(description="Tic-Tac-Toe game server")
    p.add_argument("--tcp-host", default=TCP_HOST)
    p.add_argument("--tcp-port", type=int, default=TCP_PORT)
    p.add_argument("--ws-host", default=WS_HOST)
    p.add_argument("--ws-port", type=int, default=WS_PORT)
    p.add_argument("--no-ws", action="store_true", help="disable WebSocket listener")
    p.add_argument("--log-file", default="server.log")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        handlers=handlers,
    )

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.getLogger("ttt").info("shutting down (Ctrl+C)")


if __name__ == "__main__":
    main()
