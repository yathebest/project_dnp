"""
Load test: spawns 3-5 raw TCP clients, pairs them into rooms, plays
random valid moves, measures RTT (client ping/pong) and round-trip
move latency. Reports p50/p95/p99 ms.

Usage:
    python tests/load_test.py --clients 5 --rounds 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from typing import Optional


def now_ms() -> int:
    return int(time.time() * 1000)


class Bot:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        room: str,
        rounds: int,
        rtt_samples: list[float],
        move_samples: list[float],
        verbose: bool = False,
        create: bool = False,
        delay_start: float = 0.0,
    ) -> None:
        self.host, self.port = host, port
        self.name = name
        self.room = room
        self.create = create
        self.delay_start = delay_start
        self.rounds_target = rounds
        self.rounds_done = 0
        self.rtt_samples = rtt_samples
        self.move_samples = move_samples
        self.verbose = verbose
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.you: Optional[str] = None
        self.last_state: Optional[dict] = None
        self.move_sent_at: dict[tuple[int, int], float] = {}
        self.done = asyncio.Event()

    async def send(self, msg: dict) -> None:
        assert self.writer is not None
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def play(self) -> None:
        if self.delay_start:
            await asyncio.sleep(self.delay_start)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        msg_type = "create" if self.create else "join"
        await self.send({"type": msg_type, "name": self.name, "room": self.room})

        ping_task = asyncio.create_task(self._ping_loop())
        join_timeout_task = asyncio.create_task(self._join_timeout())
        try:
            while not self.done.is_set():
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError:
                    continue
                await self._on_message(msg)
        finally:
            ping_task.cancel()
            join_timeout_task.cancel()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def _join_timeout(self) -> None:
        """Leave if we never get a `state` (opponent never showed up)."""
        try:
            await asyncio.sleep(8.0)
            if self.last_state is None and not self.done.is_set():
                if self.verbose:
                    print(f"  [{self.name}] no opponent, leaving")
                try:
                    await self.send({"type": "leave"})
                except Exception:
                    pass
                self.done.set()
                # Force the readline loop out by closing the writer.
                try:
                    if self.writer is not None:
                        self.writer.close()
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    async def _ping_loop(self) -> None:
        try:
            while not self.done.is_set():
                await self.send({"type": "ping", "ts": now_ms()})
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def _on_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "joined":
            self.you = msg.get("you")
            if self.verbose:
                print(f"  [{self.name}] joined {msg.get('room')} as {self.you}")
        elif t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)):
                rtt = now_ms() - ts
                if rtt >= 0:
                    self.rtt_samples.append(float(rtt))
        elif t == "state":
            self.last_state = msg
            # Confirm any outstanding move's RTT on EVERY state, even those
            # observed while it's the opponent's turn (otherwise we end up
            # measuring the opponent's think-time too).
            board = msg.get("board") or []
            if self.move_sent_at and board:
                for (r, c), t0 in list(self.move_sent_at.items()):
                    if (
                        0 <= r < 3
                        and 0 <= c < 3
                        and len(board) == 3
                        and board[r][c] is not None
                    ):
                        self.move_samples.append((time.perf_counter() - t0) * 1000.0)
                        self.move_sent_at.pop((r, c), None)
            await self._maybe_move(msg)
        elif t == "forfeit":
            self.done.set()

    async def _maybe_move(self, msg: dict) -> None:
        if msg.get("winner") is not None:
            self.rounds_done += 1
            if self.rounds_done >= self.rounds_target:
                await self.send({"type": "leave"})
                self.done.set()
                return
            await self.send({"type": "new_round"})
            return
        if msg.get("turn") != self.you:
            return  # opponent's turn; we already updated samples in _on_message

        board = msg.get("board") or [[None] * 3] * 3
        free = [(r, c) for r in range(3) for c in range(3) if board[r][c] is None]
        if not free:
            return
        r, c = random.choice(free)
        await asyncio.sleep(random.uniform(0.02, 0.06))  # tiny think time
        self.move_sent_at[(r, c)] = time.perf_counter()
        await self.send({"type": "move", "row": r, "col": c})


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def report(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:<28} no samples")
        return
    print(
        f"  {label:<28} n={len(values):<5} "
        f"min={min(values):6.2f}  p50={percentile(values,0.5):6.2f}  "
        f"p95={percentile(values,0.95):6.2f}  p99={percentile(values,0.99):6.2f}  "
        f"max={max(values):6.2f}  avg={statistics.mean(values):6.2f}"
    )


async def main_async(args: argparse.Namespace) -> None:
    rtt_samples: list[float] = []
    move_samples: list[float] = []

    n = args.clients
    # Pair clients into rooms of 2. Even index → creator, odd index → joiner.
    # Joiners get a small delay so the room exists when they connect.
    bots: list[Bot] = []
    for i in range(n):
        room = f"L{i // 2}"
        is_creator = (i % 2 == 0)
        delay = 0.0 if is_creator else 0.05
        bots.append(Bot(
            args.host, args.port, f"bot{i}", room, args.rounds,
            rtt_samples, move_samples, args.verbose,
            create=is_creator, delay_start=delay,
        ))

    print(f"Spawning {n} clients across {(n + 1) // 2} rooms, {args.rounds} rounds each…")
    t0 = time.time()
    await asyncio.gather(*(b.play() for b in bots), return_exceptions=True)
    elapsed = time.time() - t0
    print(f"\nFinished in {elapsed:.2f}s")
    print("\nLatency report (milliseconds):")
    report("RTT (client ping->pong)",  rtt_samples)
    report("Move RTT (move->state)",   move_samples)


def main() -> None:
    p = argparse.ArgumentParser(description="Tic-Tac-Toe load test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--clients", type=int, default=5)
    p.add_argument("--rounds",  type=int, default=5)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
