#!/usr/bin/env python3
"""Gearhead Labs STAGING - hosted verification runner (A4 of the hosted TEST verification procedure, revision 10).

Runs exactly ONE test suite against the staging (TEST) project and writes one evidence file plus a raw client log.
It is started only by .github/workflows/hosted-verification.yml, in the job that waits for the staging-db approval.

  Suites:  P1-P2  read-only database checks (sequence, version, isolation, role/database overrides, cron jobs)
           D1     probe, chunks mode: how the hosted runtime delivers request bodies
           D3     probe, decode mode: UTF-8 decoding of the B2-7 / B2-11 byte sequences
           D4     real receiver: the 8,192-byte boundary (B8192, B8193, BLIE, B9000), one evidence window each
           D2     real receiver: 10 MB oversize request O (chunked, slow) and 4,096-byte control C
           P3     two database sessions: rollup holds the analytics lock 1.5 s while L1 arrives through the receiver
           P4     about 20 minutes of paced ingestion (<= 250 events, repeats included) alongside the scheduled rollup

What it never does: insert into an analytics table directly, use a service-role key, read or print a secret,
record request bodies, IP addresses or connection strings, contact any host other than
https://<TEST_REF>.supabase.co and the staging database, or run more than the one selected suite.
Database reads run in READ ONLY transactions. The only database writes are the ones Revision 10 specifies:
analytics_rollup(today) and analytics_rollup_pending() (P3, P4). Every event goes through the HTTP receiver.

Results use Revision 10's own words and categories; nothing here turns an "inconclusive" or "not determined" into a pass.
Exit status: 0 = the suite's verdict is PASS; 2 = the suite ran and its verdict is anything else (read the evidence);
1 = refused or failed before or during the run (a safeguard, a prerequisite, or an error).
"""
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time

PRODUCTION_REF = "jmztpjudwzjvrcdtjynd"
STAGING_REPOSITORY = "highandmighty354-dev/gearhead-labs-staging"
SUITES = ["P1-P2", "D1", "D3", "D4", "D2", "P3", "P4"]
SUITE_CODE = {"P1-P2": "0012", "D1": "00d1", "D3": "00d3", "D4": "00d4", "D2": "00d2", "P3": "00f3", "P4": "00f4"}
PROBE_SUITES = {"D1", "D3"}
RECEIVER_SUITES = {"D4", "D2", "P3", "P4"}
# Owner attestations (repository variables) that the separately approved steps have happened. Exact values only.
PREREQUISITES = {
    "HV_PROBE_STATUS": "deployed-under-A9",     # D1, D3: the temporary probe was approved and deployed (A9)
    "HV_RECEIVER_STATUS": "deployed-under-A7",  # D4, D2, P3, P4: the real receiver was deployed (A7)
    "HV_TEST_WINDOW": "open-under-A8",          # D4, D2, P3, P4: ANALYTICS_INGEST_ENABLED=true on staging (A8)
}
EXPECTED_HISTORY = ["0001", "0002", "0003", "0004", "0005", "0101", "0102", "0103", "0201", "0301", "0401", "0402",
                    "0403", "0404", "0405", "0406", "0407", "0408", "0409", "0410", "0501"]
