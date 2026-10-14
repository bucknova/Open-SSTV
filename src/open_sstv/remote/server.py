# SPDX-License-Identifier: GPL-3.0-or-later
"""Embedded read-only HTTP server for the remote gallery (Phase 1).

Wraps a stdlib :class:`~http.server.ThreadingHTTPServer` in a daemon
thread so it runs alongside the Qt event loop without blocking it — the
whole point of the Phase 1 spike is to prove this coexistence.  The
handler is deliberately tiny:

    GET /                     → the read-only viewer page (no token needed
                                to load the shell; it needs the token to
                                call the APIs below)
    GET /api/gallery          → JSON list of images
    GET /api/thumb/<id>       → cached thumbnail PNG
    GET /api/image/<id>       → original image bytes

Security posture for this phase (a LAN dev-token surface, no TX):

- **Bearer/query token on every ``/api`` call**, compared with
  :func:`hmac.compare_digest`.  A random token is generated if the caller
  supplies none, so the server is never unprotected.
- **Images are addressed by opaque id only** — the id→path mapping lives
  in :class:`~open_sstv.remote.service.GalleryService` and only ever
  resolves to files inside the configured gallery directories, so no
  client-supplied path ever reaches the filesystem.
- **Read-only.**  Only ``GET`` is handled; there is no route that writes,
  deletes, or transmits.

Phase 2 adds the live view plane on the same stdlib server: ``GET
/api/events`` is a **Server-Sent Events** stream fed by
:class:`~open_sstv.remote.events.EventHub`, and ``GET /api/rx/preview``
serves the current in-progress decode.  The view plane is one-directional
(app→browser), so SSE fits without an async framework or WebSockets —
stdlib only, still no new dependency.
"""
from __future__ import annotations

import hmac
import io
import ipaddress
import json
import logging
import math
import mimetypes
import queue
import re
import secrets
import socket
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from open_sstv.remote.compose import ComposeService
from open_sstv.remote.control import ControlPlane, Result
from open_sstv.remote.events import EventHub
from open_sstv.remote.page import render_page

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from open_sstv.remote.service import GalleryService

#: Seconds a stalled SSE reader waits before re-checking the stop flag.
#: Short so :meth:`RemoteServer.stop` returns promptly; the wait is idle
#: (blocked on a queue) so a low value costs nothing.
_SSE_POLL_S = 1.0
#: Comment-line keep-alive cadence — proves the connection to the client
#: (and surfaces a dead socket to us) between real events.
_SSE_PING_S = 15.0
#: Ceiling on simultaneous SSE streams (each parks a request thread).
#: A home station has a handful of viewers; this bounds thread/memory use
#: if a client reconnect-storms or many tabs pile up.
_MAX_SSE_CLIENTS = 16
#: How often the control-plane safety sweep runs.  Must be well under
#: ``control.HEARTBEAT_TIMEOUT_S`` (2.5 s) so the dead-man's-switch fires
#: promptly after a lost heartbeat.
_TICK_INTERVAL_S = 0.5
#: Control-plane POST bodies are tiny JSON; reject anything larger.
_MAX_BODY_BYTES = 64 * 1024
#: Compose uploads carry a base64 photo — allow more (base64 inflates the
#: 12 MB photo cap by ~⅓).
_MAX_UPLOAD_BYTES = 18 * 1024 * 1024
#: Map a ControlPlane ``Result.error`` code to an HTTP status.
_ERR_STATUS = {
    "busy": HTTPStatus.CONFLICT,
    "not_lease_holder": HTTPStatus.FORBIDDEN,
    "tx_disabled": HTTPStatus.FORBIDDEN,
    "no_rig": HTTPStatus.CONFLICT,
    "bad_token": HTTPStatus.BAD_REQUEST,
    "confirm_expired": HTTPStatus.CONFLICT,
    "cooldown": HTTPStatus.TOO_MANY_REQUESTS,
}

#: Shortest operator-chosen access token the server will use (v0.6.14 audit
#: S-M2).  The token is the only thing between a LAN host and the rig's PTT,
#: so a memorable "demo"/"1234" is brute-forceable.  16 characters of even
#: lowercase letters is ~75 bits; the generated default is 128 bits.
MIN_TOKEN_LEN = 16

