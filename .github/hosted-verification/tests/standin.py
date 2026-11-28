"""LOCAL TEST STAND-INS for hv.py: an HTTP/1.1 server on 127.0.0.1 playing the TEST receiver (analytics-ingest) and the
temporary probe (b1-chunk-probe). Never used against staging.

The receiver stand-in follows supabase/functions/_shared/analytics-ingest.js (gearhead-labs main 215b1a0, 5687519b...):
OPTIONS -> 204 (+CORS for the allowed origin); kill switch -> 204 before the body is read; non-POST and foreign origin -> 204;
a per-instance limiter (120/min); a declared Content-Length over 8,192 -> 204 unread ("too large"); otherwise at most 8,192
BYTES are read (the reader stops as soon as the next chunk would pass the limit), decoded once (BOM removed, invalid bytes ->
U+FFFD), parsed, and handed to public.analytics_ingest(p_batch, p_meta) as service_role on a local database. Always 204.
The probe stand-in implements the contract in README.md ("Probe contract"), which the A9 probe must match.
"""
import json
import re
import socket
import threading
import time

BOT_UA = re.compile(r"bot|crawl|spider|slurp|headless|lighthouse|pagespeed|preview|facebookexternalhit|embedly|curl|wget|python|"
                    r"httpclient|axios|node-fetch|go-http|java/|phantom|puppeteer|playwright|selenium|scrapy", re.I)
MAX = 8192


