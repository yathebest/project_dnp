<<<<<<< HEAD
# project_dnp
=======
# Multiplayer Tic-Tac-Toe — Python Sockets + Web UI

DNP course **Project 4**. A turn-based Tic-Tac-Toe game server built on
raw Python TCP sockets, with a web frontend that connects via WebSocket
to the same process.

* **`server.py`** — single asyncio process that opens *two* listeners
  sharing one in-memory `RoomManager`:
  * raw TCP on **`5555`** (newline-delimited JSON) → CLI clients,
    load test, disconnect test
  * WebSocket on **`8765`** (same JSON, one frame per message) →
    browser frontend
* **`client_cli.py`** — raw TCP CLI client (interactive play, ping/pong
  RTT badge)
* **`web/`** — static HTML/CSS/JS frontend, no build step
* **`tests/`** — automated validation scripts (load, disconnect,
  reconnect)

```
                            ┌─────────────────────────────┐
  CLI client ── TCP :5555 ──┤                             │
                            │   server.py (asyncio loop)  │
  Browser ──── WS  :8765 ───┤   ┌─────────────────────┐   │
                            │   │   RoomManager       │   │
  Load test ── TCP :5555 ───┤   │ (in-memory state)   │   │
                            │   └─────────────────────┘   │
                            │   logging → server.log      │
                            └─────────────────────────────┘
```

The browser path is just an adapter; the **TCP path is the artifact
that satisfies the "Python sockets" requirement** and the path that the
load and disconnect tests exercise.

---

## Quick start

```bash
pip install -r requirements.txt   # only dep: websockets
python server.py                  # starts both listeners
```

Server log:

```
2026-04-26 23:28:49.689 [INFO] TCP listening on 0.0.0.0:5555
2026-04-26 23:28:49.701 [INFO] WS  listening on 0.0.0.0:8765
```

### Play in the browser

Open `web/index.html` directly in two browser windows (modern browsers
allow `ws://localhost:8765` from `file://`). The lobby UI has:

* **Live room list** in the *Join* tab — every open room shows its
  code, current player count (e.g. `1/2`), a 🔒 icon if it requires
  a password, and an "in progress" badge if a round has started. The
  list refreshes itself every 5 s and the server also pushes updates
  whenever a room is created/joined/closed. There's a manual refresh
  button next to the list.
