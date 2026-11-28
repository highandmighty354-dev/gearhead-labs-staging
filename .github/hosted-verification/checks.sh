# Gearhead Labs STAGING - shell checks for .github/workflows/hosted-verification.yml (A4). Sourced by its steps.
# Pure shell plus read-only GitHub API calls: no secret, no contact with the staging receiver or database here
# (tls_probe only opens a TLS handshake to the database pooler, exactly as the migration workflow does).
# Every failure prints "HARD FAIL: <reason>" and exits non-zero, which stops the job.
#
# verify_target, pooler_host, write_ca, tls_probe and check_environment are copied UNCHANGED from
# .github/workflows/staging-migrations.yml (revision 5, the reference implementation); tests/test_checks.sh checks that
# they still match it byte for byte. The other functions are new for hosted verification.
set -euo pipefail
LC_ALL=C; export LC_ALL
fail() { echo "::error::$*" >&2; echo "HARD FAIL: $*" >&2; exit 1; }

HV_SUITES="P1-P2 D1 D3 D4 D2 P3 P4"

# --- A. How this run was started, and what was chosen -------------------------------------------
check_context() {
  [ "${EVENT:-}" = workflow_dispatch ] || fail "started by '${EVENT:-?}', not by the Run workflow button"
  [ "${REPO:-}" = "$STAGING_REPOSITORY" ] || fail "running in '${REPO:-?}', not $STAGING_REPOSITORY"
  [ "${GIT_REF:-}" = refs/heads/main ] || fail "started from '${GIT_REF:-?}', not refs/heads/main"
  [ "${RUN_ATTEMPT:-}" = 1 ] || fail "this is re-run attempt ${RUN_ATTEMPT:-?}; re-runs are refused - start a new run (new IDs, new approval)"
  case "${SUITE:-}" in P1-P2|D1|D3|D4|D2|P3|P4) ;; *) fail "test suite must be exactly one of: $HV_SUITES (got '${SUITE:-}')" ;; esac
}

# --- B. The TEST website origin (owner's decision; never invented here) -------------------------------
# Needed by every suite that sends HTTP. P1-P2 sends nothing and does not need it.
check_origin() {
  local o="${ANALYTICS_TEST_ORIGIN:-}" host
  [ "${SUITE:-}" = P1-P2 ] && { echo "Origin: not needed for P1-P2 (no HTTP request)"; return 0; }
  [ -n "$o" ] || fail "repository variable ANALYTICS_TEST_ORIGIN is not set: the owner has not chosen the TEST website origin"
  [[ $o =~ ^https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+)(:[0-9]{1,5})?$ ]] \
    || fail "ANALYTICS_TEST_ORIGIN must be a bare https origin (https://host or https://host:port, lowercase, no path or slash)"
  host="${BASH_REMATCH[1]}"
  case "$host" in gearhead-labs.com|*.gearhead-labs.com) fail "ANALYTICS_TEST_ORIGIN is the PRODUCTION website" ;; esac
  case "$host" in *.supabase.co|*.supabase.in) fail "ANALYTICS_TEST_ORIGIN is a Supabase address, not a website origin" ;; esac
  case "$o" in *"$PRODUCTION_REF"*) fail "ANALYTICS_TEST_ORIGIN names the PRODUCTION project" ;; esac
  echo "Origin OK: $o"
}