EXPECTED_PUBLIC_TABLES = 32
ANALYTICS_LOCK_KEY = 7104202611130501            # pg_advisory_xact_lock key in 0501 (analytics write lock)
HOLD_S = 1.5                                     # P3: Session A holds the lock this long after the rollup call
LOCK_TIMEOUT_S = 3.0                             # analytics_ingest() waits at most 3 s, then 'busy'
P3_MARGIN_S = 0.25                               # P3: room for the confirming sample and COMMIT inside A's 3 s
MAX_BYTES = 8192
CRON_JOBS = {"analytics-rollup-recent": "*/15 * * * *", "analytics-purge": "17 4 * * *"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
RESPONSE_HEADERS_KEPT = ("content-type", "content-length", "access-control-allow-origin", "vary", "sb-request-id", "server")
SETTLE_S = 1.0                                   # wait after a receiver response before the "after" snapshot


class Refused(Exception):
    """A safeguard or prerequisite refused the run. Nothing further is sent."""


def utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def iso(t=None):
    return (t or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_hex(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- validation of the run's configuration
REF_RE = re.compile(r"^[a-z0-9]{20}$")
ORIGIN_RE = re.compile(r"^https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+)(:([0-9]{1,5}))?$")
PRODUCTION_SITE_HOSTS = ("gearhead-labs.com",)


def check_ref(ref, typed):
    if not ref:
        raise Refused("EXPECTED_TEST_REF is not set: the target cannot be verified")
    if not REF_RE.match(ref):
        raise Refused("EXPECTED_TEST_REF is not a 20-character project ref")
    if ref == PRODUCTION_REF:
        raise Refused("EXPECTED_TEST_REF is the PRODUCTION project")
    if typed == PRODUCTION_REF:
        raise Refused("the typed ref is the PRODUCTION project")
    if typed != ref:
        raise Refused("the typed ref does not match EXPECTED_TEST_REF")
    return ref


def check_origin(origin, ref):
    """The TEST website origin is the owner's decision: it must be set, well formed, https, and not production."""
    if not origin:
        raise Refused("repository variable ANALYTICS_TEST_ORIGIN is not set: the TEST website origin has not been chosen")
    m = ORIGIN_RE.match(origin)
    if not m:
        raise Refused("ANALYTICS_TEST_ORIGIN is not a bare https origin (https://host or https://host:port, lowercase, no path)")
    host = m.group(1)
    if m.group(6) is not None and not 1 <= int(m.group(6)) <= 65535:
        raise Refused("ANALYTICS_TEST_ORIGIN has an invalid port")
    for prod in PRODUCTION_SITE_HOSTS:
        if host == prod or host.endswith("." + prod):
            raise Refused("ANALYTICS_TEST_ORIGIN is the PRODUCTION website")
    if PRODUCTION_REF in origin:
        raise Refused("ANALYTICS_TEST_ORIGIN names the PRODUCTION project")
    if host.endswith(".supabase.co") or host.endswith(".supabase.in"):
        raise Refused("ANALYTICS_TEST_ORIGIN is a Supabase address, not a website origin")
    return origin


def check_prerequisites(suite, env):
    """D1/D3 need the A9 probe; D4/D2/P3/P4 need the A7 receiver and the A8 window. Exact owner-set values only."""
    need = []
    if suite in PROBE_SUITES:
        need = ["HV_PROBE_STATUS"]
    elif suite in RECEIVER_SUITES:
        need = ["HV_RECEIVER_STATUS", "HV_TEST_WINDOW"]
    for name in need:
        if env.get(name, "") != PREREQUISITES[name]:
            raise Refused(f"prerequisite missing for {suite}: repository variable {name} must be exactly "
                          f"'{PREREQUISITES[name]}' (set by the owner only after that step was separately approved and done)")
    return need


def check_db_url(url, ref, ca_file):
    if not url:
        raise Refused("no database connection string: secret STAGING_DB_PASSWORD missing or the approved step did not build it")
    if PRODUCTION_REF in url:
        raise Refused("the connection string names the PRODUCTION project")
    if not (url.startswith(f"postgresql://postgres.{ref}:") or url.startswith(f"postgres://postgres.{ref}:")):
        raise Refused("the connection string is not for the verified staging project")
    tail = f":5432/postgres?sslmode=verify-full&sslrootcert={ca_file}"
    if not url.endswith(tail) or url.count("sslmode=") != 1:
        raise Refused("the connection string does not require sslmode=verify-full against the pinned roots")
    if not re.search(r"@aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com" + re.escape(tail) + "$", url):
        raise Refused("the connection string's host is not a Supabase Session pooler")


def check_context(env):
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise Refused("not started by the Run workflow button")
    if env.get("GITHUB_REPOSITORY") != STAGING_REPOSITORY:
        raise Refused("not running in the staging repository")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise Refused("not started from main")
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise Refused("re-run attempts are refused: start a new run (new IDs, new approval)")
    if env.get("HV_SUITE") not in SUITES:
        raise Refused("unknown or missing test suite")
    if not re.fullmatch(r"[0-9]{1,15}", env.get("GITHUB_RUN_ID", "")):
        raise Refused("no workflow run id")


# ---------------------------------------------------------------- synthetic identifiers (T10)
class Ids:
    """0a000000-<suite>-4<seq>-8<kind>-<run id>: fixed synthetic range, unique per run (the run id is embedded) and
    per suite; re-runs are refused, so no id is ever reused. D2's O and C use random ids in the same range."""
    KIND = {"event": 0, "vid": 1, "sid": 2, "pv": 3}

    def __init__(self, suite, run_id):
        self.code = SUITE_CODE[suite]
        self.run = int(run_id)
        if not 0 < self.run < 16 ** 12:
            raise Refused("run id out of range for the synthetic id format")
        self.issued = set()

    def make(self, kind, seq):
        if not 0 <= seq < 4096:
            raise ValueError("sequence out of range")
        u = f"0a000000-{self.code}-4{seq:03x}-8{self.KIND[kind]:03x}-{self.run:012x}"
        self.issued.add(u)
        return u

    def run_suffix(self):
        return f"-{self.run:012x}"

    def random_event(self):
        h = secrets.token_hex(12)
        u = f"0a000000-{h[0:4]}-4{h[4:7]}-{'89ab'[int(h[7], 16) % 4]}{h[8:11]}-{h[11:23]}"
        self.issued.add(u)
        return u


def event(eid, vid, sid, seq, page, pv_id):
    return {"event_id": eid, "name": "page_view", "ts_client": utc_now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "vid": vid, "sid": sid, "seq": seq, "page": page, "pv_id": pv_id, "viewport": "m", "props": {}}


def batch_bytes(events, size=None, extra=None):
    """A valid TEST batch, optionally with an ignored field, padded with spaces (JSON ignores them) to exactly `size` bytes."""
    obj = {"env": "test", "events": events}
    if extra:
        obj.update(extra)
    b = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if size is None:
        return b
    if len(b) > size:
        raise ValueError(f"batch is {len(b)} bytes, more than {size}")
    return b + b" " * (size - len(b))


# ---------------------------------------------------------------- evidence
SECRETISH = [re.compile(r"postgres(ql)?://"), re.compile(r"sslrootcert="), re.compile(r"\bpassword\b", re.I),
             re.compile(r"service_role_key|SERVICE_ROLE_KEY|sb_secret_|eyJhbGciOi"),
             re.compile(r"(?<![0-9.])(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}(?![0-9.])"),
             re.compile(r"\b[0-9a-fA-F]{1,4}(:[0-9a-fA-F]{0,4}){4,7}\b")]


def scrub_check(text, forbidden=()):
    """Refuse to write evidence that could hold a secret, a connection string or an IP address."""
    for s in forbidden:
        if s and s in text:
            raise Refused("evidence would contain a secret value: not written")
    for rx in SECRETISH:
        m = rx.search(text)
        if m:
            raise Refused(f"evidence would contain a forbidden pattern ({rx.pattern[:24]}...): not written")


class Evidence:
    def __init__(self, meta):
        self.doc = {"schema": "gearhead-hosted-verification-evidence/1", "started_at": iso(), **meta,
                    "steps": [], "requests": [], "results": {}, "inconclusive": [], "limitations": [], "verdict": None}
        self.client = []
        self.lock = threading.Lock()

    def step(self, name, **data):
        with self.lock:
            self.doc["steps"].append({"at": iso(), "step": name, **data})

    def clog(self, label, what, **data):
        with self.lock:
            self.client.append({"t": time.time(), "at": iso(), "request": label, "event": what, **data})

    def limitation(self, text):
        if text not in self.doc["limitations"]:
            self.doc["limitations"].append(text)

    def inconclusive(self, text):
        self.doc["inconclusive"].append(text)

    def write(self, out_dir, forbidden=()):
        self.doc["finished_at"] = iso()
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        base = f"hosted-verification-{self.doc.get('suite', 'x')}-run{self.doc.get('run_id', '0')}-{stamp}"
        ev_text = json.dumps(self.doc, indent=2, sort_keys=False, default=str)
        cl_text = "".join(json.dumps(x, default=str) + "\n" for x in self.client)
        scrub_check(ev_text, forbidden)
        scrub_check(cl_text, forbidden)
        os.makedirs(out_dir, exist_ok=True)
        p1 = os.path.join(out_dir, base + ".json")
        p2 = os.path.join(out_dir, base + "-client-log.jsonl")
        with open(p1, "w") as f:
            f.write(ev_text)
        with open(p2, "w") as f:
            f.write(cl_text)
        return p1, p2


# ---------------------------------------------------------------- the HTTP/1.1 client (one request per connection)
class Target:
    """Where requests go. Production use: only https://<TEST_REF>.supabase.co (built by for_ref)."""

    def __init__(self, scheme, host, port, ref=None, tls_context=None):
        self.scheme, self.host, self.port, self.ref = scheme, host, port, ref
        self.tls_context = tls_context

    @classmethod
    def for_ref(cls, ref):
        ctx = ssl.create_default_context()           # system trust store; certificate chain AND hostname are verified
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        return cls("https", f"{ref}.supabase.co", 443, ref, ctx)

    def receiver_path(self):
        return "/functions/v1/analytics-ingest"

    def probe_path(self):
        return "/functions/v1/b1-chunk-probe"


def http_request(target, ev, label, method, path, headers, body=None, *, mode="length", declared="auto",
                 piece=16384, delay=0.0, splits=None, after_response_s=None, timeout=60.0, keep_body=False):
    """Sends one request and returns what happened, recording every write in the client log (sizes only, never bytes).

    mode "length": Content-Length = len(body) (or `declared` if given), body written in `piece`-byte writes.
    mode "chunked": Transfer-Encoding: chunked, no Content-Length; one HTTP chunk per piece (or per `splits` segment).
    after_response_s: keep offering the rest of the body for this long after the response (D2); default: finish writing.
    """
    body = body or b""
    rec = {"request": label, "method": method, "path": path, "mode": mode, "body_bytes": len(body),
           "content_length_sent": None, "status": None, "error": None}
    hdrs = [("Host", target.host)] + list(headers)
    if method == "POST" or body:
        if mode == "length":
            cl = len(body) if declared == "auto" else declared
            hdrs.append(("Content-Length", str(cl)))
            rec["content_length_sent"] = cl
        else:
            hdrs.append(("Transfer-Encoding", "chunked"))
    hdrs.append(("Connection", "close"))
    head = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs) + "\r\n").encode("latin-1")
    if splits:
        pieces, last = [], 0
        for s in list(splits) + [len(body)]:
            pieces.append(body[last:s])
            last = s
    else:
        pieces = [body[i:i + piece] for i in range(0, len(body), piece)] if body else []
    state = {"written": 0, "at_response": None, "resp_t": None, "status_line": None, "headers": {}, "resp_body": b"",
             "reader_end": None, "write_end": None}
    lk = threading.Lock()
    t0 = time.monotonic()
    ev.clog(label, "connect", host_kind="test-project", port=target.port)
    try:
        raw = socket.create_connection((target.host, target.port), timeout=timeout)
    except OSError as e:
        rec["error"] = f"connect failed: {type(e).__name__}"
        ev.clog(label, "connect_failed", error=type(e).__name__)
        return rec
    sock = raw
    try:
        if target.scheme == "https":
            sock = target.tls_context.wrap_socket(raw, server_hostname=target.host)
            cert = sock.getpeercert() or {}
            subj = dict(x[0] for x in cert.get("subject", ()))
            iss = dict(x[0] for x in cert.get("issuer", ()))
            sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
            rec["tls"] = {"verified": True, "version": sock.version(), "subject_cn": subj.get("commonName"),
                          "issuer_cn": iss.get("commonName"), "issuer_o": iss.get("organizationName"),
                          "san_matches_host": any(s == target.host or (s.startswith("*.") and target.host.endswith(s[1:])
                                                                       and target.host.count(".") == s.count("."))
                                                  for s in sans),
                          "not_after": cert.get("notAfter"), "alpn": sock.selected_alpn_protocol()}
            ev.clog(label, "tls_ok", version=sock.version())
        sock.settimeout(timeout)
    except (ssl.SSLError, ssl.CertificateError, OSError) as e:
        rec["error"] = f"TLS failed: {type(e).__name__}"
        ev.clog(label, "tls_failed", error=type(e).__name__)
        try:
            raw.close()
        except OSError:
            pass
        return rec

    def reader():
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    state["reader_end"] = "eof"
                    break
                if state["resp_t"] is None:
                    with lk:
                        state["resp_t"] = time.monotonic()
                        state["at_response"] = state["written"]
                    ev.clog(label, "response_first_byte", body_bytes_written=state["at_response"])
                buf += chunk
                if len(buf) > 2 * 1024 * 1024:
                    state["reader_end"] = "too_much"
                    break
                if state["status_line"] is None and b"\r\n\r\n" in buf:
                    hd, _, rest = buf.partition(b"\r\n\r\n")
                    lines = hd.decode("latin-1").split("\r\n")
                    state["status_line"] = lines[0]
                    for ln in lines[1:]:
                        k, _, v = ln.partition(":")
                        state["headers"][k.strip().lower()] = v.strip()
                    buf = rest
                    ev.clog(label, "response_head", status_line=lines[0][:40])
                if state["status_line"] is not None:
                    cl = state["headers"].get("content-length")
                    if cl is not None and cl.isdigit() and len(buf) >= int(cl):
                        state["reader_end"] = "complete"
                        break
                    if state["headers"].get("transfer-encoding", "").lower() == "chunked" and buf.endswith(b"0\r\n\r\n"):
                        state["reader_end"] = "complete"
                        break
                    st = state["status_line"].split(" ")
                    if len(st) > 1 and st[1] in ("204", "304"):
                        state["reader_end"] = "complete"
                        break
        except socket.timeout:
            state["reader_end"] = "timeout"
        except ConnectionResetError:
            state["reader_end"] = "reset"
        except OSError as e:
            state["reader_end"] = f"error:{type(e).__name__}"
        state["resp_body"] = buf

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    write_end = "completed"
    try:
        sock.sendall(head)
        ev.clog(label, "head_sent", bytes=len(head))
        for i, p in enumerate(pieces):
            if after_response_s is None and state["resp_t"] is not None and state["reader_end"] in ("complete", "eof"):
                write_end = "stopped_after_response"
                break
            if after_response_s is not None and state["resp_t"] is not None and time.monotonic() - state["resp_t"] >= after_response_s:
                write_end = "stopped_after_fixed_period"
                break
            data = (f"{len(p):x}\r\n".encode() + p + b"\r\n") if mode == "chunked" else p
            sock.sendall(data)
            with lk:
                state["written"] += len(p)
            ev.clog(label, "write", body_bytes=len(p), wire_bytes=len(data), total_body_bytes=state["written"])
            if delay and i < len(pieces) - 1:
                time.sleep(delay)
        else:
            if mode == "chunked":
                sock.sendall(b"0\r\n\r\n")
                ev.clog(label, "chunked_end_sent")
    except socket.timeout:
        write_end = "timed_out"
    except ConnectionResetError:
        write_end = "reset"
    except BrokenPipeError:
        write_end = "closed_by_peer"
    except OSError as e:
        write_end = f"error:{type(e).__name__}"
    state["write_end"] = write_end
    t_write_done = time.monotonic()
    ev.clog(label, "writer_end", state=write_end, total_body_bytes=state["written"])
    rt.join(timeout)
    if rt.is_alive():
        state["reader_end"] = state["reader_end"] or "timeout"
    try:
        sock.close()
    except OSError:
        pass
    sl = state["status_line"] or ""
    m = re.match(r"HTTP/1\.[01] ([0-9]{3})", sl)
    rec["status"] = int(m.group(1)) if m else None
    if rec["status"] is None and not rec["error"]:
        rec["error"] = f"no HTTP response ({state['reader_end']})"
    rec["response_headers"] = {k: state["headers"][k] for k in RESPONSE_HEADERS_KEPT if k in state["headers"]}
    rec["response_body_bytes"] = len(state["resp_body"])
    rec["body_bytes_written"] = state["written"]
    rec["body_bytes_written_at_response"] = state["at_response"]
    rec["ms_to_response"] = None if state["resp_t"] is None else round((state["resp_t"] - t0) * 1000, 1)
    rec["ms_total"] = round((time.monotonic() - t0) * 1000, 1)
    rec["ms_writing"] = round((t_write_done - t0) * 1000, 1)
    rec["writer_end_state"] = write_end
    rec["reader_end_state"] = state["reader_end"]
    if keep_body:
        b = state["resp_body"]
        if state["headers"].get("transfer-encoding", "").lower() == "chunked":
            b = dechunk(b)
        rec["_body"] = b                           # used in memory (probe JSON); stripped before evidence is written
    return rec


def dechunk(b):
    out, i = b"", 0
    while True:
        j = b.find(b"\r\n", i)
        if j < 0:
            return None
        try:
            n = int(b[i:j].split(b";")[0], 16)
        except ValueError:
            return None
        if n == 0:
            return out
        out += b[j + 2:j + 2 + n]
        i = j + 2 + n + 2


def record_request(ev, rec):
    clean = {k: v for k, v in rec.items() if not k.startswith("_")}
    ev.doc["requests"].append(clean)
    return clean


def std_headers(origin, run_id, content=True):
    h = [("User-Agent", UA), ("Origin", origin), ("Accept", "*/*"), ("x-hv-run", f"hv-{run_id}")]
    if content:
        h.append(("Content-Type", "text/plain;charset=UTF-8"))
    return h


# ---------------------------------------------------------------- database (psycopg 3)
class DB:
    """One session. ro() runs a query in a READ ONLY transaction; write() is used only for the two rollup calls."""

    def __init__(self, conn, ev, name):
        self.conn, self.ev, self.name = conn, ev, name

    def ro(self, sql, params=None):
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION READ ONLY")
            cur = self.conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def one(self, sql, params=None):
        r = self.ro(sql, params)
        return r[0][0] if r else None

    def write(self, sql, params=None):
        self.ev.step("database_write", session=self.name, statement=sql.strip().split("(")[0][:60])
        with self.conn.transaction():
            cur = self.conn.execute(sql, params)
            return cur.fetchall() if cur.description else []


def connect(url, app):
    import psycopg  # installed hash-pinned by the workflow
    conn = psycopg.connect(url, autocommit=True, application_name=app, connect_timeout=20)
    return conn


def assert_tls(conn, ca_file):
    if not conn.pgconn.ssl_in_use:
        raise Refused("the database connection is not using TLS")
    dsn = conn.info.get_parameters()
    if dsn.get("sslmode") != "verify-full":
        raise Refused("the database connection is not sslmode=verify-full")
    if dsn.get("sslrootcert") != ca_file:
        raise Refused("the database connection does not use the pinned root certificates")


def db_identity(db):
    r = db.ro("""SELECT current_user, current_database(), version(), current_setting('server_version_num')::int,
                        (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user),
                        (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'),
                        (SELECT to_regclass('supabase_migrations.schema_migrations') IS NOT NULL)""")[0]
    hist = [x[0] for x in db.ro("SELECT version FROM supabase_migrations.schema_migrations ORDER BY version")] if r[6] else None
    return {"current_user": r[0], "database": r[1], "version": r[2], "server_version_num": r[3],
            "sees_all_rows": r[4], "public_tables": r[5], "migration_history": hist}


def check_identity(idn, where):
    if idn["current_user"] != "postgres" or idn["database"] != "postgres":
        raise Refused(f"{where}: connected as an unexpected user or database")
    if idn["migration_history"] != EXPECTED_HISTORY:
        raise Refused(f"{where}: the migration history is not exactly 0001-0409, 0410, 0501 (wrong project or state)")
    if idn["public_tables"] != EXPECTED_PUBLIC_TABLES:
        raise Refused(f"{where}: the database has {idn['public_tables']} public tables, not {EXPECTED_PUBLIC_TABLES}")
    if idn["server_version_num"] < 170000:
        raise Refused(f"{where}: PostgreSQL older than 17")
    if not idn["sees_all_rows"]:
        raise Refused(f"{where}: the database user cannot see rows of RLS-forced tables, so snapshots would be blind")


TODAY_SQL = "(now() AT TIME ZONE 'America/New_York')::date"


def snap_stats(db):
    rows = db.ro(f"""SELECT day::text, env, outcome, reason, n FROM public.analytics_ingest_stats
                     WHERE day >= {TODAY_SQL} - 1 ORDER BY 1, 2, 3, 4""")
    return {f"{d}|{e}|{o}|{r}": int(n) for d, e, o, r, n in rows}


def stats_delta(before, after):
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in sorted(keys) if after.get(k, 0) != before.get(k, 0)}


