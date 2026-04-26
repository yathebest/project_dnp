"""
Reconnect smoke test.

  1. Alice + Bob join room "RC".
  2. Alice's socket is dropped abruptly mid-game.
  3. Within the grace window, a NEW socket joins as the same name+room.
  4. The server should restore Alice's seat and resume the game.

Exits 0 on success, 1 on failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional


async def send_json(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def read_until(
    reader: asyncio.StreamReader, want_type: str, timeout: float
) -> Optional[dict]:
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                return None
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            if msg.get("type") == want_type:
                return msg
    except asyncio.TimeoutError:
        return None


async def run(host: str, port: int) -> int:
    print("Connecting Alice and Bob to room RC…")
    a_r, a_w = await asyncio.open_connection(host, port)
    b_r, b_w = await asyncio.open_connection(host, port)
    await send_json(a_w, {"type": "create", "name": "Alice", "room": "RC"})
    a_joined = await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join",   "name": "Bob",   "room": "RC"})
    b_joined = await read_until(b_r, "joined", 2.0)
    assert a_joined and b_joined, "missing 'joined'"
    print(f"  Alice={a_joined.get('you')}  Bob={b_joined.get('you')}")
    await read_until(a_r, "state", 2.0)
    await read_until(b_r, "state", 2.0)

    # X plays one move (whichever player got X).
    x_w = a_w if a_joined.get("you") == "X" else b_w
    x_r = a_r if a_joined.get("you") == "X" else b_r
    o_r = b_r if a_joined.get("you") == "X" else a_r
    await send_json(x_w, {"type": "move", "row": 1, "col": 1})
    await read_until(x_r, "state", 2.0)
    await read_until(o_r, "state", 2.0)

    # Drop Alice abruptly.
    print("  >>> Dropping Alice's socket…")
    a_w.close()
    try:
        await a_w.wait_closed()
    except Exception:
        pass

    # Bob sees opponent_left.
    msg = await read_until(b_r, "opponent_left", 3.0)
    assert msg is not None, "Bob did not receive opponent_left"
    print("  Bob saw opponent_left [OK]")

    # Now reconnect as Alice within the grace window.
    print("  >>> Reconnecting as Alice…")
    a2_r, a2_w = await asyncio.open_connection(host, port)
    await send_json(a2_w, {"type": "join", "name": "Alice", "room": "RC"})
    j = await read_until(a2_r, "joined", 3.0)
    assert j and j.get("rejoined"), f"expected rejoined=True, got {j}"
    print(f"  Alice rejoined as {j.get('you')} [OK]")

    msg = await read_until(b_r, "opponent_rejoined", 3.0)
    assert msg is not None, "Bob did not receive opponent_rejoined"
    print("  Bob saw opponent_rejoined [OK]")

    # Both should now receive a fresh state with the previously placed (1,1).
    s = await read_until(a2_r, "state", 3.0)
    assert s and s["board"][1][1] is not None, "board not restored"
    print(f"  Board state restored: cell (1,1)={s['board'][1][1]} [OK]")

    a2_w.close(); b_w.close()
    try:
        await a2_w.wait_closed()
        await b_w.wait_closed()
    except Exception:
        pass
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    args = p.parse_args()
    try:
        rc = asyncio.run(run(args.host, args.port))
    except AssertionError as e:
        print(f"FAIL: {e}"); sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}"); sys.exit(2)
    print("\nPASS"); sys.exit(rc)


if __name__ == "__main__":
    main()
