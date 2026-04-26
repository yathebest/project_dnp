"""
Negative-path tests for the new lobby protocol:
  * `room_exists` when creating a room that already exists
  * `room_not_found` when joining a non-existent room
  * `room_full` when joining a 2-player room as a third player
  * `wrong_password` when joining a locked room with no/bad password
  * Successful join with the correct password
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional


async def send_json(w: asyncio.StreamWriter, msg: dict) -> None:
    w.write((json.dumps(msg) + "\n").encode("utf-8"))
    await w.drain()


async def read_json(r: asyncio.StreamReader, timeout: float = 2.0) -> Optional[dict]:
    try:
        line = await asyncio.wait_for(r.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8").strip())
    except json.JSONDecodeError:
        return None


async def read_until(
    r: asyncio.StreamReader, want_type: str, timeout: float = 2.0
) -> Optional[dict]:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await read_json(r, timeout=max(0.01, deadline - time.time()))
        if msg is None:
            return None
        if msg.get("type") == want_type:
            return msg
    return None


async def open_(host, port):
    return await asyncio.open_connection(host, port)


async def close_(*ws):
    for w in ws:
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass


async def case_room_exists(host, port) -> None:
    print("* create-then-create same code -> room_exists")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "EXIST"})
    j = await read_until(a_r, "joined", 2.0); assert j is not None, "A no joined"
    await send_json(b_w, {"type": "create", "name": "B", "room": "EXIST"})
    err = await read_until(b_r, "error", 2.0)
    assert err and err.get("code") == "room_exists", f"expected room_exists, got {err}"
    print(f"  got code={err['code']} message={err['message']!r}  [OK]")
    await close_(a_w, b_w)


async def case_room_not_found(host, port) -> None:
    print("* join non-existent room -> room_not_found")
    r, w = await open_(host, port)
    await send_json(w, {"type": "join", "name": "Ghost", "room": "NOPE9X"})
    err = await read_until(r, "error", 2.0)
    assert err and err.get("code") == "room_not_found", f"expected room_not_found, got {err}"
    print(f"  got code={err['code']}  [OK]")
    await close_(w)


async def case_room_full(host, port) -> None:
    print("* join a 2-player room as third -> room_full")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    c_r, c_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "FULL"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join",   "name": "B", "room": "FULL"})
    await read_until(b_r, "joined", 2.0)
    await send_json(c_w, {"type": "join",   "name": "C", "room": "FULL"})
    err = await read_until(c_r, "error", 2.0)
    assert err and err.get("code") == "room_full", f"expected room_full, got {err}"
    print(f"  got code={err['code']}  [OK]")
    await close_(a_w, b_w, c_w)


async def case_password_flow(host, port) -> None:
    print("* password-protected room")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    c_r, c_w = await open_(host, port)
    d_r, d_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "VIP", "password": "s3cret"})
    j = await read_until(a_r, "joined", 2.0)
    assert j and j.get("has_password") is True, f"expected has_password=True, got {j}"
    print(f"  A created VIP with password (has_password={j.get('has_password')})  [OK]")

    # Wrong password
    await send_json(b_w, {"type": "join", "name": "B", "room": "VIP", "password": "wrong"})
    err = await read_until(b_r, "error", 2.0)
    assert err and err.get("code") == "wrong_password", f"expected wrong_password, got {err}"
    print(f"  B with wrong password: code={err['code']}  [OK]")

    # No password at all
    await send_json(c_w, {"type": "join", "name": "C", "room": "VIP"})
    err = await read_until(c_r, "error", 2.0)
    assert err and err.get("code") == "wrong_password", f"expected wrong_password, got {err}"
    print(f"  C with no password: code={err['code']}  [OK]")

    # Correct password
    await send_json(d_w, {"type": "join", "name": "D", "room": "VIP", "password": "s3cret"})
    j = await read_until(d_r, "joined", 2.0)
    assert j and j.get("you") in ("X", "O"), f"D did not join, got {j}"
    print(f"  D with correct password: joined as {j.get('you')}  [OK]")

    await close_(a_w, b_w, c_w, d_w)


async def case_auto_match(host, port) -> None:
    print("* auto-match pairs two clients into one room")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await send_json(a_w, {"type": "auto", "name": "AutoA"})
    j_a = await read_until(a_r, "joined", 2.0)
    assert j_a, "A did not get joined from auto"
    await send_json(b_w, {"type": "auto", "name": "AutoB"})
    j_b = await read_until(b_r, "joined", 2.0)
    assert j_b, "B did not get joined from auto"
    assert j_a["room"] == j_b["room"], f"auto-match did not pair them ({j_a['room']} vs {j_b['room']})"
    print(f"  paired in room {j_a['room']}  [OK]")
    await close_(a_w, b_w)


async def main_async(host: str, port: int) -> int:
    await case_room_exists(host, port)
    await case_room_not_found(host, port)
    await case_room_full(host, port)
    await case_password_flow(host, port)
    await case_auto_match(host, port)
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    args = p.parse_args()
    try:
        rc = asyncio.run(main_async(args.host, args.port))
    except AssertionError as e:
        print(f"\nFAIL: {e}"); sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}"); sys.exit(2)
    print("\nPASS"); sys.exit(rc)


if __name__ == "__main__":
    main()
