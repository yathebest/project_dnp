# Tic-Tac-Toe multiplayer (DNP project 4)

Turn-based tic-tac-toe over Python sockets with a web UI.

The server runs on a single asyncio event loop and exposes two listeners
that share the same in-memory state:

- raw TCP on port `5555` for CLI clients and load tests
- WebSocket on port `8765` for the browser

Both speak the same JSON protocol (one JSON object per line on TCP, one
per frame on WebSocket).

## Files

```
server.py             game server (TCP + WebSocket)
client_cli.py         terminal client over raw TCP
requirements.txt      dependencies (only `websockets`)
web/
    index.html        page markup
    style.css         styles
    app.js            WebSocket client
tests/
    load_test.py      3-5 concurrent clients, prints latency
    disconnect_test.py  abrupt disconnect + grace timer
    reconnect_test.py   reconnect inside grace window
    error_test.py     room_exists/full/wrong_password/...
    lobby_test.py     room list and chat
```

## Install and run

```
pip install -r requirements.txt
python server.py
```

The server prints:

```
TCP listening on 0.0.0.0:5555
WS  listening on 0.0.0.0:8765
```

### Browser

Open `web/index.html` in two browser windows. Modern browsers allow
`ws://localhost:8765` from `file://` so no HTTP server is needed. If you
need to access from another machine, run:

```
python -m http.server 8080 -d web
```

The lobby has three tabs:

- **Join** — shows the list of available rooms (auto-refresh every 5 s
  plus a manual refresh button). Each row shows the code, player count
  (`1/2` or `2/2`) and a lock icon if the room has a password. Clicking
  a room opens a small dialog asking for your name and password.
- **Create** — make a new room with the code you choose. Optional
  password. Fails if the code is already taken.
- **Quick match** — auto-pair with the next available public player.

After two players are in a room, the chat panel appears next to the
board.

### Terminal

```
# create a room
python client_cli.py --name Alice --room MYROOM --create

# join it
python client_cli.py --name Bob --room MYROOM

# password-protected
python client_cli.py --name Alice --room VIP --create --password s3cret
python client_cli.py --name Bob   --room VIP --password s3cret

# auto-match
python client_cli.py --name Charlie
```

In the terminal client type `row,col` (e.g. `1,2`) to play a move,
`n` to start a new round, `q` to quit.

## Features

- two players per room, marks X and O, X starts
- best-of-N rounds (scores X / O / draws are kept until someone leaves)
- room codes 1-16 alphanumeric chars, optional password (up to 64 chars)
- four ways to pick a room: create / join by code / pick from list / quick match
- per-room chat, only between the two seated players
- chat history (last 50 messages) sent to a player when they join the room
- live room list pushed to all observers when something changes
- ping/pong RTT badge in the browser
- 15 second grace window after an unexpected disconnect (player can rejoin)
- when a player gracefully leaves or grace expires, the room stays alive
  (`1/2`, joinable) so a new opponent can take the empty seat
- structured logs in `server.log` (CONNECT, JOIN, SYNC, MOVE, ROUND STARTED/FINISHED,
  CHAT, DISCONNECT, RECONNECT, ROOM RESET, ROOM CLOSED)

## Protocol

JSON messages over TCP (newline-terminated) or WebSocket (one per frame).

Client to server:

| type         | fields                                  |
|--------------|-----------------------------------------|
| `create`     | `name`, `room`, `password` (optional)   |
| `join`       | `name`, `room`, `password` (optional)   |
| `auto`       | `name`                                  |
| `list_rooms` | -                                       |
| `chat`       | `text`                                  |
| `move`       | `row`, `col` (0..2)                     |
| `new_round`  | -                                       |
| `ping`       | `ts`                                    |
| `leave`      | -                                       |

Server to client:

| type                | fields                                                            |
|---------------------|-------------------------------------------------------------------|
| `rooms`             | `rooms`: list of `{code, players, max, has_password, in_progress, joinable}` |
| `joined`            | `room`, `you`, `opponent`, `rejoined`, `has_password`             |
| `state`             | `room`, `round`, `board`, `turn`, `you`, `opponent`, `scores`, `winner`, `line`, `server_ts` |
| `chat`              | `nickname`, `text`, `ts`                                          |
| `chat_history`      | `messages`                                                        |
| `pong`              | `ts`, `server_ts`                                                 |
| `opponent_left`     | `name`, `grace_seconds`                                           |
| `opponent_rejoined` | -                                                                 |
| `log`               | `level`, `message`                                                |
| `error`             | `code`, `message`                                                 |

Error codes: `room_exists`, `room_not_found`, `room_full`,
`wrong_password`, `bad_room_code`, `bad_password`, `no_active_game`,
`game_over`, `not_your_turn`, `out_of_bounds`, `cell_occupied`,
`round_running`, `bad_move`, `unknown_type`, `invalid_json`.

## Tests

Run with the server up:

```
python tests/error_test.py        # room_exists / not_found / full / password
python tests/lobby_test.py        # rooms snapshot, chat in room, room survives leave
python tests/disconnect_test.py   # ~16 s, abrupt disconnect + grace
python tests/reconnect_test.py    # reconnect inside grace
python tests/load_test.py --clients 5 --rounds 5
```

Latency on loopback (Windows 11), 5 clients, 5 rounds each:

| metric                     | p50  | p95  | p99  |
|----------------------------|------|------|------|
| client RTT (ping->pong) ms | 1.0  | ~20  | ~30  |
| move RTT (move->state) ms  | 0.5  | 0.8  | 0.9  |

## Server CLI

```
python server.py [--tcp-host 0.0.0.0] [--tcp-port 5555]
                 [--ws-host  0.0.0.0] [--ws-port  8765]
                 [--no-ws]
                 [--log-file server.log] [--log-level INFO]
```

## Notes

- state is in memory only, restarting the server drops all rooms
- reconnect identity is just `(name, room)`, no tokens
- passwords are compared as plain strings, this is a room lock not real
  authentication
- no encryption, intended for trusted networks