def delta_test_only(delta):
    """Delta keyed by outcome/reason for env 'test' only, plus a flag for any change in another environment."""
    out, other = {}, False
    for k, v in delta.items():
        _, env, outcome, reason = k.split("|", 3)
        if env == "test":
            key = outcome + (":" + reason if reason else "")
            out[key] = out.get(key, 0) + v
        else:
            other = True
    return out, other


def snap_events(db, event_ids):
    if not event_ids:
        return {}
    rows = db.ro("""SELECT event_id::text, id, env, name, ts_server FROM public.analytics_events
                    WHERE event_id = ANY(%s::uuid[]) ORDER BY id""", (list(event_ids),))
    out = {}
    for eid, i, env, name, ts in rows:
        out.setdefault(eid, []).append({"id": int(i), "env": env, "name": name, "ts_server": iso(ts) if ts else None})
    return out


def max_event_id(db):
    return int(db.one("SELECT coalesce(max(id), 0) FROM public.analytics_events"))


def new_event_ids(db, after_id):
    return [r[0] for r in db.ro("SELECT event_id::text FROM public.analytics_events WHERE id > %s ORDER BY id", (after_id,))]


def headroom(db, n_events):
    """Rate-limit and storage headroom for n events from the shared 'unknown' sender (ANALYTICS_ADDRESS_SOURCE unset)."""
    r = db.ro(f"""
      WITH lim AS (SELECT public.analytics_limits() AS l),
           t AS (SELECT {TODAY_SQL} AS today),
           k AS (SELECT sha256(convert_to('global|project', 'UTF8')) AS g,
                        (SELECT sha256(convert_to(s.salt::text || '|unknown', 'UTF8'))
                           FROM public.analytics_rate_salt s, t WHERE s.day = t.today) AS u)
      SELECT public.analytics_storage_bytes(),
             ((l->>'storage_mb')::bigint - (l->>'rollup_headroom_mb')::bigint) * 1048576,
             (l->>'batch_reserve_bytes')::bigint, (l->>'event_reserve_bytes')::bigint,
             coalesce((SELECT n FROM public.analytics_rate WHERE kind = 'global_day' AND key = k.g
                         AND win_start = (t.today::timestamp AT TIME ZONE 'America/New_York')), 0),
             (l->>'global_day')::int,
             coalesce((SELECT n FROM public.analytics_rate WHERE kind = 'global_hour' AND key = k.g
                         AND win_start = date_trunc('hour', now())), 0),
             (l->>'global_hour')::int,
             coalesce((SELECT n FROM public.analytics_rate WHERE kind = 'net_10min' AND key = k.u
                         AND win_start = date_bin('10 minutes', now(), timestamptz '2000-01-01')), 0),
             (l->>'unknown_10min')::int,
             coalesce((SELECT n FROM public.analytics_rate WHERE kind = 'net_day' AND key = k.u
                         AND win_start = (t.today::timestamp AT TIME ZONE 'America/New_York')), 0),
             (l->>'unknown_day')::int,
             extract(epoch FROM (date_bin('10 minutes', now(), timestamptz '2000-01-01') + interval '10 minutes' - now()))
      FROM lim, t, k""")[0]
    storage, accept, b_res, e_res, gd, gd_cap, gh, gh_cap, u10, u10_cap, ud, ud_cap, secs_left = r
    need_storage = int(b_res) + n_events * int(e_res)
    h = {"events": n_events, "storage_bytes": int(storage), "storage_accept_limit": int(accept),
         "global_day": [int(gd), int(gd_cap)], "global_hour": [int(gh), int(gh_cap)],
         "unknown_10min": [int(u10), int(u10_cap)], "unknown_day": [int(ud), int(ud_cap)],
         "seconds_left_in_10min_window": round(float(secs_left), 1)}
    problems = []
    if int(storage) + need_storage > int(accept):
        problems.append("storage_cap")
    for name in ("global_day", "global_hour", "unknown_10min", "unknown_day"):
        used, cap = h[name]
        if used + n_events > cap:
            problems.append(name)
    h["ok"] = not problems
    h["would_refuse"] = problems
    return h


# ---------------------------------------------------------------- one evidence window (D4, D2, P4 checks)
def window(ctx, label, ids, n_events, send):
    db, ev = ctx.db, ctx.ev
    before_stats = snap_stats(db)
    before_rows = snap_events(db, ids)
    before_max = max_event_id(db)
    hr = headroom(db, n_events)
    w = {"request": label, "event_ids": list(ids), "before": {"at": iso(), "rows": before_rows, "max_event_id": before_max},
         "headroom": hr}
    if any(before_rows.values()):
        w["outcome"] = "not run: an event id of this request already exists (id reuse refused)"
        return w
    if not hr["ok"]:
        w["outcome"] = f"not run: a limit could refuse the request ({', '.join(hr['would_refuse'])})"
        return w
    rec = send()
    w["http"] = record_request(ev, rec)
    time.sleep(SETTLE_S)
    after_stats = snap_stats(db)
    w["after"] = {"at": iso(), "rows": snap_events(db, ids), "max_event_id": max_event_id(db)}
    w["stats_delta_all"] = stats_delta(before_stats, after_stats)
    w["stats_delta_test"], w["other_env_changed"] = delta_test_only(w["stats_delta_all"])
    new_ids = new_event_ids(db, before_max)
    w["new_rows_in_window"] = len(new_ids)
    w["foreign_rows_in_window"] = len([x for x in new_ids if x not in set(ids)])
    return w


def classify_window(w, expected_delta, expected_rows):
    """Revision 10 categories for one request. expected_rows: {event_id: 0 or 1}."""
    if "http" not in w:
        return w["outcome"].split(":")[0] if w.get("outcome", "").startswith("not run") else "not run"
    d = w["stats_delta_test"]
    rows_ok = all(len(w["after"]["rows"].get(e, [])) == n for e, n in expected_rows.items())
    if w["foreign_rows_in_window"] or w["other_env_changed"]:
        return "not determined: another traffic source in the window"
    if any(k.startswith("rate_limited") for k in d):
        return "not tested: rate_limited"
    st = w["http"]["status"]
    if (st is None or st != 204) and not d:
        return "not reached the function (platform error or no response, no statistics change)"
    if d == expected_delta and rows_ok and st == 204:
        return "as expected"
    if d == expected_delta and rows_ok:
        return f"differs from Revision 10: HTTP status {st} instead of 204 (database result as expected)"
    return "differs from Revision 10"


# ---------------------------------------------------------------- suites
class Ctx:
    def __init__(self, suite, run_id, origin, target, ev, db=None, db_url=None, ca_file=None, connect_fn=None):
        self.suite, self.run_id, self.origin, self.target, self.ev = suite, run_id, origin, target, ev
        self.db, self.db_url, self.ca_file = db, db_url, ca_file
        self.connect_fn = connect_fn
        self.ids = Ids(suite, run_id)


def receiver_present(ctx):
    """OPTIONS preflight with the TEST origin: the receiver answers 204 with this origin allowed, before its kill switch,
    without reading a body or calling the database. A missing function or another origin setting refuses the suite."""
    rec = http_request(ctx.target, ctx.ev, "receiver-preflight", "OPTIONS", ctx.target.receiver_path(),
                       [("User-Agent", UA), ("Origin", ctx.origin), ("Access-Control-Request-Method", "POST"),
                        ("Access-Control-Request-Headers", "content-type")])
    record_request(ctx.ev, rec)
    ok = rec["status"] == 204 and rec["response_headers"].get("access-control-allow-origin") == ctx.origin
    ctx.ev.step("receiver_presence_check", status=rec["status"], origin_allowed=ok)
    ctx.ev.limitation("The workflow cannot read function settings or the deployed source: that the kill switch is ON (A8) "
                      "is the owner's attestation HV_TEST_WINDOW; the deployed receiver source is confirmed under A7, not here.")
    if not ok:
        raise Refused("the TEST receiver is absent, or does not allow ANALYTICS_TEST_ORIGIN (OPTIONS preflight "
                      f"answered {rec['status']}): deploy and configure it under A7 first")


def suite_p1p2(ctx):
    db, ev = ctx.db, ctx.ev
    seq = db.ro("""SELECT cache_size, increment_by, cycle FROM pg_sequences
                   WHERE schemaname = 'public' AND sequencename = 'analytics_events_id_seq'""")
    serial = db.one("SELECT pg_get_serial_sequence('public.analytics_events', 'id')")
    version = db.one("SELECT version()")
    iso_level = db.one("SHOW default_transaction_isolation")
    overrides = db.ro("""SELECT coalesce(r.rolname, '(all roles)'), coalesce(d.datname, '(all databases)'), s.setconfig
                         FROM pg_db_role_setting s
                         LEFT JOIN pg_roles r ON r.oid = s.setrole
                         LEFT JOIN pg_database d ON d.oid = s.setdatabase
                         WHERE (s.setrole = 0 OR r.rolname IN ('postgres', 'authenticator', 'service_role'))
                           AND (s.setdatabase = 0 OR d.datname = current_database())""")
    roles_present = [r[0] for r in db.ro("""SELECT rolname FROM pg_roles WHERE rolname IN ('postgres', 'authenticator', 'service_role')
                                           ORDER BY 1""")]
    iso_overrides = [{"role": r, "database": d, "setting": c} for r, d, cfg in overrides for c in (cfg or [])
                     if c.split("=", 1)[0] == "default_transaction_isolation"]
    has_cron = db.one("SELECT to_regclass('cron.job') IS NOT NULL")
    jobs = db.ro("SELECT jobname, schedule, command, active FROM cron.job WHERE jobname = ANY(%s) ORDER BY jobname",
                 (list(CRON_JOBS),)) if has_cron else []
    res = {"P1": {"sequence": [{"cache_size": int(a), "increment_by": int(b), "cycle": bool(c)} for a, b, c in seq],
                  "serial_sequence": serial, "version": version},
           "P2": {"default_transaction_isolation": iso_level, "roles_checked_present": roles_present,
                  "isolation_overrides": iso_overrides,
                  "all_overrides_for_checked_roles": [{"role": r, "database": d, "settings": list(cfg or [])} for r, d, cfg in overrides],
                  "cron_jobs": [{"jobname": a, "schedule": b, "command": c, "active": bool(d)} for a, b, c, d in jobs]}}
    p1 = (len(seq) == 1 and int(seq[0][0]) == 1 and int(seq[0][1]) == 1 and seq[0][2] is False
          and serial == "public.analytics_events_id_seq" and bool(version))
    got_jobs = {a: b for a, b, _, _ in jobs}
    p2 = iso_level == "read committed" and not iso_overrides and got_jobs == CRON_JOBS
    res["P1"]["pass"] = p1
    res["P2"]["pass"] = p2
    if "authenticator" not in roles_present:
        ev.limitation("Role 'authenticator' does not exist here, so it has no override to check.")
    ev.limitation("P1: the 0501 apply log's post-condition is not re-read here; 0501 being recorded in the history means its "
                  "single transaction, which ends in ANALYTICS_0501_POSTCONDITION, committed (see A6 run #8 evidence).")
    ev.limitation("P2 shows defaults only, not the isolation level a running cron job or API call actually used (Revision 10).")
    if iso_level != "read committed" or iso_overrides:
        ev.inconclusive("P2 finding: isolation is not plain read committed for every checked role (reported, not assumed safe).")
    ev.doc["results"] = res
    return "PASS" if p1 and p2 else "FAIL"


def probe_call(ctx, label, probe_mode, body, **kw):
    hdrs = std_headers(ctx.origin, ctx.run_id) + [("x-probe-run", f"hv-{ctx.run_id}"), ("x-probe-mode", probe_mode)]
    rec = http_request(ctx.target, ctx.ev, label, "POST", ctx.target.probe_path(), hdrs, body, keep_body=True, **kw)
    parsed = None
    if rec["status"] == 200 and rec.get("_body"):
        try:
            parsed = json.loads(rec["_body"])
        except ValueError:
            parsed = None
    rec.pop("_body", None)
    return rec, parsed


def valid_chunks_reply(p):
    return (isinstance(p, dict) and p.get("mode") == "chunks" and isinstance(p.get("chunks"), list)
            and all(isinstance(x, int) and x >= 0 for x in p["chunks"]) and isinstance(p.get("total"), int)
            and (p.get("declared_length") is None or isinstance(p.get("declared_length"), (int, float)))
            and (p.get("ms_to_first_chunk") is None or isinstance(p.get("ms_to_first_chunk"), (int, float))))


def valid_decode_reply(p):
    return (isinstance(p, dict) and p.get("mode") == "decode" and p.get("outcome") in ("accepted", "refused")
            and isinstance(p.get("code_points"), list) and all(isinstance(x, int) and 0 <= x <= 0x10FFFF for x in p["code_points"]))


D1_SIZES = [1024, 8192, 8193, 65536, 1048576]
D1_WAYS = {"content-length": dict(mode="length", piece=16384, delay=0.0),
           "chunked": dict(mode="chunked", piece=4096, delay=0.0),
           "slow-chunked": dict(mode="chunked", piece=4096, delay=0.1)}


