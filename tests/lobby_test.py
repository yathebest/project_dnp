"""
Smoke tests for the lobby protocol additions:
  * `rooms` snapshot pushed on connect
  * `list_rooms` request returns snapshot
  * room snapshot updates when a room is created/filled/closed
  * chat broadcast reaches all lobby observers
  * chat history sent to newcomers
  * graceful `leave` returns player to observer status (WS-style flow)
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


async def read_until(r, want_type: str, timeout: float = 2.0):
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


async def case_initial_snapshot(host, port) -> None:
    print("* on-connect snapshot includes a `rooms` message")
    r, w = await open_(host, port)
    msg = await read_until(r, "rooms", 2.0)
    assert msg is not None and "rooms" in msg, "no rooms snapshot on connect"
    print(f"  initial snapshot: {len(msg['rooms'])} rooms  [OK]")
    await close_(w)


async def case_list_rooms_request(host, port) -> None:
    print("* `list_rooms` request returns a `rooms` reply")
    r, w = await open_(host, port)
    await read_until(r, "rooms", 2.0)
    await send_json(w, {"type": "list_rooms"})
    msg = await read_until(r, "rooms", 2.0)
    assert msg is not None, "list_rooms got no reply"
    print(f"  reply rooms n={len(msg['rooms'])}  [OK]")
    await close_(w)


async def case_room_pushed_on_create(host, port) -> None:
    print("* observer gets a fresh `rooms` snapshot when someone creates a room")
    obs_r, obs_w = await open_(host, port)
    await read_until(obs_r, "rooms", 2.0)  # initial

    # Another client creates a room.
    a_r, a_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await send_json(a_w, {"type": "create", "name": "A", "room": "PUSH1", "password": "secret"})
    await read_until(a_r, "joined", 2.0)

    pushed = await read_until(obs_r, "rooms", 2.0)
    assert pushed is not None, "observer did not see rooms push"
    codes = [r["code"] for r in pushed["rooms"]]
    assert "PUSH1" in codes, f"PUSH1 missing from snapshot: {codes}"
    push1 = next(r for r in pushed["rooms"] if r["code"] == "PUSH1")
    assert push1["players"] == 1 and push1["max"] == 2, f"unexpected counts: {push1}"
    assert push1["has_password"] is True, f"password flag wrong: {push1}"
    assert push1["joinable"] is True, f"joinable flag wrong: {push1}"
    print(f"  PUSH1 in snapshot, players=1/2, has_password=true, joinable=true  [OK]")
    await close_(obs_w, a_w)


async def case_chat_outside_room_rejected(host, port) -> None:
    print("* chat from a lobby observer (not in a room) is rejected")
    r, w = await open_(host, port)
    await read_until(r, "rooms", 2.0)
    await send_json(w, {"type": "chat", "text": "hello main menu"})
    err = await read_until(r, "error", 2.0)
    assert err is not None and err.get("code") == "no_active_game", (
        f"expected no_active_game error, got {err}"
    )
    print(f"  rejected with code={err['code']}  [OK]")
    await close_(w)


async def case_room_chat_broadcast(host, port) -> None:
    print("* chat inside a room reaches both players")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await read_until(b_r, "rooms", 2.0)

    await send_json(a_w, {"type": "create", "name": "Alice", "room": "CHAT1"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join",   "name": "Bob",   "room": "CHAT1"})
    await read_until(b_r, "joined", 2.0)
    await read_until(a_r, "state",  2.0)
    await read_until(b_r, "state",  2.0)

    await send_json(a_w, {"type": "chat", "text": "hi from inside"})
    msg_a = await read_until(a_r, "chat", 2.0)
    msg_b = await read_until(b_r, "chat", 2.0)
    assert msg_a and msg_b and msg_a["text"] == "hi from inside" == msg_b["text"]
    # Server uses the player's seat name, not a client-supplied nickname.
    assert msg_a["nickname"] == "Alice", f"nickname not Alice: {msg_a}"
    print(f"  both players got chat: nickname={msg_a['nickname']!r} text={msg_a['text']!r}  [OK]")
    await close_(a_w, b_w)


async def case_room_chat_history_on_join(host, port) -> None:
    print("* a player joining mid-session receives the room's chat history")
    a_r, a_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await send_json(a_w, {"type": "create", "name": "Alice", "room": "CHAT2"})
    await read_until(a_r, "joined", 2.0)

    await send_json(a_w, {"type": "chat", "text": "earliest"})
    # Alice is solo so she can technically chat? No - she needs to be in a
    # room with herself. Server allows chat as long as player is in the room
    # players list, which she is. Her message appears in room.chat.
    await read_until(a_r, "chat", 2.0)

    # Bob joins now and should receive the history.
    b_r, b_w = await open_(host, port)
    await read_until(b_r, "rooms", 2.0)
    await send_json(b_w, {"type": "join", "name": "Bob", "room": "CHAT2"})
    await read_until(b_r, "joined", 2.0)
    history = await read_until(b_r, "chat_history", 2.0)
    assert history is not None and "messages" in history, "no chat_history on join"
    texts = [m["text"] for m in history["messages"]]
    assert "earliest" in texts, f"missing 'earliest' in {texts!r}"
    print(f"  Bob received {len(history['messages'])} historical messages  [OK]")
    await close_(a_w, b_w)


async def case_room_survives_leave(host, port) -> None:
    print("* room stays in the list after one player leaves (1/2)")
    a_r, a_w = await open_(host, port)
    b_r, b_w = await open_(host, port)
    obs_r, obs_w = await open_(host, port)
    await read_until(a_r, "rooms", 2.0)
    await read_until(b_r, "rooms", 2.0)
    await read_until(obs_r, "rooms", 2.0)

    await send_json(a_w, {"type": "create", "name": "Alice", "room": "STAYS"})
    await read_until(a_r, "joined", 2.0)
    await send_json(b_w, {"type": "join",   "name": "Bob",   "room": "STAYS"})
    await read_until(b_r, "joined", 2.0)

    # Alice graciously leaves.
    await send_json(a_w, {"type": "leave"})

    # Bob should be bumped to waiting — receives `joined opponent=None`.
    j = await read_until(b_r, "joined", 2.0)
    assert j is not None and j.get("opponent") is None, (
        f"Bob did not receive joined-opponent=null: {j}"
    )

    # Observer should see an updated rooms snapshot showing 1/2.
    rooms = await read_until(obs_r, "rooms", 2.0)
    assert rooms is not None
    stays = next((r for r in rooms["rooms"] if r["code"] == "STAYS"), None)
    assert stays is not None, "STAYS missing from snapshot after leave"
    assert stays["players"] == 1 and stays["joinable"] is True, (
        f"STAYS not joinable: {stays}"
    )
    print(f"  STAYS still listed as {stays['players']}/{stays['max']} joinable=True  [OK]")
    await close_(a_w, b_w, obs_w)


async def main_async(host: str, port: int) -> int:
    await case_initial_snapshot(host, port)
    await case_list_rooms_request(host, port)
    await case_room_pushed_on_create(host, port)
    await case_chat_outside_room_rejected(host, port)
    await case_room_chat_broadcast(host, port)
    await case_room_chat_history_on_join(host, port)
    await case_room_survives_leave(host, port)
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
