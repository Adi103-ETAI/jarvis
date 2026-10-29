"""WhatsApp channel adapter — Business Cloud API over HTTPS, stdlib only.

Phase 8.1 (second channel; decision D9). OpenClaw's WhatsApp support
runs on Baileys — a Node library that reverse-engineers the WhatsApp
Web multi-device protocol. That path is refused here twice over: it
sits outside WhatsApp's terms (the linked number risks a ban) and it
cannot exist inside a stdlib-only, auditable rim. JARVIS speaks the
OFFICIAL surface instead: the WhatsApp Business Cloud API — one HTTPS
POST (Graph API) to send, one webhook to receive. No protocol
reverse-engineering, no Node sidecar, no npm tree.

The adapter keeps the contract Telegram set (dumb on purpose):

    normalize_webhook   one Meta webhook payload -> normalized messages
    send_message        chunked text out via graph.facebook.com
    run_loop            an HTTP webhook receiver until `stop` is set

Trust stays where it always is (D7/D8): admission, pairing, lanes and
relay live in the gateway and policy.toml; this adapter authenticates,
parses and formats ONLY. The webhook verify-token handshake (GET) and
the X-Hub-Signature-256 check (POST, enforced when `app_secret` is
configured) are authentication, not authorization — a forged message
that somehow passed them would still hit the same pairing/allowlist
admission every other channel faces.

Everything is injectable: `transport` is a callable(method_path,
payload, timeout) -> dict, so sends are tested without sockets; the
webhook server binds an ephemeral port in tests via `webhook_port=0`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from jarvis.channels.telegram import AdapterError  # one wire-error type

GRAPH_API = "https://graph.facebook.com"
GRAPH_VERSION = "v21.0"
CHUNK_LIMIT = 4000        # WhatsApp text hard limit is 4096 chars
WEBHOOK_PORT = 8080
WEBHOOK_PATH = "/webhook"
SEEN_MAX = 1024           # inbound dedupe window (Meta retries webhooks)


def http_transport(token: str, phone_number_id: str,
                   version: str = GRAPH_VERSION):
    """Default transport: Graph API over HTTPS via urllib.

    Returns a callable(method_path, payload, timeout) -> parsed dict.
    The token is baked into the Authorization header once, here — it
    never travels further than this closure.
    """
    base = f"{GRAPH_API}/{version}/{phone_number_id}"

    def call(method_path: str, payload: dict | None, timeout: float) -> dict:
        req = urllib.request.Request(
            f"{base}/{method_path}", data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return call


def signature_ok(app_secret: str, raw: bytes, header: str | None) -> bool:
    """Meta's X-Hub-Signature-256 check: HMAC-SHA256(app_secret, body)."""
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.split("=", 1)[1])


def normalize_webhook(payload: dict) -> list[dict]:
    """One raw Meta webhook payload -> normalized messages (or []).

    v0 scope: ordinary TEXT messages only. Statuses (delivery receipts),
    media, template and interactive messages are dropped — the same
    documented limit the Telegram adapter carries. Group messages are
    recognized by the payload's group id (chat ids look like
    `<id>@g.us`); everything else is a private chat whose chat_id is
    the sender's WhatsApp id (country code + number, no plus).
    """
    out: list[dict] = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = {}
            for c in value.get("contacts") or []:
                contacts[str(c.get("wa_id", ""))] = \
                    ((c.get("profile") or {}).get("name") or "")
            for m in value.get("messages") or []:
                if str(m.get("type", "text")) != "text":
                    continue
                text = (m.get("text") or {}).get("body")
                sender = str(m.get("from", "") or "")
                if not text or not sender:
                    continue
                group_id = str(value.get("group_id", "") or "")
                if group_id:
                    chat_id, chat_type = group_id, "group"
                else:
                    chat_id, chat_type = sender, "private"
                ts = str(m.get("timestamp", "") or "")
                out.append({
                    "update_id": None,               # webhook push: no offset
                    "message_id": str(m.get("id", "") or ""),
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "sender_id": sender,
                    "sender_name": contacts.get(sender) or sender,
                    "text": text,
                    "reply_to_sender_id": None,      # v0: quoted context unparsed
                    "date": int(ts) if ts.isdigit() else None,
                })
    return out