def suite_d1(ctx):
    ev = ctx.ev
    cases, contract_ok, absent = [], True, False
    for size in D1_SIZES:
        body = (b"0123456789abcdef" * (size // 16 + 1))[:size]
        for way, kw in D1_WAYS.items():
            label = f"D1-{size}-{way}"
            rec, p = probe_call(ctx, label, "chunks", body, timeout=120.0, **kw)
            c = {"case": label, "body_bytes": size, "sent_as": way, "content_length_sent": rec["content_length_sent"],
                 "status": rec["status"], "error": rec["error"], "ms_to_response": rec["ms_to_response"]}
            if rec["status"] == 404 and not cases:
                record_request(ev, rec)
                absent = True
                break
            if rec["status"] == 200 and valid_chunks_reply(p):
                c.update({"chunks": p["chunks"], "n_chunks": len(p["chunks"]), "total": p["total"],
                          "declared_length": p.get("declared_length"), "ms_to_first_chunk": p.get("ms_to_first_chunk"),
                          "total_equals_sent": p["total"] == size,
                          "platform_added_or_changed_length": (way != "content-length" and p.get("declared_length") is not None)
                          or (way == "content-length" and p.get("declared_length") != size),
                          "largest_chunk": max(p["chunks"]) if p["chunks"] else 0})
            elif rec["status"] == 200:
                contract_ok = False
                c["probe_reply"] = "does not match the probe contract (README)"
            else:
                c["platform_rejection_or_error"] = True
            record_request(ev, rec)
            cases.append(c)
        if absent:
            break
    if absent:
        raise Refused("the probe b1-chunk-probe answered 404: it is not deployed (A9), or the run header was refused")
    over = [c for c in cases if c["body_bytes"] > MAX_BYTES and "chunks" in c]
    ev.doc["results"] = {"D1": {"cases": cases,
                                "largest_chunk_delivered_for_bodies_over_8192": max((c["largest_chunk"] for c in over), default=None),
                                "note": "Chunk sizes are as observed; nothing is predicted."}}
    if not contract_ok:
        ev.inconclusive("D1: at least one probe reply did not match the probe contract; those cases are not determined.")
        return "NOT DETERMINED"
    rejected = [c["case"] for c in cases if c.get("platform_rejection_or_error")]
    if rejected:
        ev.inconclusive("D1: the platform refused or failed some requests before the probe answered, so their chunk sizes "
                        "were not observed (recorded per case): " + ", ".join(rejected))
        return "INCONCLUSIVE: chunk sizes not observed for " + ", ".join(rejected)
    return "PASS"     # every case observed; the observations are in the evidence (results.D1.cases)


def d3_cases(ctx):
    fx = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "d3-expected.json")))
    cases = [{"case": c["id"], "bytes": bytes(c["bytes"]), "expected": c["code_points"], "send": {}} for c in fx["cases"]]
    i = ctx.ids
    ev1 = event(i.make("event", 1), i.make("vid", 1), i.make("sid", 1), 0, "hosted-verification/d3", i.make("pv", 1))
    s = batch_bytes([ev1], extra={"pad": "XX"})
    k = s.index(b"XX")
    with_invalid = s[:k] + b"\xff\xfe" + s[k + 2:]
    b = b"\xef\xbb\xbf" + with_invalid
    cases.append({"case": "B2-7-batch-bom-invalid", "bytes": b, "expected": local_decode(b), "send": {}})
    ev2 = event(i.make("event", 2), i.make("vid", 1), i.make("sid", 1), 1, "hosted-verification/d3", i.make("pv", 2))
    b11 = batch_bytes([ev2], 4000, extra={"pad": "ü€𝄞"})
    text = b11.decode("utf-8")
    at = len(text[:text.index("€")].encode("utf-8")) + 1          # cut inside the 3-byte "€"
    four = len(text[:text.index("𝄞")].encode("utf-8")) + 2        # cut inside the 4-byte "𝄞"
    cases.append({"case": "B2-11-split", "bytes": b11, "expected": [ord(ch) for ch in text],
                  "send": {"mode": "chunked", "splits": [at, four], "delay": 0.3}, "split_chars": ["€", "𝄞"]})
    return cases


def local_decode(b):
    """The decoder readBodyLimited() uses (WHATWG TextDecoder utf-8): leading BOM removed, invalid bytes -> U+FFFD.
    tests/test_hv.py checks this equals Node's TextDecoder for every D3 case."""
    t = b.decode("utf-8", "replace")
    if t.startswith("﻿"):
        t = t[1:]
    return [ord(c) for c in t]


def suite_d3(ctx):
    ev = ctx.ev
    out, all_equal, contract_ok, refused = [], True, True, []
    for n, c in enumerate(d3_cases(ctx)):
        rec, p = probe_call(ctx, "D3-" + c["case"], "decode", c["bytes"], **c["send"])
        if n == 0 and rec["status"] == 404:
            record_request(ev, rec)
            raise Refused("the probe b1-chunk-probe answered 404: it is not deployed (A9), or the run header was refused")
        record_request(ev, rec)
        exp = c["expected"]
        r = {"case": c["case"], "body_bytes": len(c["bytes"]), "status": rec["status"],
             "expected_code_points": len(exp), "expected_sha256": sha256_hex(json.dumps(exp).encode()),
             "expected_fffd": exp.count(0xFFFD)}
        if rec["status"] == 200 and valid_decode_reply(p):
            got = p["code_points"]
            r.update({"observed_code_points": len(got), "observed_sha256": sha256_hex(json.dumps(got).encode()),
                      "observed_fffd": got.count(0xFFFD), "outcome": p["outcome"],
                      "equal_byte_for_byte": got == exp and p["outcome"] == "accepted",
                      "starts_with_bom": bool(got) and got[0] == 0xFEFF})
            if p["outcome"] != "accepted":
                refused.append(c["case"])
            if c.get("split_chars"):
                r["split_chars_decoded_once"] = all(got.count(ord(ch)) == 1 for ch in c["split_chars"])
            if got != exp:
                r["first_difference_at"] = next((k for k, (a, b) in enumerate(zip(got, exp)) if a != b), min(len(got), len(exp)))
                all_equal = False
        else:
            contract_ok = False
            r["probe_reply"] = "no valid decode reply (status or contract)"
        out.append(r)
    ev.doc["results"] = {"D3": {"cases": out, "note": "Code points are compared in memory; only counts and SHA-256 are "
                                "recorded (no request bodies in evidence). Decoding behaviour is observed, not changed."}}
    ev.limitation("D3: whether the platform delivered the B2-11 body to the function in the chunks it was sent in is not "
                  "reported by decode mode; the case proves the decoded result only.")
    if not contract_ok:
        ev.inconclusive("D3: some cases got no valid probe reply; not determined.")
        return "NOT DETERMINED"
    if not all_equal:
        return "FAIL"
    if refused:   # Revision 10: "Refusing invalid UTF-8 remains an open decision" - a refusal is never a pass
        ev.inconclusive("D3: the probe refused the decode for " + ", ".join(refused) + "; whether invalid UTF-8 should be "
                        "refused is an open decision in Revision 10, so these cases are not passed.")
        return "INCONCLUSIVE: decode refused for " + ", ".join(refused)
    return "PASS"


def suite_d4(ctx):
    ev, i = ctx.ev, ctx.ids
    receiver_present(ctx)
    plan = [("B8192", 8192, dict(mode="chunked", piece=1024), {"accepted": 1}, 1),
            ("B8193", 8193, dict(mode="chunked", piece=1024), {"rejected:bad_batch": 1}, 0),
            ("BLIE", 20480, dict(mode="length", declared=100, piece=4096), {"rejected:bad_batch": 1}, 0),
            ("B9000", 9000, dict(mode="length", declared=9000, piece=4096), {}, 0)]
    results, net = [], {"events": 0, "bad_batch": 0}
    for n, (label, size, kw, exp_delta, exp_rows) in enumerate(plan, start=1):
        eid = i.make("event", n)
        e = event(eid, i.make("vid", 1), i.make("sid", n), 0, "hosted-verification/d4", i.make("pv", n))
        body = batch_bytes([e], size)
        w = window(ctx, label, [eid], 1, lambda: http_request(
            ctx.target, ev, label, "POST", ctx.target.receiver_path(), std_headers(ctx.origin, ctx.run_id), body, **kw))
        w["body_bytes"] = size
        w["expected"] = {"http": 204, "rows": exp_rows, "stats_delta": exp_delta or "no change at all"}
        w["result"] = classify_window(w, exp_delta, {eid: exp_rows})
        if (label == "B8192" and w["result"].startswith("differs") and not w["stats_delta_test"]
                and w["http"]["status"] == 204 and not any(w["after"]["rows"].values())):
            w["result"] = "not determined: 204 with no statistics change and no row (the switch may be off)"
        if label == "B9000":
            w["function_log"] = "unavailable: this workflow cannot read Edge Function logs"
            ev.limitation("D4 B9000: the 'analytics-ingest: too large' log line and 'no database call' cannot be read by this "
                          "workflow; they must come from the function log (dashboard or connector). D4 is not fully passed without it.")
        if "after" in w:
            net["events"] += sum(len(v) for v in w["after"]["rows"].values())
            net["bad_batch"] += w["stats_delta_test"].get("rejected:bad_batch", 0)
        results.append(w)
        if label == "B8192" and w["result"] != "as expected":
            if not w.get("stats_delta_test") and w.get("http", {}).get("status") == 204:
                ev.inconclusive("D4: B8192 got 204 with no statistics change and no row: the receiver's switch may be off "
                                "(test window not open) - remaining D4 requests were not sent.")
            else:
                ev.inconclusive("D4: B8192 did not behave as expected - remaining D4 requests were not sent.")
            break
    ev.limitation("D4 BLIE: over HTTP/1.1 the bytes after Content-Length 100 are not part of the request; a server sees "
                  "them as a malformed second message and may answer it with an error before or instead of the function's "
                  "204 (Node's default does). The first status received is reported and compared with 204 literally.")
    ev.doc["results"] = {"D4": {"requests": results, "net_change": net,
                                "expected_net_change": {"events": 1, "bad_batch": 2}}}
    done = [w["result"] for w in results]
    if len(done) == 4 and all(r == "as expected" for r in done) and net == {"events": 1, "bad_batch": 2}:
        return "NOT FULLY PASSED: all four as expected; B9000 function-log evidence unavailable"
    diff = [f"{w['request']} ({w['result'][len('differs from Revision 10'):].lstrip(': ') or 'see evidence'})"
            for w in results if w["result"].startswith("differs")]
    if diff:
        return "FAIL: " + "; ".join(diff)
    return "INCONCLUSIVE"


