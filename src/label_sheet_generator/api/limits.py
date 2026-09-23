"""Body-size enforcement and rate limiting.

Both exist because the previous app had neither: a 380KB JSON body with 20,000
records was accepted and rendered, and an upload was read with a single
unbounded ``await file.read()`` on the event loop. On a 256MB free-tier
container that is an out-of-memory kill, not a slow request.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Most forwarded hops considered. A client can prepend arbitrarily many
#: entries; keeping only the tail bounds both the work and the key length.
MAX_FORWARDED_HOPS = 16

#: Bound on a bucket-map key, which is attacker-influenced when a proxy is
#: trusted. Comfortably longer than any IPv6 address.
MAX_CLIENT_KEY_LENGTH = 64


class BodySizeLimitMiddleware:
    """Reject oversized bodies by declared length *and* by actual bytes read.

    ``Content-Length`` is a claim, not a fact: it can be absent under chunked
    transfer encoding and it can lie. Checking it first is the cheap path that
    rejects before any body is buffered; counting the streamed bytes is the one
    that actually holds.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._too_large(scope, receive, send, declared)
            return

        counter = {"seen": 0, "tripped": False}

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                counter["seen"] += len(message.get("body", b""))
                if counter["seen"] > self.max_bytes:
                    counter["tripped"] = True
                    # Truncate the stream so the handler sees a clean end rather
                    # than continuing to buffer a body we have already refused.
                    return {"type": "http.disconnect"}
            return message

        # Truncating the stream makes the handler fail to parse its body, so it
        # answers 400 "error parsing the body" -- technically true, but it tells
        # the caller nothing about the real cause. Swallow that response and
        # send the 413 instead. Nothing has reached the client yet at this
        # point, so both the status and the body can still be replaced.
        swallowed = {"started": False}

        async def watching_send(message: Message) -> None:
            if not counter["tripped"]:
                await send(message)
                return
            if message["type"] == "http.response.start":
                swallowed["started"] = True
                return
            if message["type"] == "http.response.body" and swallowed["started"]:
                return
            await send(message)

        await self.app(scope, counting_receive, watching_send)

        if counter["tripped"]:
            await self._too_large(scope, receive, send, counter["seen"])

    async def _too_large(self, scope: Scope, receive: Receive, send: Send, actual: int) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": (f"request body is {actual} bytes; the maximum is {self.max_bytes}"),
                    "limit_name": "max_body_bytes",
                    "limit": self.max_bytes,
                    "actual": actual,
                    "details": [],
                }
            },
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", []):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    """Per-client token bucket, thread-safe, with bounded memory.

    Deliberately in-process: this app is a single container on a free tier, so
    a Redis dependency would cost more than it buys. The bucket map is swept
    when it grows past ``max_clients`` so a spray of forged source addresses
    cannot grow it without bound.
    """

    #: Buckets idle longer than this are eligible for eviction.
    IDLE_EVICT_S = 300.0

    def __init__(self, *, rate_per_second: float, burst: int, max_clients: int = 4096) -> None:
        self.rate = rate_per_second
        self.burst = float(burst)
        self.max_clients = max_clients
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> tuple[bool, float]:
        """Consume one token. Returns ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > self.max_clients:
                self._evict(now)

            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, updated=now)
                self._buckets[client] = bucket
            else:
                elapsed = now - bucket.updated
                bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
                bucket.updated = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            deficit = 1.0 - bucket.tokens
            return False, max(1.0, deficit / self.rate if self.rate else 1.0)

    def _evict(self, now: float) -> None:
        cutoff = now - self.IDLE_EVICT_S
        stale = [key for key, bucket in self._buckets.items() if bucket.updated < cutoff]
        for key in stale:
            del self._buckets[key]
        if len(self._buckets) > self.max_clients:
            # Still oversized: drop the oldest half rather than refuse service.
            ordered = sorted(self._buckets.items(), key=lambda item: item[1].updated)
            for key, _ in ordered[: len(ordered) // 2]:
                del self._buckets[key]


def client_key(scope: Scope, *, trusted_proxy_hops: int) -> str:
    """Identify the client, honouring ``X-Forwarded-For`` only behind a proxy.

    ``trusted_proxy_hops`` is how many proxies sit in front of this process. A
    client can append anything it likes to the header, so only the entry that
    many hops from the right is trustworthy; with zero hops the header is
    ignored entirely.
    """
    if trusted_proxy_hops > 0:
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        # RFC 7230 lets a repeated field arrive as several lines, equivalent to
        # one comma-joined value. Reading only the first line let a client send
        # its own X-Forwarded-For and have the proxy append a second one, so
        # every request landed in a different bucket and the limiter did nothing.
        raw = b",".join(value for key, value in headers if key == b"x-forwarded-for")
        if raw:
            parts: list[str] = [
                item.strip() for item in raw.decode("latin-1", "replace").split(",") if item.strip()
            ][-MAX_FORWARDED_HOPS:]
            index = len(parts) - trusted_proxy_hops
            if 0 <= index < len(parts):
                return parts[index][:MAX_CLIENT_KEY_LENGTH]
    client = scope.get("client")
    if client:
        return str(client[0])[:MAX_CLIENT_KEY_LENGTH]
    return "unknown"