def ua_family(ua):
    if not ua:
        return "Unknown"
    if BOT_UA.search(ua):
        return "Bot"
    for rx, name in ((r"SamsungBrowser/", "Samsung Internet"), (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"),
                     (r"Firefox/|FxiOS/", "Firefox"), (r"CriOS/|Chrome/", "Chrome"), (r"Safari/", "Safari")):
        if re.search(rx, ua):
            return name
    return "Other"


def declared_length(v):
    v = (v or "").strip()
    return float(v) if re.fullmatch(r"[0-9]{1,15}(\.[0-9]{1,15})?", v) else None


class Conn:
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def read_until(self, sep):
        while sep not in self.buf:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        a, _, self.buf = self.buf.partition(sep)
        return a

    def read_some(self):
        if self.buf:
            d, self.buf = self.buf, b""
            return d
        return self.sock.recv(65536)

    def read_exact(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        a, self.buf = self.buf[:n], self.buf[n:]
        return a


def body_chunks(c, headers):
    """Yields the request body as the server receives it (for a chunked body: each chunk's payload)."""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            line = c.read_until(b"\r\n")
            n = int(line.split(b";")[0], 16)
            if n == 0:
                c.read_until(b"\r\n")
                return
            data = c.read_exact(n)
            c.read_exact(2)
            yield data
    elif headers.get("content-length", "").strip().isdigit():
        left = int(headers["content-length"])
        while left > 0:
            d = c.read_some()
            if not d:
                return
            d, rest = d[:left], d[left:]
            c.buf = rest + c.buf
            left -= len(d)
            yield d


def decode(b):
    t = b.decode("utf-8", "replace")
    return t[1:] if t.startswith("﻿") else t


class StandIn:
    def __init__(self, db_url=None, origin="https://test-site.example", receiver=True, probe=True, enabled=True,
                 latency_s=0.0, ingest_connect=None):
        self.db_url, self.origin, self.receiver, self.probe, self.enabled = db_url, origin, receiver, probe, enabled
        self.latency_s = latency_s
        self.requests = []          # (method, path) of every request received
        self.logs = []
        self.lock = threading.Lock()
        self.ingest_connect = ingest_connect
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]
        self.limiter = {}
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(s,), daemon=True).start()

    def _reply(self, s, status, headers=(), body=b""):
        reason = {200: "OK", 204: "No Content", 404: "Not Found"}.get(status, "X")
        h = [f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in headers] + ["Connection: close"]
        if status != 204:
            h.append(f"Content-Length: {len(body)}")
        try:
            s.sendall(("\r\n".join(h) + "\r\n\r\n").encode() + (body if status != 204 else b""))
        except OSError:
            pass

    def _handle(self, s):
        s.settimeout(20)
        c = Conn(s)
        try:
            head = c.read_until(b"\r\n\r\n").decode("latin-1").split("\r\n")
            method, path, _ = head[0].split(" ", 2)
            headers = {}
            for ln in head[1:]:
                k, _, v = ln.partition(":")
                headers[k.strip().lower()] = v.strip()
            with self.lock:
                self.requests.append((method, path))
            if self.latency_s:
                time.sleep(self.latency_s)
            if path == "/functions/v1/analytics-ingest" and self.receiver:
                self._receiver(s, c, method, headers)
            elif path == "/functions/v1/b1-chunk-probe" and self.probe:
                self._probe(s, c, method, headers)
            else:
                self._reply(s, 404, [("Content-Type", "application/json")], b'{"code":"NOT_FOUND"}')
        except (EOFError, OSError, ValueError):
            pass
        finally:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    # ---- the receiver (mirrors makeAnalyticsIngestHandler)
    def _receiver(self, s, c, method, h):
        origin = h.get("origin", "")
        ok_origin = origin == self.origin
        cors = [("Vary", "Origin")] + ([("Access-Control-Allow-Origin", origin), ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                                        ("Access-Control-Allow-Headers", "content-type"), ("Access-Control-Max-Age", "86400")]
                                       if ok_origin else [])
        done = lambda why=None: (why and self.logs.append(why), self._reply(s, 204, cors))  # noqa: E731
        if method == "OPTIONS":
            return done()
        if not self.enabled:
            return done("analytics-ingest: disabled")
        if method != "POST":
            return done("analytics-ingest: method")
        if not ok_origin:
            return done("analytics-ingest: origin refused")
        now = time.time()
        e = self.limiter.get("unknown")
        if not e or now - e[0] >= 60:
            self.limiter["unknown"] = [now, 1]
        else:
            e[1] += 1
            if e[1] > 120:
                return done("analytics-ingest: rate limited (this instance)")
        dl = declared_length(h.get("content-length"))
        if dl is not None and dl > MAX:
            return done("analytics-ingest: too large")
        kept, oversize = b"", False
        for chunk in body_chunks(c, h):
            if len(kept) + len(chunk) > MAX:
                oversize = True
                break
            kept += chunk
        text = None if oversize else decode(kept)
        ua = h.get("user-agent", "")
        try:
            body = None if oversize else json.loads(text)
        except ValueError:
            body = None
        events = body.get("events") if isinstance(body, dict) and isinstance(body.get("events"), list) and len(body["events"]) <= 20 else None
        meta = {"env": "test", "client_env": body.get("env")[:10] if isinstance(body, dict) and isinstance(body.get("env"), str) else None,
                "net": "", "ua_family": ua_family(ua), "bot": bool(BOT_UA.search(ua) or not ua or (isinstance(body, dict) and body.get("wd") is True)),
                "client_errors": 0, "client_dropped": 0}
        conn = self.ingest_connect()
        try:
            conn.execute("SET ROLE service_role")
            conn.execute("SELECT public.analytics_ingest(%s::jsonb, %s::jsonb)", (json.dumps(events), json.dumps(meta)))
        except Exception as ex:  # the real receiver logs the error code only and still answers 204
            self.logs.append("analytics-ingest: database error " + type(ex).__name__)
        finally:
            conn.close()
        if oversize:
            return self._reply(s, 204, cors)      # then the connection closes: the rest of the upload is abandoned
        return done()

    # ---- the probe (contract: README.md "Probe contract")
    def _probe(self, s, c, method, h):
        if method != "POST" or not h.get("x-probe-run", "").startswith("hv-"):
            return self._reply(s, 404, [("Content-Type", "application/json")], b'{"code":"NOT_FOUND"}')
        mode = h.get("x-probe-mode", "")
        t0 = time.monotonic()
        if mode == "chunks":
            sizes, first = [], None
            for ch in body_chunks(c, h):
                if first is None:
                    first = (time.monotonic() - t0) * 1000
                sizes.append(len(ch))
            dl = h.get("content-length")
            out = {"mode": "chunks", "chunks": sizes, "total": sum(sizes),
                   "declared_length": int(dl) if dl and dl.isdigit() else None,
                   "ms_to_first_chunk": None if first is None else round(first, 1)}
        elif mode == "decode":
            kept, oversize = b"", False
            for ch in body_chunks(c, h):
                if len(kept) + len(ch) > MAX:
                    oversize = True
                    break
                kept += ch
            cps = [] if oversize else [ord(x) for x in decode(kept)]
            out = {"mode": "decode", "outcome": "refused" if oversize else "accepted", "oversize": oversize,
                   "bytes": len(kept), "code_points": cps}
        else:
            return self._reply(s, 404, [("Content-Type", "application/json")], b'{"code":"NOT_FOUND"}')
        self._reply(s, 200, [("Content-Type", "application/json")], json.dumps(out).encode())
