"""Telegram channel adapter — Bot API over HTTPS, stdlib only.

Phase 8 (OpenClaw-adapted). OpenClaw normalizes every platform through
an adapter with four duties: authentication, inbound parsing, access
control, outbound formatting. JARVIS splits those duties: this adapter
owns authentication + parsing + formatting ONLY. Access control NEVER
lives here — every admission decision lives in the gateway where the
policy rules are visible and testable. The adapter is deliberately
dumb: it authenticates with the bot token, long-polls getUpdates,
normalizes updates, and sends chunked text.

Everything is injectable: `transport` is a callable(method, payload,
timeout) -> dict, so tests run the whole loop without sockets.
"""

from __future__ import annotations

import json
import urllib.request

TELEGRAM_API = "https://api.telegram.org"
CHUNK_LIMIT = 3800        # sendMessage hard limit is 4096; margin for markup
POLL_TIMEOUT = 25.0       # long-poll hold, server-side (seconds)
ERROR_BACKOFF = 3.0       # pause after a failed poll before retrying


class AdapterError(RuntimeError):
    """A Telegram API call failed (transport error or ok=false)."""


def http_transport(token: str):
    """Default transport: Bot API over HTTPS via urllib.

    Returns a callable(method, payload, timeout) -> parsed response dict.
    The token is baked into the URL once, here — it never travels
    further than this closure.
    """
    def call(method: str, payload: dict | None, timeout: float) -> dict:
        url = f"{TELEGRAM_API}/bot{token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return call


def normalize_message(update: dict) -> dict | None:
    """One raw Telegram update -> normalized message dict, or None.

    v0 scope: ordinary `message` updates with text only. Edits, channel
    posts, callbacks and media are dropped (documented limit, phase 8).
    """
    msg = update.get("message") or {}
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}
    text = msg.get("text")
    if not text or not sender.get("id") or not chat.get("id"):
        return None
    reply_to = (msg.get("reply_to_message") or {}).get("from") or {}
    return {
        "update_id": update.get("update_id"),
        "message_id": msg.get("message_id"),
        "chat_id": str(chat["id"]),
        "chat_type": str(chat.get("type", "")),   # private | group | supergroup
        "sender_id": str(sender["id"]),
        "sender_name": (sender.get("username") or sender.get("first_name")
                        or str(sender["id"])),
        "text": text,
        "reply_to_sender_id": (str(reply_to["id"]) if reply_to.get("id") else None),
        "date": msg.get("date"),
    }


class TelegramAdapter:
    """One Telegram bot connection: poll in, chunked text out."""

    name = "telegram"

    def __init__(self, token: str, *, transport=None,
                 chunk_limit: int = CHUNK_LIMIT, poll_timeout: float = POLL_TIMEOUT,
                 error_backoff: float = ERROR_BACKOFF):
        if not token:
            raise ValueError("telegram adapter needs a bot token")
        self._call = transport or http_transport(token)
        self.chunk_limit = chunk_limit
        self.poll_timeout = poll_timeout
        self.error_backoff = error_backoff
        self.offset = 0
        self.bot_id: str | None = None
        self.bot_username: str | None = None

    # ------------------------------------------------------------------ api

    def api(self, method: str, payload: dict | None = None,
            timeout: float = 30.0) -> dict:
        """One Bot API call; returns the `result` payload.

        Raises AdapterError on ok=false or transport failure — callers
        decide whether that is fatal (startup) or survivable (poll loop).
        """
        try:
            out = self._call(method, payload or {}, timeout)
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001 — urllib raises too many shapes
            raise AdapterError(f"{method}: {e}") from e
        if not isinstance(out, dict) or out.get("ok") is not True:
            desc = (out.get("description", "unparseable response")
                    if isinstance(out, dict) else str(out))
            raise AdapterError(f"{method}: {desc}")
        return out.get("result")

    def identify(self) -> bool:
        """getMe — bot id + username (they power implicit mentions).

        Best effort: a failure only degrades mention detection; polling
        continues with pattern-based mentions alone.
        """
        try:
            me = self.api("getMe", {}, timeout=10.0) or {}
        except AdapterError:
            return False
        self.bot_id = str(me["id"]) if me.get("id") else None
        self.bot_username = me.get("username")
        return self.bot_id is not None

    # --------------------------------------------------------------- inbound

    def poll_once(self) -> list[dict]:
        """One getUpdates long poll -> normalized messages.

        The offset advances past every seen update (confirmed or not —
        v0 accepts the redelivery risk over the loss risk; Telegram
        redelivers unconfirmed updates, which the admission pipeline
        dedupes by message content at personal scale).
        """
        result = self.api("getUpdates", {
            "offset": self.offset,
            "timeout": int(self.poll_timeout),
            "allowed_updates": ["message"],
        }, timeout=self.poll_timeout + 10.0) or []
        out = []
        for upd in result:
            uid = upd.get("update_id")
            if isinstance(uid, int) and uid >= self.offset:
                self.offset = uid + 1
            msg = normalize_message(upd)
            if msg:
                out.append(msg)
        return out

    # --------------------------------------------------------------- outbound

    def send_message(self, chat_id: str, text: str,
                     reply_to: int | None = None) -> int:
        """Send text to a chat, chunked at the platform limit.

        Returns the number of chunks sent; raises AdapterError on the
        first failed chunk (caller decides retry policy).
        """
        sent = 0
        for chunk in _chunks(text or "", self.chunk_limit):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if reply_to:
                payload["reply_to_message_id"] = reply_to
            self.api("sendMessage", payload)
            sent += 1
        return sent

    # ------------------------------------------------------------------ loop

    def run_loop(self, on_message, stop) -> None:
        """Poller thread body: long-poll until `stop` is set.

        Errors back off and retry — the poller is never fatal. One bad
        message never kills the loop either; the gateway's admission
        pipeline does the per-message work.
        """
        self.identify()
        while not stop.is_set():
            try:
                batch = self.poll_once()
            except AdapterError:
                stop.wait(self.error_backoff)
                continue
            for msg in batch:
                if stop.is_set():
                    break
                try:
                    on_message(msg)
                except Exception:  # noqa: BLE001 — presenter/loop stays alive
                    pass


def _chunks(text: str, limit: int) -> list[str]:
    text = text.strip() or "(empty)"
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts
