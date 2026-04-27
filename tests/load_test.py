import argparse
import asyncio
import json
import random
import statistics
import time


def now_ms():
    return int(time.time() * 1000)


class Bot:
    def __init__(self, host, port, name, room, rounds, rtt, moves,
                 verbose=False, create=False, delay=0.0):
        self.host = host
        self.port = port
        self.name = name
        self.room = room
        self.create = create
        self.delay = delay
        self.rounds_target = rounds
        self.rounds_done = 0
        self.rtt = rtt
        self.moves = moves
        self.verbose = verbose
        self.reader = None
        self.writer = None
        self.you = None
        self.last_state = None
        self.move_sent_at = {}
        self.done = asyncio.Event()

    async def send(self, msg):
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def play(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        msg_type = "create" if self.create else "join"
        await self.send({"type": msg_type, "name": self.name, "room": self.room})

        ping_task = asyncio.create_task(self.ping_loop())
        idle_task = asyncio.create_task(self.idle_timeout())
        try:
            while not self.done.is_set():
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    continue
                await self.on_message(msg)
        finally:
            ping_task.cancel()
            idle_task.cancel()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def idle_timeout(self):
        try:
            await asyncio.sleep(8.0)
            if self.last_state is None and not self.done.is_set():
                if self.verbose:
                    print("  [%s] no opponent, leaving" % self.name)
                try:
                    await self.send({"type": "leave"})
                except Exception:
                    pass
                self.done.set()
                try:
                    if self.writer is not None:
                        self.writer.close()
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    async def ping_loop(self):
        try:
            while not self.done.is_set():
                await self.send({"type": "ping", "ts": now_ms()})
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def on_message(self, msg):
        t = msg.get("type")
        if t == "joined":
            self.you = msg.get("you")
            if self.last_state is not None and msg.get("opponent") is None:
                self.done.set()
            if self.verbose:
                print("  [%s] joined %s as %s" % (self.name, msg.get("room"), self.you))
        elif t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)):
                rtt = now_ms() - ts
                if rtt >= 0:
                    self.rtt.append(float(rtt))
        elif t == "state":
            self.last_state = msg
            board = msg.get("board") or []
            if self.move_sent_at and len(board) == 3:
                for (r, c), t0 in list(self.move_sent_at.items()):
                    if 0 <= r < 3 and 0 <= c < 3 and board[r][c] is not None:
                        self.moves.append((time.perf_counter() - t0) * 1000.0)
                        self.move_sent_at.pop((r, c), None)
            await self.maybe_move(msg)
        elif t == "forfeit":
            self.done.set()

    async def maybe_move(self, msg):
        if msg.get("winner") is not None:
            self.rounds_done += 1
            if self.rounds_done >= self.rounds_target:
                await self.send({"type": "leave"})
                self.done.set()
                return
            await self.send({"type": "new_round"})
            return
        if msg.get("turn") != self.you:
            return
        board = msg.get("board") or [[None] * 3] * 3
        free = [(r, c) for r in range(3) for c in range(3) if board[r][c] is None]
        if not free:
            return
        r, c = random.choice(free)
        await asyncio.sleep(random.uniform(0.02, 0.06))
        self.move_sent_at[(r, c)] = time.perf_counter()
        await self.send({"type": "move", "row": r, "col": c})


def percentile(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def report(label, values):
    if not values:
        print("  %-28s no samples" % label)
        return
    print("  %-28s n=%-5d min=%6.2f  p50=%6.2f  p95=%6.2f  p99=%6.2f  max=%6.2f  avg=%6.2f" % (
        label, len(values), min(values),
        percentile(values, 0.5), percentile(values, 0.95), percentile(values, 0.99),
        max(values), statistics.mean(values)))


async def main_async(args):
    rtt = []
    moves = []
    bots = []
    for i in range(args.clients):
        room = "L%d" % (i // 2)
        is_creator = (i % 2 == 0)
        delay = 0.0 if is_creator else 0.05
        bots.append(Bot(args.host, args.port, "bot%d" % i, room, args.rounds,
                        rtt, moves, args.verbose,
                        create=is_creator, delay=delay))

    print("Spawning %d clients across %d rooms, %d rounds each..." % (
        args.clients, (args.clients + 1) // 2, args.rounds))
    t0 = time.time()
    await asyncio.gather(*(b.play() for b in bots), return_exceptions=True)
    print("\nFinished in %.2fs" % (time.time() - t0))
    print("\nLatency report (milliseconds):")
    report("RTT (client ping->pong)", rtt)
    report("Move RTT (move->state)", moves)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--clients", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