def suite_d2(ctx):
    ev, i = ctx.ev, ctx.ids
    receiver_present(ctx)
    o_id, c_id = i.random_event(), i.random_event()
    if o_id == c_id:
        raise Refused("random id collision")
    vid = i.make("vid", 1)
    o_ev = event(o_id, vid, i.make("sid", 1), 0, "hosted-verification/d2", i.make("pv", 1))
    c_ev = event(c_id, vid, i.make("sid", 2), 0, "hosted-verification/d2", i.make("pv", 2))
    o_body = batch_bytes([o_ev], 10485760)
    c_body = batch_bytes([c_ev], 4096)
    ev.step("d2_sizes_recorded_before_sending", O_intended_bytes=len(o_body), C_bytes=len(c_body))
    wo = window(ctx, "D2-O", [o_id], 1, lambda: http_request(
        ctx.target, ev, "D2-O", "POST", ctx.target.receiver_path(), std_headers(ctx.origin, ctx.run_id), o_body,
        mode="chunked", piece=65536, delay=0.1, after_response_s=30.0, timeout=90.0))
    if "http" in wo:
        h = wo["http"]
        wo["bytes"] = {"intended_total": len(o_body), "written_when_response_arrived": h["body_bytes_written_at_response"],
                       "written_after_response": (h["body_bytes_written"] - h["body_bytes_written_at_response"])
                       if h["body_bytes_written_at_response"] is not None else None,
                       "final_written": h["body_bytes_written"], "sender_end_state": h["writer_end_state"],
                       "receiver_side_end": h["reader_end_state"], "fixed_period_after_response_s": 30.0}
    d = wo.get("stats_delta_test")
    if "http" not in wo:
        o_cat = "3. Not determined (window not run: " + wo["outcome"] + ")"
    elif wo["foreign_rows_in_window"] or wo["other_env_changed"] or any(k.startswith("rate_limited") for k in d):
        o_cat = "3. Not determined"
    elif d == {"rejected:bad_batch": 1} and not wo["after"]["rows"].get(o_id):
        o_cat = "1. Rejected, nothing inserted"
    elif not d and (wo["http"]["status"] != 204):
        o_cat = "2. Not reached the function"
    else:
        o_cat = "3. Not determined"
    wo["category"] = o_cat
    wc = window(ctx, "D2-C", [c_id], 1, lambda: http_request(
        ctx.target, ev, "D2-C", "POST", ctx.target.receiver_path(), std_headers(ctx.origin, ctx.run_id), c_body,
        mode="length", piece=4096))
    if "http" in wc:
        ok_c = (wc["stats_delta_test"] == {"accepted": 1} and len(wc["after"]["rows"].get(c_id, [])) == 1
                and not wc["foreign_rows_in_window"] and not wc["other_env_changed"])
        wc["category"] = "accepted, exactly one row" if ok_c else "not as expected"
    else:
        wc["category"] = "not run: " + wc["outcome"]
    ev.doc["results"] = {"D2": {"O": wo, "C": wc,
                                "cancellation": "not verifiable on the hosted platform: server-side cancellation cannot be "
                                "verified on the hosted platform with the approved probe scope. The local tests B2-2, B2-3, "
                                "B2-8, B2-9, B2-10 and B2-16 remain the evidence for cancellation behaviour.",
                                "note": "O is judged only from O's own window and rows; C proves only that the batch format is valid."}}
    ev.limitation("D2: function log lines for the O and C windows are unavailable to this workflow.")
    ev.limitation("D2: bytes 'accepted' after the response include data held in network buffers; an early response or a "
                  "closed connection does not show where the stream was abandoned (Revision 10).")
    return f"O: {o_cat}; C: {wc['category']}; cancellation: not verifiable on the hosted platform"


def lock_parts(key):
    return key >> 32, key & 0xFFFFFFFF


