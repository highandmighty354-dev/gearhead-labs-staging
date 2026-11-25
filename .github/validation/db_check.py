#!/usr/bin/env python3
"""Gearhead Labs staging validation: judge the database checks. No network, no secret, standard library only.

  db_check.py audit LIVE.json REPLICA.json EXPECTED.json   protection audit: live == replica built from the files, section by section
  db_check.py matrix STDERR PSQL_RC BEFORE.json EXPECTED.json   access matrix: the run ended on the sentinel and every cell is as expected
  db_check.py counts BEFORE.json AFTER.json                  nothing left behind: identical counts, no test user

Exit 0 = PASS. Any difference, missing value or unreadable input prints "HARD FAIL: ..." and exits 1. There is no warning level.
"""
import json, re, sys

failures = []


def fail(msg):
    failures.append(msg)


def done(label):
    if failures:
        for f in failures:
            print("HARD FAIL: " + f, file=sys.stderr)
        print(f"{label}: FAILED ({len(failures)} problem(s))")
        sys.exit(1)
    print(f"{label}: PASS")
    sys.exit(0)


def load_json_line(path, key):
    """psql -At prints the single JSON value; accept exactly one non-empty line."""
    with open(path) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    if len(lines) != 1:
        raise SystemExit(f"HARD FAIL: {path} must hold exactly one line of output, found {len(lines)}")
    obj = json.loads(lines[0])
    if key and isinstance(obj, dict) and key in obj and len(obj) == 1:
        obj = obj[key]
    return obj


def audit(live_p, replica_p, expected_p):
    live, rep, exp = load_json_line(live_p, "audit"), load_json_line(replica_p, "audit"), json.load(open(expected_p))
    sections = ["tables", "rls", "views", "policies", "table_grants", "column_grants", "functions", "anon_executable_functions",
                "triggers", "columns", "constraints", "enums", "reference_data"]
    for s in sections:
        if s not in live or s not in rep:
            fail(f"section {s!r} missing from the {'live' if s not in live else 'replica'} audit")
            continue
        a, b = live[s], rep[s]
        if isinstance(a, list):
            if len(a) != len(set(a)):
                fail(f"{s}: the live list has duplicates")
            extra, missing = sorted(set(a) - set(b)), sorted(set(b) - set(a))
            for x in extra[:20]:
                fail(f"{s}: present on staging but not created by the migration files: {x}")
            for x in missing[:20]:
                fail(f"{s}: created by the migration files but missing on staging: {x}")
            if extra[20:] or missing[20:]:
                fail(f"{s}: {len(extra)} extra and {len(missing)} missing in total")
            print(f"  {s}: {len(a)} item(s), {'identical' if not extra and not missing else 'DIFFERENT'}")
        elif a != b:
            fail(f"{s}: staging {a} != files {b}")
        else:
            print(f"  {s}: identical ({len(a)} table(s))")
    rls = live.get("rls", [])
    bad = [r for r in rls if ":rls=true:force=true" not in r]
    for r in bad:
        fail(f"table without forced row-level security: {r}")
    if live.get("views"):
        fail(f"views exist in the exposed schema; each must be reviewed: {live['views']}")
    if sorted(live.get("tables", [])) != sorted(exp["exposed_tables"]):
        fail(f"exposed tables {live.get('tables')} differ from the pinned list used by the web-API probes {exp['exposed_tables']}")
    for g in live.get("table_grants", []):
        t, who, priv = g.split("|")
        if who in ("anon", "PUBLIC"):
            fail(f"table privilege granted to {who}: {g}")
        if priv in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            fail(f"unexpected privilege for {who}: {g}")
        if priv in ("INSERT", "UPDATE") and t in ("formula_versions", "calculation_records", "entitlement_grants",
                                                     "subscriptions", "billing_customers", "stripe_events"):
            fail(f"client role may write {t}: {g}")
    for f in live.get("functions", []):
        name, secdef, cfg = f.split("|")[0], "|secdef=true|" in f, f.split("|cfg=")[1].split("|")[0]
        if secdef and 'search_path=""' not in cfg:
            fail(f"owner-rights function without a pinned empty search_path: {f}")
    # Explicit policy for anonymous EXECUTE, judged on its own: it fails even when the migration files themselves (and so
    # the replica) grant the same thing.
    policy = exp.get("anon_function_policy") or {}
    allowed = policy.get("allowed_trigger_functions")
    if not isinstance(allowed, list) or not allowed:
        fail("expected.json has no anon_function_policy.allowed_trigger_functions list")
        allowed = []
    if "anon_executable_functions" not in live:
        fail("the live audit has no anon_executable_functions section")
    seen = set()
    for f in live.get("anon_executable_functions", []):
        parts = f.split("|")
        sig, returns = parts[0], (parts[1][len("returns="):] if len(parts) > 1 and parts[1].startswith("returns=") else None)
        seen.add(sig)
        if returns != "trigger":
            fail(f"anonymous callers can execute {sig}, which returns {returns!r}, not a trigger: it is callable through the web API")
        elif sig not in allowed:
            fail(f"anonymous callers can execute trigger function {sig}, which is not in the reviewed policy")
    for sig in allowed:
        if sig not in seen:
            fail(f"policy lists {sig} as anonymous-executable but staging does not grant it: update the reviewed policy")
    print(f"  anon_executable_functions: {len(seen)} function(s), all trigger functions in the reviewed policy"
          if not [x for x in failures if "anonymous" in x or "policy lists" in x] else "  anon_executable_functions: POLICY VIOLATION")
    done("protection audit (staging == migration files)")