# --- C. Prerequisites that separate approvals must have completed (owner-set repository variables) ----
# D1, D3: HV_PROBE_STATUS=deployed-under-A9.  D4, D2, P3, P4: HV_RECEIVER_STATUS=deployed-under-A7 and
# HV_TEST_WINDOW=open-under-A8. Exact values only; anything else refuses the suite before any request.
check_prerequisites() {
  case "${SUITE:-}" in
    P1-P2) echo "Prerequisites: none for P1-P2 (read-only database checks)" ;;
    D1|D3)
      [ "${HV_PROBE_STATUS:-}" = deployed-under-A9 ] \
        || fail "$SUITE needs the temporary probe approved and deployed under A9 (variable HV_PROBE_STATUS must be exactly deployed-under-A9)"
      echo "Prerequisites: probe attested deployed under A9" ;;
    D4|D2|P3|P4)
      [ "${HV_RECEIVER_STATUS:-}" = deployed-under-A7 ] \
        || fail "$SUITE needs the real receiver deployed under A7 (variable HV_RECEIVER_STATUS must be exactly deployed-under-A7)"
      [ "${HV_TEST_WINDOW:-}" = open-under-A8 ] \
        || fail "$SUITE needs the test window opened under A8 (variable HV_TEST_WINDOW must be exactly open-under-A8)"
      echo "Prerequisites: receiver attested deployed under A7; test window attested open under A8" ;;
    *) fail "unknown suite '${SUITE:-}'" ;;
  esac
}

