import argparse
import asyncio
import json
import sys


async def send_json(w, msg):
    w.write((json.dumps(msg) + "\n").encode("utf-8"))
    await w.drain()


async def read_until(reader, want_type, timeout):
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


async def run(host, port):
    print("Connecting Alice and Bob to room RC...")
    a_r, a_w = await asyncio.open_connection(host, port)
    b_r, b_w = await asyncio.open_connection(host, port)
    await send_json(a_w, {"type": "create", "name": "Alice", "room": "RC"})
    a_joined = await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join", "name": "Bob", "room": "RC"})
    b_joined = await read_until(b_r, "joined", 2.0)
    assert a_joined and b_joined, "missing 'joined'"
    print("  Alice=%s  Bob=%s" % (a_joined.get("you"), b_joined.get("you")))
    await read_until(a_r, "state", 2.0)
    await read_until(b_r, "state", 2.0)

    x_w = a_w if a_joined.get("you") == "X" else b_w
    x_r = a_r if a_joined.get("you") == "X" else b_r
    o_r = b_r if a_joined.get("you") == "X" else a_r
    await send_json(x_w, {"type": "move", "row": 1, "col": 1})
    await read_until(x_r, "state", 2.0)
    await read_until(o_r, "state", 2.0)

    print("  >>> Dropping Alice's socket...")
    a_w.close()
    try:
        await a_w.wait_closed()
    except Exception:
        pass

    msg = await read_until(b_r, "opponent_left", 3.0)
    assert msg is not None, "Bob did not receive opponent_left"
    print("  Bob saw opponent_left [OK]")

    print("  >>> Reconnecting as Alice...")
    a2_r, a2_w = await asyncio.open_connection(host, port)
    await send_json(a2_w, {"type": "join", "name": "Alice", "room": "RC"})
    j = await read_until(a2_r, "joined", 3.0)
    assert j and j.get("rejoined"), "expected rejoined=True, got %r" % j
    print("  Alice rejoined as %s [OK]" % j.get("you"))

    msg = await read_until(b_r, "opponent_rejoined", 3.0)
    assert msg is not None, "Bob did not receive opponent_rejoined"
    print("  Bob saw opponent_rejoined [OK]")

    s = await read_until(a2_r, "state", 3.0)
    assert s and s["board"][1][1] is not None, "board not restored"
    print("  Board state restored: cell (1,1)=%s [OK]" % s["board"][1][1])

    a2_w.close()
    b_w.close()
    for w in (a2_w, b_w):
        try:
            await w.wait_closed()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    args = parser.parse_args()
    try:
        rc = asyncio.run(run(args.host, args.port))
    except AssertionError as e:
        print("FAIL: %s" % e)
        sys.exit(1)
    except Exception as e:
        print("ERROR: %s" % e)
        sys.exit(2)
    print("\nPASS")
    sys.exit(rc)
