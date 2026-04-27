import argparse
import asyncio
import json
import sys
import time


GRACE_BUFFER = 2.0


async def send_json(writer, msg):
    writer.write((json.dumps(msg) + "\n").encode("utf-8"))
    await writer.drain()


async def read_json(reader, timeout):
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


async def drain_until(reader, want_type, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await read_json(reader, timeout=max(0.01, deadline - time.time()))
        if msg is None:
            return None
        if msg.get("type") == want_type:
            return msg
    return None


async def run(host, port, grace_seconds):
    print("Connecting two clients to %s:%d (room=DC)..." % (host, port))

    a_reader, a_writer = await asyncio.open_connection(host, port)
    b_reader, b_writer = await asyncio.open_connection(host, port)
    await send_json(a_writer, {"type": "create", "name": "Alice", "room": "DC"})
    a_joined = await drain_until(a_reader, "joined", 2.0)
    assert a_joined is not None, "Alice did not get 'joined' from create"
    await send_json(b_writer, {"type": "join", "name": "Bob", "room": "DC"})
    b_joined = await drain_until(b_reader, "joined", 2.0)
    assert b_joined is not None, "missing 'joined' messages"
    print("  Alice=%s  Bob=%s" % (a_joined.get("you"), b_joined.get("you")))

    await drain_until(a_reader, "state", 2.0)
    await drain_until(b_reader, "state", 2.0)

    if a_joined.get("you") == "X":
        x_writer, x_reader = a_writer, a_reader
        o_writer, o_reader = b_writer, b_reader
        x_name = "Alice"
    else:
        x_writer, x_reader = b_writer, b_reader
        o_writer, o_reader = a_writer, a_reader
        x_name = "Bob"
    print("  X is %s; making 2 moves before disconnect..." % x_name)

    await send_json(x_writer, {"type": "move", "row": 0, "col": 0})
    await drain_until(x_reader, "state", 2.0)
    await drain_until(o_reader, "state", 2.0)
    await send_json(o_writer, {"type": "move", "row": 1, "col": 1})
    await drain_until(o_reader, "state", 2.0)
    await drain_until(x_reader, "state", 2.0)

    print("  >>> Dropping Alice's TCP socket abruptly...")
    drop_t0 = time.perf_counter()
    a_writer.close()
    try:
        await a_writer.wait_closed()
    except Exception:
        pass

    msg = await drain_until(b_reader, "opponent_left", 3.0)
    assert msg is not None, "Bob did not receive 'opponent_left' within 3s"
    detect_ms = (time.perf_counter() - drop_t0) * 1000.0
    print("  Bob received 'opponent_left' in %.1f ms [OK]" % detect_ms)
    print("     payload: grace_seconds=%s name=%s" % (
        msg.get("grace_seconds"), msg.get("name")))

    print("  Waiting out the %ds grace window..." % grace_seconds)
    msg = await drain_until(b_reader, "joined", grace_seconds + GRACE_BUFFER)
    assert msg is not None, "Bob did not receive a fresh 'joined' after grace expired"
    assert msg.get("opponent") is None, (
        "expected opponent=None after grace, got %r" % msg.get("opponent"))
    print("  Bob received fresh 'joined' (you=%s, opponent=None) [OK]" % msg.get("you"))

    o_reader, o_writer = await asyncio.open_connection(host, port)
    rooms = await drain_until(o_reader, "rooms", 2.0)
    assert rooms is not None, "third observer did not get rooms snapshot"
    dc = next((r for r in rooms.get("rooms", []) if r.get("code") == "DC"), None)
    assert dc is not None, "room DC vanished after grace expired"
    assert dc.get("players") == 1 and dc.get("max") == 2 and dc.get("joinable") is True, (
        "DC not joinable after grace: %r" % dc)
    print("  Room DC still listed as %d/%d joinable=True [OK]" % (
        dc["players"], dc["max"]))

    b_writer.close()
    o_writer.close()
    for w in (b_writer, o_writer):
        try:
            await w.wait_closed()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--grace", type=int, default=15)
    args = parser.parse_args()

    try:
        rc = asyncio.run(run(args.host, args.port, args.grace))
    except AssertionError as e:
        print("\nFAIL: %s" % e)
        sys.exit(1)
    except Exception as e:
        print("\nERROR: %s" % e)
        sys.exit(2)
    print("\nPASS")
    sys.exit(rc)
