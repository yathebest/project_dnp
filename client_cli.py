import argparse
import asyncio
import json
import sys
import time


def now_ms():
    return int(time.time() * 1000)


def render_board(board):
    rows = []
    for i, row in enumerate(board):
        cells = [c if c else " " for c in row]
        rows.append(" " + " | ".join(cells))
        if i < 2:
            rows.append("---+---+---")
    return "\n".join(rows)


LOBBY_ERRORS = {
    "room_not_found", "room_full", "room_exists",
    "wrong_password", "bad_room_code", "bad_password",
}


class Client:
    def __init__(self, host, port, name, room, create=False, password=None):
        self.host = host
        self.port = port
        self.name = name
        self.room = room
        self.create = create
        self.password = password
        self.reader = None
        self.writer = None
        self.you = None
        self.last_state = None
        self.rtt_ms = None
        self.stop = asyncio.Event()

    async def send(self, msg):
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def recv_loop(self):
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
            await self.on_message(msg)

    async def on_message(self, msg):
        t = msg.get("type")
        if t == "error" and msg.get("code") in LOBBY_ERRORS:
            print("\n[lobby error: %s]" % (msg.get("message") or msg.get("code")))
            self.stop.set()
            return
        if t == "joined":
            self.you = msg.get("you")
            print("\n[joined room=%s you=%s opponent=%s rejoined=%s]" % (
                msg.get("room"), self.you, msg.get("opponent"), msg.get("rejoined")))
        elif t == "state":
            self.last_state = msg
            self.print_state(msg)
        elif t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)):
                self.rtt_ms = now_ms() - ts
        elif t == "opponent_left":
            print("\n[opponent %s disconnected; %ss grace before forfeit]" % (
                msg.get("name"), msg.get("grace_seconds")))
        elif t == "opponent_rejoined":
            print("\n[opponent rejoined]")
        elif t == "forfeit":
            print("\n[FORFEIT - winner=%s reason=%s]" % (
                msg.get("winner"), msg.get("reason")))
        elif t == "log":
            print("\n[server: %s]" % msg.get("message"))
        elif t == "error":
            print("\n[error: %s]" % msg.get("message"))
        elif t == "rooms":
            pass
        elif t == "chat":
            print("\n[chat] %s: %s" % (msg.get("nickname"), msg.get("text")))
        elif t == "chat_history":
            for m in msg.get("messages", []):
                print("[chat] %s: %s" % (m.get("nickname"), m.get("text")))

    def print_state(self, msg):
        board = msg.get("board", [[None] * 3] * 3)
        turn = msg.get("turn")
        winner = msg.get("winner")
        scores = msg.get("scores", {})
        rno = msg.get("round")
        rtt = ("%dms" % self.rtt_ms) if self.rtt_ms is not None else "n/a"
        print()
        print("--- Round %s  scores X=%s O=%s draws=%s  RTT=%s ---" % (
            rno, scores.get("X"), scores.get("O"), scores.get("draws"), rtt))
        print(render_board(board))
        if winner == "draw":
            print(">> DRAW. Send 'n' to start a new round.")
        elif winner:
            print(">> WINNER: %s. Send 'n' to start a new round." % winner)
        else:
            who = "your" if turn == self.you else "opponent's"
            print("Turn: %s (%s turn). Type row,col (e.g. 1,2) or 'q'." % (turn, who))

    async def ping_loop(self):
        while not self.stop.is_set():
            try:
                await self.send({"type": "ping", "ts": now_ms()})
            except Exception:
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def input_loop(self):
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

    async def run(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        if self.create:
            initial = {"type": "create", "name": self.name, "room": self.room}
        elif self.room:
            initial = {"type": "join", "name": self.name, "room": self.room}
        else:
            initial = {"type": "auto", "name": self.name}
        if self.password:
            initial["password"] = self.password
        await self.send(initial)
        await asyncio.gather(self.recv_loop(), self.ping_loop(), self.input_loop())
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--name", default="player")
    parser.add_argument("--room", default=None)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--password", default=None)
    args = parser.parse_args()
    if args.create and not args.room:
        parser.error("--create requires --room")
    client = Client(args.host, args.port, args.name, args.room,
                    create=args.create, password=args.password)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass
