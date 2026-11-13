/* EC4 Live — browser dashboard for the local Link Visual feed. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const player = $("#player");
const grid = $("#grid");
const empty = $("#empty");
const busy = $("#busy");
const busyText = $("#busy-text");
const toastEl = $("#toast");

const state = {
  cameras: [],
  cameraSignature: "",
  selectedName: null,
  activeName: null,
  inst: null,
  streamInfo: null,
  streamState: "off", // off | starting | live | reconnecting | error
  selecting: false,
  generation: 0,
  ms: null,
  sb: null,
  queue: [],
  recvBuf: new Uint8Array(0),
  fetchAbort: null,
  objectUrl: null,
  retryTimer: null,
  codec: "",
  mode: localStorage.getItem("ec4.mode") || "grid",
  status: null,
  pollTimer: null,
};

/* ---------------------------------------------------------------- helpers */
function showToast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.classList.toggle("error", isError);
  toastEl.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toastEl.classList.remove("show"), 4200);
}

function setBusy(on, text = "Working…") {
  busyText.textContent = text;
  busy.classList.toggle("hidden", !on);
}

function setConn(text, cls = "") {
  const el = $("#conn");
  el.className = "conn " + cls;
  el.querySelector("span").textContent = text;
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    cache: "no-store",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    ...opts,
  });
  let data = null;
  try {
    data = await resp.json();
  } catch (_) {
    data = { ok: false, error: `HTTP ${resp.status}` };
  }
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

function setStreamState(next) {
  state.streamState = next;
  updateMeta();
  updateControls();
}

/* ---------------------------------------------------------------- cameras */
async function loadCameras(force = false) {
  let data;
  try {
    data = await api("/api/cameras" + (force ? "?force=1" : ""));
  } catch (err) {
    setConn("Server offline", "offline");
    return null;
  }
  if (!data.ok) {
    showToast(data.error || "Could not read the camera list", true);
    setConn("App offline", "offline");
    return null;
  }
  const cams = data.cameras || [];
  const sig = cams.map((c) => c.name).join("|");
  state.cameras = cams;
  if (sig !== state.cameraSignature) {
    state.cameraSignature = sig;
    if (state.activeName && !cams.some((c) => c.name === state.activeName)) {
      stopStream();
      state.activeName = null;
      state.inst = null;
      state.streamInfo = null;
      setStreamState("off");
      setActiveTile(null);
      updateEmpty();
    }
    if (state.selectedName && !cams.some((c) => c.name === state.selectedName)) {
      state.selectedName = null;
    }
    if (!state.selectedName) state.selectedName = autoCamera();
    renderTiles();
    if (state.activeName) setActiveTile(state.activeName);
  } else {
    updateMeta();
  }
  renderChips();
  updateEmpty();
  updateControls();
  if (!cams.length) {
    setConn("No cameras found", "offline");
  } else if (state.streamState === "live") {
    setConn("Live · local P2P", "live");
  } else if (state.status && state.status.connected) {
    setConn("Camera on · press Start", "");
  } else {
    setConn("Camera off", "");
  }
  return data;
}

function renderChips() {
  const bar = $("#camera-bar");
  bar.innerHTML = "";
  for (const cam of state.cameras) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cam-chip";
    if (cam.name === state.selectedName) btn.classList.add("selected");
    if (cam.name === state.activeName) btn.classList.add("active");
    if (cam.playing || cam.name === state.activeName) btn.classList.add("playing");
    if (!cam.linkvisual) btn.classList.add("unsupported");
    btn.innerHTML = `<span class="chip-dot"></span><span>${escapeHtml(cam.name)}</span>`;
    btn.addEventListener("click", () => selectCamera(cam.name));
    bar.appendChild(btn);
  }
}