class WhatsAppCloudAdapter:
    """One WhatsApp Business number: webhook in, chunked text out."""

    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, *, verify_token=None,
                 app_secret=None, webhook_host: str = "0.0.0.0",
                 webhook_port: int = WEBHOOK_PORT, webhook_path: str = WEBHOOK_PATH,
                 api_version: str = GRAPH_VERSION, transport=None,
                 chunk_limit: int = CHUNK_LIMIT):
        if not token:
            raise ValueError("whatsapp adapter needs a cloud API access token")
        if not phone_number_id:
            raise ValueError("whatsapp adapter needs phone_number_id "
                             "(Meta App > WhatsApp > API Setup)")
        self._call = transport or http_transport(token, phone_number_id,
                                                 api_version)
        self.phone_number_id = str(phone_number_id)
        self.chunk_limit = int(chunk_limit)
        self.webhook_host = str(webhook_host)
        self.webhook_port = int(webhook_port)
        self.webhook_path = "/" + str(webhook_path or WEBHOOK_PATH).lstrip("/")
        # The verify token only guards Meta's subscription handshake; a
        # deterministic derivation from the access token keeps it secret
        # from anyone who cannot read policy.toml. Override in policy.
        vt = str(verify_token).strip() if verify_token else ""
        self.verify_token = vt or hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        self.app_secret = str(app_secret).strip() if app_secret else None
        # Telegram parity: no bot identity on WhatsApp; group mention
        # detection falls back to policy mention_patterns.
        self.bot_id: str | None = None
        self.bot_username: str | None = None
        self._seen: dict[str, bool] = {}      # wamid -> True, trimmed to SEEN_MAX
        self._spine_lock = threading.Lock()   # webhook threads enter one at a time
        self.bound_port: int | None = None    # real port after bind (status/tests)
        self._vt_from_policy = bool(vt)

    # ----------------------------------------------------------------- config

    @classmethod
    def from_config(cls, cfg):
        """[channels.whatsapp] ChannelConfig -> adapter, or None when the
        one required credential (phone_number_id) is missing. Unknown
        stanza keys ride in cfg.options; every value gets the safe
        fallback, never an exception."""
        o = cfg.options or {}
        phone_id = str(o.get("phone_number_id", "") or "").strip()
        if not phone_id or not cfg.token:
            return None

        def num(key, default):
            try:
                return int(o.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            token=cfg.token,
            phone_number_id=phone_id,
            verify_token=str(o.get("webhook_verify_token", "") or "").strip() or None,
            app_secret=str(o.get("app_secret", "") or "").strip() or None,
            webhook_host=str(o.get("webhook_host", "") or "0.0.0.0"),
            webhook_port=num("webhook_port", WEBHOOK_PORT),
            webhook_path=str(o.get("webhook_path", "") or WEBHOOK_PATH),
            api_version=str(o.get("api_version", "") or GRAPH_VERSION),
            chunk_limit=num("chunk_limit", CHUNK_LIMIT),
        )

    def startup_notes(self) -> list[str]:
        """Journalled once at start; the signature stance must be visible."""
        notes = [f"webhook http://{self.webhook_host}:{self.webhook_port}"
                 f"{self.webhook_path} (verify token "
                 f"{'from policy' if getattr(self, '_vt_from_policy', False) else 'derived'})"]
        if self.app_secret:
            notes.append("webhook signature enforcement ON (X-Hub-Signature-256)")
        else:
            notes.append("app_secret not set — webhook signature NOT verified "
                         "(dev mode; set [channels.whatsapp] app_secret before real traffic)")
        return notes

    # -------------------------------------------------------------------- api

    def api(self, method_path: str, payload: dict | None = None,
            timeout: float = 30.0) -> dict:
        """One Graph API call. Raises AdapterError on a transport failure
        or a Graph error envelope — callers decide retry policy."""
        try:
            out = self._call(method_path, payload or {}, timeout)
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001 — urllib raises too many shapes
            raise AdapterError(f"{method_path}: {e}") from e
        if not isinstance(out, dict) or out.get("error"):
            err = out.get("error") if isinstance(out, dict) else None
            desc = (err.get("message", "unparseable error envelope")
                    if isinstance(err, dict) else str(out)[:200])
            raise AdapterError(f"{method_path}: {desc}")
        return out

    # --------------------------------------------------------------- outbound

    def send_message(self, chat_id: str, text: str,
                     reply_to: str | None = None) -> int:
        """Send text to a WhatsApp id, chunked at the platform limit.

        Returns the number of chunks sent; raises AdapterError on the
        first failed chunk. Group ids (`@g.us`) are a best-effort v0
        extra (no recipient_type); private ids require
        recipient_type=individual.
        """
        sent = 0
        for chunk in _chunks(text or "", self.chunk_limit):
            payload: dict = {"messaging_product": "whatsapp", "to": str(chat_id),
                             "type": "text",
                             "text": {"preview_url": False, "body": chunk}}
            if not str(chat_id).endswith("@g.us"):
                payload["recipient_type"] = "individual"
            self.api("messages", payload)
            sent += 1
        return sent

    # ---------------------------------------------------------------- webhook

    def run_loop(self, on_message, stop) -> None:
        """Webhook receiver thread body: serve until `stop` is set.

        GET  = Meta's subscription handshake (hub.challenge echo on a
               matching hub.verify_token, 403 otherwise).
        POST = one webhook payload; signature-checked when app_secret is
               configured, normalized, deduped by message id (Meta
               redelivers on timeout), then handed to on_message one at
               a time — the spine keeps its single-threaded discipline.
        """
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console clean
                pass

            def _reply(self, code: int, body: str = "") -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                q = parse_qs(urlparse(self.path).query)
                if (q.get("hub.mode", [""])[0] == "subscribe"
                        and q.get("hub.verify_token", [""])[0] == adapter.verify_token):
                    self._reply(200, q.get("hub.challenge", [""])[0])
                else:
                    self._reply(403, "forbidden")

            def do_POST(self) -> None:
                raw = self.rfile.read(
                    int(self.headers.get("Content-Length", 0) or 0))
                if self.path.split("?")[0] != adapter.webhook_path:
                    self._reply(404, "not found")
                    return
                if adapter.app_secret and not signature_ok(
                        adapter.app_secret, raw,
                        self.headers.get("X-Hub-Signature-256", "")):
                    self._reply(403, "bad signature")
                    return
                self._reply(200, "ok")           # ack fast; work after
                try:
                    msgs = normalize_webhook(json.loads(raw.decode("utf-8") or "{}"))
                except Exception:  # noqa: BLE001 — a malformed POST is not fatal
                    return
                for m in msgs:
                    mid = m.get("message_id")
                    if mid and adapter._seen.get(mid):
                        continue                  # Meta redelivery — spine stays quiet
                    adapter._remember(mid)
                    with adapter._spine_lock:
                        try:
                            on_message(m)
                        except Exception:  # noqa: BLE001 — presenter law
                            pass

        httpd = ThreadingHTTPServer((self.webhook_host, self.webhook_port), Handler)
        self.bound_port = httpd.server_address[1]
        httpd.timeout = 0.5
        try:
            while not stop.is_set():
                httpd.handle_request()
        finally:
            httpd.server_close()

    # -------------------------------------------------------------- internals

    def _remember(self, mid) -> None:
        if not mid:
            return
        self._seen[str(mid)] = True
        while len(self._seen) > SEEN_MAX:
            self._seen.pop(next(iter(self._seen)))


def _chunks(text: str, limit: int) -> list[str]:
    text = text.strip() or "(empty)"
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts
