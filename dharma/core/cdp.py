"""Minimal Chrome DevTools Protocol client for Dharma.

Connects to a Chrome/Chromium instance with remote debugging enabled and
feeds generated test cases directly via CDP — no browser harness needed.

Requires Chrome launched with::

    chrome --remote-debugging-port=9222

Usage::

    dharma -grammars grammar.dg -cdp
    dharma -grammars grammar.dg -cdp -cdp-port 9222 -cdp-ttl 2.0
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .dharma import DharmaMachine

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw HTTP helper (zero-dependency)
# ---------------------------------------------------------------------------

async def _http_get_json(host: str, port: int, path: str) -> Any:
    """Perform a minimal HTTP GET and return the parsed JSON body."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        # Read until the server closes the connection (Connection: close).
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.wait_for(reader.read(1 << 16), timeout=10.0)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    _, _, body = data.partition(b"\r\n\r\n")
    if not body:
        raise ConnectionError("Empty response from Chrome debug endpoint")
    return json.loads(body)


# ---------------------------------------------------------------------------
# WebSocket client (CDP speaks WebSocket)
# ---------------------------------------------------------------------------

class _WSClient:
    """Minimal async WebSocket client for CDP communication."""

    __slots__ = ("_reader", "_writer", "_closed")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @classmethod
    async def connect(cls, url: str) -> _WSClient:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        path = parsed.path or "/"

        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()

        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        status_line = response.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            raise ConnectionError(f"WebSocket upgrade failed: {status_line.decode()}")

        expected_accept = base64.b64encode(
            hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()
        ).decode("ascii")
        if expected_accept.encode("ascii") not in response:
            raise ConnectionError("Invalid Sec-WebSocket-Accept from Chrome")

        return cls(reader, writer)

    async def send_text(self, text: str) -> None:
        """Send a masked text frame (RFC 6455 — clients must mask)."""
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))

        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        header.extend(masked)

        self._writer.write(bytes(header))
        await self._writer.drain()

    async def recv(self) -> bytes | None:
        """Read one unmasked frame from the server."""
        try:
            hdr = await self._reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        length = hdr[1] & 0x7F
        try:
            if length == 126:
                length = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            return await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    def close(self) -> None:
        self._closed = True
        if not self._writer.is_closing():
            self._writer.close()


# ---------------------------------------------------------------------------
# CDP connection (JSON‑RPC over WebSocket)
# ---------------------------------------------------------------------------

class CDPConnection:
    """Manages a CDP session: sends commands, dispatches events."""

    def __init__(self, ws: _WSClient) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls, ws_url: str) -> CDPConnection:
        ws = await _WSClient.connect(ws_url)
        conn = cls(ws)
        conn._reader_task = asyncio.create_task(conn._read_loop())
        return conn

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a CDP command and await its result."""
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        await self._ws.send_text(json.dumps(msg))
        return await asyncio.wait_for(future, timeout=30.0)

    async def wait_event(self, method: str, timeout: float = 30.0) -> dict:
        """Block until a specific CDP event arrives.

        Non-matching events are re-queued so other consumers can process them.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        requeue: list[dict] = []
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError(f"Timeout waiting for {method}")
                event = await asyncio.wait_for(self._events.get(), timeout=remaining)
                if event.get("method") == method:
                    return event
                requeue.append(event)
        finally:
            for evt in requeue:
                await self._events.put(evt)

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        # Cancel any pending request futures so callers don't hang
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._ws.close()

    async def _read_loop(self) -> None:
        while True:
            data = await self._ws.recv()
            if data is None:
                break
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            else:
                await self._events.put(msg)


# ---------------------------------------------------------------------------
# CDP runner
# ---------------------------------------------------------------------------

class DharmaCDPRunner:
    """Feeds generated test cases to Chrome via CDP.

    Usage::

        runner = DharmaCDPRunner(machine)
        asyncio.run(runner.run())
    """

    _FALLBACK_TEMPLATE = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script>window.onerror=function(m,s,l,c,e){'
        'try{document.title="ERROR: "+m}catch(x){}'
        '}</script></head><body><script type="module">\n'
        '%s\n</script></body></html>'
    )

    def __init__(
        self,
        machine: DharmaMachine,
        host: str = "127.0.0.1",
        port: int = 9222,
        ttl: float = 2.0,
    ) -> None:
        self.machine = machine
        self.host = host
        self.port = port
        self.ttl = ttl

    async def run(self) -> None:
        """Connect to Chrome and loop: inject test case → wait → repeat."""
        # Discover a page target
        targets = await _http_get_json(self.host, self.port, "/json/list")
        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            target = await _http_get_json(self.host, self.port, "/json/new?about:blank")
        else:
            target = page_targets[0]

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ConnectionError(
                "Chrome target has no webSocketDebuggerUrl. "
                "Is --remote-debugging-port set?"
            )

        logger.info("CDP connecting to %s", ws_url)
        conn = await CDPConnection.open(ws_url)

        try:
            await conn.send("Page.enable")
            result = await conn.send("Page.getFrameTree")
            frame_id = result["result"]["frameTree"]["frame"]["id"]

            title = target.get("title", target.get("url", "unknown"))
            print(f"\n  \033[1;36m→ CDP connected — target: {title}\033[0m\n")

            count = 0
            while True:
                # Reset page state
                await conn.send("Page.navigate", {"url": "about:blank"})
                await asyncio.sleep(0.05)

                # Inject generated test case
                content = self.machine.generate_content()
                if self.machine.template:
                    html = content
                else:
                    html = self._FALLBACK_TEMPLATE % content
                await conn.send("Page.setDocumentContent", {
                    "frameId": frame_id,
                    "html": html,
                })

                count += 1
                if count % 100 == 0:
                    logger.info("CDP: %d test cases sent", count)

                await asyncio.sleep(self.ttl)
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.exception("CDP error")
        finally:
            await conn.close()
            logger.info("CDP: %d total test cases", count)