function renderTiles() {
  // Move the live video out before destroying its container.
  if (player.parentElement) player.remove();
  grid.innerHTML = "";
  for (const cam of state.cameras) {
    const tile = document.createElement("article");
    tile.className = "tile";
    tile.dataset.name = cam.name;
    tile.tabIndex = 0;
    tile.setAttribute("role", "button");
    tile.setAttribute("aria-label", `Start ${cam.name}`);

    const slot = document.createElement("div");
    slot.className = "video-slot";
    tile.appendChild(slot);

    const placeholder = document.createElement("div");
    placeholder.className = "placeholder";
    placeholder.textContent = "◉";
    tile.appendChild(placeholder);

    const hint = document.createElement("div");
    hint.className = "watch-hint";
    hint.innerHTML = "<span>CLICK TO START</span>";
    tile.appendChild(hint);

    const overlay = document.createElement("div");
    overlay.className = "tile-overlay";
    overlay.innerHTML = `
      <div>
        <div class="tile-name">${escapeHtml(cam.name)}</div>
        <div class="tile-meta"><span class="badge off"><i class="dot"></i><span class="badge-text">OFFLINE</span></span><span class="res"></span></div>
      </div>
      <div class="tile-actions">
        <button class="icon-btn snap" type="button" title="Save snapshot">⤓</button>
        <button class="icon-btn full" type="button" title="Fullscreen">⛶</button>
      </div>`;
    tile.appendChild(overlay);

    const act = (fn) => (event) => {
      event.stopPropagation();
      fn();
    };
    overlay.querySelector(".full").addEventListener("click", act(() => goFullscreen()));
    overlay.querySelector(".snap").addEventListener("click", act(() => snapshot()));

    tile.addEventListener("click", () => selectCamera(cam.name));
    tile.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectCamera(cam.name);
      }
    });
    grid.appendChild(tile);
  }
  grid.classList.toggle("single", state.mode === "single");
  updateMeta();
}

function setActiveTile(name) {
  for (const tile of grid.querySelectorAll(".tile")) {
    tile.classList.toggle("active", tile.dataset.name === name);
  }
  if (!name) {
    player.classList.remove("attached");
    updateEmpty();
    return;
  }
  const tile = grid.querySelector(`.tile[data-name="${cssEscape(name)}"]`);
  if (tile) {
    const slot = tile.querySelector(".video-slot");
    if (player.parentElement !== slot) {
      slot.appendChild(player);
      if (state.hasStream) player.play().catch(() => {});
    }
  }
  updateEmpty();
}

function updateEmpty() {
  const hasActive = Boolean(state.activeName && grid.querySelector(".tile.active"));
  const title = empty.querySelector(".empty-title");
  const sub = empty.querySelector(".empty-sub");
  const background = Boolean(state.status && state.status.connected);
  if (title && sub) {
    title.textContent = background ? "Camera is on" : "Camera is off";
    if (background) {
      sub.textContent = state.selectedName
        ? `Press Start to view ${state.selectedName}, or Stop to turn it off.`
        : "Press Start to view, or Stop to turn it off.";
    } else {
      sub.textContent = state.selectedName
        ? `Press Start to watch ${state.selectedName}, or click a camera.`
        : "Press Start or click a camera to begin.";
    }
  }
  empty.classList.toggle("hidden", hasActive);
  if (!hasActive) empty.classList.remove("hidden");
}

function updateMeta() {
  for (const tile of grid.querySelectorAll(".tile")) {
    const name = tile.dataset.name;
    const badge = tile.querySelector(".badge");
    const text = tile.querySelector(".badge-text");
    const res = tile.querySelector(".res");
    const cam = state.cameras.find((c) => c.name === name);
    tile.classList.toggle(
      "selected",
      name === state.selectedName && name !== state.activeName
    );
    let label = "OFF";
    let cls = "badge off";
    let meta = "";
    if (name === state.activeName) {
      if (state.streamState === "live") {
        label = "LIVE";
        cls = "badge live";
        if (state.streamInfo) {
          const w = state.streamInfo.width;
          const h = state.streamInfo.height;
          if (w && h) meta = `${w}×${h}`;
        }
        if (state.status && state.status.width && state.status.height) {
          meta = `${state.status.width}×${state.status.height}`;
        }
        if (state.status && state.status.instances) {
          const inst = state.status.instances.find((i) => i.inst === state.inst);
          if (inst && inst.fps > 0.1) meta += `${meta ? " · " : ""}${inst.fps.toFixed(0)} fps`;
        }
      } else if (state.streamState === "starting") {
        label = "STARTING";
        cls = "badge starting";
        meta = "connecting local P2P…";
      } else if (state.streamState === "reconnecting") {
        label = "RECONNECTING";
        cls = "badge starting";
      } else if (state.streamState === "error") {
        label = "UNAVAILABLE";
        cls = "badge off";
      }
    } else if (cam && cam.playing) {
      label = "ON";
      cls = "badge starting";
      meta = "background";
    }
    tile.classList.toggle("live", state.streamState === "live" && name === state.activeName);
    badge.className = cls;
    text.textContent = label;
    res.textContent = meta;
  }
  renderChips();
}

/* ---------------------------------------------------------------- select */
async function selectCamera(name) {
  if (!name || state.selecting) return;
  if (name === state.activeName && state.streamState === "live") return;

  state.selecting = true;
  state.selectedName = name;
  state.activeName = name;
  localStorage.setItem("ec4.camera", name);
  stopStream();
  setActiveTile(name);
  setStreamState("starting");
  setConn("Starting…", "");
  renderChips();
  updateControls();
  setBusy(true, `Starting ${name}…  this can take ~10 seconds`);
  try {
    const data = await api("/api/select", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    state.inst = data.inst;
    state.streamInfo = data;
    state.codec = data.codec || "";
    setActiveTile(name);
    player.classList.add("attached");
    setStreamState("live");
    state.hasStream = true;
    startStream(data.stream, 0);
    setBusy(false);
    showToast(`${name} live`);
    setTimeout(() => loadCameras(false), 1200);
  } catch (err) {
    setBusy(false);
    setStreamState("error");
    stopStream();
    showToast(err.message || `Could not start ${name}`, true);
  } finally {
    state.selecting = false;
    updateControls();
  }
}

/* ---------------------------------------------------------------- streaming */
function stopStream() {
  state.generation += 1;
  state.hasStream = false;
  if (state.fetchAbort) {
    try { state.fetchAbort.abort(); } catch (_) {}
    state.fetchAbort = null;
  }
  state.queue = [];
  state.recvBuf = new Uint8Array(0);
  if (state.sb && state.ms && state.ms.readyState === "open") {
    try { state.ms.removeSourceBuffer(state.sb); } catch (_) {}
  }
  state.sb = null;
  state.ms = null;
  try {
    player.pause();
    player.removeAttribute("src");
    player.load();
  } catch (_) {}
  player.classList.remove("attached");
  if (state.objectUrl) {
    try { URL.revokeObjectURL(state.objectUrl); } catch (_) {}
    state.objectUrl = null;
  }
}

function startStream(url, attempt = 0) {
  const gen = state.generation;
  if (!player.parentElement) setActiveTile(state.activeName);
  player.muted = true;
  player.classList.add("attached");

  // A retry creates a fresh MediaSource.  Drop the previous SourceBuffer and
  // queued fragments, otherwise new fragments are appended to the old source
  // and the player stays black.
  state.queue = [];
  state.recvBuf = new Uint8Array(0);
  state.sb = null;
  if (state.objectUrl) {
    try { URL.revokeObjectURL(state.objectUrl); } catch (_) {}
    state.objectUrl = null;
  }

  const ms = new MediaSource();
  state.ms = ms;
  const objectUrl = URL.createObjectURL(ms);
  state.objectUrl = objectUrl;
  player.src = objectUrl;

  ms.addEventListener("sourceopen", () => {
    if (gen !== state.generation) return;
    fetchStream(url, gen);
  }, { once: true });

  player.play().catch(() => {});
}

async function fetchStream(url, gen) {
  const ctrl = new AbortController();
  state.fetchAbort = ctrl;
  state.recvBuf = new Uint8Array(0);
  try {
    const resp = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
    if (!resp.ok) {
      if (resp.status === 404 && gen === state.generation) {
        stopStream();
        state.activeName = null;
        state.inst = null;
        state.streamInfo = null;
        setStreamState("off");
        setActiveTile(null);
        updateEmpty();
        showToast("Camera is no longer configured", true);
        loadCameras(false);
        return;
      }
      throw new Error(`stream HTTP ${resp.status}`);
    }
    if (!resp.body) throw new Error("streaming not supported by this browser");
    const reader = resp.body.getReader();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (gen !== state.generation) return;
      if (!value || !value.length) continue;
      state.recvBuf = concatBytes(state.recvBuf, value);
      parseFrames(gen);
    }
    throw new Error("stream ended");
  } catch (err) {
    if (gen !== state.generation) return;
    if (err && err.name === "AbortError") return;
    if (gen === state.generation) {
      setStreamState("reconnecting");
      clearTimeout(state.retryTimer);
      state.retryTimer = setTimeout(() => {
        if (gen === state.generation && state.activeName) {
          startStream(url, 0);
        }
      }, 1600);
    }
  }
}

