#!/usr/bin/env node
"use strict";
/*
 * JARVIS WhatsApp bridge — the D9 escape hatch, made honest.
 *
 * UNTRUSTED DUMB TRANSPORT: this sidecar links ONE WhatsApp account
 * (Baileys, WhatsApp-Web emulation) and pipes JSON lines on stdio.
 * It contains ZERO JARVIS logic — no policy, no pairing, no sessions,
 * no trust. Admission, lanes and pairing are decided in the Python
 * rim; this process is only a pair of ears and a mouth that happens
 * to hold the session keys.
 *
 * Protocol (one JSON object per line):
 *   stdout: {"type":"status","state":"qr|connecting|ready|close","qr"|"info":...}
 *           {"type":"message","message":{...normalized, same shape as the
 *            cloud adapter...}}
 *           {"type":"sent","id":N,"ok":true|false,"error":"..."}
 *   stdin:  {"type":"send","id":N,"to":"digits-or-jid","text":"..."}
 *
 * Nothing listens on any port. Stderr is for human diagnostics and
 * passes through to the console host.
 *
 * Ban-risk hygiene (the discipline Hermes' own docs recommend): the
 * bridge sends only what the rim hands it — one line, one send, no
 * bulk, no broadcasts, no status targets. Reply-only traffic inside
 * real conversations. Use a dedicated number.
 */

const path = require("path");
const pino = require("pino");
const qrcode = require("qrcode-terminal");
const baileys = require("baileys");
const makeWASocket = baileys.default;
const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = baileys;

// ------------------------------------------------------------------ args

const argv = process.argv.slice(2);
function argOf(flag, fallback) {
  const i = argv.indexOf(flag);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--")
    ? argv[i + 1] : fallback;
}
const AUTH_DIR = path.resolve(argOf("--auth-dir", path.join(__dirname, "auth")));

// ------------------------------------------------------------------ wire

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}
function status(state, extra) {
  emit(Object.assign({ type: "status", state: state }, extra || {}));
}

function normalizeJid(to) {
  const t = String(to || "").trim();
  if (!t) return null;
  if (t.indexOf("@") >= 0) return t;
  const digits = t.replace(/[^0-9]/g, "");
  return digits ? digits + "@s.whatsapp.net" : null;
}

function digits(jid) {
  return String(jid || "").split("@")[0];
}

function extractText(m) {
  if (!m || !m.message) return null;
  const msg = m.message;
  return msg.conversation
    || (msg.extendedTextMessage && msg.extendedTextMessage.text)
    || null;
}

// ----------------------------------------------------------------- socket

let sock = null;

function attach(sock) {
  sock.ev.on("connection.update", (u) => {
    const connection = u.connection;
    const qr = u.qr;
    if (qr) {
      try { qrcode.generate(qr, { small: true }); } catch (e) {}
      status("qr", { qr: qr, info: "scan from the phone that owns the number; expires ~60s" });
    }
    if (connection === "connecting") status("connecting");
    if (connection === "open") status("ready");
    if (connection === "close") {
      const err = (u.lastDisconnect && u.lastDisconnect.error) || {};
      const code = (err.output && err.output.statusCode) || err.code || 0;
      if (code === DisconnectReason.loggedOut) {
        status("close", { info: "logged out — clear the auth dir and re-link (new QR)" });
        process.exit(1);              // the rim respawns; the operator re-scans
      }
      status("close", { info: "reconnecting (code " + code + ")" });
      start();                        // self-heal inside the bridge too
    }
  });

  sock.ev.on("messages.upsert", (up) => {
    const messages = (up && up.messages) || [];
    for (const m of messages) {
      try {
        if (m.key && m.key.fromMe) continue;              // our own sends echo
        const jid = String((m.key && m.key.remoteJid) || "");
        if (!jid || jid.endsWith("@status") || jid.endsWith("@broadcast")) continue;
        const text = extractText(m);
        if (!text) continue;                              // v0: text only
        const isGroup = jid.endsWith("@g.us");
        const sender = isGroup ? digits((m.key && m.key.participant) || jid) : digits(jid);
        emit({
          type: "message",
          message: {
            update_id: null,
            message_id: String((m.key && m.key.id) || ""),
            chat_id: isGroup ? jid : sender,
            chat_type: isGroup ? "group" : "private",
            sender_id: sender,
            sender_name: String(m.pushName || sender),
            text: String(text),
            reply_to_sender_id: null,                     // v0: quoted context unparsed
            date: Number(m.messageTimestamp) || null,
          },
        });
      } catch (e) {
        process.stderr.write("bridge: message normalize failed: " + e.message + "\n");
      }
    }
  });
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try { version = (await fetchLatestBaileysVersion()).version; } catch (e) {}
  sock = makeWASocket({
    version: version,
    auth: state,
    printQRInTerminal: false,        // we render + emit the QR ourselves
    logger: pino({ level: "silent" }),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });
  sock.ev.on("creds.update", saveCreds);
  attach(sock);
}

// ------------------------------------------------------------------ stdin

const readline = require("readline");
const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  let cmd;
  try { cmd = JSON.parse(line); } catch (e) { return; }
  if (!cmd || cmd.type !== "send" || !sock) return;
  const jid = normalizeJid(cmd.to);
  const text = String(cmd.text || "");
  if (!jid || !text) {
    emit({ type: "sent", id: cmd.id, ok: false, error: "bad to/text" });
    return;
  }
  if (jid.endsWith("@broadcast") || jid.endsWith("@status")) {
    emit({ type: "sent", id: cmd.id, ok: false, error: "broadcast/status target refused" });
    return;
  }
  sock.sendMessage(jid, { text: text })
    .then(() => emit({ type: "sent", id: cmd.id, ok: true }))
    .catch((e) => emit({
      type: "sent", id: cmd.id, ok: false,
      error: String((e && e.message) || e),
    }));
});

process.on("SIGTERM", () => process.exit(0));
process.on("SIGINT", () => process.exit(0));

status("starting", { info: "auth dir " + AUTH_DIR });
start().catch((e) => {
  status("close", { info: "start failed: " + String((e && e.message) || e) });
  process.exit(1);                   // the rim respawns with backoff
});
