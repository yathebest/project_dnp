"""
Raw TCP CLI client for the Tic-Tac-Toe server.

Newline-delimited JSON protocol, identical to what server.py expects on
port 5555. Two coroutines run concurrently:
  * receiver - reads lines from the server and prints them.
  * input loop - reads moves from stdin and sends them.

Also runs a periodic ping/pong every 2 seconds so we can show RTT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Optional


def now_ms() -> int:
    return int(time.time() * 1000)


def render_board(board: list[list[Optional[str]]]) -> str:
    rows = []
    for i, row in enumerate(board):
        cells = [c if c else " " for c in row]
        rows.append(" " + " | ".join(cells))
        if i < 2:
            rows.append("---+---+---")
    return "\n".join(rows)


class Client:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        room: Optional[str],
        *,
        create: bool = False,
        password: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.room = room
        self.create = create
        self.password = password
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.you: Optional[str] = None
        self.last_state: Optional[dict] = None
        self.rtt_ms: Optional[float] = None
        self.stop = asyncio.Event()

    async def send(self, msg: dict) -> None:
        assert self.writer is not None
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def recv_loop(self) -> None:
        assert self.reader is not None
        while not self.stop.is_set():
            line = await self.reader.readline()
            if not line:
                print("\n[server closed connection]")
                self.stop.set()
                return
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            await self._on_message(msg)

    async def _on_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "error" and msg.get("code") in {
            "room_not_found", "room_full", "room_exists",
            "wrong_password", "bad_room_code", "bad_password",
        }:
            print(f"\n[lobby error: {msg.get('message') or msg.get('code')}]")
            self.stop.set()
            return
        if t == "joined":
            self.you = msg.get("you")
            print(
                f"\n[joined room={msg.get('room')} you={self.you} "
                f"opponent={msg.get('opponent')} rejoined={msg.get('rejoined')}]"
            )
        elif t == "state":
            self.last_state = msg
            self._print_state(msg)
        elif t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)):
                self.rtt_ms = (now_ms() - ts)
        elif t == "opponent_left":
            print(
                f"\n[opponent {msg.get('name')} disconnected; "
                f"{msg.get('grace_seconds')}s grace before forfeit]"
            )
        elif t == "opponent_rejoined":
            print("\n[opponent rejoined]")
        elif t == "forfeit":
            print(f"\n[FORFEIT - winner={msg.get('winner')} reason={msg.get('reason')}]")
        elif t == "log":
            print(f"\n[server: {msg.get('message')}]")
        elif t == "error":
            print(f"\n[error: {msg.get('message')}]")
        else:
            print(f"\n[unknown message: {msg}]")

    def _print_state(self, msg: dict) -> None:
        board = msg.get("board", [[None] * 3] * 3)
        turn = msg.get("turn")
        winner = msg.get("winner")
        scores = msg.get("scores", {})
        rno = msg.get("round")
        rtt = f"{self.rtt_ms:.0f}ms" if self.rtt_ms is not None else "n/a"
        print()
        print(f"--- Round {rno}  scores X={scores.get('X')} O={scores.get('O')} "
              f"draws={scores.get('draws')}  RTT={rtt} ---")
        print(render_board(board))
        if winner == "draw":
            print(">> DRAW. Send 'n' to start a new round.")
        elif winner:
            print(f">> WINNER: {winner}. Send 'n' to start a new round.")
        else:
            who = "your" if turn == self.you else "opponent's"
            print(f"Turn: {turn} ({who} turn). Type row,col (e.g. 1,2) or 'q'.")

    async def ping_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.send({"type": "ping", "ts": now_ms()})
            except Exception:
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def input_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                self.stop.set()
                return
            line = line.strip()
            if not line:
                continue
            if line in ("q", "quit", "exit"):
                await self.send({"type": "leave"})
                self.stop.set()
                return
            if line in ("n", "new", "again"):
                await self.send({"type": "new_round"})
                continue
            try:
                parts = [int(x) for x in line.replace(" ", "").split(",")]
                if len(parts) != 2:
                    raise ValueError
                row, col = parts
            except ValueError:
                print("[usage: row,col  e.g. 1,2  |  n=new round  |  q=quit]")
                continue
            await self.send({"type": "move", "row": row, "col": col})

    async def run(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        if self.create:
            initial: dict = {"type": "create", "name": self.name, "room": self.room}
        elif self.room:
            initial = {"type": "join", "name": self.name, "room": self.room}
        else:
            initial = {"type": "auto", "name": self.name}
        if self.password is not None and self.password != "":
            initial["password"] = self.password
        await self.send(initial)
        await asyncio.gather(self.recv_loop(), self.ping_loop(), self.input_loop())
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


def main() -> None:
    p = argparse.ArgumentParser(description="Tic-Tac-Toe TCP CLI client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--name", default="player")
    p.add_argument("--room", default=None, help="room code; omit for auto-match")
    p.add_argument("--create", action="store_true",
                   help="create a new room (fails if --room already exists)")
    p.add_argument("--password", default=None,
                   help="set on --create; required when joining a locked room")
    args = p.parse_args()
    if args.create and not args.room:
        p.error("--create requires --room")
    client = Client(
        args.host, args.port, args.name, args.room,
        create=args.create, password=args.password,
    )
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
