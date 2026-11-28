# Gearhead Labs Analytics — Hosted TEST Verification Procedure for C1/B1 (revision 11, for review)

2026-11-27 · Trent · **Preparation only. Nothing here is authorized.** No Supabase project was contacted, no credential was read, and no file in either repository was changed while writing this.

**Controlling reference:** the approved C1/B1 reproducibility procedure and hosted TEST verification plan, **revision 3**: `C1-B1-hosted-test-and-reproducibility-plan.md`, SHA-256 `32e68dd819f80a63210e13b9543aad970c6ed5640bbeb848522fa14a88ea9a25`. This document does not override it. The approval record for that exact hash was not independently located. Revision 5 corrects four points in revision 4: D2, D4, the migration-order statement and the prerequisite wording. Revision 6 restores the D2 procedure verbatim from revision 3, section 3.4. Revision 9 applies the owner's decisions on the corrected Owner Decision Register. Revision 10 makes three corrections: complete source hashes, the D2 cancellation wording, and migration option (b). Revision 11 (2026-11-28, owner-approved Option B) changes P3 only: its timing and pass-condition wording, and the two P3 references that allowed a direct `analytics_ingest()` call instead of the real receiver (the A10 row and the API-path coverage row), so P3 runs only through the real receiver; revision 10 (SHA-256 `7df810201d2f23b427b68e43c02c7311a34967bceb25aa3bf753294ad775c482`) remains the unchanged historical reference.

**Local reproducibility:** revision 3 §2 is the controlling local reproducibility procedure, with the identities in revision 3 §0. It is not reproduced here; refer to revision 3 §2.

Labels: **[V]** verified by me from the repository or GitHub records · **[R]** recorded earlier, not re-checked tonight · **[A]** assumption · **[U]** not established (the outcome must be observed, not predicted) · **[D]** your decision needed.

Sources: `docs/ANALYTICS-IMPLEMENTATION.md` on `main` at `215b1a0`; the approved plan (revision 3); revision 8; the corrected Owner Decision Register; `supabase/PROVISIONING.md`; and the staging repository `gearhead-labs-staging` at `5f27ea1` (read-only).

## Bottom line

Hosted TEST verification **cannot start yet**. Four technical capabilities are missing:

1. a Stage 3 route to apply 0501;
2. a receiver deployment method;
3. a hosted HTTP test client;
4. a way to hold two database sessions open at once for P3.

Separately, one **owner decision** is needed: the migration order involving 0410 and 0501. Each capability needs your approval; sections 1, 2 and 7 list them. Before staging validation can be relied on after 0501, its workflow must also be updated (T11).

## 1. TEST project prerequisites