#: Overall deadline for a client to deliver its request line + headers
#: (v0.6.14 audit S-M4).  ``timeout`` on the handler is per-recv, so a
#: client trickling one header byte every few seconds held a request thread
#: forever without ever authenticating.  This bounds the whole header phase
#: (and the idle gap before the next request on a keep-alive connection).
_HEADER_DEADLINE_S = 10.0
#: Once authenticated, a POST body gets the header deadline plus time at
#: this minimum rate — generous for an 18 MB photo over a slow phone link,
#: but still finite.  Only reached *after* the token check.
_BODY_MIN_RATE_BPS = 32 * 1024
#: How often the reaper checks request deadlines.
_REAPER_INTERVAL_S = 0.25
#: Ceiling on simultaneously open connections (v0.6.14 audit S-M4).  Each
#: one is a thread and a file descriptor in the *app* process — exhausting
#: FDs would break audio-device and serial-port opens mid-session.  Extra
#: connections are closed at accept, before a byte is read.
_MAX_CONNECTIONS = 32
#: Per-IP ceiling on SSE streams (v0.6.14 audit S-L5), so one client can't
#: take every ``_MAX_SSE_CLIENTS`` slot and lock other viewers out.
_MAX_SSE_PER_IP = 4
#: Concurrent compose renders (v0.6.14 audit S-L3).  Each decodes up to a
#: 32 MP photo; several at once can OOM the app mid-transmission.
_MAX_CONCURRENT_COMPOSE = 2

#: Per-IP auth backoff (v0.6.14 audit S-M2).  A client gets this many bad
#: tokens for free; each further failure locks it out for an exponentially
#: growing time (2 s, 4 s, 8 s …), capped.  The record is forgotten once the
#: client has stayed clean for ``_AUTH_WINDOW_S`` after its lockout ends.
_AUTH_FREE_FAILURES = 5
_AUTH_WINDOW_S = 60.0
_AUTH_MAX_LOCKOUT_S = 300.0
#: Bound on tracked addresses so a spray of source IPs can't grow memory.
_AUTH_MAX_TRACKED = 1024

#: Neutral ``Server:`` banner — don't advertise the Python version.
_SERVER_BANNER = "Open-SSTV"

#: ``token=…`` in a logged request line (v0.6.14 audit S-M1).
_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"]*", re.IGNORECASE)


def token_chars_ok(token: str) -> bool:
    """True if *token* is printable ASCII (space through ``~``) only.

    v0.6.14 sweep: the page sends the token in an ``Authorization: Bearer``
    header, and ``fetch()`` throws on any header character above U+00FF —
    so a non-ASCII token could never authenticate from the browser.
    Control characters are refused for the same reason.
    """
    return all(" " <= c <= "~" for c in token)


def token_is_strong(token: str) -> bool:
    """True if an operator-chosen *token* is usable: long enough, and
    printable ASCII (v0.6.14 sweep) so a browser can send it."""
    return len(token) >= MIN_TOKEN_LEN and token_chars_ok(token)


def _redact(text: str) -> str:
    """Mask any ``token=`` query value in *text*.

    v0.6.14 audit S-M1: the access log line is the request line, which the
    page's ``<img>``/``EventSource`` URLs still carry the token in — and
    log files ride along in diagnostics exports.
    """
    return _TOKEN_IN_URL.sub(r"\1<redacted>", text)


def _local_hostnames() -> frozenset[str]:
    """Names this machine answers to, for Host-header validation.

    ``localhost`` plus the OS hostname, its first label, and the mDNS
    ``<name>.local`` form — what a phone on the LAN would type.  Deliberately
    no DNS lookups (``getfqdn`` can stall for seconds on macOS).
    """
    names = {"localhost"}
    try:
        hostname = socket.gethostname().strip().lower().rstrip(".")
    except OSError:  # pragma: no cover - platform oddity
        hostname = ""
    if hostname:
        base = hostname.split(".")[0]
        names.update({hostname, base, f"{base}.local"})
    return frozenset(names)