# --- D. Nothing else may touch staging at the same time (read-only GitHub API) -----------------------
# Refuses if a staging migration or validation run, or another hosted-verification run, is queued or running.
check_no_other_runs() {
  local api="${GITHUB_API_URL:-}" wf st f
  [[ $api == https://* ]] || fail "GITHUB_API_URL is not an https address: other runs cannot be checked"
  [ -n "${GH_TOKEN:-}" ] || fail "no read token: other runs cannot be checked"
  for wf in staging-migrations.yml staging-validation.yml hosted-verification.yml; do
    for st in in_progress queued waiting requested pending; do
      f="$RUNNER_TEMP/runs-$wf-$st.json"
      curl --fail --silent --show-error --proto '=https' -H "Authorization: Bearer $GH_TOKEN" \
           -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
           -o "$f" "$api/repos/$REPO/actions/workflows/$wf/runs?status=$st&per_page=100" \
        || fail "could not list $st runs of $wf: other runs cannot be checked"
      python3 - "$f" "$wf" "$st" "${RUN_ID:-}" <<'PYEOF' || fail "another staging run is active (see above): wait for it to finish, then start a new run"
import json, sys
runs = json.load(open(sys.argv[1])).get("workflow_runs", [])
others = [r for r in runs if str(r.get("id")) != sys.argv[4]]
if others:
    print(f"HARD FAIL: {len(others)} {sys.argv[3]} run(s) of {sys.argv[2]}: " + ", ".join(str(r.get('run_number')) for r in others), file=sys.stderr)
    sys.exit(1)
PYEOF
    done
  done
  echo "No other staging migration, validation or hosted-verification run is queued or running."
}

# --- E. Reference functions (unchanged copy) ------------------------------------------------------
verify_target() {
  local expected="${EXPECTED_TEST_REF:-}" typed="${CONFIRM_REF:-}" url="${POOLER_URL:-}" ref cfg site_refs
  [ -n "$expected" ] || fail "variable EXPECTED_TEST_REF is not set: the target cannot be verified"
  [ -n "$url" ]      || fail "variable STAGING_POOLER_URL is not set: the target cannot be verified"
  [ -n "$typed" ]    || fail "no project ref was typed: the target cannot be verified"
  [[ $expected =~ ^[a-z0-9]{20}$ ]] || fail "EXPECTED_TEST_REF is not a 20-character project ref (lowercase letters and digits only)"
  [ "$expected" != "$PRODUCTION_REF" ] || fail "EXPECTED_TEST_REF is the PRODUCTION project"
  [ "$typed" != "$PRODUCTION_REF" ]    || fail "the typed ref is the PRODUCTION project"
  case "$url" in *"$PRODUCTION_REF"*) fail "STAGING_POOLER_URL names the PRODUCTION project" ;; esac
  ref="$(printf '%s' "$url" | sed -nE 's#^postgres(ql)?://postgres\.([a-z0-9]{20}):\[YOUR-PASSWORD\]@aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com:5432/postgres$#\2#p')"
  [ -n "$ref" ] || fail "STAGING_POOLER_URL is not an unmodified Session pooler string: postgres.<ref>:[YOUR-PASSWORD]@aws-N-<region>.pooler.supabase.com:5432/postgres"
  [ "$ref" = "$expected" ] || fail "STAGING_POOLER_URL is for project $ref, not EXPECTED_TEST_REF $expected"
  [ "$typed" = "$expected" ] || fail "the typed ref does not match EXPECTED_TEST_REF"
  cfg="supabase-config.js"
  [ -f "$cfg" ] && [ ! -L "$cfg" ] || fail "$cfg is missing: the target cannot be cross-checked"
  grep -qF "$PRODUCTION_REF" "$cfg" && fail "$cfg names the PRODUCTION project"
  site_refs="$(grep -oE 'https://[a-z0-9]+\.supabase\.co' "$cfg" | sort -u)"
  [ "$site_refs" = "https://$expected.supabase.co" ] || fail "$cfg must name exactly one project, https://$expected.supabase.co (found: ${site_refs:-none})"
  echo "$ref"
}

# --- G. TLS: pinned Supabase roots, certificate chain + hostname (verify-full) ----------------------
# pooler_host: the host part of the already-verified STAGING_POOLER_URL.
pooler_host() {
  printf '%s' "${POOLER_URL:-}" | sed -nE 's#^postgres(ql)?://postgres\.[a-z0-9]{20}:\[YOUR-PASSWORD\]@(aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com):5432/postgres$#\2#p'
}
# write_ca FILE: writes the pinned roots and refuses unless the file holds exactly the two pinned certificates.
write_ca() {
  local file="$1" got
  printf '%s' "$SUPABASE_ROOT_CA_PEM" > "$file"
  [ "$(grep -c -- '-----BEGIN CERTIFICATE-----' "$file")" = 2 ] || fail "the pinned CA bundle does not hold exactly 2 certificates"
  got="$(python3 - "$file" <<'PYEOF'
import hashlib, re, ssl, sys
pem = open(sys.argv[1]).read()
blocks = re.findall(r"-----BEGIN CERTIFICATE-----\n.*?\n-----END CERTIFICATE-----", pem, re.S)
print(" ".join(sorted(hashlib.sha256(ssl.PEM_cert_to_DER_cert(b + "\n")).hexdigest().upper() for b in blocks)))
PYEOF
)" || fail "the pinned CA bundle could not be read"
  [ "$got" = "$SUPABASE_ROOT_CA_SHA256_FINGERPRINTS" ] || fail "the CA bundle's certificate fingerprints are not the two pinned Supabase roots"
  chmod a-w "$file"
}
# tls_probe HOST CAFILE: no password involved. The pooler must complete a TLS handshake whose certificate chains to
# a pinned root and names HOST; system CA stores are not consulted. Any failure stops the run.
tls_probe() {
  local host="$1" ca="$2" out rc=0
  [ -n "$host" ] || fail "no pooler host to check: the target cannot be verified"
  out="$(timeout 30 openssl s_client -starttls postgres -connect "$host:5432" -servername "$host" \
           -verify_hostname "$host" -verify_return_error -CAfile "$ca" -no-CApath -no-CAstore -brief < /dev/null 2>&1)" || rc=$?
  printf '%s\n' "$out" | sed 's/^/  tls: /' >&2
  [ "$rc" -eq 0 ] || fail "TLS check of $host failed: the certificate is not from a pinned Supabase root, does not name $host, has expired, or TLS is not offered"
  printf '%s\n' "$out" | grep -qx 'Verification: OK' || fail "TLS check of $host did not report 'Verification: OK'"
  # OpenSSL prints "Verified peername" only after -verify_hostname matched; for a wildcard certificate it prints
  # the certificate name that matched (e.g. *.pooler.supabase.com), not $host, so only its presence is required.
  printf '%s\n' "$out" | grep -q '^Verified peername: [^ ]' || fail "TLS check of $host did not confirm the hostname"
  echo "TLS OK: $host presents a certificate chaining to a pinned Supabase root and naming $host"
}