| # | Prerequisite | Status |
|---|---|---|
| T1 | **The TEST project is the staging project `npizzzfvbtrlpnxfhqks`**, in its own repository `gearhead-labs-staging`. Production is `jmztpjudwzjvrcdtjynd` and is refused everywhere | [R]; **[D]** confirm (approval A13, before any hosted contact). The main repository's `rehearsal` environment is unused and has no connection setting [V], so it is not the TEST route |
| T2 | **Migration level 0409.** Staging run #3 (dry run) and run #4 (apply, 2026-11-24) both did Stage 1 (0001–0409). Stage 2 (0410) has never run | [V] from GitHub records only; the database itself is unconfirmed. First read-only check: the migration history |
| T3 | **Project active.** Free-plan projects pause after a period of inactivity | Unknown; check on first contact. Restoring a paused project is a hosted change and needs approval |
| T4 | **pg_cron available.** 0501 creates the extension and schedules `analytics-rollup-recent` (every 15 min) and `analytics-purge` (04:17 daily) only if the extension is available; otherwise it only prints a notice | [V] in the 0501 source; availability on staging unknown [A: Supabase offers it] |
| T5 | **A way to apply 0501.** The staging workflow has only Stage 1 (0001–0409) and Stage 2 (0410) [V]. It has no 0501 stage, and the record says 0501 is never applied by hand | **Missing.** Needs a Stage 3 added to `staging-migrations.yml`, plus the 0501 file at `8762f0fe…` copied from `main` `215b1a0` |
| T6 | **A receiver deploy method.** The staging repository has no `supabase/functions` folder [V] | **Missing.** See section 3, step R1 |
| T7 | **Function settings, typed by you or set through an approved tool:** `ANALYTICS_ENV=test`, `ANALYTICS_ALLOWED_ORIGINS` = one TEST-only origin, `ANALYTICS_ADDRESS_SOURCE` unset (= `none`), `ANALYTICS_INGEST_ENABLED` unset (off) until the test window | Not set |
| T8 | **A test client that can reach the TEST function.** This workspace cannot reach `*.supabase.co` [R: the earlier lookup failed]. You only have your phone during work hours | **Missing.** Recommended: a manual, approval-gated *Hosted verification* workflow in the staging repository, run on a GitHub runner |
| T9 | **Database sessions.** Read-only snapshots can go through the Supabase connector, named to the staging project on every call. P3 needs **two sessions held open at once**, which single connector calls cannot do | **Missing.** The same staging workflow, using the existing `staging-db` approval environment and its password, never shown in chat |
| T10 | **Synthetic data only.** Fixed test-id format in the `0a000000-…` range, made unique for every run: each id embeds that run's number, and no id is reused across runs or reruns (except D2, which uses two new random, unique synthetic event ids as specified in D2), `env` = `test`, no real visitor or real Stripe data | Rule. A reused id is not accepted again: `event_id` is `UNIQUE` and inserts use `ON CONFLICT (event_id) DO NOTHING`, so a repeat is counted as `duplicate`, not `accepted` [V, 0501] |
| T11 | **Staging validation after 0501.** The staging validation workflow accepts only 19 or 20 recorded migrations (`staging-validation.yml` line 658) [V]; with 0410 and 0501 applied, staging records 21 | **Missing.** The workflow must be separately updated and approved (A15) before staging validation is relied on after 0501. Not changed here |

