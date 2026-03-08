"""Async HTTP + WebSocket server for Dharma.

Serves a built-in browser harness at ``/`` and streams generated test cases
over WebSocket at ``/ws``.  No external files or dependencies needed — just
run ``dharma -grammars grammar.dg -server`` and open the printed URL.

The browser harness:
  - Auto-reconnects on disconnect
  - Tracks test count, error count, and generation rate
  - Provides pause/resume and speed controls
  - Sandboxes test cases in an iframe
  - Works in any modern browser
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dharma import DharmaMachine

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Embedded browser harness (served at /)
# ---------------------------------------------------------------------------

_HARNESS_HTML = (Path(__file__).parent / "harness.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# WebSocket connection
# ---------------------------------------------------------------------------

class WebSocketConnection:
    """Manages a single WebSocket connection over asyncio streams."""

    __slots__ = ("reader", "writer", "_closed")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._closed = False

    async def handshake(self, headers: dict[str, str]) -> bool:
        """Complete the WebSocket upgrade using pre-parsed HTTP headers."""
        ws_key = headers.get("sec-websocket-key", "")
        if not ws_key:
            return False

        accept = base64.b64encode(
            hashlib.sha1(ws_key.encode("ascii") + _WS_GUID).digest()
        ).decode("ascii")

        self.writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await self.writer.drain()
        return True

    async def read_frame(self) -> tuple[int, bytes] | None:
        """Read one complete WebSocket message, reassembling fragmented frames."""
        fragments: list[bytes] = []
        message_opcode: int | None = None

        while True:
            try:
                header = await self.reader.readexactly(2)
            except (asyncio.IncompleteReadError, ConnectionError):
                return None

            fin = bool(header[0] & 0x80)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F

            try:
                if length == 126:
                    length = struct.unpack("!H", await self.reader.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
                mask = await self.reader.readexactly(4) if masked else None
                payload = bytearray(await self.reader.readexactly(length))
            except (asyncio.IncompleteReadError, ConnectionError):
                return None

            if mask:
                payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))

            # Control frames (close/ping/pong) can appear between data fragments
            if opcode >= 0x8:
                return opcode, bytes(payload)

            if opcode != 0x0:  # Not a continuation — start of a new message
                message_opcode = opcode
                fragments = [bytes(payload)]
            else:  # Continuation frame
                fragments.append(bytes(payload))

            if fin:
                return (message_opcode or 0x1), b"".join(fragments)

    async def send_text(self, message: str) -> None:
        """Send a UTF-8 text frame."""
        await self._send_frame(0x1, message.encode("utf-8"))

    async def send_close(self) -> None:
        """Send a close frame (idempotent, best-effort)."""
        if not self._closed:
            self._closed = True
            try:
                await self._send_frame(0x8, b"")
            except ConnectionError:
                pass

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        out = bytearray()
        out.append(0x80 | opcode)
        length = len(payload)
        if length <= 125:
            out.append(length)
        elif length <= 65535:
            out.append(126)
            out.extend(struct.pack("!H", length))
        else:
            out.append(127)
            out.extend(struct.pack("!Q", length))
        out.extend(payload)
        self.writer.write(bytes(out))
        await self.writer.drain()

    def close(self) -> None:
        """Close the underlying transport."""
        self._closed = True
        if not self.writer.is_closing():
            self.writer.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_response(status: str, content_type: str, body: bytes) -> bytes:
    """Build a minimal HTTP/1.1 response."""
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class DharmaWebSocketServer:
    """HTTP + WebSocket server for Dharma.

    Serves the browser fuzzing harness at ``/`` and streams generated test
    cases over WebSocket at ``/ws``.

    Usage::

        server = DharmaWebSocketServer(machine)
        asyncio.run(server.start())
    """

    _FALLBACK_TEMPLATE = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script>window.onerror=function(m,s,l,c,e){'
        'try{'
        'if(window.opener){window.opener.postMessage({type:"dharma-error",message:m+" at "+l+":"+c},"*")}'
        'else if(parent!==window){parent.postMessage({type:"dharma-error",message:m+" at "+l+":"+c},"*")}'
        '}catch(x){}}</script></head><body><script type="module">\n'
        '%s\n</script></body></html>'
    )

    def __init__(
        self,
        machine: DharmaMachine,
        host: str = "127.0.0.1",
        port: int = 9090,
        harness_mode: str = "iframe",
        error_log: str = "dharma-errors.log",
    ) -> None:
        self.machine = machine
        self.host = host
        self.port = port
        self.harness_mode = harness_mode
        self.error_log = error_log
        self._server: asyncio.Server | None = None
        self._error_count: int = 0

    def _log_error(self, message: str) -> None:
        """Append a test error to the error log file."""
        self._error_count += 1
        try:
            with open(self.error_log, "a") as f:
                f.write(f"{self._error_count}: {message}\n")
        except OSError:
            logger.debug("Could not write to error log %s", self.error_log)

    async def start(self) -> None:
        """Start the server and serve forever."""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
        )
        url = f"http://{self.host}:{self.port}"
        logger.info("Dharma server running at %s", url)
        print(f"\n  \033[1;36m→ Open {url} in your browser\033[0m\n")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Gracefully shut down."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Server stopped.")

    # -- connection dispatch --

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Route incoming TCP connections to HTTP or WebSocket handler."""
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            return

        request_line, *header_lines = raw.decode("ascii", errors="replace").split("\r\n")
        headers: dict[str, str] = {}
        for line in header_lines:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        parts = request_line.split()
        path = parts[1] if len(parts) > 1 else "/"
        peer = writer.get_extra_info("peername")

        # WebSocket upgrade on /ws
        if path == "/ws" and "upgrade" in headers.get("connection", "").lower():
            await self._handle_websocket(reader, writer, headers, peer)
        else:
            await self._handle_http(writer, path)

    # -- HTTP --

    def _wrap_content(self, content: str) -> str:
        """Ensure content is a valid HTML document.

        If the machine has a template, the content is already complete HTML.
        Otherwise wrap the raw generated code in a minimal HTML+module-script shell.
        """
        if self.machine.template:
            return content
        return self._FALLBACK_TEMPLATE % content

    async def _handle_http(self, writer: asyncio.StreamWriter, path: str) -> None:
        match path:
            case "/" | "/index.html":
                html = _HARNESS_HTML.replace(
                    '<option value="iframe">Iframe</option>',
                    '<option value="iframe"{}>{}</option>'.format(
                        ' selected' if self.harness_mode == 'iframe' else '',
                        'Iframe',
                    ),
                ).replace(
                    '<option value="tab">New Tab</option>',
                    '<option value="tab"{}>{}</option>'.format(
                        ' selected' if self.harness_mode == 'tab' else '',
                        'New Tab',
                    ),
                )
                if self.harness_mode == 'tab':
                    html = html.replace(
                        'mode = "iframe"',
                        'mode = "tab"',
                    ).replace(
                        '<body>',
                        '<body class="mode-tab">',
                    )
                body = html.encode("utf-8")
                writer.write(_http_response("200 OK", "text/html; charset=utf-8", body))
            case "/testcase":
                try:
                    body = self._wrap_content(self.machine.generate_content()).encode("utf-8")
                except Exception:
                    body = b"<!-- generation error -->"
                writer.write(_http_response("200 OK", "text/html; charset=utf-8", body))
            case "/health":
                writer.write(_http_response("200 OK", "application/json", b'{"status":"ok"}'))
            case _:
                writer.write(_http_response("404 Not Found", "text/plain", b"Not Found"))
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    # -- WebSocket --

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        peer: object,
    ) -> None:
        conn = WebSocketConnection(reader, writer)
        if not await conn.handshake(headers):
            logger.warning("WebSocket handshake failed from %s", peer)
            conn.close()
            return

        logger.info("WebSocket connected: %s", peer)
        try:
            while True:
                result = await conn.read_frame()
                if result is None:
                    break

                opcode, payload = result
                match opcode:
                    case 0x8:  # close
                        break
                    case 0x9:  # ping → pong
                        await conn._send_frame(0xA, payload)
                        continue
                    case 0xA:  # pong
                        continue
                    case 0x1:  # text
                        text = payload.decode("utf-8")
                    case 0x2:  # binary
                        text = payload.decode("utf-8", errors="replace")
                    case _:
                        continue

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Bad JSON from %s: %.100r", peer, text)
                    continue

                status = msg.get("status")
                if status == "open":
                    logger.info("Client %s ready.", peer)
                if status in ("open", "success"):
                    try:
                        content = self._wrap_content(self.machine.generate_content())
                        await conn.send_text(content)
                    except Exception:
                        logger.exception("Generation error for %s", peer)
                elif status == "error":
                    error_msg = msg.get("message", "unknown")
                    logger.warning("Test error from %s: %s", peer, error_msg)
                    self._log_error(error_msg)
                elif status == "closed":
                    logger.info("Client %s closed.", peer)
                    break
                else:
                    logger.warning("Unexpected message from %s: %r", peer, msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            logger.debug("Connection lost: %s", peer)
        finally:
            await conn.send_close()
            conn.close()
            logger.info("Disconnected: %s", peer)
