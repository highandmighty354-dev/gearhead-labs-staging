"""Tests for hv.py. LOCAL ONLY: no network beyond 127.0.0.1, no Supabase contact.

  python3 -m unittest discover -s .github/hosted-verification/tests -v
Integration tests (each suite end to end) need a local PostgreSQL 17 prepared as in README.md ("Local tests") and:
  HV_TEST_PG_URL        postgresql://postgres@127.0.0.1:<port>/postgres  (the runner's user, like hosted 'postgres')
  HV_TEST_PG_ADMIN_URL  postgresql://supabase_admin@127.0.0.1:<port>/postgres (the stand-in receiver's own connection)
Without them the integration tests are skipped (and reported as skipped).
"""
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import hv  # noqa: E402
import standin  # noqa: E402

PG = os.environ.get("HV_TEST_PG_URL")
PG_ADMIN = os.environ.get("HV_TEST_PG_ADMIN_URL")
REF = "npizzzfvbtrlpnxfhqks"
ORIGIN = "https://test-site.example"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # 0501's uuid_re
_run = [int(time.time()) % 10_000_000 * 100]


def next_run_id():
    _run[0] += 1
    return str(_run[0])


def good_env(**over):
    ca = os.path.join(tempfile.mkdtemp(), "ca.pem")
    open(ca, "w").write("x")
    e = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": hv.STAGING_REPOSITORY, "GITHUB_REF": "refs/heads/main",
         "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123456", "GITHUB_SHA": "a" * 40, "HV_SUITE": "D4",
         "EXPECTED_TEST_REF": REF, "CONFIRM_REF": REF, "ANALYTICS_TEST_ORIGIN": ORIGIN,
         "HV_PROBE_STATUS": "deployed-under-A9", "HV_RECEIVER_STATUS": "deployed-under-A7", "HV_TEST_WINDOW": "open-under-A8",
         "HV_CA_FILE": ca, "HV_OUT": tempfile.mkdtemp(),
         "HV_DB_URL": f"postgresql://postgres.{REF}:s3cr%2Ft@aws-1-us-east-2.pooler.supabase.com:5432/postgres?sslmode=verify-full&sslrootcert={ca}"}
    e.update(over)
    return e