**Files that must be the ones tested** [V on `main` `215b1a0fd748c8bc72595caa1418d562d7e6a627`, the merge of PR #29]:

| File | SHA-256 |
|---|---|
| `0501_analytics.sql` | `8762f0fe793f10404d744befdaef35aafc9424409a5206ac162fa20392fc8098` |
| `_shared/analytics-ingest.js` | `5687519b0a81b30864b9a2d92a1c90f28beadcd080144b11a1098156fd22dd10` |
| `analytics-ingest/index.ts` | `eee6c4a7769a5d1d83e18d0969e4471ed3eb575e5b043992953e935bdb0d01bb` |
| `0501_analytics.rollback.sql` (TEST only) | `624361a8121a64234aeaf4a8593fa908aa2cdd1ddf118228b9702afd5d828422` |
| `premium-foundation/tests/analytics.test.js` | `7cc1358b698e703869d83fce06c4e1190847fba60ea8528d48ba2c58e392a4e5` |
| `premium-foundation/evidence/analytics-test-results.json` | `a2e41899ab2e405f9910438cfb788d813f4c5cabc1c21aa67f99967f168dcb47` |

Every step re-checks these before it runs.

**Historical identities** (revision 3 §0; superseded by the merge, not the deployed source): base commit `e806ec6d026ea898d3d013ee55aab65792c9a24c`; patch `c1-b1.patch` `e4256f0c98275d0e7d85c63761e151729478119c123ec465c3102385317388ba`; package `gearhead-analytics-c1-b1-local.zip` `194f49c63d9b4fe2cd13482821f29847c79f6b7930b723ce0275e7749d4be48e`.

## 2. Migration order and why

**Order: 0001–0409 (already on staging) → 0410 → 0501.**

- **Why 0410 before 0501:**
  - migrations are numbered to apply in filename order;
  - production's history is forward-only;
  - TEST must rehearse the exact order production will use, or it proves nothing about production.
- **What the workflows enforce** [V, from their source]:
  - **staging workflow:** each stage has a fixed file list and an exact list of files the CLI must report as pending (Stage 1: 0001–0409; Stage 2: 0410 only). There is no stage for 0501, and none for 0410 after 0501;
  - **production provisioning workflow:** the pending list must equal its expected pending list exactly (a rehearsal target: an exact end of the approved order).

  An order other than 0410 then 0501 is not covered by any existing stage or package.
- **What the Supabase CLI itself does with 0410 after 0501 is unverified** [A].
  - Revision 3 records the expectation as an assumption, not a fact: the CLI may report 0410 as out of order and refuse it unless `--include-all` is given.
  - This hasn't been tested.
- **0410 is a process prerequisite, not a schema dependency of 0501:** 0501 depends on `auth.users` and the 0402 billing tables (`stripe_events`, `subscriptions`, `plan_prices`), all in Stage 1 [V]. It does not touch the formula-version tables that 0410 changes.
- **The blocker:** 0410 belongs to PR #31 (Stage 2 calculator repairs), which is an **unmerged draft**. Production is at 0409 [R]. **A missing or withdrawn 0410 stops the proposed stage 3.**
- **Neither migration is authorized for application** on staging, TEST or production. Each apply is its own approval (A5, A6).
- **[D] Unresolved owner decision:**
  - **(a)** apply 0410 to staging as the first migration step, only when separately approved (A5) (Stage 2 is already built and pinned to PR #31's `62b8a65`; its 0410 equals PR #31's byte for byte [V]), then 0501, and commit to production applying 0410 before 0501.

  I recommend (a). No out-of-order path for 0410 is offered. Revision 9's option (b), renumbering one of the two migrations, is removed: renumbering 0410 to come after 0501 would let stage 3 proceed without 0410, contradicting the stop rule above, and no renumbering is chosen here. If (a) is not accepted, the migration path is an unresolved owner decision and stage 3 stays stopped.
- Each apply is one transaction with its own post-condition. 0501's post-condition refuses to finish unless it finds:
  - RLS enabled and forced, with no policies;
  - no client privileges and narrow function privileges;
  - the events id sequence owned by the table, with cache 1, increment 1 and no cycle.

## 3. Receiver deployment and verification steps

Each step is a separate approval (section 7). Nothing happens before the earlier step's evidence is reviewed. No step may contact a hosted project before the TEST project identity is confirmed (A13).

1. **Pre-flight (read-only):** confirm the staging project ref and that it is not production's. Then capture snapshot S0 (section 6).
2. **0410:** Stage 2 dry run, then apply. Capture snapshot S1.
3. **0501:** Stage 3 dry run, then apply. Capture snapshot S2 and run the P1/P2 read-only checks.
4. **R1 — deploy `analytics-ingest` to staging only:**
   - **files:** `index.ts` + `_shared/analytics-ingest.js` from `215b1a0`, hashes as in section 1;
   - **JWT checking off** (`--no-verify-jwt`), because `sendBeacon` can't send an Authorization header;
   - **settings:** `ANALYTICS_ENV=test`, `ANALYTICS_ALLOWED_ORIGINS=<TEST origin>`, `ANALYTICS_ADDRESS_SOURCE` unset, and **`ANALYTICS_INGEST_ENABLED` unset**.

   Deploy options, **[D]**:
   - the Supabase connector's deploy tool (no CLI for you; every call names the staging project);
   - a CLI deploy in the staging workflow, which needs a Supabase access token as a staging-only secret, created by you;
   - the dashboard.

   I recommend the connector, with the deployed source read back and hashed.
5. **R2 — verify it is off (an active synthetic HTTP request, A14):** send one synthetic TEST batch, with its own run-unique event id, over HTTP to the hosted TEST receiver. This is an active hosted test operation. Expected: 204, and no change to `analytics_events` or `analytics_ingest_stats`, because the kill switch is expected to stop ingestion before the body is read. The 204 is not evidence by itself.
6. **Open the test window (A8):** set `ANALYTICS_INGEST_ENABLED=true` on staging only.
7. **Run D1/D3, D4, D2, P3, P4** (section 5), in that order. D1/D3 need the probe, which must first be separately approved, written, reviewed and deployed (A9); it is not authorized or deployed now, and D1/D3 cannot run until A9 is given.
8. **R4 — close:** unset `ANALYTICS_INGEST_ENABLED`, then repeat R2 (A14, a new run-unique event id) to prove the receiver is off. Capture snapshot S3.

## 4. Probe function (D1, D3)

- **Purpose:** the real receiver never reports how the hosted runtime delivers a body. The probe measures only that, and returns no data from the database.
- **Scope:**
  - **name:** `b1-chunk-probe`, staging only, deployed with the same JWT setting as the receiver so the path matches;
  - **no database client, no secrets, no logging of request bodies;**
  - it answers only POSTs carrying the header `x-probe-run: <this run's id>`; anything else gets a 404;
  - **mode `chunks`:** reads the raw body stream and returns JSON listing each chunk's byte length, the total, the declared `Content-Length` and the time to first chunk;
  - **mode `decode`:** passes the request to the unchanged `readBodyLimited()`, imported from the same `_shared/analytics-ingest.js` (`5687519b…`), and returns the decoded text as escaped code points plus the accept/refuse outcome.
- **Only these two modes.** The probe does not log cancellation, and no cancellation mode is added. Hosted cancellation therefore remains **not verifiable on the hosted platform** (D2).
- **Source:** written and reviewed as its own file before any deploy. **It doesn't exist yet**, and writing it is a separate approval. No probe is to be created now.
- **Lifetime:** deployed for the D1/D3 runs only, then **deleted** in the same session.
- **Evidence:** the function list before deploy, after deploy and after deletion, showing it is absent.

## 5. Test procedures and expected evidence

**Senders:** all HTTP tests run from the approved staging workflow with run-unique synthetic event ids (T10), except D2, which uses two new random, unique synthetic event ids as specified in D2. Each test has a pass rule and an "inconclusive" rule, except where a test defines its own result categories (D2, D4); those categories then apply instead. Inconclusive means a platform behaviour got in the way. It is recorded as a finding, never as a pass.

### Database (PostgreSQL 17)

**P1 — Sequence settings (read-only, after S2)**
- **Steps:**
  - check that the 0501 apply log shows the post-condition passed;
  - query `pg_sequences` for `analytics_events_id_seq`;
  - run `pg_get_serial_sequence('public.analytics_events','id')`.
- **Pass:**
  - cache 1, increment 1, cycle false;
  - the identity sequence is `public.analytics_events_id_seq`;
  - `version()` is recorded.

**P2 — Isolation level (read-only)**
- **Steps:**
  - `SHOW default_transaction_isolation`;
  - `pg_db_role_setting` for `postgres`, `authenticator`, `service_role` and the database;
  - the `cron.job` rows for both analytics jobs.
- **Pass:**
  - `read committed`, with no role or database override;
  - both jobs present, with the schedules above.
- **Any other level is reported as a finding, not assumed safe.**
- **Limitation:** this shows the default, not the level a running cron job or API call actually used. Proving that needs a write (a temporary job or function), and isn't proposed.

**P3 — Lock wait (C2-1 and C2-9 on hosted)**
It needs two sessions, run by the staging workflow:
1. Session A (as postgres) begins, calls analytics_rollup(today), and holds the advisory lock.
2. During the hold, one valid TEST batch (event L1, run-unique) is ingested through the real receiver.
3. Once the monitor observes the identified analytics_ingest session waiting on Session A’s advisory lock, Session A continues holding the lock for at least 1.5 seconds from that observation.
4. Session A then commits. Its total lock-hold duration, measured from lock acquisition through release, must remain under 3 seconds.
5. Then analytics_rollup_pending() runs.

- Pass:
  - the same identified ingest session is observed waiting continuously on the advisory lock for at least 1.5 seconds and less than 3 seconds;
  - a pg_locks snapshot shows Session A holding the advisory lock and the identified ingest waiting on it;
  - Session A’s total lock-hold duration is under 3 seconds;
  - L1’s id is above the watermark A recorded;
  - the next run reopens today;
  - afterwards, today’s rolled-up page views equal raw page views, and no day holds an event above its watermark.
- Inconclusive: if the ingest arrives after A commits, or the identified waiting session cannot be observed, report the reason. Repeat only with a new run-unique L1.

**P4 — Concurrent ingest and rollup through the API**
- **Steps:**
  - over about 20 minutes, so at least one scheduled `analytics-rollup-recent` run falls inside, send **≤ 250 events**;
  - pace below about 100 requests a minute, with some batches repeating earlier event ids from the same run.
- **The limits behind this size:**
  - with `ANALYTICS_ADDRESS_SOURCE=none`, every sender shares the unknown allowance of **300 per 10 min** and 2,000 a day;
  - the receiver's per-copy limiter allows 120 requests a minute.

  Going above these would test the limits, not C1.
- **Pass:**
  - at least one succeeded `cron.job_run_details` run inside the window;
  - accepted = sent − duplicates;
  - duplicates counted once;
  - after a final run, rolled-up = raw and zero days above their watermark;
  - no `rate_limited` or `busy` refusals. Any that appear are reported with counts.

### Receiver (Deno, hosted)

**D1 — Chunk sizes (probe, `chunks` mode)**
- **Steps:** bodies of 1 KB, 8,192 B, 8,193 B, 64 KB and 1 MB, each sent three ways:
  - with an honest `Content-Length`;
  - chunked with no length;
  - with a slow, chunked send.
- **Pass:** a table of the chunk sizes the hosted runtime delivered, per case. From it, the largest "one extra chunk" the limit can pull past 8,192 bytes.
- **[U]:** the chunk sizes themselves are not established; they must be observed, not predicted. They set the "8,192 + one chunk" memory figure.
- **Also records:** whether the platform refuses large bodies itself (status ≠ 200, or a gateway error) before the function sees them.

**D2 — Cancellation (real receiver), as defined in revision 3, section 3.4, with its two probe statements corrected to the approved probe scope (revision 10)**

**Purpose:**
- Show, on the hosted runtime, that a body larger than 8,192 bytes sent without a usable `Content-Length` is rejected and
  inserts nothing.
- Collect whatever evidence the platform allows about whether the receiver stopped reading it.

**Two requests, two event ids.** Generate two new random synthetic event ids, unique to this test and not used anywhere else:
- **O**, for the oversize request;
- **C**, for the under-limit control.

Each request carries only its own id. Nothing about one request is inferred from the other.

| | Oversize request (id O) | Control request (id C) |
|---|---|---|
| Body | A valid batch containing one event with id O, padded with spaces to **10,485,760 bytes (10 MB)**; the intended total is recorded before sending | The same batch shape containing one event with id C, padded to **4,096 bytes**; size recorded |
| Sending | Chunked, no `Content-Length`, slowly (for example 64 KB per 100 ms), by a client that logs every write | Normal request with its true `Content-Length` |
| Order | Its own window: no other request to the TEST receiver from the "before" snapshot until the "after" snapshot | Its own window, after the oversize window closes |

**Snapshots (read-only queries on TEST), taken for each request separately, immediately before and after it:**
1. `analytics_events` rows with that request's event id. Expected before: 0. O is never checked against C's rows, or the reverse.
2. `analytics_ingest_stats` for today, `env = 'test'`: every outcome and reason row, so the delta covers exactly that request's
   window.
3. The rate-limit headroom for the TEST allowances (`analytics_rate` counters). If a limit could refuse the request, the window
   is not run.
4. The TEST receiver's function log lines for the window [A: availability].

**Reported separately for each request:**

| Dimension | Oversize (O) | Control (C) |
|---|---|---|
| HTTP | Status, and arrival time relative to the first byte sent. **Not evidence of anything else** | Status. **Not evidence of anything else** |
| Internal accept / reject | Rejected as over-limit **only if** its window's statistics delta is exactly `rejected/bad_batch` +1, with no other change. (This path writes no log line.) A `rate_limited` delta, an empty delta, or a platform error means "not determined", not rejected | Accepted only if its window's delta is exactly `accepted` +1 |
| Database insertion | Zero rows with id O after its window. This is judged only from O's own query; C's insertion is **not** evidence about O | Exactly one row with id C after its window |

The control shows only that the batch format is valid, so a `bad_batch` result for O can be attributed to size, not content. It
proves nothing about O's insertion or rejection.

**Bytes-sent and cancellation evidence (oversize request only).** The client records:
- **intended total size:** 10,485,760 bytes;
- **bytes written when the response arrived:** the client's running count at the moment the status line is received;
- **after the response,** the client keeps offering the rest of the body for a fixed period (for example 30 s), and records:
  - whether writes still succeed;
  - how many further bytes are accepted;
  - whether the connection is closed or reset by the far end, and when;
- **final bytes written** and the client's end state (completed, closed by peer, reset, timed out).

**What these observations can and cannot show:**
- An early response alone does **not** prove that the server cancelled the stream. The receiver can answer while the platform
  keeps accepting, or buffering, the rest of the upload.
- Bytes written before the response, and the far end closing or resetting the connection shortly after it, are **consistent
  with** the stream being abandoned somewhere on the server side. They cannot show *where*: in the function's reader, or in a
  gateway or proxy in front of it. Accepted bytes also include data held in network buffers.
- **The unmodified receiver writes nothing about cancellation:** no log line, no database record. With it, **server-side
  cancellation cannot be verified on the hosted platform.** That will be reported as a limitation, not claimed.
- The approved probe (§4) has only the `chunks` and `decode` modes. It does not log cancellation and provides no
  function-side cancellation evidence. No additional probe mode or expanded probe scope is authorized or proposed.
- Cancellation itself stays verified **locally only** (B2-2, B2-3, B2-8, B2-9, B2-10 and B2-16 assert `cancel()` was called and
  bounded the bytes pulled).

**Result categories for the D2 report:**
1. "Rejected, nothing inserted": O's delta is `bad_batch` +1 and O has no row.
2. "Not reached the function": a platform error, and no statistics change.
3. "Not determined": a `rate_limited` delta, another traffic source in the window, or missing snapshots.

Hosted rejection and insertion are evaluated from the evidence above. Cancellation is reported as **"not verifiable on the
hosted platform"**: server-side cancellation cannot be verified on the hosted platform with the approved probe scope. The local
cancellation tests (B2-2, B2-3, B2-8, B2-9, B2-10 and B2-16) remain the evidence for cancellation behaviour.

**Verified means:**
- every row above has its evidence captured (run links, query outputs with timestamps, client logs, function log excerpts)
  in one TEST evidence file;
- every outcome is stated in all three dimensions where they apply;
- all data is synthetic;
- production is untouched.

**D3 — UTF-8 decoding (probe, `decode` mode)**
- **Steps:** send the exact B2-7 byte sequences (invalid UTF-8, plus a leading byte-order mark), and a valid multi-byte character split across chunks (B2-11).
- **Pass:** the escaped code points equal the local results byte for byte:
  - U+FFFD for each invalid sequence;
  - the byte-order mark removed;
  - the split character decoded once.

  Behaviour is only observed, not changed. Refusing invalid UTF-8 remains an open decision.

**D4 — The 8,192 boundary (real receiver)**
Four requests to the real receiver (TEST env), each a valid batch with its own run-unique event id, each in its own evidence window.

**Before each request:**
- read the rate-limit headroom (`analytics_rate` counters). If a limit could refuse the request, the window is not run. A `rate_limited` result means "not tested", not a pass or a fail;
- no other traffic reaches the TEST receiver from the "before" snapshot until the "after" snapshot.

**For each request, record separately:**
- **HTTP:** the status. `204` is expected for all four and is **not** evidence of ingestion by itself;
- **database insertion:** checked by that request's event id, not by counts;
- **ingestion statistics:** the exact before/after delta of `analytics_ingest_stats`, compared by `(day, env='test', outcome, reason)`;
- **function log:** the log lines for the window [A: availability and retention of these logs].

| Request | HTTP status | Database insertion (by event ID) | Ingestion-statistics delta | Function log |
|---|---|---|---|---|
| `B8192`: a valid TEST batch padded to exactly 8,192 bytes, no `Content-Length` | 204 | Stored once | `accepted` +1; nothing else | None: no log line |
| `B8193`: the same at 8,193 bytes, no `Content-Length` | 204 | Not stored | `rejected: bad_batch` +1; nothing else | None: no log line |
| `BLIE`: `Content-Length: 100` with a 20 KB body | 204 | Not stored | `rejected: bad_batch` +1; nothing else | None: no log line |
| `B9000`: a declared `Content-Length: 9000` | 204 | Not stored | **No change at all** | **`analytics-ingest: too large`** present; **no database call** (refused before reading) |

- **Expected net change:** `analytics_events` +1, `bad_batch` +2.
- **Not reached:** if a platform error replaces the function's answer, the request is recorded as "not reached".
- **[U]:** whether the platform passes `B8193` and `BLIE` to the function as streams or buffers them (D1/D2).
- **Inconclusive:** if the platform rewrites or adds a `Content-Length` (D1 shows this), the request isn't the one intended. Record the platform's behaviour.

## 6. Snapshots, records, logs and byte accounting

**Database snapshots** (read-only; each saved as a timestamped query output):

| Snapshot | When | Contents |
|---|---|---|
| S0 | Before anything | Project ref + `version()`; migration history; extensions; list of `analytics_*` objects (expect none) |
| S1 | After 0410 | History ends at 0410; 0410's own post-condition passed |
| S2 | After 0501 | History ends at 0501. Analytics tables: row counts (expect 0), RLS forced, no policies. Privileges. The P1 sequence; the P2 settings; `cron.job` |
| S3 | After the window | Row counts per table; every `analytics_events` row (`id`, `event_id`, `env`, `name`, `ts_server`); `analytics_ingest_stats` by outcome and reason; `analytics_rollup_runs` (day, `max_event_id`, `ran_at`); `cron.job_run_details` for the window; storage-size figure; function list (probe absent); function settings by **name only** |

**Event records:** a table of every synthetic event id sent, with its run number (the two `R2` requests, `O`, `C`, `L1`, `B8192`, `B8193`, `BLIE`, `B9000`, the P4 ids). For each: request, expected outcome, found in `analytics_events` (yes/no) and server time.

**Logs:**
- the workflow run logs and links;
- the receiver's function logs for the window. It logs only short reason lines and never addresses or payloads; check this holds;
- `cron.job_run_details`;
- the probe's responses.

**Byte accounting, per HTTP request:**
- body size and whether `Content-Length` was sent;
- the client's bytes uploaded;
- time to the response;
- status;
- for the probe: chunk list and total.

**Kept in:** one TEST evidence file. Adding it to the repository later is its own approval.

## 7. Approvals required, each separate

A13 comes before every other approval that contacts a hosted project.

| # | Approval | Changes |
|---|---|---|
| A0 | This procedure, as written or amended | Nothing |
| A1 | **Migration order** (section 2: accept (a), or leave unresolved, in which case stage 3 stays stopped) | Nothing yet |
| A2 | Read-only queries on the staging project through the connector (after A13) | Nothing (reads) |
| A3 | Staging repository change: add **Stage 3 (0501)** and the 0501 file; review, then merge | Staging repo only |
| A4 | Staging repository change: the **Hosted verification** workflow (HTTP senders, P3 sessions), run only on demand and gated by `staging-db` | Staging repo only |
| A5 | **0410** on staging: Stage 2 dry run, then apply, separately (the first migration step; a missing or withdrawn 0410 stops A6) | Staging database |
| A6 | **0501** on staging: Stage 3 dry run, then apply, separately | Staging database |
| A7 | Deploy method choice and the **R1 deploy**, with settings and the switch **off** | Staging function |
| A8 | **Open the test window:** `ANALYTICS_INGEST_ENABLED=true` on staging | Staging setting |
| A9 | Write the probe source (`chunks` and `decode` modes only); then, separately, **deploy** it; then **delete** it | Staging function |
| A10 | Run D1/D3, D4, D2, P3 and P4, in that order (synthetic writes to staging). D1/D3 only after A9. P3 only through the real receiver; no direct `analytics_ingest()` call | Staging data |
| A11 | **Close:** switch off, probe removed, synthetic data **kept** (staging stays forward-only) | Staging setting |
| A12 | Record the evidence in the repository and correct `ANALYTICS-IMPLEMENTATION.md` | Main repo |
| A13 | **Confirm the TEST project identity** (`npizzzfvbtrlpnxfhqks`, not production) before any hosted contact | Nothing |
| A14 | **R2:** one synthetic TEST batch sent over HTTP to the TEST receiver while the switch is off, before A8 and again at close (R4). An active hosted test request; expected: no database or ingestion-statistics change | Staging data (none expected) |
| A15 | Staging repository change: update staging validation to accept the 21 migrations recorded after 0410 and 0501 (T11); review, then merge | Staging repo only |

If a check differs from this plan at any step: stop, preserve everything, and report.

## 8. Unresolved decisions, risks and missing prerequisites

- **Missing:** T5 (0501 apply route), T6 (deploy method), T8 (test client), T9 (two sessions) and T11 (staging validation for 21 migrations). See A3, A4, A7 and A15.
- **[D] 0410 order:** blocked by PR #31 being an unmerged draft. A missing or withdrawn 0410 stops the proposed stage 3.
- **[D] TEST origin** for `ANALYTICS_ALLOWED_ORIGINS`. Suggested: the staging site's own address. Never the production site.
- **Wrong-project risk:** the Supabase connector can reach both projects. Mitigations:
  - every call names `npizzzfvbtrlpnxfhqks` and is refused if it names production's ref;
  - the project ref and `version()` are read back first, every session.
- **Staging may be paused** (Free plan). Restoring it is a hosted change.
- **Platform buffering** means hosted cancellation is **not verifiable on the hosted platform** (D2); it does not change D2's rejection result. Hosted B1 is an acceptance limit, not a memory ceiling, unless D1 shows small chunks.
- **P2 can only show defaults.** The isolation level a cron job or API call actually ran under is not observed.
- **The insert rule is policy only.** P3 and P4 insert only through `analytics_ingest()`. Direct inserts on hosted are not allowed.
- **The rate limits will shape P4.** It is sized to stay under them; anything refused is reported, not hidden.
- **Assumptions and unestablished behaviour (revision 3 §4):**
  - PostgreSQL makes a committing transaction's work visible before it releases its locks, so the next lock holder sees it. Standard behaviour, consistent with the local stress test (0 violations); not separately proven [A];
  - Supabase CLI behaviour when 0501 is applied before 0410 [A] (section 2);
  - Supabase platform request buffering, size limits and status codes ahead of the function [U];
  - availability and retention of Edge Function log lines on the TEST project [A];
  - transaction isolation used by `pg_cron` and the database API on hosted PostgreSQL 17 [A] (P2).
- **The documentation is now out of date after the merge.** Its status table, §3.11, §5.2a, §8 and §9 still say "PR #29 draft" and "unmerged", and §8 cites only PostgreSQL 16. It is **not edited** (A12). §9's "Merging is itself a site release" happened on 2026-11-27 (Pages run succeeded, tracking off).
- **Hosted Supabase versions differ from the local ones.** The CLI pins `supabase/postgres:17.12.0.005` and edge-runtime v1.80.0 [V], but the hosted project's versions are unknown until S0. Local tests used community PostgreSQL 17.12 and a source build of the runtime.
- **Out of scope:** E1 / the address-header probe (§9a item 3), CORS and `sendBeacon` from the real site (item 4), storage re-measurement (item 5), magic-link sign-in (item 6), and the production path (PR #27 re-pin to `8762f0fe…`, production apply). Each is a later, separate approval.

## 9. Evidence: established locally vs still outstanding on hosted

| Topic | Local, established | Hosted, outstanding |
|---|---|---|
| C1 watermark logic (C2-1…10) | 10/10 on PostgreSQL 16.15 and on 17.12 (2026-11-26/27) | P3, P4 on staging's PostgreSQL 17 |
| Sequence cache/increment/cycle | C2-10 on 16 and 17 | P1 |
| Isolation level | Local default (read committed) | P2: hosted defaults and role overrides |
| pg_cron jobs | Not tested anywhere | S2 `cron.job` rows; P4 run details |
| API path (PostgREST → `analytics_ingest`) | Not tested anywhere | D4, P3, P4 |
| B1 byte limit (B2-1…17) | 17/17 in Node (Node 22) and twice in Supabase edge-runtime v1.80.0 built from source | D4 through the real hosted receiver |
| Chunk sizes delivered | In-memory streams only | D1 |
| Cancellation | In-memory streams (B2-2, B2-3, B2-8, B2-9, B2-10, B2-16) | Not verifiable on the hosted platform with the unmodified receiver (D2) |
| UTF-8 decoding | B2-7, B2-11; local decoder identical to `Request.text()` | D3 |
| Full site regression | 52/52 suites on `3f02a5c` (run #25) | None needed for C1/B1 |
| 0501 file on `main` | Merged in `215b1a0` (byte-identical to the tested file) | Applied nowhere |

**Nothing hosted has been verified.** Stopped here; waiting for your review.
