"""A stateful Slack digital twin for henry — its slice only, socket-mode + web.

Real henry connects to this unmodified: point AsyncWebClient.base_url here and
(1) apps.connections.open returns THIS twin's websocket, (2) the twin pushes
event envelopes shaped from Slack's real Events API, (3) chat.postMessage etc.
land here and mutate thread state. Event shapes come from Slack's spec, not from
what henry expects — that's what makes it catch bugs instead of confirming them.
"""

from __future__ import annotations

import json
import socket
import time
import uuid

from aiohttp import WSMsgType, web

BOT = "UBOT"
# Mirrors henry/slack/app.py — the text henry posts before it has an answer.
PLACEHOLDER = "Working on it..."


class SlackTwin:
    def __init__(self) -> None:
        self.ws: web.WebSocketResponse | None = None
        self.threads: dict[tuple[str, str], list[dict]] = {}  # (channel, thread_ts) -> messages
        self.posted: list = []  # everything henry sent us
        self.acks: set[str] = set()
        self._ts = 100.0
        self._port = 0
        self._runner: web.AppRunner | None = None

    def _next_ts(self) -> str:
        self._ts += 1.0
        return f"{self._ts:.6f}"

    # ---- what henry POSTS to us (outbound Web API) ----
    async def chat_postMessage(self, p: dict) -> dict:
        ch, thread_ts, text = p.get("channel", ""), p.get("thread_ts"), p.get("text", "")
        ts = self._next_ts()
        # a placeholder with no thread_ts starts its own thread at its ts
        root = thread_ts or ts
        msg = {"user": BOT, "bot_id": "B1", "text": text, "ts": ts, "thread_ts": root}
        self.threads.setdefault((ch, root), []).append(msg)
        self.posted.append(("chat_postMessage", p))
        return {"ok": True, "channel": ch, "ts": ts, "message": msg}

    async def chat_update(self, p: dict) -> dict:
        ch, ts, text = p.get("channel", ""), p.get("ts", ""), p.get("text", "")
        for msgs in self.threads.values():
            for m in msgs:
                if m["ts"] == ts:
                    m["text"] = text
        self.posted.append(("chat_update", p))
        return {"ok": True, "channel": ch, "ts": ts, "text": text}

    async def conversations_replies(self, p: dict) -> dict:
        ch, ts = p.get("channel", ""), p.get("ts", "")
        return {"ok": True, "messages": self.threads.get((ch, ts), [])}

    # ---- what WE push to henry (inbound Events API, shaped from Slack's spec) ----
    async def _push_event(self, event: dict) -> None:
        envelope = {
            "token": "verification",
            "team_id": "T1",
            "api_app_id": "A1",
            "event_id": f"Ev{uuid.uuid4().hex[:10]}",
            "event_time": int(time.time()),
            "type": "event_callback",
            "event": event,
            "authorizations": [{"team_id": "T1", "user_id": BOT, "is_bot": True}],
        }
        frame = {
            "type": "events_api",
            "envelope_id": uuid.uuid4().hex,
            "payload": envelope,
            "accepts_response_payload": False,
        }
        assert self.ws is not None, "henry hasn't connected to the twin socket yet"
        await self.ws.send_json(frame)

    async def mention(self, channel: str, user: str, text: str) -> str:
        """A user @mentions henry — arrives as an app_mention event."""
        ts = self._next_ts()
        await self._push_event(
            {
                "type": "app_mention",
                "user": user,
                "text": f"<@{BOT}> {text}",
                "ts": ts,
                "channel": channel,
                "event_ts": ts,
            }
        )
        return ts  # the thread root

    async def mention_in_thread(self, channel: str, thread_ts: str, user: str, text: str) -> str:
        """A user @mentions henry inside an existing thread — an app_mention event with thread_ts."""
        ts = self._next_ts()
        await self._push_event(
            {
                "type": "app_mention",
                "user": user,
                "text": f"<@{BOT}> {text}",
                "ts": ts,
                "channel": channel,
                "thread_ts": thread_ts,
                "event_ts": ts,
            }
        )
        return ts

    async def reply_in_thread(self, channel: str, thread_ts: str, user: str, text: str, **extra) -> None:
        """A user replies IN THE THREAD without @mentioning — arrives as a `message`
        event with thread_ts."""
        ts = self._next_ts()
        # The user's message also lands in the thread state so conversations.replies
        # reflects it, like real Slack.
        event = {
            "type": "message",
            "user": user,
            "text": text,
            "ts": ts,
            "channel": channel,
            "channel_type": "channel",
            "thread_ts": thread_ts,
            "event_ts": ts,
        }
        event.update(extra)  # e.g. bot_id=..., subtype=... for the negative cases
        self.threads.setdefault((channel, thread_ts), []).append(
            {"user": user, "text": text, "ts": ts, "thread_ts": thread_ts, **extra}
        )
        await self._push_event(event)

    async def wait_for_bot_reply(self, channel: str, thread_ts: str, since: int, timeout: float = 8.0) -> bool:
        """True if henry posted a NEW message in this thread after `since`."""
        import asyncio

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.bot_msgs(channel, thread_ts) > since:
                return True
            await asyncio.sleep(0.05)
        return False

    def bot_msgs(self, channel: str, thread_ts: str) -> int:
        return len([m for m in self.threads.get((channel, thread_ts), []) if m.get("user") == BOT])

    def bot_texts(self, channel: str, thread_ts: str) -> list[str]:
        """Every message henry has in this thread, placeholder included.

        Henry posts a placeholder and then chat.update's it in place, so a text
        read here reflects the latest edit — same as opening the thread in Slack.
        """
        return [m["text"] for m in self.threads.get((channel, thread_ts), []) if m.get("user") == BOT]

    async def wait_for_final(
        self,
        channel: str,
        thread_ts: str,
        *,
        timeout: float = 30.0,
        placeholder: str = PLACEHOLDER,
    ) -> str:
        """Henry's first real answer in this thread, or a marker string on timeout.

        Waits past the placeholder: a run that calls tools edits the placeholder
        into the answer, so the message existing is not the same as it being done.
        """
        import asyncio

        deadline = time.time() + timeout
        while time.time() < deadline:
            for text in self.bot_texts(channel, thread_ts):
                if text and text != placeholder:
                    return text
            await asyncio.sleep(0.05)
        return "(no final reply within timeout)"

    # ---- server ----
    async def start(self) -> str:
        app = web.Application()

        async def web_api(request: web.Request) -> web.Response:
            method = request.match_info["method"]
            if request.method == "GET":  # slack_sdk uses GET for read methods
                p = dict(request.query)
            else:
                body = await request.post()
                p = dict(body) if body else {}
                if not p:
                    try:
                        p = json.loads(await request.text() or "{}")
                    except Exception:
                        p = {}
            if method == "auth.test":
                return web.json_response(
                    {
                        "ok": True,
                        "url": "https://t1.slack.com/",
                        "team": "T1",
                        "user": "henry",
                        "team_id": "T1",
                        "user_id": BOT,
                        "bot_id": "B1",
                    }
                )
            if method == "apps.connections.open":
                return web.json_response({"ok": True, "url": f"ws://127.0.0.1:{self._port}/socket"})
            if method == "chat.postMessage":
                return web.json_response(await self.chat_postMessage(p))
            if method == "chat.update":
                return web.json_response(await self.chat_update(p))
            if method == "conversations.replies":
                return web.json_response(await self.conversations_replies(p))
            return web.json_response({"ok": True})  # any other call henry makes

        async def socket_route(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse(autoping=True)
            await ws.prepare(request)
            self.ws = ws
            await ws.send_json({"type": "hello", "num_connections": 1, "connection_info": {"app_id": "A1"}})
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("envelope_id"):
                        self.acks.add(d["envelope_id"])  # henry acked an event
            return ws

        app.router.add_post("/api/{method}", web_api)
        app.router.add_get("/api/{method}", web_api)
        app.router.add_get("/socket", socket_route)
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        self._port = sk.getsockname()[1]
        sk.close()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self._port).start()
        return f"http://127.0.0.1:{self._port}/api/"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