function parseFrames(gen) {
  while (state.recvBuf.length >= 4) {
    const view = new DataView(state.recvBuf.buffer, state.recvBuf.byteOffset, 4);
    const len = view.getUint32(0, false);
    if (len === 0 || state.recvBuf.length < 4 + len) break;
    const payload = state.recvBuf.slice(4, 4 + len);
    state.recvBuf = state.recvBuf.slice(4 + len);
    if (gen === state.generation) handleFrame(payload);
  }
}

function handleFrame(payload) {
  if (!state.sb) {
    const codec = parseAvcCodec(payload) || state.codec || 'video/mp4; codecs="avc1.4D4028"';
    const source = codec.startsWith("avc1.") ? `video/mp4; codecs="${codec}"` : 'video/mp4; codecs="avc1.4D4028"';
    if (!MediaSource.isTypeSupported(source)) {
      showToast(`Browser cannot decode ${source}`, true);
      return;
    }
    try {
      state.sb = state.ms.addSourceBuffer(source);
      state.codec = codec;
      state.sb.mode = "segments";
      state.sb.addEventListener("updateend", () => {
        trimBuffer();
        pumpQueue();
        if (state.streamState !== "live") setStreamState("live");
        if (state.streamState === "live") updateMeta();
      });
      state.sb.addEventListener("error", () => {
        setStreamState("reconnecting");
      });
    } catch (err) {
      showToast("MSE setup failed: " + err.message, true);
      return;
    }
    player.play().catch(() => {});
  }
  state.queue.push(payload);
  pumpQueue();
}

function pumpQueue() {
  const sb = state.sb;
  if (!sb || sb.updating || !state.queue.length) return;
  const chunk = state.queue.shift();
  try {
    sb.appendBuffer(chunk);
  } catch (err) {
    if (err && err.name === "QuotaExceededError") {
      trimBuffer(true);
      state.queue.unshift(chunk);
    } else {
      console.warn("appendBuffer", err);
      setStreamState("reconnecting");
    }
  }
}

function trimBuffer(force = false) {
  const sb = state.sb;
  if (!sb || sb.updating) return;
  const b = sb.buffered;
  if (!b.length) return;
  const start = b.start(0);
  const end = b.end(b.length - 1);
  const target = player.currentTime;
  if (target - start > 12 || force) {
    try {
      sb.remove(start, Math.max(start + 1, target - 6));
    } catch (_) {}
  }
  if (end - target > 8) {
    // Keep the player near the live edge.
    try { player.currentTime = end - 0.8; } catch (_) {}
  }
}

/* ---------------------------------------------------------------- MSE codec */
function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function parseAvcCodec(init) {
  try {
    const dv = new DataView(init.buffer, init.byteOffset, init.byteLength);
    let found = null;
    const containers = new Set(["moov", "trak", "mdia", "minf", "stbl", "stsd", "avc1", "avc3", "edts", "dinf", "mvex"]);
    const walk = (start, end) => {
      let off = start;
      while (off + 8 <= end) {
        let size = dv.getUint32(off);
        const type = String.fromCharCode(init[off + 4], init[off + 5], init[off + 6], init[off + 7]);
        let head = 8;
        if (size === 1) {
          if (off + 16 > end) break;
          size = Number(dv.getBigUint64(off + 8));
          head = 16;
        }
        if (size < head || off + size > end) break;
        const pStart = off + head;
        const pEnd = off + size;
        if (type === "avcC") {
          if (pStart + 4 <= pEnd) {
            const profile = init[pStart + 1];
            const compat = init[pStart + 2];
            const level = init[pStart + 3];
            found = "avc1." + [profile, compat, level].map((n) => n.toString(16).padStart(2, "0")).join("").toUpperCase();
          }
          return;
        }
        if (containers.has(type)) {
          let child = pStart;
          if (type === "stsd") child += 8;
          if (type === "avc1" || type === "avc3") child += 78;
          walk(child, pEnd);
          if (found) return;
        }
        off += size;
      }
    };
    walk(0, init.length);
    return found;
  } catch (_) {
    return null;
  }
}

/* ---------------------------------------------------------------- actions */
function goFullscreen() {
  const el = player.parentElement ? player : document.documentElement;
  if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
}