def matrix(stderr_p, rc, before_p, expected_p):
    exp = json.load(open(expected_p))
    before = load_json_line(before_p, "counts")
    err = open(stderr_p).read()
    if rc == 0:
        raise SystemExit("HARD FAIL: the matrix ended without its sentinel error, so its writes may have committed: treat staging as unknown")
    sentinels = re.findall(r"ERROR:\s+GH_TEST_SENTINEL (\{.*\})\s*$", err, re.M)
    others = [l for l in err.splitlines() if "ERROR:" in l and "GH_TEST_SENTINEL" not in l]
    if len(sentinels) != 1 or others:
        raise SystemExit(f"HARD FAIL: the matrix did not end on exactly one sentinel (sentinels {len(sentinels)}, other errors {others[:3]})")
    res = json.loads(sentinels[0])
    applied_0410 = exp["migration_0410"]["version"] in before["migrations"]
    ref_totals = {t: before["tables"][t] for t in exp["reference_tables"]}
    seeded = exp["seeded_rows_per_user"]

    def want_write(cell, expected):
        return cell == expected if expected.startswith("OK") else (cell == expected or cell.startswith(expected + " "))

    for who, tier in (("anon", "anon"), ("A", "premium"), ("B", "premium"), ("C", "free")):
        r = res.get(who)
        if not isinstance(r, dict):
            fail(f"no results for caller {who}")
            continue
        if r.get("uid_ok") is not True:
            fail(f"{who}: auth.uid() was {r.get('uid')!r}, not the simulated identity")
        for t in exp["customer_tables"] + exp["reference_tables"]:
            cell = r.get("read:" + t)
            if who == "anon" or t in exp["unreadable_by_signed_in"]:
                if cell != "ERR 42501":
                    fail(f"{who} read {t}: expected permission denied, got {cell}")
                continue
            if not isinstance(cell, dict):
                fail(f"{who} read {t}: expected rows, got {cell}")
                continue
            if t in exp["reference_tables"]:
                if cell.get("total") != ref_totals[t]:
                    fail(f"{who} read {t}: sees {cell.get('total')} row(s), the table holds {ref_totals[t]}")
                continue
            if who in ("A", "B"):
                n = seeded.get(t, seeded["_default"])
                if (cell.get("own"), cell.get("other"), cell.get("total")) != (n, 0, n):
                    fail(f"{who} read {t}: expected own {n}, other 0 (positive control), got {cell}")
            else:  # C owns only its account and profile
                n = 1 if t in ("accounts", "profiles") else 0
                if (cell.get("own"), cell.get("other"), cell.get("total")) != (n, 0, n):
                    fail(f"C read {t}: expected own {n}, other 0, got {cell}")
        w = r.get("writes") or {}
        ew = exp["writes"][tier]
        if set(w) != set(ew):
            fail(f"{who} writes: attempted {sorted(w)} but expected {sorted(ew)}")
        for k, e in ew.items():
            if k in w and not want_write(w[k], e):
                fail(f"{who} {k}: expected {e}, got {w[k]}")
    c2 = res.get("C2") or {}
    fps = exp["migration_0410"]["new_fingerprints"]
    if not c2:
        fail("no formula-version compatibility results (C2)")
    for calc in fps:
        new_own = c2.get(f"{calc}:new_own")
        if applied_0410 and new_own != "accepted":
            fail(f"C2 {calc}: 0410 is recorded, so its new fingerprint must be accepted for its own calculator, got {new_own}")
        if not applied_0410 and new_own != "refused 23503":
            fail(f"C2 {calc}: 0410 is not recorded, so its new fingerprint must be refused, got {new_own}")
        if c2.get(f"{calc}:new_row_present") is not applied_0410:
            fail(f"C2 {calc}: new formula-version row present = {c2.get(f'{calc}:new_row_present')}, 0410 recorded = {applied_0410}")
        if c2.get(f"{calc}:new_other") != "refused 23503":
            fail(f"C2 {calc}: new fingerprint under another calculator must be refused, got {c2.get(f'{calc}:new_other')}")
        old_own = [v for k, v in c2.items() if k.startswith(f"{calc}:old_own(")]
        old_other = [v for k, v in c2.items() if k.startswith(f"{calc}:old_other(")]
        if old_own != ["accepted"]:
            fail(f"C2 {calc}: existing fingerprint must stay accepted for its own calculator, got {old_own}")
        if old_other != ["refused 23503"]:
            fail(f"C2 {calc}: existing fingerprint under another calculator must be refused, got {old_other}")
    c3 = res.get("C3") or {}
    for k in ("owner_update", "owner_delete", "owner_truncate"):
        if not str(c3.get(k, "")).startswith("refused P0001 DF_IMMUTABLE"):
            fail(f"C3 {k}: formula versions must refuse this even for the owner, got {c3.get(k)}")
    want_fv = exp["migration_0410"]["formula_versions_with" if applied_0410 else "formula_versions_without"]
    if res.get("fv_count_inside") != want_fv or before["tables"].get("formula_versions") != want_fv:
        fail(f"formula versions: expected {want_fv} (0410 recorded: {applied_0410}), "
             f"inside {res.get('fv_count_inside')}, before {before['tables'].get('formula_versions')}")
    print(f"  callers checked: anon, A, B, C; 0410 recorded: {applied_0410}; formula versions {want_fv}")
    done("access matrix and formula versions")