class Unit(unittest.TestCase):
    def test_ref_checks(self):
        self.assertEqual(hv.check_ref(REF, REF), REF)
        for exp, typed, msg in [("", REF, "not set"), ("ABC", "ABC", "20-character"), (hv.PRODUCTION_REF, hv.PRODUCTION_REF, "PRODUCTION"),
                                (REF, hv.PRODUCTION_REF, "PRODUCTION"), (REF, "a" * 20, "does not match"), (REF, "", "does not match")]:
            with self.assertRaisesRegex(hv.Refused, msg):
                hv.check_ref(exp, typed)

    def test_origin_checks(self):
        self.assertEqual(hv.check_origin("https://staging.example.org", REF), "https://staging.example.org")
        self.assertEqual(hv.check_origin("https://highandmighty354-dev.github.io", REF), "https://highandmighty354-dev.github.io")
        self.assertEqual(hv.check_origin("https://a.example:8443", REF), "https://a.example:8443")
        bad = {"": "not set", "http://a.example": "bare https", "https://a.example/": "bare https", "https://a.example/x": "bare https",
               "https://A.example": "bare https", "https://localhost": "bare https", "https://www.gearhead-labs.com": "PRODUCTION",
               "https://gearhead-labs.com": "PRODUCTION", "https://x.gearhead-labs.com": "PRODUCTION",
               f"https://{REF}.supabase.co": "Supabase", f"https://{hv.PRODUCTION_REF}.example": "PRODUCTION",
               "https://a.example:99999": "invalid port", "https://a.example, https://b.example": "bare https"}
        for o, msg in bad.items():
            with self.assertRaisesRegex(hv.Refused, msg, msg=o):
                hv.check_origin(o, REF)

    def test_prerequisites(self):
        full = {k: v for k, v in hv.PREREQUISITES.items()}
        self.assertEqual(hv.check_prerequisites("P1-P2", {}), [])
        for s in ("D1", "D3"):
            self.assertEqual(hv.check_prerequisites(s, full), ["HV_PROBE_STATUS"])
            for bad in ({}, {"HV_PROBE_STATUS": "deployed"}, {"HV_PROBE_STATUS": "deployed-under-A9 "}):
                with self.assertRaisesRegex(hv.Refused, "A9"):
                    hv.check_prerequisites(s, bad)
        for s in ("D4", "D2", "P3", "P4"):
            hv.check_prerequisites(s, full)
            with self.assertRaisesRegex(hv.Refused, "HV_RECEIVER_STATUS"):
                hv.check_prerequisites(s, {"HV_TEST_WINDOW": "open-under-A8"})
            with self.assertRaisesRegex(hv.Refused, "HV_TEST_WINDOW"):
                hv.check_prerequisites(s, {"HV_RECEIVER_STATUS": "deployed-under-A7"})
            with self.assertRaisesRegex(hv.Refused, "HV_RECEIVER_STATUS"):
                hv.check_prerequisites(s, {"HV_PROBE_STATUS": "deployed-under-A9"})

    def test_db_url_checks(self):
        ca = "/tmp/x/ca.pem"
        good = f"postgresql://postgres.{REF}:pw@aws-1-us-east-2.pooler.supabase.com:5432/postgres?sslmode=verify-full&sslrootcert={ca}"
        hv.check_db_url(good, REF, ca)
        bad = {"": "secret STAGING_DB_PASSWORD", good.replace(REF, hv.PRODUCTION_REF): "PRODUCTION",
               good.replace(REF, "b" * 20): "not for the verified", good.replace("verify-full", "require"): "verify-full",
               good.replace(f"&sslrootcert={ca}", ""): "verify-full", good + "&sslmode=disable": "verify-full",
               good.replace("aws-1-us-east-2.pooler.supabase.com", "evil.example"): "Session pooler",
               good.replace(f"sslrootcert={ca}", "sslrootcert=/tmp/other.pem"): "verify-full"}
        for u, msg in bad.items():
            with self.assertRaisesRegex(hv.Refused, msg, msg=u):
                hv.check_db_url(u, REF, ca)

    def test_context_checks(self):
        base = good_env()
        hv.check_context(base)
        for k, v, msg in [("GITHUB_EVENT_NAME", "push", "Run workflow"), ("GITHUB_EVENT_NAME", "schedule", "Run workflow"),
                          ("GITHUB_REF", "refs/heads/feature", "main"), ("GITHUB_RUN_ATTEMPT", "2", "re-run"),
                          ("HV_SUITE", "all", "suite"), ("HV_SUITE", "", "suite"), ("GITHUB_REPOSITORY", "x/y", "staging repository")]:
            with self.assertRaisesRegex(hv.Refused, msg):
                hv.check_context({**base, k: v})

    def test_ids(self):
        ids = hv.Ids("D4", "46120704519")
        a, b = ids.make("event", 1), ids.make("event", 2)
        self.assertNotEqual(a, b)
        for u in (a, b, ids.make("vid", 1), ids.make("sid", 4095), ids.random_event(), ids.random_event()):
            self.assertRegex(u, UUID_RE)
            self.assertTrue(u.startswith("0a000000-"))
        self.assertTrue(a.endswith(f"-{46120704519:012x}"))
        self.assertNotEqual(hv.Ids("P3", "46120704519").make("event", 1), a, "suite code keeps suites apart")
        self.assertNotEqual(hv.Ids("D4", "46120704520").make("event", 1), a, "the run id keeps runs apart")
        with self.assertRaises(hv.Refused):
            hv.Ids("D4", str(16 ** 12))

    def test_batch_sizes(self):
        ids = hv.Ids("D4", "7")
        e = hv.event(ids.make("event", 1), ids.make("vid", 1), ids.make("sid", 1), 0, "hosted-verification/d4", ids.make("pv", 1))
        for n in (4096, 8192, 8193, 9000, 20480, 10485760):
            b = hv.batch_bytes([e], n)
            self.assertEqual(len(b), n)
            o = json.loads(b)
            self.assertEqual(o["env"], "test")
            self.assertEqual(o["events"][0]["event_id"], e["event_id"])
        self.assertRegex(e["ts_client"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
        self.assertRegex(e["page"], r"^[a-z0-9:_./-]+$")

    def test_p4_plan(self):
        plan = hv.p4_plan(hv.Ids("P4", "9"))
        sent = sum(len(b["events"]) for b in plan)
        rep = sum(len(b["events"]) for b in plan if b["repeat_of"] is not None)
        uniq = {e["event_id"] for b in plan for e in b["events"]}
        self.assertLessEqual(sent, 250)
        self.assertEqual(sent, 250)
        self.assertGreater(rep, 0)
        self.assertEqual(len(uniq), sent - rep)
        self.assertLess(60.0 / (hv.P4_DURATION_S / len(plan)), 100)
        for b in plan:
            self.assertLessEqual(len(hv.batch_bytes(b["events"])), hv.MAX_BYTES)
            if b["repeat_of"] is not None:
                self.assertLess(b["repeat_of"], b["index"])

    def test_d3_expected_matches_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        ctx = hv.Ctx("D3", "11", ORIGIN, None, hv.Evidence({}))
        cases = hv.d3_cases(ctx)
        self.assertEqual(len(cases), 8)
        script = ("const cs=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                  "console.log(JSON.stringify(cs.map(b=>[...new TextDecoder('utf-8').decode(new Uint8Array(b))].map(c=>c.codePointAt(0)))))")
        out = subprocess.run([node, "-e", script], input=json.dumps([list(c["bytes"]) for c in cases]), capture_output=True, text=True,
                             check=True).stdout
        node_cps = json.loads(out)
        for c, n in zip(cases, node_cps):
            self.assertEqual(c["expected"], n, c["case"])
            self.assertEqual(hv.local_decode(c["bytes"]), n, c["case"])
        fx = json.load(open(os.path.join(os.path.dirname(HERE), "d3-expected.json")))
        self.assertEqual([c["id"] for c in fx["cases"]], ["B2-7-fffe", "B2-7-8041", "B2-7-e282", "B2-7-c0af", "B2-7-eda080", "B2-7-bom"])

    def test_scrub(self):
        hv.scrub_check(json.dumps({"version": "PostgreSQL 17.6 on aarch64-unknown-linux-gnu, compiled by gcc (GCC) 13.2.0, 64-bit",
                                   "at": "2026-11-28T07:30:46.123Z", "id": "0a000000-00d4-4001-8000-000abcdef012",
                                   "schedule": "*/15 * * * *", "notAfter": "Feb 10 12:00:00 2027 GMT"}))
        for bad in ["postgresql://x", "sslrootcert=/a", "the password is", "SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9",
                    "client 198.51.100.7 connected", "addr 2001:db8:0:0:1:2:3:4", "sb_secret_abc"]:
            with self.assertRaises(hv.Refused, msg=bad):
                hv.scrub_check(json.dumps({"x": bad}))
        with self.assertRaises(hv.Refused):
            hv.scrub_check("abc s3cr/t xyz", ["s3cr/t"])

    def test_dechunk(self):
        self.assertEqual(hv.dechunk(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"), b"abcde")
        self.assertIsNone(hv.dechunk(b"zz\r\n"))

    def test_no_direct_analytics_writes_in_runner(self):
        src = open(os.path.join(os.path.dirname(HERE), "hv.py")).read()
        self.assertNotRegex(src, r"\b(INSERT\s+INTO|UPDATE\s+public\.|DELETE\s+FROM|TRUNCATE)\b")
        self.assertNotRegex(src, r'\("(?i:apikey|authorization)"', "no API key or Authorization header is ever sent")
        self.assertNotRegex(src, r'(environ|env)(\.get)?[\(\[]"[A-Z_]*KEY', "no key is read from the environment")
        writes = re.findall(r"\.write\(\"(SELECT [^\"]+)\"", src)
        self.assertEqual(sorted(set(writes)), ["SELECT public.analytics_rollup_pending()"])
        self.assertEqual(len(re.findall(r'"SELECT public\.analytics_rollup\(%s::date\)"', src)), 1, "P3's one rollup call")

    def test_tls_verification_refuses_untrusted_certificate(self):
        d = tempfile.mkdtemp()
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{d}/k.pem", "-out", f"{d}/c.pem",
                        "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(f"{d}/c.pem", f"{d}/k.pem")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        got = []

        def serve():
            s, _ = srv.accept()
            try:
                t = sctx.wrap_socket(s, server_side=True)
                got.append(t.recv(100))
            except (ssl.SSLError, OSError):
                got.append(None)
        threading.Thread(target=serve, daemon=True).start()
        t = hv.Target.for_ref(REF)
        t.host, t.port = "localhost", srv.getsockname()[1]
        ev = hv.Evidence({})
        rec = hv.http_request(t, ev, "tls-test", "POST", "/functions/v1/analytics-ingest", [], b"x")
        self.assertIsNone(rec["status"])
        self.assertIn("TLS failed", rec["error"])
        time.sleep(0.2)
        self.assertNotIn(b"POST", b"".join(x or b"" for x in got), "no HTTP bytes reach a server whose certificate is not trusted")

    def test_target_is_fixed_to_the_test_project(self):
        t = hv.Target.for_ref(REF)
        self.assertEqual((t.scheme, t.host, t.port), ("https", f"{REF}.supabase.co", 443))
        self.assertEqual(t.receiver_path(), "/functions/v1/analytics-ingest")
        self.assertEqual(t.probe_path(), "/functions/v1/b1-chunk-probe")
        self.assertTrue(t.tls_context.check_hostname)
        self.assertEqual(t.tls_context.verify_mode, ssl.CERT_REQUIRED)


class MainRefusals(unittest.TestCase):
    """main() refuses before any database connection or HTTP request; evidence still records the refusal."""

    def setUp(self):
        self.calls = []
        self.orig_connect, self.orig_http = hv.connect, hv.http_request
        hv.connect = lambda *a, **k: self.calls.append("db") or (_ for _ in ()).throw(AssertionError("connected"))
        hv.http_request = lambda *a, **k: self.calls.append("http") or (_ for _ in ()).throw(AssertionError("sent"))

    def tearDown(self):
        hv.connect, hv.http_request = self.orig_connect, self.orig_http

    def refused(self, env, pattern):
        code = hv.main(env)
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [], "nothing contacted")
        files = os.listdir(env["HV_OUT"])
        ev = json.load(open(os.path.join(env["HV_OUT"], [f for f in files if f.endswith(".json")][0])))
        self.assertRegex(ev["verdict"], "^REFUSED: .*" + pattern)
        for f in files:
            txt = open(os.path.join(env["HV_OUT"], f)).read()
            self.assertNotIn("s3cr", txt)
            self.assertNotIn("postgresql://", txt)

    def test_wrong_project(self):
        self.refused(good_env(CONFIRM_REF="a" * 20), "does not match")

    def test_production_ref(self):
        self.refused(good_env(CONFIRM_REF=hv.PRODUCTION_REF), "PRODUCTION")
        self.refused(good_env(EXPECTED_TEST_REF=hv.PRODUCTION_REF, CONFIRM_REF=hv.PRODUCTION_REF), "PRODUCTION")

    def test_wrong_branch(self):
        self.refused(good_env(GITHUB_REF="refs/heads/a4-hosted-verification"), "main")

    def test_rerun(self):
        self.refused(good_env(GITHUB_RUN_ATTEMPT="2"), "re-run")

    def test_not_manual(self):
        for e in ("push", "pull_request", "schedule", "deployment"):
            self.refused(good_env(GITHUB_EVENT_NAME=e), "Run workflow")

    def test_missing_origin(self):
        self.refused(good_env(ANALYTICS_TEST_ORIGIN=""), "ANALYTICS_TEST_ORIGIN is not set")
        self.refused(good_env(ANALYTICS_TEST_ORIGIN="https://www.gearhead-labs.com"), "PRODUCTION website")

    def test_missing_secret(self):
        self.refused(good_env(HV_DB_URL=""), "STAGING_DB_PASSWORD")
        self.refused(good_env(HV_CA_FILE="/nonexistent/ca.pem"), "root certificate")

    def test_absent_receiver_and_probe_prerequisites(self):
        for s in ("D4", "D2", "P3", "P4"):
            self.refused(good_env(HV_SUITE=s, HV_RECEIVER_STATUS=""), "HV_RECEIVER_STATUS")
            self.refused(good_env(HV_SUITE=s, HV_TEST_WINDOW=""), "HV_TEST_WINDOW")
        for s in ("D1", "D3"):
            self.refused(good_env(HV_SUITE=s, HV_PROBE_STATUS=""), "HV_PROBE_STATUS")

    def test_unknown_suite(self):
        self.refused(good_env(HV_SUITE="ALL"), "suite")


class ProbeVerdicts(unittest.TestCase):
    """Regression tests (PR #5 audit): a refused D3 decode and a D1 platform rejection are never PASS. No database needed."""

    def run_probe_suite(self, suite, **standin_kw):
        st = standin.StandIn(origin=ORIGIN, **standin_kw)
        self.addCleanup(st.close)
        ev = hv.Evidence({"suite": suite, "run_id": "1"})
        ctx = hv.Ctx(suite, next_run_id(), ORIGIN, hv.Target("http", "127.0.0.1", st.port), ev)
        return hv.run_suite(ctx), ev

    def test_d3_accepted_decode_still_passes(self):
        v, ev = self.run_probe_suite("D3")
        self.assertEqual(v, "PASS")

    def test_d3_refused_decode_with_matching_code_points_is_not_pass(self):
        v, ev = self.run_probe_suite("D3", probe_refuse_decode=True)
        cases = ev.doc["results"]["D3"]["cases"]
        refused = [c["case"] for c in cases if c["outcome"] == "refused"]
        self.assertTrue(refused)
        self.assertTrue(all(c["observed_sha256"] == c["expected_sha256"] for c in cases), "code points still match")
        self.assertTrue(all(not c["equal_byte_for_byte"] for c in cases if c["outcome"] == "refused"))
        self.assertNotEqual(v, "PASS")
        self.assertEqual(v, "INCONCLUSIVE: decode refused for " + ", ".join(refused))
        self.assertTrue(any("open decision in Revision 10" in x for x in ev.doc["inconclusive"]))

    def test_d3_refused_exit_status_is_not_pass(self):
        orig = hv.run_suite
        hv.run_suite = lambda ctx: "INCONCLUSIVE: decode refused for B2-7-fffe"
        self.addCleanup(setattr, hv, "run_suite", orig)
        orig_connect, orig_assert = hv.connect, hv.assert_tls
        hv.connect, hv.assert_tls = (lambda *a, **k: None), (lambda *a, **k: None)
        self.addCleanup(setattr, hv, "connect", orig_connect)
        self.addCleanup(setattr, hv, "assert_tls", orig_assert)
        self.assertEqual(hv.main(good_env(HV_SUITE="D3")), 2, "a non-PASS verdict exits 2, so the run shows failed")

    def test_d1_platform_rejection_is_inconclusive(self):
        v, ev = self.run_probe_suite("D1", probe_reject_over=65536)
        cases = ev.doc["results"]["D1"]["cases"]
        rejected = [c["case"] for c in cases if c.get("platform_rejection_or_error")]
        self.assertEqual(rejected, ["D1-1048576-content-length", "D1-1048576-chunked", "D1-1048576-slow-chunked"])
        self.assertTrue(all(c["status"] == 413 and "chunks" not in c for c in cases if c["case"] in rejected))
        self.assertEqual(len([c for c in cases if "chunks" in c]), 12, "the other observations are kept")
        self.assertNotIn("PASS", v)
        self.assertEqual(v, "INCONCLUSIVE: chunk sizes not observed for " + ", ".join(rejected))
        self.assertTrue(any("not observed" in x for x in ev.doc["inconclusive"]))

    def test_d1_clean_run_returns_exact_pass(self):
        v, ev = self.run_probe_suite("D1")
        self.assertEqual(v, "PASS", "exactly PASS, the only verdict main() treats as success")
        cases = ev.doc["results"]["D1"]["cases"]
        self.assertEqual(len(cases), 15)
        self.assertTrue(all("chunks" in c and c["total_equals_sent"] for c in cases), "every observation kept")
        self.assertIsNotNone(ev.doc["results"]["D1"]["largest_chunk_delivered_for_bodies_over_8192"])
        self.assertEqual(ev.doc["inconclusive"], [])

    def _main_with_real_d1(self, **standin_kw):
        """main() with every safeguard as usual; only the network target is swapped for the local stand-in and the
        database identity step is skipped (D1 does not use the database). The real D1 logic decides the verdict."""
        st = standin.StandIn(origin=ORIGIN, **standin_kw)
        self.addCleanup(st.close)
        orig = (hv.run_suite, hv.connect, hv.assert_tls)

        def run_suite(ctx):
            ctx.target, ctx.db = hv.Target("http", "127.0.0.1", st.port), None
            return orig[0](ctx)
        hv.run_suite, hv.connect, hv.assert_tls = run_suite, (lambda *a, **k: None), (lambda *a, **k: None)
        try:
            env = good_env(HV_SUITE="D1")
            code = hv.main(env)
        finally:
            hv.run_suite, hv.connect, hv.assert_tls = orig
        d = env["HV_OUT"]
        ev = json.load(open(os.path.join(d, [f for f in os.listdir(d) if f.endswith(".json")][0])))
        return code, ev

    def test_d1_clean_run_exits_0(self):
        code, ev = self._main_with_real_d1()
        self.assertEqual(ev["verdict"], "PASS")
        self.assertEqual(code, 0)
        self.assertEqual(len(ev["results"]["D1"]["cases"]), 15, "observations are in the written evidence")
        self.assertEqual(len(ev["requests"]), 15)

    def test_d1_platform_rejection_exits_2(self):
        code, ev = self._main_with_real_d1(probe_reject_over=65536)
        self.assertTrue(ev["verdict"].startswith("INCONCLUSIVE: chunk sizes not observed for "), ev["verdict"])
        self.assertEqual(code, 2)
        self.assertEqual(len([c for c in ev["results"]["D1"]["cases"] if "chunks" in c]), 12)

    def test_non_pass_verdicts_exit_2(self):
        for verdict, code in (("PASS", 0), ("INCONCLUSIVE: chunk sizes not observed for D1-1048576-chunked", 2),
                              ("INCONCLUSIVE: decode refused for B2-7-fffe", 2), ("FAIL", 2)):
            orig = hv.run_suite
            hv.run_suite = lambda ctx, v=verdict: v
            orig_connect, orig_assert = hv.connect, hv.assert_tls
            hv.connect, hv.assert_tls = (lambda *a, **k: None), (lambda *a, **k: None)
            try:
                self.assertEqual(hv.main(good_env(HV_SUITE="D1")), code, verdict)
            finally:
                hv.run_suite, hv.connect, hv.assert_tls = orig, orig_connect, orig_assert


@unittest.skipUnless(PG and PG_ADMIN, "local PostgreSQL stand-in not configured (HV_TEST_PG_URL, HV_TEST_PG_ADMIN_URL)")
class Integration(unittest.TestCase):
    """Each suite end to end against the local PostgreSQL 17 stand-in and the HTTP stand-ins."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        with psycopg.connect(PG_ADMIN, autocommit=True) as c:
            c.execute("DELETE FROM public.analytics_rate")   # local stand-in only: start each test session with fresh allowances

    def mk(self, suite, **standin_kw):
        import psycopg
        kw = {"origin": ORIGIN, **standin_kw}
        st = standin.StandIn(ingest_connect=lambda: psycopg.connect(PG_ADMIN, autocommit=True, application_name="standin-ingest"), **kw)
        self.addCleanup(st.close)
        run_id = next_run_id()
        ev = hv.Evidence({"suite": suite, "run_id": run_id})
        conn = psycopg.connect(PG, autocommit=True, application_name="hv-test-main")
        self.addCleanup(conn.close)
        ctx = hv.Ctx(suite, run_id, ORIGIN, hv.Target("http", "127.0.0.1", st.port), ev, hv.DB(conn, ev, "main"), PG, None,
                     lambda url, app: psycopg.connect(url, autocommit=True, application_name=app))
        return ctx, st, ev

    def admin(self, sql, params=None):
        with self.psycopg.connect(PG_ADMIN, autocommit=True) as c:
            c.execute(sql, params)

    def write_evidence(self, ev):
        d = tempfile.mkdtemp()
        p1, p2 = ev.write(d)
        json.load(open(p1))
        return p1

    def test_readonly_snapshots_refuse_writes(self):
        ctx, _, _ = self.mk("P1-P2")
        with self.assertRaises(self.psycopg.errors.ReadOnlySqlTransaction):
            ctx.db.ro("SELECT public.analytics_rollup_pending()")

    def test_assert_tls_refuses_plaintext(self):
        ctx, _, _ = self.mk("P1-P2")
        with self.assertRaisesRegex(hv.Refused, "not using TLS"):
            hv.assert_tls(ctx.db.conn, "/x")

    def test_p1p2(self):
        ctx, st, ev = self.mk("P1-P2")
        self.assertEqual(hv.run_suite(ctx), "PASS")
        r = ev.doc["results"]
        self.assertEqual(r["P1"]["sequence"], [{"cache_size": 1, "increment_by": 1, "cycle": False}])
        self.assertEqual(r["P2"]["default_transaction_isolation"], "read committed")
        self.assertEqual({j["jobname"]: j["schedule"] for j in r["P2"]["cron_jobs"]}, hv.CRON_JOBS)
        self.assertEqual(st.requests, [], "P1-P2 sends no HTTP request")
        self.write_evidence(ev)

    def test_p2_override_is_a_finding(self):
        self.admin("ALTER ROLE service_role SET default_transaction_isolation = 'serializable'")
        try:
            ctx, _, ev = self.mk("P1-P2")
            self.assertEqual(hv.run_suite(ctx), "FAIL")
            self.assertTrue(ev.doc["results"]["P2"]["isolation_overrides"])
        finally:
            self.admin("ALTER ROLE service_role RESET default_transaction_isolation")

    def test_identity_refuses_wrong_state(self):
        self.admin("DELETE FROM supabase_migrations.schema_migrations WHERE version = '0501'")
        try:
            ctx, st, _ = self.mk("D4")
            with self.assertRaisesRegex(hv.Refused, "migration history"):
                hv.run_suite(ctx)
            self.assertEqual(st.requests, [], "no request when the database is not the expected project/state")
        finally:
            self.admin("INSERT INTO supabase_migrations.schema_migrations (version) VALUES ('0501')")

    def test_d1(self):
        ctx, st, ev = self.mk("D1")
        self.assertEqual(hv.run_suite(ctx), "PASS")
        cases = ev.doc["results"]["D1"]["cases"]
        self.assertEqual(len(cases), 15)
        for c in cases:
            self.assertEqual(c["status"], 200, c["case"])
            self.assertTrue(c["total_equals_sent"], c["case"])
            self.assertFalse(c["platform_added_or_changed_length"], c["case"])
        self.assertEqual([m for m, _ in st.requests], ["POST"] * 15)
        self.write_evidence(ev)

    def test_d1_d3_absent_probe(self):
        for s in ("D1", "D3"):
            ctx, st, ev = self.mk(s, probe=False)
            with self.assertRaisesRegex(hv.Refused, "not deployed"):
                hv.run_suite(ctx)
            self.assertEqual(len(st.requests), 1, "stops after the first request answers 404")

    def test_d3(self):
        ctx, st, ev = self.mk("D3")
        self.assertEqual(hv.run_suite(ctx), "PASS")
        cases = ev.doc["results"]["D3"]["cases"]
        self.assertEqual(len(cases), 8)
        self.assertTrue(all(c["equal_byte_for_byte"] for c in cases))
        self.assertTrue([c for c in cases if c["case"] == "B2-11-split"][0]["split_chars_decoded_once"])
        self.assertFalse(any(c["starts_with_bom"] for c in cases))
        p = self.write_evidence(ev)
        self.assertNotIn("XX", open(p).read().replace("XXXX", ""), "no request body in evidence")

    def test_d4(self):
        ctx, st, ev = self.mk("D4")
        v = hv.run_suite(ctx)
        self.assertEqual(v, "NOT FULLY PASSED: all four as expected; B9000 function-log evidence unavailable")
        rs = {w["request"]: w for w in ev.doc["results"]["D4"]["requests"]}
        self.assertEqual([w["result"] for w in rs.values()], ["as expected"] * 4)
        self.assertEqual(rs["B8192"]["stats_delta_test"], {"accepted": 1})
        self.assertEqual(rs["B8193"]["stats_delta_test"], {"rejected:bad_batch": 1})
        self.assertEqual(rs["BLIE"]["stats_delta_test"], {"rejected:bad_batch": 1})
        self.assertEqual(rs["B9000"]["stats_delta_test"], {})
        self.assertEqual(rs["B8192"]["http"]["content_length_sent"], None)
        self.assertEqual(rs["BLIE"]["http"]["content_length_sent"], 100)
        self.assertEqual(rs["BLIE"]["http"]["body_bytes"], 20480)
        self.assertEqual(ev.doc["results"]["D4"]["net_change"], {"events": 1, "bad_batch": 2})
        self.assertIn("analytics-ingest: too large", st.logs)
        self.assertEqual(st.requests[0][0], "OPTIONS")
        self.write_evidence(ev)

    def test_d4_absent_receiver_or_wrong_origin(self):
        for kw in ({"receiver": False}, {"origin": "https://other.example"}):
            ctx, st, ev = self.mk("D4", **kw)
            with self.assertRaisesRegex(hv.Refused, "absent, or does not allow"):
                hv.run_suite(ctx)
            self.assertEqual([m for m, _ in st.requests], ["OPTIONS"], "no POST when the receiver is absent")

    def test_d4_window_closed(self):
        ctx, st, ev = self.mk("D4", enabled=False)
        self.assertEqual(hv.run_suite(ctx), "INCONCLUSIVE")
        self.assertEqual([m for m, _ in st.requests], ["OPTIONS", "POST"], "stops after B8192 shows nothing")
        self.assertTrue(any("switch may be off" in x for x in ev.doc["inconclusive"]))

    def test_d4_no_headroom_sends_nothing(self):
        import psycopg
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f"INSERT INTO public.analytics_rate_salt (day) VALUES ({hv.TODAY_SQL}) ON CONFLICT DO NOTHING")
            salt = c.execute(f"SELECT salt::text FROM public.analytics_rate_salt WHERE day = {hv.TODAY_SQL}").fetchone()[0]
        key = f"(sha256(convert_to('{salt}|unknown', 'UTF8')))"
        win = "date_bin('10 minutes', now(), timestamptz '2000-01-01')"
        self.admin(f"INSERT INTO public.analytics_rate (kind, key, win_start, n) VALUES ('net_10min', {key}, {win}, 300) "
                   f"ON CONFLICT (kind, key, win_start) DO UPDATE SET n = 300")
        try:
            ctx, st, ev = self.mk("D4")
            self.assertEqual(hv.run_suite(ctx), "INCONCLUSIVE")
            w = ev.doc["results"]["D4"]["requests"][0]
            self.assertIn("unknown_10min", w["outcome"])
            self.assertEqual([m for m, _ in st.requests], ["OPTIONS"], "no POST when a limit could refuse it")
        finally:
            self.admin(f"DELETE FROM public.analytics_rate WHERE kind = 'net_10min' AND key = {key} AND win_start = {win}")

    def test_d2(self):
        ctx, st, ev = self.mk("D2")
        v = hv.run_suite(ctx)
        self.assertEqual(v, "O: 1. Rejected, nothing inserted; C: accepted, exactly one row; cancellation: not verifiable on the hosted platform")
        o = ev.doc["results"]["D2"]["O"]
        self.assertEqual(o["bytes"]["intended_total"], 10485760)
        self.assertIsNotNone(o["bytes"]["written_when_response_arrived"])
        self.assertLess(o["bytes"]["final_written"], 10485760, "the stand-in abandoned the upload")
        self.assertIn(o["bytes"]["sender_end_state"], ("closed_by_peer", "reset", "stopped_after_fixed_period"))
        self.assertNotEqual(o["event_ids"], ev.doc["results"]["D2"]["C"]["event_ids"])
        self.assertNotRegex(o["event_ids"][0], hv.Ids("D2", ctx.run_id).run_suffix() + "$", "O is random, not the fixed format")
        self.write_evidence(ev)

    def test_p3_without_query_visibility_is_inconclusive(self):
        self.admin("REVOKE pg_read_all_stats FROM postgres")
        ctx, st, ev = self.mk("P3")
        v = hv.run_suite(ctx)
        r = ev.doc["results"]["P3"]
        self.assertEqual(v, "INCONCLUSIVE")
        self.assertFalse(r["waiter"]["identified_as_analytics_ingest"])
        self.assertTrue(any("not inferred from http timing" in x.lower() for x in ev.doc["limitations"]))
        self.assertTrue(r["l1_id_above_watermark"] and r["next_run_reopened_today"] and r["consistency"]["ok"])

    def test_p3_with_query_visibility(self):
        self.admin("GRANT pg_read_all_stats TO postgres")
        try:
            ctx, st, ev = self.mk("P3")
            v = hv.run_suite(ctx)
            r = ev.doc["results"]["P3"]
            w = r["waiter"]
            print("\n    P3 timing:", json.dumps({**w, "a_total_hold_s": r["a_total_hold_s"]}), "verdict", v, file=sys.stderr)
            self.assertEqual(r["hold_outcome"], "held")
            self.assertTrue(w["identified_as_analytics_ingest"], "the waiting session is the ingest (pg_stat_activity)")
            self.assertTrue(r["pg_locks_snapshot_shows_a_and_ingest"])
            snap = r["pg_locks_snapshot_during_hold"]
            self.assertIn({"session": "A", "granted": True}, [{k: x[k] for k in ("session", "granted")} for x in snap])
            self.assertTrue(any(x["session"] == "waiter" and x["granted"] is False and x["query_is_ingest"] for x in snap))
            # L1 is confirmed waiting BEFORE the 1.5 s is measured, and the hold is then demonstrated in full
            tl = r["timeline_ms"]
            self.assertLess(tl["rollup_returned"], tl["first_waiter_seen"])
            self.assertLess(tl["first_waiter_seen"], tl["hold_demonstrated"])
            self.assertLessEqual(tl["hold_demonstrated"], tl["commit"])
            self.assertGreaterEqual(w["hold_demonstrated_s_after_first_wait"], hv.HOLD_S)
            # the literal Revision 10 rule, from pg_locks sample times only
            self.assertGreaterEqual(w["observed_wait_s_lower_bound"], hv.HOLD_S)
            self.assertLess(w["observed_wait_s_upper_bound"], hv.LOCK_TIMEOUT_S)
            self.assertTrue(r["rule_wait_ge_hold_and_lt_3s"])
            self.assertTrue(r["a_total_hold_under_3s"])
            self.assertTrue(r["session_a_lock_granted_seen"])
            self.assertTrue(r["l1_id_above_watermark"])
            self.assertTrue(r["next_run_reopened_today"])
            self.assertTrue(r["consistency"]["ok"])
            self.assertEqual(r["http"]["status"], 204)
            self.assertEqual(r["stats_delta_test"], {"accepted": 1}, "accepted, not busy")
            self.assertEqual(v, "PASS")
            self.write_evidence(ev)
        finally:
            self.admin("REVOKE pg_read_all_stats FROM postgres")

    def test_p3_request_after_release_is_inconclusive(self):
        ctx, st, ev = self.mk("P3", latency_s=2.0)     # reaches the database after A's waiting window has closed
        self.assertEqual(hv.run_suite(ctx), "INCONCLUSIVE")
        r = ev.doc["results"]["P3"]
        self.assertEqual(r["hold_outcome"], "no waiter seen in time")
        self.assertTrue(r["a_total_hold_under_3s"])
        self.assertTrue(any("arrived after A committed" in x for x in ev.doc["inconclusive"]))

    def test_p3_competing_waiter_is_inconclusive(self):
        import psycopg
        self.admin("GRANT pg_read_all_stats TO postgres")
        stop = threading.Event()

        def competitor():
            with psycopg.connect(PG_ADMIN, autocommit=True, application_name="competing-waiter") as c:
                while not stop.is_set():          # wait until A holds the analytics lock, then queue for it too
                    if c.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND objid = %s",
                                 (hv.lock_parts(hv.ANALYTICS_LOCK_KEY)[1],)).fetchone()[0]:
                        break
                    time.sleep(0.005)
                c.execute("BEGIN")
                c.execute("SET LOCAL lock_timeout = '5s'")
                c.execute("SELECT pg_advisory_xact_lock(%s)", (hv.ANALYTICS_LOCK_KEY,))
                c.execute("COMMIT")
        th = threading.Thread(target=competitor, daemon=True)
        th.start()
        try:
            ctx, st, ev = self.mk("P3")
            v = hv.run_suite(ctx)
            self.assertEqual(v, "INCONCLUSIVE")
            self.assertGreaterEqual(ev.doc["results"]["P3"]["waiters"], 2)
            self.assertTrue(any("more than one session waited" in x for x in ev.doc["inconclusive"]))
        finally:
            stop.set()
            th.join(10)
            self.admin("REVOKE pg_read_all_stats FROM postgres")

    def _p4(self, simulate_cron):
        import psycopg
        self.admin("DELETE FROM public.analytics_rate")      # local stand-in only: fresh allowances for each P4 run
        ctx, st, ev = self.mk("P4")
        stop = threading.Event()

        def cron():
            time.sleep(4)
            if stop.is_set() or not simulate_cron:
                return
            with psycopg.connect(PG_ADMIN, autocommit=True) as c:
                t0 = c.execute("SELECT now()").fetchone()[0]
                c.execute("SELECT public.analytics_rollup_pending()")
                c.execute("""INSERT INTO cron.job_run_details (jobid, status, start_time, end_time, command)
                             SELECT jobid, 'succeeded', %s, now(), command FROM cron.job WHERE jobname = 'analytics-rollup-recent'""", (t0,))
        th = threading.Thread(target=cron, daemon=True)
        th.start()
        idn = hv.db_identity(ctx.db)
        hv.check_identity(idn, "test")
        v = hv.suite_p4(ctx, duration_s=40)      # 63 requests in 40 s: about 95 a minute, under the suite's 100/min guard
        stop.set()
        th.join()
        return v, ev

    def test_p4(self):
        v, ev = self._p4(True)
        r = ev.doc["results"]["P4"]
        self.assertEqual(v, "PASS", json.dumps(r["checks"]))
        self.assertEqual(r["done"]["events_sent"], 250)
        self.assertEqual(r["stats_delta_test"].get("accepted"), r["done"]["unique"])
        self.assertEqual(r["stats_delta_test"].get("duplicate"), r["done"]["repeats"])
        self.write_evidence(ev)

    def test_p4_without_a_cron_run_fails(self):
        v, ev = self._p4(False)
        self.assertEqual(v, "FAIL")
        self.assertFalse(ev.doc["results"]["P4"]["checks"]["cron_succeeded_in_window"])


REAL_JS = os.environ.get("HV_TEST_REAL_RECEIVER_JS")


@unittest.skipUnless(PG and PG_ADMIN and REAL_JS and os.environ.get("NODE_PATH"),
                     "real receiver cross-check not configured (HV_TEST_REAL_RECEIVER_JS, NODE_PATH with the npm package pg)")
class RealReceiver(Integration):
    """D4, D2 and P3 against the UNMODIFIED receiver handler (gearhead-labs _shared/analytics-ingest.js, 5687519b...)."""

    def mk(self, suite, enabled=True, **_):
        import psycopg
        p = subprocess.Popen(["node", os.path.join(HERE, "real_receiver.mjs"), REAL_JS, PG_ADMIN, ORIGIN], stdout=subprocess.PIPE,
                             text=True, env={**os.environ, "RR_ENABLED": "true" if enabled else "false"})
        self.addCleanup(p.kill)
        self.rr_proc = p
        line = p.stdout.readline().strip()
        self.assertTrue(line.startswith("PORT "), line)
        run_id = next_run_id()
        ev = hv.Evidence({"suite": suite, "run_id": run_id})
        conn = psycopg.connect(PG, autocommit=True, application_name="hv-test-main")
        self.addCleanup(conn.close)
        ctx = hv.Ctx(suite, run_id, ORIGIN, hv.Target("http", "127.0.0.1", int(line.split()[1])), ev, hv.DB(conn, ev, "main"), PG,
                     None, lambda url, app: psycopg.connect(url, autocommit=True, application_name=app))
        return ctx, _Requests(), ev

    def test_d4(self):
        ctx, _, ev = self.mk("D4")
        v = hv.run_suite(ctx)
        rs = {w["request"]: w for w in ev.doc["results"]["D4"]["requests"]}
        for r in ("B8192", "B8193", "BLIE", "B9000"):
            self.assertEqual(rs[r]["result"], "as expected", r)
            self.assertEqual(rs[r]["http"]["status"], 204, r)
        self.assertEqual(rs["BLIE"]["http"]["content_length_sent"], 100)
        self.assertEqual(rs["BLIE"]["body_bytes"], 20480)
        self.assertEqual(rs["BLIE"]["stats_delta_test"], {"rejected:bad_batch": 1})
        self.assertEqual(sum(len(x) for x in rs["BLIE"]["after"]["rows"].values()), 0, "no BLIE row")
        self.assertEqual(rs["B8192"]["stats_delta_test"], {"accepted": 1})
        self.assertEqual(rs["B8193"]["stats_delta_test"], {"rejected:bad_batch": 1})
        self.assertEqual(rs["B9000"]["stats_delta_test"], {})
        self.assertEqual(ev.doc["results"]["D4"]["net_change"], {"events": 1, "bad_batch": 2})
        self.assertEqual(v, "NOT FULLY PASSED: all four as expected; B9000 function-log evidence unavailable")
        # the trailing 20,380 bytes still produced a parse error in the harness: logged, and answered only after the 204
        self.rr_proc.kill()
        log = self.rr_proc.stdout.read()
        self.assertIn("CLIENT_ERROR HPE_CLOSED_CONNECTION bytesParsed=", log)
        self.assertIn("inflight=true", log)

    def test_d4_window_closed(self):
        ctx, _, ev = self.mk("D4", enabled=False)
        self.assertEqual(hv.run_suite(ctx), "INCONCLUSIVE")

    def test_d2(self):
        ctx, _, ev = self.mk("D2")
        self.assertEqual(hv.run_suite(ctx), "O: 1. Rejected, nothing inserted; C: accepted, exactly one row; "
                                            "cancellation: not verifiable on the hosted platform")
        print("\n    D2 real-receiver bytes:", json.dumps(ev.doc["results"]["D2"]["O"]["bytes"]), file=sys.stderr)

    def test_p3_with_query_visibility(self):
        Integration.test_p3_with_query_visibility(self)

    # suites that do not involve the receiver, or need stand-in features, are covered by Integration
    test_p1p2 = test_p2_override_is_a_finding = test_identity_refuses_wrong_state = None
    test_d1 = test_d1_d3_absent_probe = test_d3 = test_d4_absent_receiver_or_wrong_origin = test_d4_no_headroom_sends_nothing = None
    test_p3_without_query_visibility_is_inconclusive = test_p3_request_after_release_is_inconclusive = None
    test_p4 = test_p4_without_a_cron_run_fails = test_readonly_snapshots_refuse_writes = test_assert_tls_refuses_plaintext = None


class _Requests:
    requests, logs = [], []


if __name__ == "__main__":
    unittest.main(verbosity=2)