function snapshot() {
  if (!player.videoWidth) {
    showToast("No frame to save yet", true);
    return;
  }
  const canvas = document.createElement("canvas");
  canvas.width = player.videoWidth;
  canvas.height = player.videoHeight;
  canvas.getContext("2d").drawImage(player, 0, 0);
  canvas.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    a.download = `EC4-${state.activeName || "camera"}-${stamp}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    showToast("Snapshot saved");
  }, "image/png");
}

async function stopAll() {
  stopStream();
  state.activeName = null;
  state.inst = null;
  state.streamInfo = null;
  setStreamState("off");
  setActiveTile(null);
  renderChips();
  updateEmpty();
  try {
    await api("/api/stop", { method: "POST", body: "{}" });
    state.status = { ...(state.status || {}), connected: false, selected_camera: null };
    showToast("Stopped");
  } catch (err) {
    showToast(err.message, true);
  }
  updateControls();
  loadCameras(false);
}

function startSelected() {
  if (state.selecting) return;
  const name = state.selectedName || autoCamera();
  if (!name) {
    showToast("No camera selected", true);
    return;
  }
  state.selectedName = name;
  localStorage.setItem("ec4.camera", name);
  selectCamera(name);
}

function updateControls() {
  const start = $("#btn-start");
  const stop = $("#btn-stop");
  if (!start || !stop) return;
  const active = ["starting", "live", "reconnecting"].includes(state.streamState);
  const background = Boolean(state.status && state.status.connected);
  start.disabled = active || state.selecting || !state.cameras.length;
  stop.disabled = !active && !background;
  start.title = state.selectedName
    ? `Start ${state.selectedName}`
    : "Start the selected camera";
}

/* ---------------------------------------------------------------- status */
async function pollStatus() {
  try {
    const st = await api("/api/status");
    state.status = st;
    if (state.streamState === "starting") {
      setConn("Starting…", "");
    } else if (state.streamState === "reconnecting") {
      setConn("Reconnecting…", "");
    } else if (state.streamState === "error") {
      setConn("Camera error", "offline");
    } else if (!st.connected) {
      setConn("Camera off", "");
    } else if (state.streamState === "live") {
      setConn("Live · local P2P", "live");
    } else {
      setConn("Camera on · press Start", "");
    }
    if (st.selected_camera && !state.selectedName) {
      state.selectedName = st.selected_camera;
      renderChips();
    }
    if (st.width && st.height && st.selected_camera === state.activeName) {
      if (!state.streamInfo) state.streamInfo = {};
      state.streamInfo.width = st.width;
      state.streamInfo.height = st.height;
    }
    updateMeta();
    updateEmpty();
    updateControls();
  } catch (_) {
    state.status = { ...(state.status || {}), connected: false };
    setConn("Server offline", "offline");
    updateEmpty();
    updateControls();
  }
}

/* ---------------------------------------------------------------- init */
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/["\\]/g, "\\$&");
}

function setMode(mode) {
  state.mode = mode === "single" ? "single" : "grid";
  localStorage.setItem("ec4.mode", state.mode);
  grid.classList.toggle("single", state.mode === "single");
  $("#mode-grid").classList.toggle("active", state.mode === "grid");
  $("#mode-single").classList.toggle("active", state.mode === "single");
  if (state.activeName) setActiveTile(state.activeName);
}

function autoCamera() {
  const hash = decodeURIComponent((location.hash || "").replace(/^#/, "")).trim();
  if (hash && state.cameras.some((c) => c.name === hash)) return hash;
  const saved = localStorage.getItem("ec4.camera");
  if (saved && state.cameras.some((c) => c.name === saved)) return saved;
  const lv = state.cameras.find((c) => c.linkvisual);
  return lv ? lv.name : state.cameras[0]?.name || null;
}

async function init() {
  setMode(state.mode);
  $("#mode-grid").addEventListener("click", () => setMode("grid"));
  $("#mode-single").addEventListener("click", () => setMode("single"));
  $("#btn-refresh").addEventListener("click", async () => {
    setBusy(true, "Scanning camera list…");
    try {
      const data = await loadCameras(true);
      if (data && data.warning) showToast(data.warning, true);
      else if (data) showToast("Camera list refreshed");
    } finally {
      setBusy(false);
    }
  });
  $("#btn-start").addEventListener("click", startSelected);
  $("#btn-stop").addEventListener("click", stopAll);
  player.addEventListener("playing", () => {
    if (state.activeName) setStreamState("live");
  });
  player.addEventListener("waiting", () => {
    if (state.streamState === "live") setConn("Buffering…", "");
  });

  await loadCameras(false);
  if (!state.selectedName) state.selectedName = autoCamera();
  updateEmpty();
  updateControls();
  pollStatus();
  state.pollTimer = setInterval(pollStatus, 2000);
}

init().catch((err) => {
  console.error(err);
  showToast(err.message || "Failed to start", true);
});