def counts(before_p, after_p):
    b, a = load_json_line(before_p, "counts"), load_json_line(after_p, "counts")
    if a.get("test_users") != 0 or b.get("test_users") != 0:
        fail(f"validation test users exist (before {b.get('test_users')}, after {a.get('test_users')})")
    for k in ("auth_users", "tables", "migrations"):
        if b.get(k) != a.get(k):
            fail(f"{k} changed during validation: before {b.get(k)} after {a.get(k)}")
    print(f"  users {a.get('auth_users')}, migrations {len(a.get('migrations') or [])}, tables {len(a.get('tables') or {})}")
    done("nothing left behind")


if __name__ == "__main__":
    try:
        cmd = sys.argv[1]
        if cmd == "audit" and len(sys.argv) == 5:
            audit(*sys.argv[2:5])
        elif cmd == "matrix" and len(sys.argv) == 6:
            matrix(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5])
        elif cmd == "counts" and len(sys.argv) == 4:
            counts(*sys.argv[2:4])
        else:
            raise SystemExit("HARD FAIL: usage: db_check.py audit|matrix|counts ...")
    except SystemExit:
        raise
    except Exception as e:  # unreadable or malformed input is a failure, never a pass
        raise SystemExit(f"HARD FAIL: {type(e).__name__}: {e}")
