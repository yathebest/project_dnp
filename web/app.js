/* Tic-Tac-Toe web client.
 *
 * The WebSocket is opened on page load (not on form submit) so we can
 * subscribe to the lobby's `rooms` snapshots and chat messages while the
 * user picks an action. Room list refreshes itself when the server pushes
 * updates; the user can also tap the refresh button to ask explicitly.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ---- Element refs --------------------------------------------------
  const lobbyWrap = $("lobbyWrap");
  const lobbyEl   = $("lobby");
  const waitingEl = $("waiting");
  const gameEl    = $("game");
  const tabsEl    = document.querySelector(".tabs");
  const tabHint   = $("tabHint");
  const inputName = $("inputName");
  const paneJoin   = $("paneJoin");
  const createForm = $("createForm");
  const quickForm  = $("quickForm");
  const inputCreateRoom     = $("inputCreateRoom");
  const inputCreatePassword = $("inputCreatePassword");
  const inputServer = $("inputServer");
  const lobbyError = $("lobbyError");
  const refreshBtn = $("refreshBtn");
  const roomListEl = $("roomList");
  const manualCode = $("manualCode");
  const manualJoinBtn = $("manualJoinBtn");

  const chatEl       = $("chat");
  const chatMessages = $("chatMessages");
  const chatForm     = $("chatForm");
  const chatInput    = $("chatInput");
  const chatStatus   = $("chatStatus");

  const joinModal       = $("joinModal");
  const joinModalIcon   = $("joinModalIcon");
  const joinModalTitle  = $("joinModalTitle");
  const joinModalCode   = $("joinModalCode");
  const joinModalCount  = $("joinModalCount");
  const joinModalLock   = $("joinModalLock");
  const joinModalPwLabel = $("joinModalPwLabel");
  const joinModalForm   = $("joinModalForm");
  const joinModalName   = $("joinModalName");
  const joinModalPassword = $("joinModalPassword");
  const joinModalCancel = $("joinModalCancel");
  const joinModalClose  = $("joinModalClose");
  const joinModalError  = $("joinModalError");

  const waitingRoom   = $("waitingRoom");
  const waitingPwTag  = $("waitingPwTag");
  const waitingCancel = $("waitingCancel");

  const boardEl    = $("board");
  const winLineSvg = $("winLine");
  const youMark = $("youMark");
  const youName = $("youName");
  const youScore = $("youScore");
  const oppMark = $("oppMark");
  const oppName = $("oppName");
  const oppScore = $("oppScore");
  const roomLabel = $("roomLabel");
  const roundNoEl = $("roundNo");
  const turnLine = $("turnLine");
  const banner = $("banner");
  const newRoundBtn = $("newRoundBtn");
  const leaveBtn = $("leaveBtn");

  const latencyText = $("latencyText");
  const latencyBadge = $("latencyBadge");
  const connStatus = $("connStatus");
  const connText = $("connText");

  const toastsEl = $("toasts");

  // ---- Persistent state ---------------------------------------------
  let ws = null;
  let wsRetryTimer = null;
  let pingTimer = null;
  let roomsRefreshTimer = null;
  let currentTab = "join"; // "join" | "create" | "quick"
  let lastRooms = [];
  let pendingJoinRoom = null; // {code, has_password}
  let myConnId = null;
  let state = {
    room: null, you: null, oppNameStr: null,
    board: emptyBoard(), turn: "X", scores: { X:0, O:0, draws: 0 },
    round: 1, winner: null, line: null, oppConnected: true,
    name: "",
  };
  let graceTimer = null;

  function emptyBoard() { return [[null,null,null],[null,null,null],[null,null,null]]; }

  // ---- Persistence (prefs only) -------------------------------------
  try {
    const saved = JSON.parse(localStorage.getItem("ttt-prefs") || "{}");
    if (saved.name)   inputName.value = saved.name;
    if (saved.server) inputServer.value = saved.server;
    if (saved.tab && ["join","create","quick"].includes(saved.tab)) currentTab = saved.tab;
  } catch {}
  function savePrefs() {
    try {
      localStorage.setItem("ttt-prefs", JSON.stringify({
        name: inputName.value, server: inputServer.value, tab: currentTab,
      }));
    } catch {}
  }
  inputName.addEventListener("change", savePrefs);
  inputServer.addEventListener("change", () => { savePrefs(); reconnect(); });

  // ---- Tabs ----------------------------------------------------------
  function setTab(name) {
    currentTab = name;
    tabsEl.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    paneJoin.classList.toggle("hidden",   name !== "join");
    createForm.classList.toggle("hidden", name !== "create");
    quickForm.classList.toggle("hidden",  name !== "quick");
    if (name === "create") {
      tabHint.textContent = "Pick a code your friend can type. Add a password to keep strangers out.";
    } else if (name === "join") {
      tabHint.textContent = "Pick a room from the list, or type a code.";
    } else {
      tabHint.textContent = "We'll pair you with the next available public player.";
    }
    lobbyError.classList.add("hidden");
    savePrefs();
  }
  tabsEl.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setTab(tab.dataset.tab));
  });
  setTab(currentTab);

  // ---- Build empty board --------------------------------------------
  function buildBoard() {
    boardEl.innerHTML = "";
    for (let r = 0; r < 3; r++) {
      for (let c = 0; c < 3; c++) {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "cell";
        cell.dataset.row = r;
        cell.dataset.col = c;
        cell.setAttribute("role", "gridcell");
        cell.setAttribute("aria-label", `row ${r+1} column ${c+1}`);
        cell.addEventListener("click", onCellClick);
        boardEl.appendChild(cell);
      }
    }
  }

  function ensureSvgGradient() {
    if (winLineSvg.querySelector("defs")) return;
    const ns = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(ns, "defs");
    const grad = document.createElementNS(ns, "linearGradient");
    grad.setAttribute("id", "winGradient");
    grad.setAttribute("x1", "0%"); grad.setAttribute("y1", "0%");
    grad.setAttribute("x2", "100%"); grad.setAttribute("y2", "0%");
    const stop1 = document.createElementNS(ns, "stop");
    stop1.setAttribute("offset", "0%"); stop1.setAttribute("stop-color", "#818cf8");
    const stop2 = document.createElementNS(ns, "stop");
    stop2.setAttribute("offset", "100%"); stop2.setAttribute("stop-color", "#e879f9");
    grad.appendChild(stop1); grad.appendChild(stop2);
    defs.appendChild(grad);
    winLineSvg.appendChild(defs);
  }

  // ---- Toasts --------------------------------------------------------
  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    toastsEl.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity 0.3s ease";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 320);
    }, 4000);
  }

  // ---- Connection / Latency badges ----------------------------------
  function setConn(stateName) {
    connStatus.classList.remove("conn-connected", "conn-disconnected", "conn-connecting");
    connStatus.classList.add("conn-" + stateName);
    connText.textContent =
      stateName === "connected"  ? "online" :
      stateName === "connecting" ? "connecting…" : "offline";
    chatStatus.textContent =
      stateName === "connected"  ? (state.oppNameStr || "alone") :
      stateName === "connecting" ? "connecting" : "offline";
  }
  function setLatency(ms, dead = false) {
    if (dead) {
      latencyBadge.className = "latency latency-dead";
      latencyText.textContent = "offline";
      return;
    }
    if (ms === null) {
      latencyBadge.className = "latency latency-unknown";
      latencyText.textContent = "RTT: —";
      return;
    }
    let cls = "latency-good";
    if (ms >= 50 && ms < 150) cls = "latency-ok";
    else if (ms >= 150)       cls = "latency-bad";
    latencyBadge.className = "latency " + cls;
    latencyText.textContent = `RTT: ${Math.round(ms)} ms`;
  }

  // ---- WebSocket -----------------------------------------------------
  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    setConn("connecting");
    const url = inputServer.value.trim() || "ws://localhost:8765";
    try {
      ws = new WebSocket(url);
    } catch (e) {
      setConn("disconnected");
      scheduleReconnect();
      return;
    }
    ws.addEventListener("open", () => {
      setConn("connected");
      myConnId = Math.random().toString(36).slice(2, 10);
      startPing();
      startRoomsRefresh();
    });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      onMessage(msg);
    });
    ws.addEventListener("close", () => {
      setConn("disconnected");
      stopPing();
      stopRoomsRefresh();
      setLatency(null, true);
      scheduleReconnect();
    });
    ws.addEventListener("error", () => {
      setConn("disconnected");
    });
  }
  function reconnect() {
    if (ws) { try { ws.close(); } catch {} }
    setTimeout(connect, 50);
  }
  function scheduleReconnect() {
    if (wsRetryTimer) return;
    wsRetryTimer = setTimeout(() => {
      wsRetryTimer = null;
      connect();
    }, 1500);
  }
  function send(msg) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(msg));
    return true;
  }
  function startPing() {
    stopPing();
    const tick = () => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    };
    tick();
    pingTimer = setInterval(tick, 2000);
  }
  function stopPing() { if (pingTimer) clearInterval(pingTimer); pingTimer = null; }
  function startRoomsRefresh() {
    stopRoomsRefresh();
    roomsRefreshTimer = setInterval(() => {
      if (lobbyWrap.classList.contains("hidden")) return; // only while lobby visible
      send({ type: "list_rooms" });
    }, 5000);
  }
  function stopRoomsRefresh() { if (roomsRefreshTimer) clearInterval(roomsRefreshTimer); roomsRefreshTimer = null; }

  // ---- Message dispatcher --------------------------------------------
  const LOBBY_ERROR_CODES = new Set([
    "room_not_found", "room_full", "room_exists",
    "wrong_password", "bad_room_code", "bad_password",
  ]);

  function onMessage(msg) {
    switch (msg.type) {
      case "rooms":
        lastRooms = msg.rooms || [];
        renderRooms();
        break;
      case "chat":
        appendChatMessage(msg);
        break;
      case "chat_history":
        renderChatHistory(msg.messages || []);
        break;
      case "joined":
        state.room = msg.room;
        state.you  = msg.you;
        state.oppNameStr = msg.opponent;
        if (msg.rejoined) toast("Reconnected to room " + msg.room, "ok");
        if (state.oppNameStr) showGame(); else showWaiting();
        break;
      case "state":
        state.board = msg.board || emptyBoard();
        state.turn  = msg.turn;
        state.scores = msg.scores || state.scores;
        state.round = msg.round;
        state.winner = msg.winner;
        state.line = msg.line;
        state.you  = msg.you ?? state.you;
        state.oppNameStr = msg.opponent ?? state.oppNameStr;
        state.oppConnected = msg.opponent_connected !== false;
        if (graceTimer) { clearInterval(graceTimer); graceTimer = null; }
        showGame();
        render();
        break;
      case "pong":
        if (typeof msg.ts === "number") setLatency(Date.now() - msg.ts);
        break;
      case "opponent_left":
        toast(`${msg.name || "Opponent"} disconnected. ${msg.grace_seconds}s before forfeit…`, "warn");
        startGraceCountdown(msg.grace_seconds || 15);
        break;
      case "opponent_rejoined":
        toast("Opponent rejoined.", "ok");
        if (graceTimer) { clearInterval(graceTimer); graceTimer = null; }
        break;
      case "forfeit":
        if (msg.reason === "opponent_left") {
          toast("Opponent left the room.", "warn");
        } else {
          toast("Opponent didn't return. You win by forfeit.", "ok");
        }
        if (graceTimer) { clearInterval(graceTimer); graceTimer = null; }
        // We may need to step back to lobby on opponent_left forfeit since
        // the server closes the room.
        break;
      case "log":
        if (lobbyWrap.classList.contains("hidden")) {
          toast(msg.message);
        }
        // If we're in lobby, the log might be "Room closed" after a forfeit
        // — show as toast too.
        else { toast(msg.message); }
        if (msg.message && /room closed/i.test(msg.message)) {
          showLobby();
        }
        break;
      case "error":
        if (LOBBY_ERROR_CODES.has(msg.code)) {
          // If the join modal is open, show the error there.
          if (!joinModal.classList.contains("hidden")) {
            joinModalError.textContent = msg.message || msg.code;
            joinModalError.classList.remove("hidden");
          } else {
            showLobbyError(msg.message || msg.code);
          }
          showLobby();
        } else {
          toast("Error: " + (msg.message || msg.code || "unknown"), "error");
        }
        break;
    }
  }

  function showLobbyError(text) {
    lobbyError.textContent = text;
    lobbyError.classList.remove("hidden");
  }

  // ---- Render: room list --------------------------------------------
  function renderRooms() {
    refreshBtn.classList.remove("spinning");
    if (!lastRooms.length) {
      roomListEl.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "room-empty";
      empty.textContent = "No rooms yet — create one or use Quick match.";
      roomListEl.appendChild(empty);
      return;
    }
    roomListEl.innerHTML = "";
    for (const r of lastRooms) {
      const row = document.createElement("div");
      row.className = "room-row" + (r.joinable ? "" : " full");
      row.tabIndex = 0;
      row.dataset.code = r.code;
      row.dataset.hasPassword = r.has_password ? "1" : "0";

      const code = document.createElement("span");
      code.className = "code";
      code.textContent = r.code;
      row.appendChild(code);

      if (r.in_progress) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "in progress";
        row.appendChild(badge);
      }

      if (r.has_password) {
        const lock = document.createElement("span");
        lock.className = "lock";
        lock.title = "password required";
        lock.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
        row.appendChild(lock);
      }

      const count = document.createElement("span");
      count.className = "count" + (r.joinable ? "" : " full-tag");
      count.textContent = `${r.players}/${r.max}`;
      row.appendChild(count);

      const onPick = () => {
        if (!r.joinable) {
          toast("Room is full.", "warn");
          return;
        }
        openJoinModal(r);
      };
      row.addEventListener("click", onPick);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(); }
      });
      roomListEl.appendChild(row);
    }
  }

  // ---- Render: chat --------------------------------------------------
  function renderChatHistory(messages) {
    chatMessages.innerHTML = "";
    if (!messages.length) {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      empty.textContent = "Say hi to other players waiting for a match.";
      chatMessages.appendChild(empty);
      return;
    }
    for (const m of messages) appendChatMessage(m, /*scroll=*/false);
    scrollChatToBottom();
  }
  function appendChatMessage(msg, scroll = true) {
    // Remove the empty placeholder if present.
    const empty = chatMessages.querySelector(".chat-empty");
    if (empty) empty.remove();

    const el = document.createElement("div");
    const isMine = msg.nickname && state.name && msg.nickname.trim() === state.name.trim();
    el.className = "chat-msg" + (isMine ? " mine" : "");

    const meta = document.createElement("div");
    meta.className = "meta";
    const nick = document.createElement("span");
    nick.className = "nick";
    nick.textContent = msg.nickname || "anonymous";
    const time = document.createElement("span");
    time.className = "time";
    if (typeof msg.ts === "number") {
      const d = new Date(msg.ts);
      time.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    meta.appendChild(nick);
    meta.appendChild(time);
    el.appendChild(meta);

    const text = document.createElement("div");
    text.className = "text";
    text.textContent = msg.text || "";
    el.appendChild(text);

    chatMessages.appendChild(el);
    if (scroll) scrollChatToBottom();
  }
  function scrollChatToBottom() { chatMessages.scrollTop = chatMessages.scrollHeight; }
  function clearChat() {
    chatMessages.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "chat-empty";
    empty.textContent = "Say hi to your opponent.";
    chatMessages.appendChild(empty);
  }

  // ---- Game render --------------------------------------------------
  function render() {
    roundNoEl.textContent = state.round;
    roomLabel.textContent = state.room || "";
    youMark.textContent = state.you ?? "?";
    oppMark.textContent = state.you === "X" ? "O" : (state.you === "O" ? "X" : "?");
    youName.textContent = state.name || "You";
    oppName.textContent = state.oppNameStr || "Opponent";

    const myScore  = state.scores[state.you] ?? 0;
    const oppMarkLetter = state.you === "X" ? "O" : "X";
    const otherScore = state.scores[oppMarkLetter] ?? 0;
    youScore.textContent = myScore;
    oppScore.textContent = otherScore;

    const cells = boardEl.querySelectorAll(".cell");
    cells.forEach((cell) => {
      const r = +cell.dataset.row, c = +cell.dataset.col;
      const v = state.board[r][c];
      const prev = cell.textContent;
      cell.classList.toggle("taken", !!v);
      cell.classList.remove("x", "o");
      cell.textContent = v ? v : "";
      if (v === "X") cell.classList.add("x");
      if (v === "O") cell.classList.add("o");
      if (v && prev !== v) cell.classList.add("placed");
      else cell.classList.remove("placed");
      const myTurn = state.turn === state.you && state.winner === null && state.oppConnected;
      cell.classList.toggle("disabled", v !== null || !myTurn);
    });

    banner.classList.add("hidden");
    banner.classList.remove("win", "lose", "draw");
    newRoundBtn.classList.add("hidden");

    if (state.winner === null) {
      const myTurn = state.turn === state.you;
      turnLine.textContent = myTurn ? "Your turn" : `${state.oppNameStr || "Opponent"}'s turn`;
      turnLine.className = "turn " + (myTurn ? "your" : "theirs");
    } else {
      turnLine.textContent = "Round over";
      turnLine.className = "turn ended";
      if (state.winner === "draw") {
        banner.textContent = "It's a draw.";
        banner.classList.add("draw");
      } else if (state.winner === state.you) {
        banner.textContent = "🎉 You win this round!";
        banner.classList.add("win");
      } else {
        banner.textContent = "You lost this round.";
        banner.classList.add("lose");
      }
      banner.classList.remove("hidden");
      newRoundBtn.classList.remove("hidden");
    }
    drawWinLine();
  }
  function drawWinLine() {
    winLineSvg.innerHTML = "";
    if (!state.line || state.line.length !== 3) return;
    ensureSvgGradient();
    const [a, , b] = state.line;
    const x1 = (a[1] + 0.5) / 3 * 100;
    const y1 = (a[0] + 0.5) / 3 * 100;
    const x2 = (b[1] + 0.5) / 3 * 100;
    const y2 = (b[0] + 0.5) / 3 * 100;
    const ns = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    winLineSvg.appendChild(line);
  }
  function startGraceCountdown(seconds) {
    if (graceTimer) clearInterval(graceTimer);
    let s = seconds;
    turnLine.textContent = `Opponent left — ${s}s`;
    turnLine.className = "turn ended";
    graceTimer = setInterval(() => {
      s -= 1;
      if (s <= 0) { clearInterval(graceTimer); graceTimer = null; return; }
      turnLine.textContent = `Opponent left — ${s}s`;
    }, 1000);
  }

  // ---- Join modal ----------------------------------------------------
  function openJoinModal(room) {
    pendingJoinRoom = room;
    joinModalCode.textContent = room.code;
    joinModalCount.textContent = `${room.players}/${room.max}`;
    if (room.has_password) {
      joinModalLock.classList.remove("hidden");
      joinModalLock.textContent = "🔒 password required";
      joinModalPwLabel.classList.remove("hidden");
      joinModalIcon.classList.add("locked");
    } else {
      joinModalLock.classList.add("hidden");
      joinModalPwLabel.classList.add("hidden");
      joinModalIcon.classList.remove("locked");
    }
    joinModalError.classList.add("hidden");
    joinModalName.value = inputName.value;
    joinModalPassword.value = "";
    joinModal.classList.remove("hidden");
    setTimeout(() => {
      if (room.has_password) joinModalPassword.focus();
      else if (!joinModalName.value) joinModalName.focus();
      else joinModal.querySelector("button[type=submit]").focus();
    }, 60);
  }
  function closeJoinModal() {
    joinModal.classList.add("hidden");
    pendingJoinRoom = null;
  }
  joinModalCancel.addEventListener("click", closeJoinModal);
  joinModalClose.addEventListener("click", closeJoinModal);
  joinModal.addEventListener("click", (ev) => {
    if (ev.target === joinModal) closeJoinModal();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !joinModal.classList.contains("hidden")) closeJoinModal();
  });

  joinModalForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!pendingJoinRoom) return;
    const name = joinModalName.value.trim();
    if (!name) { joinModalError.textContent = "Please enter a name."; joinModalError.classList.remove("hidden"); return; }
    state.name = name;
    inputName.value = name;
    savePrefs();
    state.room = pendingJoinRoom.code;

    const msg = { type: "join", name, room: pendingJoinRoom.code };
    if (pendingJoinRoom.has_password) {
      const pw = joinModalPassword.value;
      if (!pw) { joinModalError.textContent = "Password required."; joinModalError.classList.remove("hidden"); return; }
      msg.password = pw;
    }
    if (!send(msg)) {
      joinModalError.textContent = "Not connected to server.";
      joinModalError.classList.remove("hidden");
      return;
    }
    closeJoinModal();
    showWaiting();
  });

  // ---- Manual join (by code) ----------------------------------------
  manualJoinBtn.addEventListener("click", () => {
    const code = manualCode.value.trim().toUpperCase();
    if (!code) return;
    // Look up in known rooms to know whether password is required;
    // if not in list, assume no password and let server tell us if wrong.
    const found = lastRooms.find((r) => r.code === code);
    openJoinModal(found || { code, players: 0, max: 2, has_password: false, joinable: true });
  });

  // ---- Create form --------------------------------------------------
  createForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    lobbyError.classList.add("hidden");
    const name = inputName.value.trim();
    if (!name) { showLobbyError("Please enter a name."); return; }
    const code = inputCreateRoom.value.trim().toUpperCase();
    if (!code) { showLobbyError("Please enter a room code."); return; }
    state.name = name;
    state.room = code;
    savePrefs();
    const msg = { type: "create", name, room: code };
    const pw = inputCreatePassword.value;
    if (pw) msg.password = pw;
    if (!send(msg)) { showLobbyError("Not connected to server."); return; }
    showWaiting();
  });

  // ---- Quick form ---------------------------------------------------
  quickForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    lobbyError.classList.add("hidden");
    const name = inputName.value.trim();
    if (!name) { showLobbyError("Please enter a name."); return; }
    state.name = name;
    state.room = null;
    savePrefs();
    if (!send({ type: "auto", name })) { showLobbyError("Not connected to server."); return; }
    showWaiting();
  });

  // ---- Refresh button ----------------------------------------------
  refreshBtn.addEventListener("click", () => {
    refreshBtn.classList.add("spinning");
    setTimeout(() => refreshBtn.classList.remove("spinning"), 600);
    send({ type: "list_rooms" });
  });

  // ---- Chat form (only effective when seated in a room) -------------
  chatForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    if (!state.room) {
      toast("Chat is only available inside a room.", "warn");
      return;
    }
    if (!send({ type: "chat", text })) {
      toast("Not connected.", "error");
      return;
    }
    chatInput.value = "";
  });

  // ---- Game interactions --------------------------------------------
  function onCellClick(ev) {
    const cell = ev.currentTarget;
    if (cell.classList.contains("taken") || cell.classList.contains("disabled")) return;
    const r = +cell.dataset.row, c = +cell.dataset.col;
    send({ type: "move", row: r, col: c });
  }

  waitingCancel.addEventListener("click", () => {
    send({ type: "leave" });
    showLobby();
  });

  newRoundBtn.addEventListener("click", () => {
    if (send({ type: "new_round" })) {
      newRoundBtn.disabled = true;
      newRoundBtn.textContent = "Waiting for opponent…";
      setTimeout(() => { newRoundBtn.disabled = false; newRoundBtn.textContent = "Play again"; }, 5000);
    }
  });

  leaveBtn.addEventListener("click", () => {
    send({ type: "leave" });
    showLobby();
  });

  // ---- Section visibility --------------------------------------------
  function showLobby() {
    lobbyWrap.classList.remove("hidden");
    waitingEl.classList.add("hidden");
    gameEl.classList.add("hidden");
    state.room = null;
    state.you = null;
    state.oppNameStr = null;
    state.board = emptyBoard();
    state.scores = { X: 0, O: 0, draws: 0 };
    state.round = 1;
    state.winner = null;
    state.line = null;
    clearChat();
    // Ask server for fresh rooms snapshot upon return.
    send({ type: "list_rooms" });
  }
  function showWaiting() {
    lobbyWrap.classList.add("hidden");
    waitingEl.classList.remove("hidden");
    gameEl.classList.add("hidden");
    waitingRoom.textContent = state.room || "auto-match";
    const room = lastRooms.find((r) => r.code === state.room);
    waitingPwTag.classList.toggle("hidden", !(room && room.has_password));
    // Reset chat panel (it'll be repopulated when the second player joins).
    clearChat();
  }
  function showGame() {
    lobbyWrap.classList.add("hidden");
    waitingEl.classList.add("hidden");
    gameEl.classList.remove("hidden");
    chatStatus.textContent = state.oppNameStr || "alone";
  }

  // ---- Init ----------------------------------------------------------
  buildBoard();
  setConn("connecting");
  setLatency(null);
  connect();
})();
