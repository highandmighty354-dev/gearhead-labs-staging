# Hosted verification (A4)

The manual, approval-gated workflow that runs the hosted TEST tests of the **Hosted TEST Verification Procedure,
revision 10** (`Hosted-TEST-Verification-Procedure-C1-B1-revision-10.md`, SHA-256 `7df81020…c482`) against the staging
project. **A4 only adds this capability.** Running any suite is a separate approval (A10). Nothing here deploys,
configures or opens anything.

- Workflow: `.github/workflows/hosted-verification.yml`
- Runner: `hv.py`; shell checks: `checks.sh`; pinned database driver: `requirements.txt`; D3 expectations: `d3-expected.json`
- Local tests and stand-ins: `tests/`

## What one run does

1. **Preflight** (no secret, no approval, **no HTTP request**). It checks:
   - manual start from `main`, first attempt;
   - the typed ref, with the same four-way target check the migration workflow uses (production refused);
   - `ANALYTICS_TEST_ORIGIN` and the suite's prerequisites;
   - the `staging-db` Environment: only the owner may approve, and only `main` may use it;
   - that no other staging migration, validation or hosted-verification run is queued or running;
   - TLS of the database pooler against the pinned Supabase roots.
2. **Verify** waits for the owner's approval in `staging-db`. It then:
   - repeats every check above;
   - installs the hash-pinned `psycopg`;
   - re-checks TLS;
   - in the **one step that receives the password**, builds the masked `verify-full` connection string exactly as the
     migration workflow does and runs the selected suite.
3. **Evidence** is uploaded as the run's artifact (always, also after a refusal). It holds two files:
   - one timestamped JSON evidence file;
   - one raw client log (sizes and timings only).

Before writing, the runner refuses to save anything that contains:
- the password or connection string;
- a key-like string;
- an IP address.

Request bodies are never recorded.

Exit status: `0` = PASS, `2` = the suite ran and the verdict is anything else, `1` = refused or error. A run that is not
PASS shows red, so its verdict gets read.

## Suites (one per run; no "all" option)

| Suite | Needs | What it does | Database writes by the workflow |
|---|---|---|---|
| P1-P2 | nothing | Read-only:<br>• sequence (cache, increment, cycle, serial identity)<br>• `version()`<br>• default isolation<br>• overrides for `postgres`, `authenticator`, `service_role` and the database<br>• both `cron.job` rows and schedules | none |
| D1 | A9 probe | Probe `chunks` mode: 1 KB, 8,192, 8,193, 64 KB and 1 MB. Each is sent three ways:<br>• honest `Content-Length`<br>• chunked<br>• slow chunked (4 KB every 100 ms) | none |
| D3 | A9 probe | Probe `decode` mode:<br>• the five B2-7 invalid sequences<br>• the leading BOM<br>• the B2-7 batch with BOM and invalid bytes<br>• the B2-11 character split across chunks | none |
| D4 | A7 receiver, A8 window | B8192, B8193, BLIE, B9000. Each in its own window with:<br>• headroom check<br>• before/after snapshots<br>• event-ID check<br>• exact `analytics_ingest_stats` delta | none |
| D2 | A7, A8 | Two windows:<br>• **O**: 10,485,760 B, chunked, 64 KB every 100 ms, 30 s after the response<br>• **C**: 4,096 B with its true length | none |
| P3 | A7, A8 | • Session A: `analytics_rollup(today)`, then holds 1.5 s<br>• L1 sent through the receiver during the hold<br>• a monitor session watches `pg_locks`<br>• then `analytics_rollup_pending()` | `analytics_rollup(today)`, `analytics_rollup_pending()` |
| P4 | A7, A8 | About 20 minutes, 250 events in 63 requests (about 3 a minute), repeats included<br>• `cron.job_run_details` for the window<br>• final pending rollup | `analytics_rollup_pending()` |

Every event goes through the HTTP receiver. The runner never inserts into an analytics table. The P3 fallback (a direct
`analytics_ingest()` call) is **not implemented**: this workflow has the HTTP client, and a silent fallback would hide a
receiver problem.

## Owner-set repository variables (not set by A4)