def _host_allowed(host_header: str | None, names: frozenset[str]) -> bool:
    """Validate a ``Host:`` header against IP literals + known *names*.

    v0.6.14 audit S-L1: a DNS-rebinding page points the attacker's own
    hostname at this server, so the browser sends ``Host: evil.example``.
    Any IP literal (v4, or bracketed v6, with an optional port) is fine —
    a browser only sends one when the user typed it — as are ``localhost``
    and this machine's own names.  Everything else is refused.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        rest = host[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        literal = host[1:end].split("%", 1)[0]  # drop an IPv6 zone id
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return False
        return True
    if host.count(":") == 1:
        host, _, port = host.partition(":")
        if not port.isdigit():
            return False
    elif host.count(":") > 1:
        return False  # an IPv6 literal must be bracketed in a Host header
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host in names
    return True


class _AuthThrottle:
    """Per-IP failed-auth backoff (v0.6.14 audit S-M2).

    Non-blocking by design: a locked-out client is answered ``429`` at
    once — nothing sleeps, so other clients' request threads are never
    held up.  Memory is bounded by ``max_tracked`` (oldest record evicted).
    IPv6 clients are keyed by their /64, since one host can rotate through
    a whole /64 of addresses.
    """

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        *,
        free_failures: int = _AUTH_FREE_FAILURES,
        window_s: float = _AUTH_WINDOW_S,
        max_lockout_s: float = _AUTH_MAX_LOCKOUT_S,
        max_tracked: int = _AUTH_MAX_TRACKED,
    ) -> None:
        self._now = now
        self._free = free_failures
        self._window = window_s
        self._max_lockout = max_lockout_s
        self._max_tracked = max_tracked
        self._lock = threading.Lock()
        #: key → [failure count, locked-until (monotonic)]
        self._records: OrderedDict[str, list[float]] = OrderedDict()

    @staticmethod
    def _key(ip: str) -> str:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if isinstance(addr, ipaddress.IPv6Address):
            if addr.ipv4_mapped is not None:
                return str(addr.ipv4_mapped)
            return str(ipaddress.IPv6Network((addr, 64), strict=False))
        return str(addr)

    def _live(self, key: str, t: float) -> list[float] | None:
        rec = self._records.get(key)
        if rec is not None and t > rec[1] + self._window:
            # Clean for a full window after the last lockout: forget it.
            del self._records[key]
            return None
        return rec

    def locked_for(self, ip: str) -> float:
        """Seconds *ip* must still wait, or 0.0 if it may try now."""
        with self._lock:
            t = self._now()
            rec = self._live(self._key(ip), t)
            return max(0.0, rec[1] - t) if rec is not None else 0.0

    def failure(self, ip: str) -> None:
        """Record a bad token from *ip*; may start or extend a lockout."""
        key = self._key(ip)
        with self._lock:
            t = self._now()
            rec = self._live(key, t)
            if rec is None:
                rec = [0.0, t]
                self._records[key] = rec
            self._records.move_to_end(key)
            rec[0] += 1
            over = int(rec[0]) - self._free
            lockout = min(2.0 ** over, self._max_lockout) if over > 0 else 0.0
            # rec[1] doubles as "last activity", so the forget-window
            # starts when the lockout ends.  Never shorten a lockout.
            rec[1] = max(rec[1], t + lockout)
            while len(self._records) > self._max_tracked:
                self._records.popitem(last=False)

    def success(self, ip: str) -> None:
        """A good token from *ip* clears its record."""
        with self._lock:
            self._records.pop(self._key(ip), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class _BoundedHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` with a connection cap and request deadlines.

    v0.6.14 audit S-M4.  The stock server spawns a thread per connection
    with no ceiling, and its per-recv timeout lets a slow client stretch a
    request out indefinitely.  Here:

    - a non-blocking semaphore caps open connections; over the cap the
      socket is closed at accept, before a byte is read;
    - each connection registers an absolute deadline for the phase it is
      in (headers, then an authenticated body), and a reaper thread shuts
      down any socket that overruns it.  Writing a response (SSE, a big
      image) has no deadline — the per-send socket timeout covers that.
    """

    daemon_threads = True  # request threads never block shutdown

    def __init__(
        self,
        addr: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        max_connections: int,
    ) -> None:
        super().__init__(addr, handler)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._deadlines: dict[socket.socket, float] = {}
        self._deadline_lock = threading.Lock()

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        if not self._slots.acquire(blocking=False):
            _log.debug("remote: connection cap reached, refusing %s", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.set_deadline(request, None)
            self._slots.release()

    def handle_error(self, request, client_address):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        # A reaped or vanished client surfaces here as a broken pipe — that
        # is routine, not worth the stock traceback on stderr.
        _log.debug("remote: connection error from %s", client_address[0], exc_info=True)

    def set_deadline(self, sock: socket.socket, deadline: float | None) -> None:
        with self._deadline_lock:
            if deadline is None:
                self._deadlines.pop(sock, None)
            else:
                self._deadlines[sock] = deadline

    def reap(self, now: float) -> int:
        """Shut down every connection past its deadline; return how many."""
        with self._deadline_lock:
            late = [s for s, d in self._deadlines.items() if now >= d]
            for s in late:
                del self._deadlines[s]
        for s in late:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already gone
        if late:
            _log.debug("remote: dropped %d connection(s) past the request deadline", len(late))
        return len(late)
#: Content Security Policy.  The page is inline-everything, so script/style
#: need ``'unsafe-inline'`` — but ``default-src 'none'`` + ``connect-src
#: 'self'`` mean that even if markup ever slips past escaping, it can't
#: phone the token home to another origin or pull external resources.
_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'"
)

_log = logging.getLogger(__name__)


def _is_known_mode(mode: str) -> bool:
    """True if *mode* is a valid ``core.modes.Mode`` value.

    Lazy import so this module doesn't pull the DSP stack for callers that
    never transmit; if it can't import, don't block here — the transmit
    path validates again downstream.
    """
    try:
        from open_sstv.core.modes import Mode  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True
    return mode in {m.value for m in Mode}


