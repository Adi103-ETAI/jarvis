"""WhatsApp bridge adapter — Baileys sidecar client (D9 escape hatch).

Phase 8.2. The operator chose the WhatsApp-Web path (decision D9
amendment). D9's terms make it safe to say yes: the sidecar
(`sidecar/whatsapp/bridge.js`, Node + Baileys) is an UNTRUSTED DUMB
TRANSPORT — it links one WhatsApp account and pipes JSON lines on
stdio; it holds no policy, no pairing, no sessions, no JARVIS logic,
and listens on no port. Every message it emits faces the same
admission pipeline (pairing, allowFrom, lanes) as every other channel;
every text it is asked to send was already admitted. The trust
boundary is the same, only the wire is unofficial.

Rim-side discipline (this file owns it):

    spawn/respawn   self-healing: a dead or failed bridge is restarted
                    with backoff — the rim never dies with it
    distrust        bridge output is parsed defensively and coerced
                    into the normalized shape; malformed lines drop
    dedupe          reconnects redeliver; message ids are remembered
    serialization   webhook/poller threads enter the spine one at a
                    time (same law as the cloud adapter)
    send            chunked at the WhatsApp limit, one JSON line per
                    chunk, stdin lock against concurrent relay threads

`popen` is injectable so tests run the whole lifecycle against a fake
process — hermetic, no Node, no sockets.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections import deque
from itertools import count
from pathlib import Path

from jarvis.channels.telegram import AdapterError
from jarvis.channels.whatsapp import CHUNK_LIMIT, _chunks

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BRIDGE_SCRIPT = _REPO_ROOT / "sidecar" / "whatsapp" / "bridge.js"
DEFAULT_BACKOFF = 5.0          # respawn pause after a bridge death


def normalize_bridge_message(m: dict) -> dict | None:
    """One sidecar message object -> normalized message, or None.

    The bridge is untrusted, so this re-derives every field: text must
    be present, ids coerced to str, `date` to int-or-None, and the
    group/private split is decided HERE by the chat-id suffix — a
    bridge that claims "private" for a `@g.us` chat is corrected.
    """
    if not isinstance(m, dict):
        return None
    text = str(m.get("text", "") or "").strip()
    chat_id = str(m.get("chat_id", "") or "").strip()
    sender_id = str(m.get("sender_id", "") or "").strip()
    if not text or not chat_id or not sender_id:
        return None
    chat_type = "group" if chat_id.endswith("@g.us") else "private"
    ts = m.get("date")
    if not isinstance(ts, int) or isinstance(ts, bool):
        ts = None
    return {
        "update_id": None,
        "message_id": str(m.get("message_id", "") or ""),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": str(m.get("sender_name", "") or "")[:80] or sender_id,
        "text": text,
        "reply_to_sender_id": None,        # v0: quoted context unparsed
        "date": ts,
    }


class WhatsAppBridgeAdapter:
    """One WhatsApp account via the Node sidecar: JSON lines on stdio."""

    name = "whatsapp"

    def __init__(self, cmd: list[str], *, chunk_limit: int = CHUNK_LIMIT,
                 backoff: float = DEFAULT_BACKOFF, popen=None, env=None):
        if not cmd:
            raise ValueError("bridge adapter needs a command "
                             "(e.g. ['node', 'sidecar/whatsapp/bridge.js'])")
        self._cmd = [str(x) for x in cmd]
        self.chunk_limit = int(chunk_limit)
        self.backoff = max(0.05, float(backoff))
        self._popen = popen or subprocess.Popen
        self._env = env
        # Telegram parity: no bot identity on WhatsApp; mention
        # detection falls back to policy mention_patterns.
        self.bot_id: str | None = None
        self.bot_username: str | None = None
        self._proc = None
        self._ids = count(1)
        self._stdin_lock = threading.Lock()
        self._spine_lock = threading.Lock()
        self._seen: dict[str, bool] = {}
        self.last_status = "starting"
        self.last_bridge_errors: deque[str] = deque(maxlen=10)

    # ----------------------------------------------------------------- config

    @classmethod
    def from_config(cls, cfg):
        """[channels.whatsapp] with transport="bridge" -> adapter.

        Options (safe fallbacks, never exceptions): bridge_script
        (default: the repo's sidecar), node_bin (default: "node"),
        bridge_auth_dir (default: the sidecar's own auth/ dir)."""
        o = cfg.options or {}
        script = str(o.get("bridge_script", "") or "").strip() or \
            str(DEFAULT_BRIDGE_SCRIPT)
        node_bin = str(o.get("node_bin", "") or "").strip() or "node"
        cmd = [node_bin, script]
        auth_dir = str(o.get("bridge_auth_dir", "") or "").strip()
        if auth_dir:
            cmd += ["--auth-dir", auth_dir]

        def num(key, default):
            try:
                return int(o.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(cmd, chunk_limit=num("chunk_limit", CHUNK_LIMIT),
                   backoff=num("bridge_backoff_seconds", DEFAULT_BACKOFF))

    def startup_notes(self) -> list[str]:
        """Journalled once at start: the trust stance must be visible."""
        auth = "default (sidecar auth/)" if len(self._cmd) == 2 else self._cmd[-1]
        return [
            "transport = Baileys sidecar (UNTRUSTED dumb transport, D9 "
            "escape hatch): " + " ".join(self._cmd[:2]) + f" (auth {auth})",
            "no listening port — stdio only; admission still owns trust",
            "ban hygiene: reply-only, no bulk, no broadcasts; a dedicated "
            "number is strongly recommended",
        ]

    # ------------------------------------------------------------------ loop

    def run_loop(self, on_message, stop) -> None:
        """Poller thread body: keep a bridge alive until `stop`.

        A spawn failure, a crash or a clean exit all land in the same
        place: wait backoff, respawn. `stop` terminates the child so a
        blocked stdout read unblocks immediately.
        """
        while not stop.is_set():
            try:
                proc = self._popen(self._cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=None,
                                   text=True, bufsize=1, env=self._env)
            except Exception as e:  # noqa: BLE001 — never fatal
                self.last_status = f"spawn-failed ({str(e)[:120]})"
                stop.wait(self.backoff)
                continue
            self._proc = proc
            killer = threading.Thread(target=self._kill_on_stop,
                                      args=(proc, stop), daemon=True)
            killer.start()
            try:
                for raw in proc.stdout:
                    if stop.is_set():
                        break
                    self._handle_line(raw, on_message)
            except Exception:  # noqa: BLE001 — the rim never dies with the bridge
                pass
            finally:
                self.last_status = "down"
                for closer in (proc.stdin.close, proc.terminate):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass
                killer.join(timeout=2.0)
            if stop.is_set():
                break
            stop.wait(self.backoff)    # self-heal: respawn the bridge

    def _kill_on_stop(self, proc, stop) -> None:
        stop.wait()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    def _handle_line(self, raw: str, on_message) -> None:
        line = (raw or "").strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")
        if kind == "status":
            self.last_status = str(obj.get("state", "?"))
            return
        if kind == "sent" and obj.get("ok") is False:
            self.last_bridge_errors.append(str(obj.get("error", "send failed"))[:200])
            return
        if kind == "message":
            msg = normalize_bridge_message(obj.get("message"))
            if msg is None:
                return
            mid = msg["message_id"]
            if mid and self._seen.get(mid):
                return                  # reconnect redelivery — spine stays quiet
            if mid:
                self._remember(mid)
            with self._spine_lock:
                try:
                    on_message(msg)
                except Exception:  # noqa: BLE001 — presenter law
                    pass

    # --------------------------------------------------------------- outbound

    def send_message(self, chat_id: str, text: str,
                     reply_to=None) -> int:
        """Send text through the bridge, chunked; raises AdapterError if
        the bridge is down or its stdin is gone. Fire-and-forget in v0:
        send acks are not awaited, failures land in
        `last_bridge_errors` (visible in live diagnostics)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise AdapterError("bridge not running")
        sent = 0
        for chunk in _chunks(text or "", self.chunk_limit):
            line = json.dumps({"type": "send", "id": next(self._ids),
                               "to": str(chat_id), "text": chunk},
                              ensure_ascii=True)
            try:
                with self._stdin_lock:
                    proc.stdin.write(line + "\n")
                    proc.stdin.flush()
            except Exception as e:  # noqa: BLE001 — stdin gone counts as down
                raise AdapterError(f"bridge stdin: {e}") from e
            sent += 1
        return sent

    # -------------------------------------------------------------- internals

    def _remember(self, mid) -> None:
        if not mid:
            return
        self._seen[str(mid)] = True
        while len(self._seen) > 1024:
            self._seen.pop(next(iter(self._seen)))