# --- H. The approval gate, read back from GitHub (read-only; no secret involved) ------------------
# Refuses unless Environment APPROVAL_ENVIRONMENT exists with exactly: one required-reviewers rule whose only
# reviewer is APPROVER_LOGIN, self-review prevention OFF (one-owner setup: ON would deadlock), and deployments
# limited to exactly one branch pattern, "main". Administrator bypass is NOT exposed by GitHub's API; it is a
# manual setting (see the plan).
check_environment() {
  local api="${GITHUB_API_URL:-}" base
  [[ $api == https://* ]] || fail "GITHUB_API_URL is not an https address: the approval gate cannot be verified"
  [ -n "${GH_TOKEN:-}" ] || fail "no read token: the approval gate cannot be verified"
  base="$api/repos/$REPO/environments/$APPROVAL_ENVIRONMENT"
  curl --fail --silent --show-error --proto '=https' -H "Authorization: Bearer $GH_TOKEN" \
       -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
       -o "$RUNNER_TEMP/env.json" "$base" \
    || fail "could not read Environment $APPROVAL_ENVIRONMENT (missing, or GitHub unreachable): the approval gate cannot be verified"
  curl --fail --silent --show-error --proto '=https' -H "Authorization: Bearer $GH_TOKEN" \
       -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
       -o "$RUNNER_TEMP/env-branches.json" "$base/deployment-branch-policies" \
    || fail "could not read the branch rule of $APPROVAL_ENVIRONMENT: the approval gate cannot be verified"
  python3 - "$RUNNER_TEMP/env.json" "$RUNNER_TEMP/env-branches.json" <<'PYEOF' || fail "Environment $APPROVAL_ENVIRONMENT is not configured as required (see the message above)"
import json, os, sys
env = json.load(open(sys.argv[1])); br = json.load(open(sys.argv[2]))
want_env, approver = os.environ["APPROVAL_ENVIRONMENT"], os.environ["APPROVER_LOGIN"]
def no(msg): print("HARD FAIL: " + msg, file=sys.stderr); sys.exit(1)
if env.get("name") != want_env: no(f"the Environment read back is {env.get('name')!r}, not {want_env!r}")
rules = env.get("protection_rules") or []
kinds = [r.get("type") for r in rules]
unknown = sorted(set(kinds) - {"required_reviewers", "branch_policy", "wait_timer"})
if unknown: no(f"unexpected protection rule type(s) {unknown}: review the Environment before running")
rr = [r for r in rules if r.get("type") == "required_reviewers"]
if len(rr) != 1: no("the Environment has no required-reviewers rule (every run must wait for approval)")
if rr[0].get("prevent_self_review") is not False:
    no("'Prevent self-review' must be OFF: with one owner as the only reviewer, ON means no run can ever be approved")
revs = [(x.get("type"), (x.get("reviewer") or {}).get("login")) for x in (rr[0].get("reviewers") or [])]
if revs != [("User", approver)]: no(f"required reviewers must be exactly the user {approver}; found {revs}")
if "branch_policy" not in kinds: no("the Environment has no branch rule (any branch could use it)")
if env.get("deployment_branch_policy") != {"protected_branches": False, "custom_branch_policies": True}:
    no(f"deployment branches must be 'Selected branches and tags'; found {env.get('deployment_branch_policy')}")
pols = [(p.get("name"), p.get("type", "branch")) for p in br.get("branch_policies", [])]
if br.get("total_count") != 1 or pols != [("main", "branch")]: no(f"the only allowed deployment branch must be 'main'; found {pols}")
print(f"Approval gate OK: {want_env} requires {approver}, self-review allowed (no deadlock), branch main only")
PYEOF
}
