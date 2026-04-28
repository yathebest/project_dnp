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


async def case_initial_snapshot(host, port):
    print("* on-connect snapshot includes a `rooms` message")
    r, w = await open_(host, port)
    msg = await read_until(r, "rooms", 2.0)
    assert msg is not None and "rooms" in msg, "no rooms snapshot on connect"
    print("  initial snapshot: %d rooms  [OK]" % len(msg["rooms"]))
    await close_(w)


async def case_list_rooms_request(host, port):
    print("* `list_rooms` request returns a `rooms` reply")
    r, w = await open_(host, port)
    await read_until(r, "rooms", 2.0)
    await send_json(w, {"type": "list_rooms"})
    msg = await read_until(r, "rooms", 2.0)
    assert msg is not None, "list_rooms got no reply"
    print("  reply rooms n=%d  [OK]" % len(msg["rooms"]))
    await close_(w)


async def case_room_pushed_on_create(host, port):
    print("* observer gets a fresh `rooms` snapshot when someone creates a room")
    obs_r, obs_w = await open_(host, port)
    await read_until(obs_r, "rooms", 2.0)

    a_r, a_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await send_json(a_w, {"type": "create", "name": "A", "room": "PUSH1", "password": "secret"})
    await read_until(a_r, "joined", 2.0)

    pushed = await read_until(obs_r, "rooms", 2.0)
    assert pushed is not None, "observer did not see rooms push"
    codes = [r["code"] for r in pushed["rooms"]]
    assert "PUSH1" in codes, "PUSH1 missing from snapshot: %r" % codes
    push1 = next(r for r in pushed["rooms"] if r["code"] == "PUSH1")
    assert push1["players"] == 1 and push1["max"] == 2
    assert push1["has_password"] is True
    assert push1["joinable"] is True
    print("  PUSH1 in snapshot, players=1/2, has_password=true, joinable=true  [OK]")
    await close_(obs_w, a_w)


async def case_chat_outside_room_rejected(host, port):
    print("* chat from a lobby observer (not in a room) is rejected")
    r, w = await open_(host, port)
    await read_until(r, "rooms", 2.0)
    await send_json(w, {"type": "chat", "text": "hello main menu"})
    err = await read_until(r, "error", 2.0)
    assert err is not None and err.get("code") == "no_active_game"
    print("  rejected with code=%s  [OK]" % err["code"])
    await close_(w)


async def case_room_chat_broadcast(host, port):
    print("* chat inside a room reaches both players")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await read_until(b_r, "rooms", 2.0)

    await send_json(a_w, {"type": "create", "name": "Alice", "room": "CHAT1"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join", "name": "Bob", "room": "CHAT1"})
    await read_until(b_r, "joined", 2.0)
    await read_until(a_r, "state", 2.0)
    await read_until(b_r, "state", 2.0)

    await send_json(a_w, {"type": "chat", "text": "hi from inside"})
    msg_a = await read_until(a_r, "chat", 2.0)
    msg_b = await read_until(b_r, "chat", 2.0)
    assert msg_a and msg_b and msg_a["text"] == "hi from inside" == msg_b["text"]
    assert msg_a["nickname"] == "Alice"
    print("  both players got chat: nickname=%r text=%r  [OK]" % (
        msg_a["nickname"], msg_a["text"]))
    await close_(a_w, b_w)


async def case_room_chat_history_on_join(host, port):
    print("* a player joining mid-session receives the room's chat history")
    a_r, a_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await send_json(a_w, {"type": "create", "name": "Alice", "room": "CHAT2"})
    await read_until(a_r, "joined", 2.0)
    await send_json(a_w, {"type": "chat", "text": "earliest"})
    await read_until(a_r, "chat", 2.0)

    b_r, b_w = await open_(host, port)
    await read_until(b_r, "rooms", 2.0)
    await send_json(b_w, {"type": "join", "name": "Bob", "room": "CHAT2"})
    await read_until(b_r, "joined", 2.0)
    history = await read_until(b_r, "chat_history", 2.0)
    assert history is not None and "messages" in history
    texts = [m["text"] for m in history["messages"]]
    assert "earliest" in texts
    print("  Bob received %d historical messages  [OK]" % len(history["messages"]))
    await close_(a_w, b_w)


async def case_room_survives_leave(host, port):
    print("* room stays in the list after one player leaves (1/2)")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    obs_r, obs_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await read_until(b_r, "rooms", 2.0)
    await read_until(obs_r, "rooms", 2.0)

    await send_json(a_w, {"type": "create", "name": "Alice", "room": "STAYS"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join", "name": "Bob", "room": "STAYS"})
    await read_until(b_r, "joined", 2.0)

    await send_json(a_w, {"type": "leave"})

    j = await read_until(b_r, "joined", 2.0)
    assert j is not None and j.get("opponent") is None

    rooms = await read_until(obs_r, "rooms", 2.0)
    assert rooms is not None
    stays = next((r for r in rooms["rooms"] if r["code"] == "STAYS"), None)
    assert stays is not None and stays["players"] == 1 and stays["joinable"] is True
    print("  STAYS still listed as %d/%d joinable=True  [OK]" % (
        stays["players"], stays["max"]))
    await close_(a_w, b_w, obs_w)


async def main_async(host, port):
    await case_initial_snapshot(host, port)
    await case_list_rooms_request(host, port)
    await case_room_pushed_on_create(host, port)
    await case_chat_outside_room_rejected(host, port)
    await case_room_chat_broadcast(host, port)
    await case_room_chat_history_on_join(host, port)
    await case_room_survives_leave(host, port)
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