def suite_p3(ctx):
    ev, i, db = ctx.ev, ctx.ids, ctx.db
    receiver_present(ctx)
    l1 = i.make("event", 1)
    body = batch_bytes([event(l1, i.make("vid", 1), i.make("sid", 1), 0, "hosted-verification/p3", i.make("pv", 1))])
    if snap_events(db, [l1]).get(l1):
        raise Refused("L1's id already exists")
    hr = headroom(db, 1)
    if not hr["ok"]:
        ev.doc["results"] = {"P3": {"headroom": hr, "outcome": "not run: a limit could refuse L1"}}
        return "NOT RUN"
    today = db.one(f"SELECT {TODAY_SQL}::text")
    before_stats = snap_stats(db)
    sess_a = DB(ctx.connect_fn(ctx.db_url, "gh-hosted-verification-P3-A"), ev, "A")
    mon = DB(ctx.connect_fn(ctx.db_url, "gh-hosted-verification-P3-monitor"), ev, "monitor")
    mon.conn.execute("SET default_transaction_read_only = on")      # the monitor session only ever reads
    if ctx.ca_file:
        assert_tls(sess_a.conn, ctx.ca_file)
        assert_tls(mon.conn, ctx.ca_file)
    a_pid = sess_a.conn.info.backend_pid
    hi, lo = lock_parts(ANALYTICS_LOCK_KEY)
    samples, stop, t_marks, http_box = [], threading.Event(), {}, {}

    def monitor():
        last = None
        while not stop.is_set():
            tb = time.monotonic()
            rows = mon.conn.execute(
                """SELECT l.pid, l.granted, a.usename, a.backend_type, a.wait_event_type, a.wait_event,
                          CASE WHEN a.query IS NULL OR a.query LIKE '<insufficient%%' THEN NULL
                               ELSE position('analytics_ingest' in a.query) > 0 END
                   FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid
                   WHERE l.locktype = 'advisory' AND l.classid = %s AND l.objid = %s AND l.objsubid = 1
                     AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
                   ORDER BY l.granted DESC, l.pid""", (hi, lo)).fetchall()
            ta = time.monotonic()
            snap = tuple(("A" if r[0] == a_pid else f"other-{r[0]}", r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows)
            samples.append((tb, ta, snap))     # the snapshot shows the state at some instant between tb and ta
            if snap != last:
                ev.clog("P3", "pg_locks", t_before=round(tb, 3), t_after=round(ta, 3),
                        holders=[{"session": s[0].split("-")[0] if s[0] != "A" else "A", "granted": s[1], "user": s[2],
                                  "backend_type": s[3], "wait": f"{s[4]}/{s[5]}", "query_is_ingest": s[6]} for s in snap])
                last = snap
            time.sleep(0.02)

    def send_l1():
        t_marks["l1_sent"] = time.monotonic()
        http_box["rec"] = http_request(ctx.target, ev, "P3-L1", "POST", ctx.target.receiver_path(),
                                       std_headers(ctx.origin, ctx.run_id), body, mode="length")
        http_box["t_done"] = time.monotonic()

    def a_holds(snap):
        return any(x[0] == "A" and x[1] is True for x in snap)

    def waiters(snap):
        return [x for x in snap if x[0] != "A" and x[1] is False]

    mt = threading.Thread(target=monitor, daemon=True)
    mt.start()
    ht = threading.Thread(target=send_l1, daemon=True)
    hold = {"outcome": None, "first": None, "confirm": None}
    try:
        sess_a.conn.execute("BEGIN")
        sess_a.conn.execute("SET LOCAL statement_timeout = '15s'")
        t_marks["rollup_call"] = time.monotonic()
        ev.step("database_write", session="A", statement="SELECT public.analytics_rollup")
        sess_a.conn.execute("SELECT public.analytics_rollup(%s::date)", (today,))
        t_marks["rollup_returned"] = time.monotonic()
        wm = sess_a.conn.execute("SELECT max_event_id FROM public.analytics_rollup_runs WHERE day = %s::date AND env = 'test'",
                                 (today,)).fetchone()[0]
        ht.start()
        # The 1.5 s hold is measured from the first pg_locks sample that shows the ingest WAITING on A's lock, and A commits
        # only after a sample taken at least 1.5 s later still shows A holding the lock and the same session waiting.
        # A waiter must appear early enough that A's whole hold (from the rollup call) stays under the 3 s lock wait.
        waiter_deadline = t_marks["rollup_call"] + LOCK_TIMEOUT_S - HOLD_S - P3_MARGIN_S
        k = 0
        while hold["outcome"] is None:
            for tb, ta, snap in samples[k:]:
                k += 1
                if hold["first"] is None:
                    w = waiters(snap)
                    if w and a_holds(snap):
                        hold["first"] = (tb, ta, w[0][0])
                        t_marks["first_waiter_seen"] = ta
                    continue
                f_ta, f_pid = hold["first"][1], hold["first"][2]
                still = a_holds(snap) and any(x[0] == f_pid for x in waiters(snap))
                if not still:
                    hold["outcome"] = "waiter left before the hold was demonstrated"
                    break
                if tb >= f_ta + HOLD_S:
                    hold["confirm"] = (tb, ta, snap)
                    hold["outcome"] = "held"
                    t_marks["hold_demonstrated"] = ta
                    break
            now = time.monotonic()
            if hold["outcome"] is None and hold["first"] is None and now >= waiter_deadline:
                hold["outcome"] = "no waiter seen in time"
            if hold["outcome"] is None and hold["first"] is not None and now >= hold["first"][1] + LOCK_TIMEOUT_S:
                hold["outcome"] = "monitor stalled"
            if hold["outcome"] is None:
                time.sleep(0.005)
        t_marks["commit"] = time.monotonic()
        sess_a.conn.execute("COMMIT")
        t_marks["released"] = time.monotonic()
    except Exception:
        try:
            sess_a.conn.execute("ROLLBACK")
        except Exception:
            pass
        stop.set()
        raise
    ht.join(60)
    time.sleep(0.3)
    stop.set()
    mt.join(5)
    rec = http_box.get("rec") or {"request": "P3-L1", "status": None, "error": "no result"}
    record_request(ev, rec)
    time.sleep(SETTLE_S)
    l1_rows = snap_events(db, [l1]).get(l1, [])
    after_stats = snap_stats(db)
    delta_t, other_env = delta_test_only(stats_delta(before_stats, after_stats))
    # every session other than A seen waiting (granted = false) on the analytics lock, with its sample indexes
    waiting = {}
    for n, (tb, ta, snap) in enumerate(samples):
        for x in waiters(snap):
            w = waiting.setdefault(x[0], {"first": n, "last": n, "is_ingest": set(), "user": x[2]})
            w["last"] = n
            w["is_ingest"].add(x[6])
    a_granted = any(a_holds(snap) for _, _, snap in samples)
    t0 = t_marks["rollup_call"]
    res = {"today": today, "hold_s": HOLD_S, "session_a_watermark": int(wm) if wm is not None else None,
           "session_a_lock_granted_seen": a_granted, "hold_outcome": hold["outcome"],
           "a_total_hold_s": round(t_marks["released"] - t0, 3),
           "a_total_hold_under_3s": t_marks["released"] - t0 < LOCK_TIMEOUT_S,
           "http": {k2: rec.get(k2) for k2 in ("status", "error", "ms_to_response")},
           "l1_rows": l1_rows, "stats_delta_test": delta_t, "other_env_changed": other_env,
           "timeline_ms": {k2: round((v - t0) * 1000, 1) for k2, v in t_marks.items()},
           "monitor_samples": len(samples)}
    if hold["confirm"]:
        _, _, snap = hold["confirm"]
        res["pg_locks_snapshot_during_hold"] = [{"session": "A" if x[0] == "A" else "waiter", "granted": x[1], "user": x[2],
                                                 "wait": f"{x[4]}/{x[5]}", "query_is_ingest": x[6]} for x in snap]
    verdict = None
    if len(waiting) > 1:
        res["waiters"] = len(waiting)
        ev.inconclusive("P3: more than one session waited on the analytics lock - another traffic source.")
        verdict = "INCONCLUSIVE"
    elif not waiting or hold["outcome"] == "no waiter seen in time":
        ev.inconclusive("P3: the ingest was not seen waiting on the analytics lock while A held it (it arrived after A "
                        "committed, or too late for A's whole hold to stay under 3 s). Inconclusive; repeat with the timing "
                        "adjusted and a new run-unique L1.")
        verdict = "INCONCLUSIVE"
    else:
        (pid, w), = waiting.items()
        identified = w["is_ingest"] == {True}
        fi, li = w["first"], w["last"]
        start_floor = max(samples[fi - 1][0] if fi > 0 else t_marks["l1_sent"], t_marks["l1_sent"])
        gone_ta = samples[li + 1][1] if li + 1 < len(samples) else None
        lower = samples[li][0] - samples[fi][1]
        upper = (gone_ta - start_floor) if gone_ta is not None else None
        res["waiter"] = {"user": w["user"], "identified_as_analytics_ingest": identified,
                         "observed_wait_s_lower_bound": round(lower, 3),
                         "observed_wait_s_upper_bound": round(upper, 3) if upper is not None else None,
                         "first_wait_seen_ms_after_rollup_call": round((samples[fi][1] - t0) * 1000, 1),
                         "hold_demonstrated_s_after_first_wait": round(t_marks["hold_demonstrated"] - samples[fi][1], 3)
                         if "hold_demonstrated" in t_marks else None,
                         "release_minus_first_wait_s": round(t_marks["released"] - samples[fi][1], 3),
                         "bounds": "from pg_locks sample times only (never from HTTP timing)"}
        res["pg_locks_snapshot_shows_a_and_ingest"] = bool(hold["confirm"]) and hold["first"][2] == pid
        if not identified:
            ev.limitation("P3: PostgreSQL did not expose enough (query text not visible to this user) to identify the waiting "
                          "session as the ingest; a lock wait is NOT inferred from HTTP timing.")
            verdict = "INCONCLUSIVE"
        res["rule_wait_ge_hold_and_lt_3s"] = upper is not None and lower >= HOLD_S and upper < LOCK_TIMEOUT_S
    l1_id = l1_rows[0]["id"] if len(l1_rows) == 1 else None
    res["l1_id_above_watermark"] = (l1_id is not None and wm is not None and l1_id > int(wm))
    ev.step("database_write", session="main", statement="SELECT public.analytics_rollup_pending")
    n_days = db.write("SELECT public.analytics_rollup_pending()")[0][0]
    res["rollup_pending_days"] = int(n_days)
    wm_after = db.one("SELECT max_event_id FROM public.analytics_rollup_runs WHERE day = %s::date AND env = 'test'", (today,))
    res["next_run_reopened_today"] = (int(n_days) >= 1 and l1_id is not None and wm_after is not None and int(wm_after) >= l1_id)
    res["consistency"] = consistency(db, today)
    ev.doc["results"] = {"P3": res}
    ev.limitation("P3 timing: the 1.5 s hold is counted from the first pg_locks sample showing the ingest waiting on A's lock; "
                  "A commits only after a sample at least 1.5 s later still shows both. Wait bounds come from pg_locks sample "
                  "times; their precision is the sampling interval plus one monitor query.")
    for c in (sess_a, mon):
        try:
            c.conn.close()
        except Exception:
            pass
    if verdict:
        return verdict
    ok = (res["rule_wait_ge_hold_and_lt_3s"] and res["pg_locks_snapshot_shows_a_and_ingest"] and res["a_total_hold_under_3s"]
          and hold["outcome"] == "held" and res["l1_id_above_watermark"] and res["next_run_reopened_today"]
          and res["consistency"]["ok"] and a_granted and rec.get("status") == 204 and delta_t == {"accepted": 1})
    return "PASS" if ok else "FAIL"


def consistency(db, today):
    r = db.ro(f"""
      WITH b AS (SELECT (%s::date::timestamp AT TIME ZONE 'America/New_York') AS d0,
                        ((%s::date + 1)::timestamp AT TIME ZONE 'America/New_York') AS d1)
      SELECT (SELECT coalesce(sum(value), 0) FROM public.analytics_daily WHERE day = %s::date AND env = 'test' AND metric = 'pv'),
             (SELECT count(*) FROM public.analytics_events e, b WHERE e.env = 'test' AND e.name = 'page_view' AND NOT e.bot
                AND e.ts_server >= b.d0 AND e.ts_server < b.d1),
             (SELECT count(*) FROM public.analytics_sessions WHERE day = %s::date AND env = 'test' AND bot),
             (SELECT count(*) FROM (SELECT (ts_server AT TIME ZONE 'America/New_York')::date AS d, max(id) AS m
                                    FROM public.analytics_events GROUP BY 1) x
               WHERE x.m > coalesce((SELECT max(r.max_event_id) FROM public.analytics_rollup_runs r WHERE r.day = x.d), 0))""",
              (today, today, today, today))[0]
    rolled, raw, bot_sessions, days_above = (int(x) for x in r)
    return {"rolled_up_page_views_today": rolled, "raw_page_views_today": raw, "bot_sessions_today": bot_sessions,
            "days_with_event_above_watermark": days_above,
            "ok": rolled == raw and days_above == 0 and bot_sessions == 0}


P4_DURATION_S = 1200
P4_MAX_EVENTS = 250
P4_BATCH = 4


def p4_plan(ids):
    """<= 250 events, every 7th batch repeats an earlier batch's event ids from this run; one new session per batch."""
    batches, sent, seq, regular = [], 0, 0, []
    vid = ids.make("vid", 1)
    while sent < P4_MAX_EVENTS:
        n = min(P4_BATCH, P4_MAX_EVENTS - sent)
        k = len(batches)
        if k % 7 == 6 and regular:
            src = regular[-3] if len(regular) >= 3 else regular[0]
            evs = src["events"][:n]
            batches.append({"events": evs, "repeat_of": src["index"], "index": k})
        else:
            sid = ids.make("sid", k)
            evs = []
            for j in range(n):
                seq += 1
                evs.append(event(ids.make("event", seq), vid, sid, j, f"hosted-verification/p4/{k % 50}", ids.make("pv", seq)))
            b = {"events": evs, "repeat_of": None, "index": k}
            batches.append(b)
            regular.append(b)
        sent += n
    return batches


def suite_p4(ctx, duration_s=P4_DURATION_S, pace=None):
    ev, db = ctx.ev, ctx.db
    receiver_present(ctx)
    jobs = db.ro("SELECT jobid, schedule, active FROM cron.job WHERE jobname = 'analytics-rollup-recent'")
    if len(jobs) != 1 or jobs[0][1] != CRON_JOBS["analytics-rollup-recent"] or not jobs[0][2]:
        raise Refused("P4 needs the active scheduled job analytics-rollup-recent (*/15): not found as expected")
    jobid = jobs[0][0]
    plan = p4_plan(ctx.ids)
    unique = {e["event_id"] for b in plan for e in b["events"]}
    repeats = sum(len(b["events"]) for b in plan if b["repeat_of"] is not None)
    sent = sum(len(b["events"]) for b in plan)
    if any(snap_events(db, sorted(unique)).values()):
        raise Refused("P4: an event id of this run already exists (id reuse refused)")
    interval = pace if pace is not None else duration_s / len(plan)
    if 60.0 / interval >= 100:
        raise Refused("P4 pacing would reach 100 requests a minute")
    today = db.one(f"SELECT {TODAY_SQL}::text")
    t_start_db = db.one("SELECT now()")
    before = snap_stats(db)
    before_max = max_event_id(db)
    log, stopped = [], None
    t0 = time.monotonic()
    for k, b in enumerate(plan):
        target_t = t0 + k * interval
        time.sleep(max(0.0, target_t - time.monotonic()))
        hr = headroom(db, len(b["events"]))
        if not hr["ok"]:
            stopped = f"stopped before batch {k}: a limit could refuse it ({', '.join(hr['would_refuse'])})"
            break
        body = batch_bytes(b["events"])
        if len(body) > MAX_BYTES:
            raise Refused("P4 batch over 8,192 bytes")
        rec = http_request(ctx.target, ev, f"P4-{k}", "POST", ctx.target.receiver_path(), std_headers(ctx.origin, ctx.run_id),
                           body, mode="length")
        record_request(ev, rec)
        log.append({"batch": k, "events": len(b["events"]), "repeat_of": b["repeat_of"], "status": rec["status"]})
    elapsed = time.monotonic() - t0
    time.sleep(SETTLE_S)
    t_end_db = db.one("SELECT now()")
    after = snap_stats(db)
    delta_t, other_env = delta_test_only(stats_delta(before, after))
    rows = snap_events(db, sorted(unique))
    foreign = [x for x in new_event_ids(db, before_max) if x not in unique]
    cron_runs = db.ro("""SELECT status, start_time, end_time FROM cron.job_run_details
                         WHERE jobid = %s AND start_time >= %s AND start_time <= %s ORDER BY start_time""",
                      (jobid, t_start_db, t_end_db))
    ev.step("database_write", session="main", statement="SELECT public.analytics_rollup_pending")
    final_days = db.write("SELECT public.analytics_rollup_pending()")[0][0]
    today_end = db.one(f"SELECT {TODAY_SQL}::text")
    cons = consistency(db, today_end)
    sent_done = sum(x["events"] for x in log)
    repeats_done = sum(x["events"] for x in log if x["repeat_of"] is not None)
    unique_done = len({e["event_id"] for b in plan[:len(log)] for e in b["events"]})
    refusals = {k: v for k, v in delta_t.items() if k.startswith("rate_limited") or "busy" in k}
    res = {"window": {"start": iso(t_start_db), "end": iso(t_end_db), "elapsed_s": round(elapsed, 1),
                      "requests": len(log), "requests_per_minute": round(len(log) / max(elapsed / 60, 1e-9), 2)},
           "planned": {"events_sent": sent, "unique": len(unique), "repeats": repeats},
           "done": {"events_sent": sent_done, "unique": unique_done, "repeats": repeats_done, "stopped": stopped},
           "stats_delta_test": delta_t, "other_env_changed": other_env, "foreign_rows_in_window": len(foreign),
           "rows_per_unique_id_all_one": all(len(rows.get(e, [])) == 1 for e in
                                             {e["event_id"] for b in plan[:len(log)] for e in b["events"]}),
           "cron_runs_in_window": [{"status": s, "start": iso(a), "end": iso(b) if b else None} for s, a, b in cron_runs],
           "final_rollup_days": int(final_days), "consistency": cons, "refusals": refusals,
           "day_changed_during_window": today != today_end, "batches": log}
    checks = {
        "cron_succeeded_in_window": any(s == "succeeded" for s, _, _ in cron_runs),
        "accepted_equals_sent_minus_duplicates": delta_t.get("accepted", 0) == sent_done - repeats_done,
        "duplicates_counted_once": delta_t.get("duplicate", 0) == repeats_done and res["rows_per_unique_id_all_one"],
        "rolled_equals_raw_and_no_day_above_watermark": cons["ok"],
        "no_rate_limited_or_busy": not refusals,
    }
    res["checks"] = checks
    ev.doc["results"] = {"P4": res}
    if refusals:
        ev.inconclusive(f"P4: refusals reported: {refusals}")
    if stopped:
        ev.inconclusive("P4: " + stopped)
    if foreign or other_env:
        ev.inconclusive("P4: another traffic source changed analytics data during the window.")
        return "INCONCLUSIVE"
    if res["day_changed_during_window"]:
        ev.limitation("P4: the America/New_York day changed during the window; the consistency check covers the final day.")
    return "PASS" if all(checks.values()) and not stopped else "FAIL"


SUITE_FN = {"P1-P2": suite_p1p2, "D1": suite_d1, "D3": suite_d3, "D4": suite_d4, "D2": suite_d2, "P3": suite_p3, "P4": suite_p4}


def run_suite(ctx):
    ev = ctx.ev
    if ctx.db is not None:
        idn = db_identity(ctx.db)
        ev.doc["database_identity_before"] = {k: v for k, v in idn.items()}
        check_identity(idn, "before the suite")
    verdict = SUITE_FN[ctx.suite](ctx)
    if ctx.db is not None:
        idn2 = db_identity(ctx.db)
        ev.doc["database_identity_after"] = {k: v for k, v in idn2.items()}
        check_identity(idn2, "after the suite")
    ev.doc["verdict"] = verdict
    return verdict


# ---------------------------------------------------------------- command line (workflow use only)
def main(env=None):
    env = dict(os.environ if env is None else env)
    out_dir = env.get("HV_OUT", "")
    ev = Evidence({"suite": env.get("HV_SUITE"), "run_id": env.get("GITHUB_RUN_ID"), "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
                   "run_url": env.get("HV_RUN_URL"), "commit": env.get("GITHUB_SHA"), "repository": env.get("GITHUB_REPOSITORY"),
                   "test_project_ref": env.get("EXPECTED_TEST_REF"), "approval_gate": env.get("HV_APPROVAL_RESULT"),
                   "tls_database": env.get("HV_TLS_RESULT"), "origin": env.get("ANALYTICS_TEST_ORIGIN")})
    forbidden = [env.get("HV_DB_URL", "")]
    m = re.match(r"^postgres(?:ql)?://[^:/@]+:([^@]+)@", env.get("HV_DB_URL", ""))
    if m:
        from urllib.parse import unquote
        forbidden += [m.group(1), unquote(m.group(1))]
    code = 1
    try:
        if not out_dir:
            raise Refused("HV_OUT is not set")
        check_context(env)
        suite = env["HV_SUITE"]
        ref = check_ref(env.get("EXPECTED_TEST_REF", ""), env.get("CONFIRM_REF", ""))
        origin = check_origin(env.get("ANALYTICS_TEST_ORIGIN", ""), ref) if suite != "P1-P2" else None
        check_prerequisites(suite, env)
        ca = env.get("HV_CA_FILE", "")
        if not ca or not os.path.isfile(ca):
            raise Refused("the pinned root certificate file is missing")
        check_db_url(env.get("HV_DB_URL", ""), ref, ca)
        ev.step("configuration_checked", suite=suite, origin_set=origin is not None)
        target = Target.for_ref(ref)
        if target.host != f"{ref}.supabase.co" or target.scheme != "https":
            raise Refused("the request target is not the verified TEST project")
        conn = connect(env["HV_DB_URL"], f"gh-hosted-verification-{suite}")
        assert_tls(conn, ca)
        ev.step("database_connected", tls="verify-full against the pinned Supabase roots")
        ctx = Ctx(suite, env["GITHUB_RUN_ID"], origin, target, ev, DB(conn, ev, "main"), env["HV_DB_URL"], ca, connect)
        verdict = run_suite(ctx)
        code = 0 if verdict == "PASS" else 2
    except Refused as e:
        ev.doc["verdict"] = "REFUSED: " + str(e)
        print("HARD FAIL: " + str(e), file=sys.stderr)
    except Exception as e:  # report the type only: messages can carry connection details
        ev.doc["verdict"] = f"ERROR: {type(e).__name__}"
        print(f"HARD FAIL: unexpected {type(e).__name__} (details withheld: they could contain connection data)", file=sys.stderr)
    if out_dir:
        try:
            p1, p2 = ev.write(out_dir, forbidden)
            print(f"evidence: {os.path.basename(p1)}; client log: {os.path.basename(p2)}")
        except Refused as e:
            print("HARD FAIL: " + str(e), file=sys.stderr)
            code = 1
    print("verdict: " + str(ev.doc["verdict"]))
    return code


if __name__ == "__main__":
    sys.exit(main())