def normalise_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """Canonical form for extra Host-header names (lower-case, no trailing dot)."""
    return frozenset(h.strip().lower().rstrip(".") for h in hosts if h.strip())


class RemoteServer:
    """Owns the embedded HTTP server's lifecycle.

    Construct, :meth:`start`, and later :meth:`stop`.  ``start`` binds the
    socket (raising :class:`OSError` on e.g. a port already in use, for
    the caller to surface) and then serves on a background daemon thread.
    """

    def __init__(
        self,
        service: GalleryService,
        host: str = "127.0.0.1",
        port: int = 8730,
        token: str = "",
        hub: EventHub | None = None,
        control: ControlPlane | None = None,
        tx_enabled: Callable[[], bool] | None = None,
        compose: ComposeService | None = None,
        extra_hosts: Iterable[str] = (),
    ) -> None:
        self._service = service
        #: Server-side compositor (Phase 4).  Shares the gallery service's
        #: live config so it renders with the operator's callsign/grid.
        self._compose = compose if compose is not None else ComposeService(
            service.config_getter
        )
        self._host = host
        self._port = port
        #: Remote-TX control plane (Phase 3).  Defaults to one whose enable
        #: gate is ``tx_enabled`` (or hard-off) with **stub** transmit/unkey
        #: callbacks that only log — the server never touches the rig by
        #: itself.  The app (3c) passes a ControlPlane with real callbacks.
        #: A dedicated tick thread drives the dead-man's-switch, independent
        #: of any GUI event loop.
        self._tx_enabled = tx_enabled or (lambda: False)
        self._control = control if control is not None else ControlPlane(
            now=time.monotonic,
            transmit=self._stub_transmit,
            unkey=self._stub_unkey,
            enabled=self._tx_enabled,
        )
        self._tick_thread: threading.Thread | None = None
        self._reaper_thread: threading.Thread | None = None
        #: Never run unprotected: mint a token if the caller gave none.
        #: v0.6.14 audit S-M2: a too-short fixed token isn't used either.
        #: Refusing to start would silently break existing installs, so
        #: mint a strong one for this session; the app warns the operator
        #: via :attr:`token_rejected`.  The token itself is never logged.
        self._token_rejected = bool(token) and not token_is_strong(token)
        if self._token_rejected:
            _log.warning(
                "remote: configured access token is shorter than %d characters "
                "or not printable ASCII; using a random token for this session",
                MIN_TOKEN_LEN,
            )
        self._token = (
            token if token and not self._token_rejected else secrets.token_urlsafe(16)
        )
        #: Host-header names accepted on top of IP literals and this
        #: machine's own names (S-L1) — e.g. a tailnet DNS name.
        self._extra_hosts = normalise_hosts(extra_hosts)
        #: Per-IP failed-auth backoff (S-M2).  Lives on the server object so
        #: it survives across connections; reset on restart.
        self._auth_throttle = _AuthThrottle()
        #: Live event fan-out for the SSE stream.  The app publishes RX
        #: progress / gallery updates here; ``/api/events`` drains it.
        self._hub = hub if hub is not None else EventHub()
        #: Set on stop() so in-flight SSE readers exit promptly.
        self._stopping = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- default (no-rig) control callbacks ----------------------------
    # Used only when no ControlPlane is injected (Phase 3b / standalone /
    # tests).  They NEVER touch a rig — the server cannot transmit by
    # itself; the app supplies real callbacks in 3c.

    def _stub_transmit(self, image_id: str, mode: str) -> None:
        _log.info("remote TX (stub — no rig): would transmit %s in %s", image_id, mode)

    def _stub_unkey(self, reason: str) -> None:
        _log.info("remote TX (stub — no rig): would unkey (%s)", reason)

    @property
    def control(self) -> ControlPlane:
        return self._control

    def _publish_tx_state(self) -> None:
        """Fan the current control-plane state out to SSE viewers."""
        self._hub.publish({"type": "tx.state", **self._control.status()})

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Bind the socket and start serving on a daemon thread.

        Idempotent-ish: a second call while running is a no-op.  Binding
        errors propagate so the caller can log and carry on without the
        server (never blocking app launch).
        """
        if self._httpd is not None:
            return
        self._stopping.clear()
        service, token = self._service, self._token
        hub, stopping, control = self._hub, self._stopping, self._control
        compose = self._compose
        throttle = self._auth_throttle
        host_names = _local_hostnames() | self._extra_hosts
        #: S-L3: bound concurrent photo decodes/renders.
        compose_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_COMPOSE)
        #: S-L5: live SSE streams per client IP.
        sse_per_ip: dict[str, int] = {}
        sse_lock = threading.Lock()

        class _Handler(BaseHTTPRequestHandler):
            server: _BoundedHTTPServer  # narrowed for the deadline helpers
            # Neutral banner (v0.6.14 audit, Info): stock is
            # "BaseHTTP/0.6 Python/3.x.y".
            server_version = _SERVER_BANNER
            sys_version = ""

            def version_string(self) -> str:  # noqa: D102
                return _SERVER_BANNER

            #: Drop a connection that stalls part-way through its request.
            #: BaseHTTPRequestHandler leaves this at None, so a client that
            #: sent half a header line and then stopped held a request
            #: thread forever — and did so *before* auth runs, so it needed
            #: no token.  A few hundred such connections exhaust the thread
            #: pool and the file-descriptor limit, blocking the accept loop
            #: for the operator's own phone.  SSE responses are unaffected:
            #: this bounds the read of the request, not the life of a
            #: response we are writing.
            timeout = 15.0

            def handle_one_request(self) -> None:  # noqa: D102
                # v0.6.14 audit S-M4: ``timeout`` is per-recv, so a client
                # trickling a byte every few seconds never tripped it.  Arm
                # an absolute deadline for the request line + headers; the
                # server's reaper shuts the socket if it passes.  Cleared
                # in parse_request once the headers are in.
                self.server.set_deadline(
                    self.connection, time.monotonic() + _HEADER_DEADLINE_S
                )
                try:
                    super().handle_one_request()
                except TimeoutError:
                    # Stalled client: close quietly rather than logging a
                    # traceback per abandoned connection.
                    self.close_connection = True
                finally:
                    self.server.set_deadline(self.connection, None)

            def parse_request(self) -> bool:  # noqa: D102
                ok = super().parse_request()
                if ok:
                    # Headers are in: responding (incl. SSE) has no deadline.
                    self.server.set_deadline(self.connection, None)
                return ok

            # Route stdlib access logs through our logger at DEBUG rather
            # than spamming stderr with one line per request.  v0.6.14
            # audit S-M1: the request line carries ``?token=`` for <img> and
            # EventSource URLs, so redact it — logs go into diagnostics zips.
            def log_message(self, fmt: str, *args: object) -> None:
                _log.debug("remote %s - %s", self.address_string(), _redact(fmt % args))

            def _precheck(self) -> bool:
                """Host-header validation (v0.6.14 audit S-L1), before routing.

                Defeats DNS rebinding: the attacker's page can reach us by
                IP only via its *own* hostname, which isn't on the list.
                """
                if _host_allowed(self.headers.get("Host"), host_names):
                    return True
                self.close_connection = True
                self._json(HTTPStatus.MISDIRECTED_REQUEST, {"error": "unknown host"})
                return False

            def _require_auth(self, query: dict[str, list[str]]) -> bool:
                """Token check with per-IP backoff (v0.6.14 audit S-M2).

                Answers 429/401 itself and returns False on refusal.  A
                locked-out client is refused without the token even being
                compared, so guesses made during a lockout are worthless.
                """
                ip = self.client_address[0]
                wait = throttle.locked_for(ip)
                if wait > 0:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "too many failed attempts"},
                        headers={"Retry-After": str(math.ceil(wait))},
                    )
                    return False
                supplied = self._supplied_token(query)
                if not supplied:
                    # v0.6.14 sweep: no token at all isn't a guess — it's a
                    # bookmark / reload without the ?token= link.  Counting
                    # it let an operator lock themselves out by reloading.
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing token"})
                    return False
                if self._authed(supplied):
                    throttle.success(ip)
                    return True
                throttle.failure(ip)
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid or missing token"})
                return False

            def _supplied_token(self, query: dict[str, list[str]]) -> str:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    return auth[7:]
                if "token" in query:
                    return query["token"][0]
                return ""

            def _authed(self, supplied: str) -> bool:
                # Compare as bytes.  hmac.compare_digest refuses to compare
                # str containing any non-ASCII character and raises
                # TypeError — and self.path/headers are latin-1 decoded, so
                # any byte >= 0x80 in the token produced an unhandled
                # exception and a dropped connection instead of a clean 401.
                return hmac.compare_digest(
                    supplied.encode("utf-8", "surrogateescape"),
                    token.encode("utf-8", "surrogateescape"),
                )

            def _send(
                self, status: HTTPStatus, body: bytes, ctype: str,
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # Belt-and-braces: this surface never wants to be framed
                # or sniffed, and it must not be cached with its token.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", _CSP)
                self.send_header("Cache-Control", "no-store")
                # v0.6.14 audit S-L4: never leak a token-bearing URL to
                # anything this page links to or loads.
                self.send_header("Referrer-Policy", "no-referrer")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(
                self, status: HTTPStatus, obj: object,
                headers: dict[str, str] | None = None,
            ) -> None:
                self._send(
                    status, json.dumps(obj).encode("utf-8"), "application/json", headers
                )

            def _file(self, path_str: str, ctype: str) -> None:
                # ``path_str`` came from GalleryService.resolve — it is a
                # known gallery file, not client input.
                try:
                    with open(path_str, "rb") as fh:  # noqa: PTH123 — bytes read
                        data = fh.read()
                except OSError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._send(HTTPStatus.OK, data, ctype)

            def do_GET(self) -> None:  # noqa: N802 — stdlib API
                if not self._precheck():
                    return
                parts = urlsplit(self.path)
                path = parts.path
                query = parse_qs(parts.query)

                if path in ("/", "/index.html"):
                    self._send(
                        HTTPStatus.OK,
                        render_page().encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return

                if not path.startswith("/api/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return

                if not self._require_auth(query):
                    return

                if path == "/api/gallery":
                    self._json(HTTPStatus.OK, service.payload())
                    return

                if path == "/api/logbook":
                    self._json(HTTPStatus.OK, service.logbook_payload())
                    return

                if path == "/api/compose/templates":
                    self._json(HTTPStatus.OK, compose.list_templates())
                    return

                if path == "/api/events":
                    self._sse()
                    return

                if path == "/api/rx/preview":
                    png = service.live_frame()
                    if png is None:
                        self._send(HTTPStatus.NO_CONTENT, b"", "image/png")
                        return
                    self._send(HTTPStatus.OK, png, "image/png")
                    return

                if path.startswith("/api/thumb/"):
                    thumb = service.thumbnail_path(path[len("/api/thumb/"):])
                    if thumb is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    self._file(str(thumb), "image/png")
                    return

                if path.startswith("/api/image/"):
                    img = service.image_path(path[len("/api/image/"):])
                    if img is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    ctype = mimetypes.guess_type(img.name)[0] or "application/octet-stream"
                    self._file(str(img), ctype)
                    return

                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            # -- control plane (POST) ---------------------------------
            # State-changing requests: token + same-origin + JSON body with
            # a per-browser ``client`` id.  Every route maps a ControlPlane
            # Result to an HTTP status and fans the new state out over SSE.

            def _origin_ok(self) -> bool:
                """Reject cross-origin POSTs (CSRF).  A browser always sends
                Origin on a POST; a same-origin one matches Host (which
                _precheck has already validated).

                v0.6.14 audit S-L1: a POST with *no* Origin must carry the
                token as a Bearer header.  A browser can't attach that
                header cross-origin without a CORS preflight (which this
                server never answers), so the CSRF defence no longer rests
                on the token alone.  Non-browser clients send Bearer."""
                origin = self.headers.get("Origin")
                if origin is None:
                    return self.headers.get("Authorization", "").startswith("Bearer ")
                netloc = origin.split("://", 1)[-1] if "://" in origin else ""
                return netloc == self.headers.get("Host", "")

            def _read_json(
                self, max_bytes: int = _MAX_BODY_BYTES
            ) -> dict[str, object] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length <= 0 or length > max_bytes:
                    return None
                # S-M4: the body (only ever read after auth) gets a finite
                # deadline too, scaled to its declared size.
                self.server.set_deadline(
                    self.connection,
                    time.monotonic() + _HEADER_DEADLINE_S + length / _BODY_MIN_RATE_BPS,
                )
                try:
                    obj = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    return None
                finally:
                    self.server.set_deadline(self.connection, None)
                return obj if isinstance(obj, dict) else None

            def _compose_slot(self) -> bool:
                """Take a compose-render slot or answer 503 (S-L3).

                Checked *before* the up-to-18 MB body is read, so a burst of
                uploads costs nothing.  The unread body poisons keep-alive,
                hence close_connection.
                """
                if compose_slots.acquire(blocking=False):
                    return True
                self.close_connection = True
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "busy rendering — try again"},
                    headers={"Retry-After": "2"},
                )
                return False

            def _do_compose(self) -> None:
                """POST /api/compose/render — render photo+tokens+template to
                a PNG preview.  Composing never touches the rig; putting the
                result on air still goes through the control plane."""
                import base64  # noqa: PLC0415

                body = self._read_json(_MAX_UPLOAD_BYTES)
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                photo_b64 = body.get("photo")
                template_id = body.get("template_id")
                mode = body.get("mode")
                tokens = body.get("tokens")
                if not isinstance(photo_b64, str) or not isinstance(template_id, str) \
                        or not isinstance(mode, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing photo/template_id/mode"})
                    return
                try:
                    photo = base64.b64decode(photo_b64, validate=True)
                except ValueError:  # binascii.Error subclasses ValueError
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad base64 photo"})
                    return
                tok = {k: str(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}
                img = compose.render(photo, template_id, tok, mode)
                if img is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "could not render"})
                    return
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                self._send(HTTPStatus.OK, buf.getvalue(), "image/png")

            def _do_stage(self) -> None:
                """POST /api/compose/stage — render + hold the image in memory
                for transmit, returning a staging id.  Gated on remote TX
                being enabled (staging is only useful to then transmit)."""
                import base64  # noqa: PLC0415

                if not control.status().get("tx_enabled"):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "tx_disabled"})
                    return
                body = self._read_json(_MAX_UPLOAD_BYTES)
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                photo_b64 = body.get("photo")
                template_id = body.get("template_id")
                mode = body.get("mode")
                tokens = body.get("tokens")
                if not isinstance(photo_b64, str) or not isinstance(template_id, str) \
                        or not isinstance(mode, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing photo/template_id/mode"})
                    return
                try:
                    photo = base64.b64decode(photo_b64, validate=True)
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "bad base64 photo"})
                    return
                tok = {k: str(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}
                staging_id = compose.stage(photo, template_id, tok, mode)
                if staging_id is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "could not render"})
                    return
                self._json(HTTPStatus.OK, {"image_id": staging_id})

            def _tx_result(self, result: Result, extra: dict[str, object] | None = None) -> None:
                # Fan the new control state to viewers, then answer the caller.
                hub.publish({"type": "tx.state", **control.status()})
                if result.ok:
                    body: dict[str, object] = {"ok": True, **control.status()}
                    if extra:
                        body.update(extra)
                    self._json(HTTPStatus.OK, body)
                else:
                    status = _ERR_STATUS.get(result.error or "", HTTPStatus.BAD_REQUEST)
                    self._json(
                        status, {"ok": False, "error": result.error, **control.status()}
                    )

            def do_POST(self) -> None:  # noqa: N802 — stdlib API
                if not self._precheck():
                    return
                parts = urlsplit(self.path)
                path, query = parts.path, parse_qs(parts.query)
                if path not in ("/api/compose/render", "/api/compose/stage") \
                        and not path.startswith("/api/tx/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._require_auth(query):
                    return
                if not self._origin_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin refused"})
                    return
                if path in ("/api/compose/render", "/api/compose/stage"):
                    if not self._compose_slot():
                        return
                    try:
                        if path == "/api/compose/render":
                            self._do_compose()
                        else:
                            self._do_stage()
                    finally:
                        compose_slots.release()
                    return
                body = self._read_json()
                if body is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid or missing body"})
                    return
                client = body.get("client")
                if not isinstance(client, str) or not client:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing client id"})
                    return

                if path == "/api/tx/lease":
                    self._tx_result(control.take_lease(client))
                elif path == "/api/tx/release":
                    self._tx_result(control.release_lease(client))
                elif path == "/api/tx/heartbeat":
                    self._tx_result(control.heartbeat(client))
                elif path == "/api/tx/abort":
                    self._tx_result(control.abort(client))
                elif path == "/api/tx/confirm":
                    tok = body.get("token")
                    if not isinstance(tok, str):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "missing token"})
                        return
                    self._tx_result(control.confirm(client, tok))
                elif path == "/api/tx/request":
                    image_id, mode = body.get("image_id"), body.get("mode")
                    if not isinstance(image_id, str) or not isinstance(mode, str):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "missing image_id/mode"})
                        return
                    if not _is_known_mode(mode):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "unknown mode"})
                        return
                    # The image must exist: a staged compose (in memory) or a
                    # gallery file (on disk).
                    known = (
                        compose.staged_image(image_id) is not None
                        if compose.is_staged_id(image_id)
                        else service.image_path(image_id) is not None
                    )
                    if not known:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown image"})
                        return
                    result = control.request(client, image_id, mode)
                    self._tx_result(result, {"token": result.token} if result.token else None)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def _sse(self) -> None:
                """Per-IP gate around :meth:`_sse_stream` (v0.6.14 audit S-L5).

                Without it one client could hold every global SSE slot and
                lock other viewers out.  Entries are dropped at zero, so the
                map never outgrows the connection cap.
                """
                ip = self.client_address[0]
                with sse_lock:
                    if sse_per_ip.get(ip, 0) >= _MAX_SSE_PER_IP:
                        refused = True
                    else:
                        refused = False
                        sse_per_ip[ip] = sse_per_ip.get(ip, 0) + 1
                if refused:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many streams"}
                    )
                    return
                try:
                    self._sse_stream()
                finally:
                    with sse_lock:
                        left = sse_per_ip.get(ip, 1) - 1
                        if left > 0:
                            sse_per_ip[ip] = left
                        else:
                            sse_per_ip.pop(ip, None)

            def _sse_stream(self) -> None:
                """Stream live events to the client until it (or we) hang up."""
                # Bound concurrent streams so a reconnect-storm or a pile of
                # tabs can't exhaust threads/memory on the host.
                q = hub.subscribe(max_subscribers=_MAX_SSE_CLIENTS)
                if q is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "too many clients"})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", _CSP)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Accel-Buffering", "no")  # defeat proxy buffering
                self.end_headers()
                last_ping = time.monotonic()
                try:
                    # A first comment flushes headers so the browser's
                    # EventSource fires `onopen` immediately; follow it with
                    # the current TX state so the control UI initialises
                    # without waiting for the next state change.
                    self.wfile.write(b": connected\n\n")
                    init = json.dumps({"type": "tx.state", **control.status()})
                    self.wfile.write(b"data: " + init.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    while not stopping.is_set():
                        try:
                            event = q.get(timeout=_SSE_POLL_S)
                        except queue.Empty:
                            if time.monotonic() - last_ping >= _SSE_PING_S:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                last_ping = time.monotonic()
                            continue
                        payload = json.dumps(event).encode("utf-8")
                        self.wfile.write(b"data: " + payload + b"\n\n")
                        self.wfile.flush()
                        last_ping = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client went away — normal
                finally:
                    hub.unsubscribe(q)

        # v0.6.14 audit S-L2: with "LAN access" on, the host is 0.0.0.0 and
        # that is deliberate.  Binding only the detected LAN IP would cut off
        # operators who reach the station over a VPN / tailnet interface (a
        # common remote-station setup), and the detected IP goes stale when
        # DHCP or Wi-Fi changes it.  The exposure 0.0.0.0 adds is covered
        # by Host-header validation (rebinding), the token + per-IP backoff,
        # and the connection cap.
        httpd = _BoundedHTTPServer((self._host, self._port), _Handler, _MAX_CONNECTIONS)
        self._httpd = httpd
        # Reflect the actually-bound port (matters when port==0 was passed).
        self._port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            name="open-sstv-remote",
            daemon=True,
        )
        self._thread.start()
        # Drive the control-plane safety sweep (dead-man's-switch, token
        # expiry, lease lapse) on a dedicated thread — deliberately NOT the
        # GUI loop, so a stalled UI can't stop the watchdog from unkeying.
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="open-sstv-remote-tick", daemon=True
        )
        self._tick_thread.start()
        # S-M4: enforce request deadlines on a thread of its own, so a slow
        # control-plane callback can never delay a reap (or vice versa).
        self._reaper_thread = threading.Thread(
            target=self._reap_loop, args=(httpd,), name="open-sstv-remote-reaper",
            daemon=True,
        )
        self._reaper_thread.start()
        # NB: log the bind, never ``self.url`` — the URL embeds the token,
        # and the log file rides along in diagnostics exports.
        _log.info("remote gallery server listening on %s:%d", self._host, self._port)

    def _tick_loop(self) -> None:
        while not self._stopping.wait(_TICK_INTERVAL_S):
            try:
                self._control.tick()
            except Exception:  # noqa: BLE001 — the watchdog must never die
                _log.exception("remote control tick failed")

    def _reap_loop(self, httpd: _BoundedHTTPServer) -> None:
        while not self._stopping.wait(_REAPER_INTERVAL_S):
            try:
                httpd.reap(time.monotonic())
            except Exception:  # noqa: BLE001 — the reaper must never die
                _log.exception("remote deadline reaper failed")

    def stop(self) -> None:
        """Stop serving and release the socket.  Safe to call when not running."""
        httpd, thread, ticker = self._httpd, self._thread, self._tick_thread
        reaper = self._reaper_thread
        self._httpd = self._thread = self._tick_thread = self._reaper_thread = None
        # Signal SSE readers + the tick loop first so they unwind promptly.
        self._stopping.set()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        if ticker is not None:
            ticker.join(timeout=2.0)
        if reaper is not None:
            reaper.join(timeout=2.0)

    # -- introspection -------------------------------------------------

    @property
    def hub(self) -> EventHub:
        """The event fan-out the app publishes live RX events onto."""
        return self._hub

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """The bound port (resolved after :meth:`start` when port 0 was used)."""
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def extra_hosts(self) -> frozenset[str]:
        return self._extra_hosts

    @property
    def token_rejected(self) -> bool:
        """True if the configured fixed token was too short and a random
        one is in use instead (v0.6.14 audit S-M2) — the app warns."""
        return self._token_rejected

    @property
    def url(self) -> str:
        """The full URL an operator opens, token included."""
        return f"http://{self._host}:{self._port}/?token={self._token}"


__all__ = ["MIN_TOKEN_LEN", "RemoteServer", "token_is_strong"]
