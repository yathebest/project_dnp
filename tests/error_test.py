import argparse
import asyncio
import json
import sys


async def send_json(w, msg):
    w.write((json.dumps(msg) + "\n").encode("utf-8"))
    await w.drain()


async def read_json(r, timeout=2.0):
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


async def read_until(r, want_type, timeout=2.0):
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


async def case_room_exists(host, port):
    print("* create-then-create same code -> room_exists")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "EXIST"})
    j = await read_until(a_r, "joined", 2.0)
    assert j is not None, "A no joined"
    await send_json(b_w, {"type": "create", "name": "B", "room": "EXIST"})
    err = await read_until(b_r, "error", 2.0)
    assert err and err.get("code") == "room_exists", "expected room_exists, got %r" % err
    print("  got code=%s message=%r  [OK]" % (err["code"], err["message"]))
    await close_(a_w, b_w)


async def case_room_not_found(host, port):
    print("* join non-existent room -> room_not_found")
    r, w = await open_(host, port)
    await send_json(w, {"type": "join", "name": "Ghost", "room": "NOPE9X"})
    err = await read_until(r, "error", 2.0)
    assert err and err.get("code") == "room_not_found", "expected room_not_found, got %r" % err
    print("  got code=%s  [OK]" % err["code"])
    await close_(w)


async def case_room_full(host, port):
    print("* join a 2-player room as third -> room_full")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    c_r, c_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "FULL"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join", "name": "B", "room": "FULL"})
    await read_until(b_r, "joined", 2.0)
    await send_json(c_w, {"type": "join", "name": "C", "room": "FULL"})
    err = await read_until(c_r, "error", 2.0)
    assert err and err.get("code") == "room_full", "expected room_full, got %r" % err
    print("  got code=%s  [OK]" % err["code"])
    await close_(a_w, b_w, c_w)


async def case_password_flow(host, port):
    print("* password-protected room")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    c_r, c_w = await open_(host, port)
    d_r, d_w = await open_(host, port)
    await send_json(a_w, {"type": "create", "name": "A", "room": "VIP", "password": "s3cret"})
    j = await read_until(a_r, "joined", 2.0)
    assert j and j.get("has_password") is True, "expected has_password=True, got %r" % j
    print("  A created VIP with password (has_password=%s)  [OK]" % j.get("has_password"))

    await send_json(b_w, {"type": "join", "name": "B", "room": "VIP", "password": "wrong"})
    err = await read_until(b_r, "error", 2.0)
    assert err and err.get("code") == "wrong_password"
    print("  B with wrong password: code=%s  [OK]" % err["code"])

    await send_json(c_w, {"type": "join", "name": "C", "room": "VIP"})
    err = await read_until(c_r, "error", 2.0)
    assert err and err.get("code") == "wrong_password"
    print("  C with no password: code=%s  [OK]" % err["code"])

    await send_json(d_w, {"type": "join", "name": "D", "room": "VIP", "password": "s3cret"})
    j = await read_until(d_r, "joined", 2.0)
    assert j and j.get("you") in ("X", "O"), "D did not join, got %r" % j
    print("  D with correct password: joined as %s  [OK]" % j.get("you"))

    await close_(a_w, b_w, c_w, d_w)


async def case_auto_match(host, port):
    print("* auto-match pairs two clients into one room")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await send_json(a_w, {"type": "auto", "name": "AutoA"})
    j_a = await read_until(a_r, "joined", 2.0)
    assert j_a, "A did not get joined from auto"
    await send_json(b_w, {"type": "auto", "name": "AutoB"})
    j_b = await read_until(b_r, "joined", 2.0)
    assert j_b, "B did not get joined from auto"
    assert j_a["room"] == j_b["room"], "auto-match did not pair them (%s vs %s)" % (
        j_a["room"], j_b["room"])
    print("  paired in room %s  [OK]" % j_a["room"])
    await close_(a_w, b_w)


async def main_async(host, port):
    await case_room_exists(host, port)
    await case_room_not_found(host, port)
    await case_room_full(host, port)
    await case_password_flow(host, port)
    await case_auto_match(host, port)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    args = parser.parse_args()
    try:
        rc = asyncio.run(main_async(args.host, args.port))
    except AssertionError as e:
        print("\nFAIL: %s" % e)
        sys.exit(1)
    except Exception as e:
        print("\nERROR: %s" % e)
        sys.exit(2)
    print("\nPASS")
    sys.exit(rc)
