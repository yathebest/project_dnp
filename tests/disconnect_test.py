"""
Disconnect resilience test.

Scenario:
  1. Connect Alice + Bob to room "DC".
  2. Each plays a couple of moves.
  3. Alice's TCP socket is forcibly closed mid-game (simulating
     unexpected disconnect — e.g., process killed, network dropped).
  4. Verify Bob receives `opponent_left` quickly.
  5. Wait past the grace window and verify the room *resets* rather than
     closes: Bob receives a fresh `joined` with opponent=None (which
     bumps his client back to the waiting screen) and the room remains
     visible in the lobby list as `1/2`, ready for a new opponent.

Exits with code 0 on success, 1 on assertion failure.

Usage:
    python tests/disconnect_test.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Optional


GRACE_BUFFER = 2.0  # extra seconds beyond GRACE_SECONDS to wait


async def send_json(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def read_json(reader: asyncio.StreamReader, timeout: float) -> Optional[dict]:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8").strip())
    except json.JSONDecodeError:
        return None


async def drain_until(
    reader: asyncio.StreamReader, want_type: str, timeout: float
) -> Optional[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await read_json(reader, timeout=max(0.01, deadline - time.time()))
        if msg is None:
            return None
        if msg.get("type") == want_type:
            return msg
    return None


async def run(host: str, port: int, grace_seconds: int) -> int:
    print(f"Connecting two clients to {host}:{port} (room=DC)…")

    a_reader, a_writer = await asyncio.open_connection(host, port)
    b_reader, b_writer = await asyncio.open_connection(host, port)
    # Alice creates the room, Bob joins it.
    await send_json(a_writer, {"type": "create", "name": "Alice", "room": "DC"})
    a_joined_first = await drain_until(a_reader, "joined", 2.0)
    assert a_joined_first is not None, "Alice did not get 'joined' from create"
    await send_json(b_writer, {"type": "join",   "name": "Bob",   "room": "DC"})

    # Bob's "joined", and the game-start state for both.
    a_joined = a_joined_first
    b_joined = await drain_until(b_reader, "joined", 2.0)
    assert a_joined and b_joined, "missing 'joined' messages"
    print(f"  Alice={a_joined.get('you')}  Bob={b_joined.get('you')}")

    # Wait for first state (both connected).
    await drain_until(a_reader, "state", 2.0)
    await drain_until(b_reader, "state", 2.0)

    # Alice (X) plays (0,0).
    if a_joined.get("you") == "X":
        x_writer, x_reader = a_writer, a_reader
        o_writer, o_reader = b_writer, b_reader
        x_name = "Alice"
    else:
        x_writer, x_reader = b_writer, b_reader
        o_writer, o_reader = a_writer, a_reader
        x_name = "Bob"
    print(f"  X is {x_name}; making 2 moves before disconnect…")

    await send_json(x_writer, {"type": "move", "row": 0, "col": 0})
    await drain_until(x_reader, "state", 2.0)
    await drain_until(o_reader, "state", 2.0)
    await send_json(o_writer, {"type": "move", "row": 1, "col": 1})
    await drain_until(o_reader, "state", 2.0)
    await drain_until(x_reader, "state", 2.0)

    # Forcibly drop Alice without sending "leave" — abrupt close.
    print("  >>> Dropping Alice's TCP socket abruptly…")
    drop_t0 = time.perf_counter()
    a_writer.close()
    try:
        await a_writer.wait_closed()
    except Exception:
        pass

    # Bob must receive opponent_left fast.
    msg = await drain_until(b_reader, "opponent_left", 3.0)
    assert msg is not None, "Bob did not receive 'opponent_left' within 3s"
    detect_ms = (time.perf_counter() - drop_t0) * 1000.0
    print(f"  Bob received 'opponent_left' in {detect_ms:.1f} ms [OK]")
    print(f"     payload: grace_seconds={msg.get('grace_seconds')} name={msg.get('name')}")

    # Bob waits out the grace window. With the new "rooms stay alive"
    # behaviour, instead of receiving `forfeit` he receives a fresh
    # `joined` with opponent=None — that's how the server bumps him
    # back to the waiting screen.
    print(f"  Waiting out the {grace_seconds}s grace window…")
    msg = await drain_until(b_reader, "joined", grace_seconds + GRACE_BUFFER)
    assert msg is not None, "Bob did not receive a fresh 'joined' after grace expired"
    assert msg.get("opponent") is None, (
        f"expected opponent=None after grace, got {msg.get('opponent')!r}"
    )
    print(
        f"  Bob received fresh 'joined' (you={msg.get('you')}, opponent=None) [OK]"
    )

    # And the room must still be advertised as joinable (1/2) in the
    # lobby snapshot — verify by spinning up a third observer.
    o_reader, o_writer = await asyncio.open_connection(host, port)
    rooms = await drain_until(o_reader, "rooms", 2.0)
    assert rooms is not None, "third observer did not get rooms snapshot"
    dc = next((r for r in rooms.get("rooms", []) if r.get("code") == "DC"), None)
    assert dc is not None, "room DC vanished after grace expired"
    assert dc.get("players") == 1 and dc.get("max") == 2 and dc.get("joinable") is True, (
        f"DC not joinable after grace: {dc}"
    )
    print(f"  Room DC still listed as {dc['players']}/{dc['max']} joinable=True [OK]")

    b_writer.close()
    o_writer.close()
    for w in (b_writer, o_writer):
        try:
            await w.wait_closed()
        except Exception:
            pass
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Disconnect resilience test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--grace", type=int, default=15,
                   help="server's grace seconds (must match server config)")
    args = p.parse_args()

    try:
        rc = asyncio.run(run(args.host, args.port, args.grace))
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(2)
    print("\nPASS")
    sys.exit(rc)


if __name__ == "__main__":
    main()