| Variable | Value | Set when |
|---|---|---|
| `ANALYTICS_TEST_ORIGIN` | the TEST website origin, e.g. `https://host` (owner's choice; never production) | after the owner chooses it; must equal the receiver's `ANALYTICS_ALLOWED_ORIGINS` (A7) |
| `HV_PROBE_STATUS` | exactly `deployed-under-A9` | after A9 deploys the probe; clear it when A9 deletes it |
| `HV_RECEIVER_STATUS` | exactly `deployed-under-A7` | after the A7 deploy |
| `HV_TEST_WINDOW` | exactly `open-under-A8` | after A8 opens the window; clear it at A11 close |

These are **attestations**: the workflow cannot read function settings or the deployed source, because it has no
Supabase access token.

After approval, it checks what it can:
- **Receiver:** an `OPTIONS` preflight must answer 204 and allow the TEST origin. This happens before the kill switch,
  with no body read and no database call.
- **Probe:** the first probe request must not answer 404.
- **Window:** cannot be checked without an ingest. If B8192 returns 204 with no effect, D4 stops and reports "not
  determined (the switch may be off)".

## Synthetic IDs (T10)

Fixed format: `0a000000-<suite>-4<seq>-8<kind>-<run id as 12 hex digits>`.
- Suite codes: P1-P2 `0012`, D1 `00d1`, D3 `00d3`, D4 `00d4`, D2 `00d2`, P3 `00f3`, P4 `00f4`.
- Kinds: event 0, vid 1, sid 2, pv 3.

The run ID makes every ID unique to the run, and re-runs are refused. Every window also checks that its IDs do not
already exist. D2's **O** and **C** are two separately generated random IDs in the same `0a000000-` range. `env` is
always `test`. The User-Agent is a plain browser string so the receiver does not flag the synthetic events as bots.

## Probe contract (A9 must match it; the runner checks it)

Request: `POST https://<TEST_REF>.supabase.co/functions/v1/b1-chunk-probe`, with these headers:
- `x-probe-run: hv-<run id>`
- `x-probe-mode: chunks | decode`

Anything else gets a 404.

Responses (`200`, JSON):
- **chunks:** `{"mode":"chunks","chunks":[<byte length per chunk>],"total":n,"declared_length":n|null,"ms_to_first_chunk":x|null}`
- **decode:** `{"mode":"decode","outcome":"accepted"|"refused","oversize":bool,"bytes":n,"code_points":[<integers>]}`,
  decoded by the unchanged `readBodyLimited()`.

A reply in another shape is reported as **not determined**. Revision 10 doesn't define this wire format, so **this
contract is an A4 assumption for the owner to confirm in A9.**

## Known limitations (also written into every evidence file that they affect)

- **P3 timing:** Revision 10's rule is literally "wait ≥ hold (1.5 s) and < 3 s". L1 is sent when the hold starts, so
  the ingest starts waiting only after the network and platform delay, and its wait is shorter than the hold.
  - Locally (about 20–40 ms delay) the measured wait was 1.461–1.581 s, so the literal rule reports FAIL even though
    the lock behaved correctly.
  - This is implemented literally and not reinterpreted. It needs an owner decision before A10.
- **P3 identification:** this needs the database user to see other sessions' query text. Without it, P3 is
  INCONCLUSIVE; a lock wait is never inferred from HTTP timing.
- **D4 BLIE:** over HTTP/1.1, `Content-Length: 100` followed by 20 KB makes the extra bytes look like a second,
  malformed request.
  - Locally, the unmodified receiver behind Node's HTTP server still counted `bad_batch` +1, but the client saw
    **HTTP 400**, not 204.
  - The status is reported literally, so D4 then reports `FAIL: BLIE (HTTP status 400 …)`. Owner decision.
- **D4 B9000:** this workflow cannot read Edge Function logs. D4 is therefore reported as "NOT FULLY PASSED … B9000
  function-log evidence unavailable" at best.
- **D2:** cancellation is **not verifiable on the hosted platform** (Revision 10). The function logs for the O and C
  windows are unavailable.
- **P2:** shows defaults only.

## Local tests (no network beyond 127.0.0.1)

The local setup is a PostgreSQL 17 database named `postgres`, initialised in this order:
1. `premium-foundation/tests/sql/000_hosted_supabase_shim.sql`, as `supabase_admin`;
2. the 21 migrations, as `postgres`;
3. `tests/standin_db.sql`, as `supabase_admin`.

Then run:

```
python3 .github/hosted-verification/tests/test_hv.py           # unit + main() refusals (+ integration if configured)
HV_TEST_PG_URL=postgresql://postgres@127.0.0.1:PORT/postgres \
HV_TEST_PG_ADMIN_URL=postgresql://supabase_admin@127.0.0.1:PORT/postgres python3 .github/hosted-verification/tests/test_hv.py
bash .github/hosted-verification/tests/test_checks.sh          # checks.sh, with a fake GitHub API
python3 .github/hosted-verification/tests/test_workflow.py     # workflow structure
```

The optional `RealReceiver` tests run D4, D2 and P3 against the **unmodified** receiver. They need two settings:
- `HV_TEST_REAL_RECEIVER_JS`: the path to gearhead-labs `supabase/functions/_shared/analytics-ingest.js`
  (`5687519b…`, checked);
- `NODE_PATH`: the location of the npm package `pg`.
