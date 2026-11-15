"use strict";

const $ = (id) => document.getElementById(id);
const panelLogin = $("login-panel");
const panelDesk = $("desk-panel");
const indicator = $("connection");
const screen = $("screen");
const placeholder = $("placeholder");

let socket = null;
let online = false;
let frameUrl = null;
let lastPointer = { x: 0.5, y: 0.5 };
let activePointer = null;
let lastMoveAt = 0;

function showStatus(text, isOnline) {
  indicator.textContent = text;
  indicator.classList.toggle("online", Boolean(isOnline));
  $("pc-status").textContent = text;
  placeholder.hidden = Boolean(isOnline);
  if (!isOnline) screen.removeAttribute("src");
  online = Boolean(isOnline);
}

function send(packet) {
  if (online && socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(packet));
  }
}

function connect() {
  if (socket) socket.close();
  socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/viewer");
  socket.binaryType = "blob";
  showStatus("Connecting...", false);
  socket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "online") showStatus("PC online", true);
        if (data.type === "offline") showStatus("PC offline", false);
      } catch (_) { /* ignore malformed status */ }
      return;
    }
    const nextUrl = URL.createObjectURL(event.data);
    const previousUrl = frameUrl;
    frameUrl = nextUrl;
    screen.onload = () => { if (previousUrl) URL.revokeObjectURL(previousUrl); };
    screen.src = nextUrl;
  });
  socket.addEventListener("close", () => {
    showStatus("Disconnected — refresh to reconnect", false);
    socket = null;
  });
  socket.addEventListener("error", () => showStatus("Connection error", false));
}

async function checkSession() {
  try {
    const r = await fetch("/api/status", { cache: "no-store", credentials: "same-origin" });
    panelLogin.hidden = r.ok;
    panelDesk.hidden = !r.ok;
    if (r.ok) connect();
  } catch (_) {
    $("login-error").textContent = "Unable to reach the relay server.";
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  const btn = event.target.querySelector("button");
  btn.disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value })
    });
    if (!response.ok) throw new Error(response.status === 429 ? "Too many attempts. Try again later." : "Invalid password.");
    $("password").value = "";
    await checkSession();
  } catch (error) { $("login-error").textContent = error.message; }
  finally { btn.disabled = false; }
});

$("logout").addEventListener("click", async () => {
  if (socket) socket.close();
  try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); }
  finally {
    panelDesk.hidden = true;
    panelLogin.hidden = false;
    showStatus("Signed out", false);
  }
});

function pointerCoordinates(event) {
  if (!screen.naturalWidth || !screen.naturalHeight) return null;
  const rect = screen.getBoundingClientRect();
  const ratio = Math.min(rect.width / screen.naturalWidth, rect.height / screen.naturalHeight);
  const w = screen.naturalWidth * ratio;
  const h = screen.naturalHeight * ratio;
  const left = rect.left + (rect.width - w) / 2;
  const top = rect.top + (rect.height - h) / 2;
  if (event.clientX < left || event.clientX > left + w || event.clientY < top || event.clientY > top + h) return null;
  return { x: Math.max(0, Math.min(1, (event.clientX - left) / w)),
           y: Math.max(0, Math.min(1, (event.clientY - top) / h)) };
}

screen.addEventListener("pointerdown", (event) => {
  if (!online) return;
  const pos = pointerCoordinates(event);
  if (!pos) return;
  event.preventDefault();
  lastPointer = pos;
  activePointer = event.pointerId;
  screen.setPointerCapture(event.pointerId);
  send({ type: "mouse", action: "down", ...pos });
});
screen.addEventListener("pointermove", (event) => {
  if (!online) return;
  if (activePointer !== null && event.pointerId !== activePointer) return;
  const now = performance.now();
  if (now - lastMoveAt < 45) return;
  const pos = pointerCoordinates(event);
  if (!pos) return;
  lastMoveAt = now;
  lastPointer = pos;
  send({ type: "mouse", action: "move", ...pos });
});
function releasePointer(event) {
  if (event.pointerId !== activePointer) return;
  const pos = pointerCoordinates(event) || lastPointer;
  lastPointer = pos;
  send({ type: "mouse", action: "up", ...pos });
  activePointer = null;
}
screen.addEventListener("pointerup", releasePointer);
screen.addEventListener("pointercancel", releasePointer);
screen.addEventListener("contextmenu", (event) => event.preventDefault());

document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () =>
  send({ type: "mouse", action: button.dataset.action, ...lastPointer })));
document.querySelectorAll("[data-scroll]").forEach((button) => button.addEventListener("click", () =>
  send({ type: "scroll", delta: Number(button.dataset.scroll) })));
document.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", () =>
  send({ type: "key", value: button.dataset.key })));
document.querySelectorAll("[data-shortcut]").forEach((button) => button.addEventListener("click", () =>
  send({ type: "shortcut", value: button.dataset.shortcut })));
$("type-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const field = $("type-text");
  if (field.value) send({ type: "text", value: field.value });
  field.value = "";
});
window.addEventListener("pagehide", () => {
  if (socket) socket.close();
  if (frameUrl) URL.revokeObjectURL(frameUrl);
});
checkSession();