* **Three tabs:**
  * *Join* — pick a room from the list (opens a modal asking for your
    name and the room password if it's locked), or expand
    "Or join by code manually" and type a code.
  * *Create* — make a new room with the code of your choice and an
    optional password. Fails clearly if the code is taken.
  * *Quick match* — auto-pair with the next available public player.
* **Room chat** that appears next to the board once two players are
  matched. Chat is per-room and private to its two participants — there
  is no chat on the main menu (room picker). When a player joins a
  room mid-session, they receive the recent history of that room's
  chat (last 50 messages).
* **Connection pill** in the top bar shows online / connecting /
  offline state; the WebSocket auto-reconnects.

Server URL is in the *Server* details section if you need to point the
browser at a different host.

Optional — serve the page over HTTP so it's reachable from another
machine:

```bash
python -m http.server 8080 -d web
# then open http://<host>:8080 in two browsers
```

### Play in the terminal

```bash
# terminal 1: create the room
python client_cli.py --name Alice --room CLI --create

# terminal 2: join it
python client_cli.py --name Bob   --room CLI

# locked room:
python client_cli.py --name Alice --room VIP --create --password s3cret
python client_cli.py --name Bob   --room VIP --password s3cret

# auto-match (no room argument)
python client_cli.py --name Charlie
```

Type `row,col` (e.g. `1,2`) to place a mark, `n` to start a new round
after one ends, `q` to leave.

---

## Protocol

Newline-delimited JSON over TCP / one JSON frame per WebSocket message.
The schemas are identical for both transports.

### Client → Server

| Type         | Fields                                          | Notes |
|--------------|-------------------------------------------------|-------|
| `create`     | `name`, `room`, `password` (optional)           | new room with that code; fails with `room_exists` if taken |
| `join`       | `name`, `room`, `password` (optional)           | join existing room; fails with `room_not_found`, `room_full`, or `wrong_password` |
| `auto`       | `name`                                          | auto-match into any open public room (creates one if none) |
| `list_rooms` | —                                               | request a `rooms` snapshot reply |
| `chat`       | `text`                                          | broadcast to the other player in the same room — only valid when the sender is seated in a room (returns `no_active_game` otherwise). The server uses `player.name` as the nickname; the client cannot spoof it. |
| `move`       | `row` 0..2, `col` 0..2                          | only valid on your turn |
| `new_round`  | —                                               | both players must send to start round N+1 |
| `ping`       | `ts` (number, client clock)                     | echoed back as `pong` |
| `leave`      | —                                               | graceful exit; player returns to lobby observer (connection stays open) |

(Backward compatibility: `join` with empty/null room is treated as
`auto` so the legacy `{"type":"join","room":null}` clients keep working.)

### Server → Client

| Type                | Fields | Notes |
|---------------------|--------|-------|
| `rooms`             | `rooms`: `[{code, players, max, has_password, in_progress, joinable}]` | pushed to all lobby observers on every change, also returned on demand |
| `chat`              | `nickname`, `text`, `ts`            | a chat message broadcast to the room's players |
| `chat_history`      | `messages`: `[{nickname, text, ts}]` | sent right after `joined` so a player can see the room's recent (≤50) messages |
| `joined`            | `room`, `you` ("X"/"O"), `opponent`, `rejoined`, `has_password` | sent after a successful create/join/auto |
| `state`             | `room`, `round`, `board`, `turn`, `you`, `your_name`, `opponent`, `opponent_connected`, `scores`, `winner`, `line`, `server_ts` | full snapshot, broadcast after every move and round transition |
| `pong`              | `ts` (echoed), `server_ts`         | for client RTT measurement |
| `opponent_left`     | `name`, `grace_seconds` (15)        | mid-game disconnect notice |
| `opponent_rejoined` | —                                   | they came back inside the grace window |
| `forfeit`           | `winner`, `reason`                  | (legacy/reserved) — the current server does not emit `forfeit` anymore: rooms stay alive, see "Rooms persist when a player leaves" below |
| `log`               | `level`, `message`                  | informational broadcasts (round transitions, etc.) |
| `error`             | `code`, `message`                   | structured error; see codes below |

**Lobby observation:** every connection starts as a "lobby observer" —
the server pushes `rooms` snapshots to them, and they can issue
`list_rooms`/`create`/`join`/`auto`. Chat is **not** available from
the main menu; observers who try to `chat` get a `no_active_game`
error. When a player is seated in a room (after a successful
create/join/auto) they leave the observer set and gain access to that
room's chat. When they `leave` they're added back to the observer set
with their connection still open.

**Rooms persist when a player leaves.** When one of the two players
leaves a running game (gracefully via `leave`, or by timing out their
15-second grace window after an unexpected disconnect):

* the room is **not** closed;
* its game state is reset (board cleared, scores zeroed, `round_no=1`,
  chat history wiped);
* the surviving player receives a fresh `joined` with `opponent=None`
  (which sends their UI back to the waiting screen) plus a
  human-readable `log` message;
* the room re-appears in `rooms` snapshots as `1/2` and `joinable=true`,
  so a fresh opponent can take the empty seat;
* only an empty room (last person out) is actually deleted.

Reconnect within the 15s grace window still works as before — the
returning player's `joined` is sent with `rejoined=true` and the game
resumes from where it was. Only when the grace window expires without
a return is the session reset.

### Error codes

| Code              | When                                                   |
|-------------------|--------------------------------------------------------|
| `room_exists`     | `create` for a room code that already exists           |
| `room_not_found`  | `join` for a room code that doesn't exist              |
| `room_full`       | `join` for a room that already has 2 players           |
| `wrong_password`  | `join` to a locked room with no/incorrect password     |
| `bad_room_code`   | empty / >16 chars / non-alnum-dash-underscore code     |
| `bad_password`    | password >64 chars                                     |
| `not_your_turn`   | `move` when it's the opponent's turn                   |
| `cell_occupied`   | `move` to a cell that's already filled                 |
| `out_of_bounds`   | `move` with row/col outside 0..2                       |
| `game_over`       | `move` after the round has ended                       |
| `round_running`   | `new_round` before the current round finished          |
| `no_active_game`  | `move` before the room has 2 players                   |
| `bad_move`        | `move` with non-integer row/col                        |
| `unknown_type`    | unknown `type` field                                   |
| `invalid_json`    | malformed message                                      |

### State machine

```
   ┌──────┐   join   ┌─────────┐  2nd join  ┌─────────┐
   │ open │ ───────► │ waiting │ ─────────► │ playing │
   └──────┘          └─────────┘            └────┬────┘
                                                 │ winner / draw
                                                 ▼
                                             ┌────────┐  both new_round
                                             │  end   │ ─────────────┐
                                             └────────┘              │
                                                                     ▼
                                                                 (back to playing,
                                                                  round_no += 1,
                                                                  scores persist)
```

---

## Validation evidence

### 1. Synchronization & round transitions are logged

Every state broadcast emits a structured `SYNC` line; round boundaries
emit `ROUND N STARTED` / `ROUND N FINISHED`. Move handling additionally
records `move_to_broadcast_ms` (server-side latency from move receipt
to all-recipients-sent).

Excerpt from `server.log` during a real game (room `L0`, two bots):

```
2026-04-26 23:29:05.319 [INFO] ROOM CREATED room=L0
2026-04-26 23:29:05.319 [INFO] JOIN room=L0 name=bot0 mark=X transport=tcp peer=127.0.0.1:54871
2026-04-26 23:29:05.320 [INFO] JOIN room=L0 name=bot1 mark=O transport=tcp peer=127.0.0.1:54872
2026-04-26 23:29:05.320 [INFO] ROUND 1 STARTED room=L0
2026-04-26 23:29:05.321 [INFO] SYNC room=L0 round=1 turn=X move=- recipients=2 fanout_ms=0.19
2026-04-26 23:29:05.354 [INFO] SYNC room=L0 round=1 turn=O move=(2,0) recipients=2 fanout_ms=0.32
2026-04-26 23:29:05.354 [INFO] MOVE room=L0 round=1 by=X cell=(2,0) move_to_broadcast_ms=0.50
…
2026-04-26 23:29:05.540 [INFO] ROUND 1 FINISHED room=L0 winner=X line=[[2, 0], [2, 1], [2, 2]]
2026-04-26 23:29:05.541 [INFO] ROUND 2 STARTED room=L0
2026-04-26 23:29:05.541 [INFO] SYNC room=L0 round=2 turn=X move=- recipients=2 fanout_ms=0.08
```

### 2. Latency under 3–5 connected players

`tests/load_test.py` spawns *N* concurrent raw TCP clients, pairs them
into rooms, and plays scripted rounds with random moves. It reports
two distributions:

* **RTT (client ping→pong)** — round-trip including network +
  scheduling.
* **Move RTT (move→state)** — interval between client send-of-move and
  receive-of-state-confirming-it. Captured on every state, regardless
  of whose turn comes next, so it does not include the opponent's
  think time.

Measured on Windows 11, loopback, with the supplied `server.py`:

| Clients | Rounds each | RTT p50 / p95 / p99 (ms) | Move RTT p50 / p95 / p99 (ms) |
|---------|-------------|---------------------------|-------------------------------|
| 3       | 5           | 1.0  /  22.2 /  42.8      | 0.51 / 0.69 / 0.76            |
| 4       | 5           | 1.0  /  31.0 /  31.0      | 0.57 / 0.81 / 0.88            |
| 5       | 5           | 1.0  /  23.8 /  42.4      | 0.46 / 0.72 / 0.78            |

(Raw output is saved next to the script as
`tests/load_test_3clients.txt`, `tests/load_test_4clients.txt`,
`tests/load_test_5clients.txt`.)

`p99` server move-to-broadcast latency stays **sub-millisecond** under
5 simultaneous TCP clients and 80+ moves. The occasional p95/p99
spikes in client RTT are scheduler hiccups on the host (a fixed
~31 ms Windows clock granularity for asyncio sleep on this machine);
they do not propagate to the server-side metric.

Reproduce:

```bash
python tests/load_test.py --clients 5 --rounds 5
```

### 3. Unexpected disconnect handling

* **Grace window:** mid-game, when a socket closes without a `leave`,
  the surviving player gets `opponent_left` with `grace_seconds=15`.
* **Reconnect:** if a new socket joins inside the window using the
  same `(name, room)`, the seat is restored and both clients receive
  `opponent_rejoined` + a fresh `state`. No tokens, no DB —
  identity is just the name+room pair.
* **Grace expiry:** if the window expires, the room is **not** closed.
  The survivor's session is reset and they receive a fresh
  `joined` with `opponent=None`, sending their UI back to the
  waiting screen. The room remains in the lobby list as `1/2` and a
  new opponent can join at any time.

`tests/disconnect_test.py` automates the abrupt-disconnect → grace-expiry
path and asserts that the room survives:

```
Connecting two clients to 127.0.0.1:5555 (room=DC)…
  Alice=X  Bob=O
  X is Alice; making 2 moves before disconnect…
  >>> Dropping Alice's TCP socket abruptly…
  Bob received 'opponent_left' in 0.2 ms [OK]
     payload: grace_seconds=15 name=Alice
  Waiting out the 15s grace window…
  Bob received fresh 'joined' (you=O, opponent=None) [OK]
  Room DC still listed as 1/2 joinable=True [OK]

PASS
```

`tests/reconnect_test.py` automates the disconnect → reconnect path:

```
Connecting Alice and Bob to room RC…
  Alice=X  Bob=O
  >>> Dropping Alice's socket…
  Bob saw opponent_left [OK]
  >>> Reconnecting as Alice…
  Alice rejoined as X [OK]
  Bob saw opponent_rejoined [OK]
  Board state restored: cell (1,1)=X [OK]

PASS
```

Reproduce:

```bash
python tests/disconnect_test.py    # ~16s (must wait out grace window)
python tests/reconnect_test.py     # ~2s
```

### 4. Lobby errors (negative paths)

`tests/error_test.py` covers the new lobby protocol's edge cases:

```
* create-then-create same code -> room_exists       [OK]
* join non-existent room        -> room_not_found   [OK]
* join a 2-player room as third -> room_full        [OK]
* password-protected room
    create with password               [OK]
    join with wrong password           [OK]
    join with no password              [OK]
    join with correct password         [OK]
* auto-match pairs two clients into one room        [OK]

PASS
```

```bash
python tests/error_test.py
```

### 5. Lobby & chat protocol

`tests/lobby_test.py` exercises the lobby observation and per-room
chat:

```
* on-connect snapshot includes a `rooms` message              [OK]
* `list_rooms` request returns a `rooms` reply                [OK]
* observer gets a fresh `rooms` snapshot when someone creates a room
    PUSH1 in snapshot, players=1/2,
    has_password=true, joinable=true                          [OK]
* chat from a lobby observer (not in a room) is rejected
    rejected with code=no_active_game                         [OK]
* chat inside a room reaches both players
    nickname='Alice' text='hi from inside'                    [OK]
* a player joining mid-session receives the room's chat history
    Bob received 1 historical messages                        [OK]
* room stays in the list after one player leaves (1/2)
    STAYS still listed as 1/2 joinable=True                   [OK]

PASS
```

```bash
python tests/lobby_test.py
```

The corresponding server log entries:

```
[INFO] DISCONNECT name=Alice room=DC mark=X
[INFO] FORFEIT room=DC winner=O reason=opponent_grace_expired
```

For the reconnect path:

```
[INFO] DISCONNECT name=Alice room=RC mark=X
[INFO] RECONNECT room=RC name=Alice mark=X
```

You can also reproduce the disconnect manually in the browser by
closing one tab while a game is running — the other browser displays
a toast with a 15-second countdown.

---

## File layout

```
project/
├── server.py                       # asyncio TCP + WebSocket server
├── client_cli.py                   # raw-TCP CLI client
├── requirements.txt
├── server.log                      # generated at runtime
├── web/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── tests/
    ├── load_test.py
    ├── disconnect_test.py
    ├── reconnect_test.py
    ├── error_test.py               # negative paths (room_full, wrong_password, etc.)
    ├── lobby_test.py                # rooms snapshot push, list_rooms, chat, chat_history
    ├── load_test_3clients.txt      # captured output
    ├── load_test_4clients.txt
    └── load_test_5clients.txt
```

## Server CLI

```
python server.py [--tcp-host 0.0.0.0] [--tcp-port 5555]
                 [--ws-host  0.0.0.0] [--ws-port  8765]
                 [--no-ws]            # disable WebSocket listener
                 [--log-file server.log]
                 [--log-level INFO]
```

## Limitations / scope

* Single process, no horizontal scaling — adequate for the project's
  3–5 concurrent-clients goal.
* In-memory state only; restarting the server drops all rooms.
* Reconnect identity is `(name, room)` — fine for a course demo, not a
  hostile environment. Anyone sending the right name within 15 s
  takes the seat.
* Passwords are compared in plain text and are intended as a
  shared-secret room lock, not a security primitive.
* No authentication, no encryption. Use only on trusted networks.
>>>>>>> master
