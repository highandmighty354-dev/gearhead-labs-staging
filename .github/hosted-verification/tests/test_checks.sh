#!/usr/bin/env bash
# Tests for .github/hosted-verification/checks.sh. LOCAL ONLY: a fake curl stands in for the GitHub API.
#   bash .github/hosted-verification/tests/test_checks.sh        (from the repository root)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$ROOT" || exit 1
CHECKS="$ROOT/.github/hosted-verification/checks.sh"
P=0; F=0
T="$(mktemp -d)"; export RUNNER_TEMP="$T"
REF=npizzzfvbtrlpnxfhqks; PROD=jmztpjudwzjvrcdtjynd
POOL="postgresql://postgres.$REF:[YOUR-PASSWORD]@aws-1-us-east-2.pooler.supabase.com:5432/postgres"

# run NAME EXPECT(pass|fail) MESSAGE-REGEX -- command...   (runs in a clean subshell with checks.sh sourced)
run() {
  local name="$1" want="$2" rx="$3"; shift 4
  local out rc=0
  out="$( ( export STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging PRODUCTION_REF=$PROD \
              APPROVAL_ENVIRONMENT=staging-db APPROVER_LOGIN=highandmighty354-dev
            # shellcheck source=/dev/null
            . "$CHECKS"; "$@" ) 2>&1 )" || rc=$?
  if { [ "$want" = pass ] && [ "$rc" = 0 ]; } || { [ "$want" = fail ] && [ "$rc" != 0 ]; }; then
    if [ -z "$rx" ] || printf '%s' "$out" | grep -qE -- "$rx"; then P=$((P+1)); echo "PASS  $name"; return; fi
  fi
  F=$((F+1)); echo "FAIL  $name (rc=$rc)"; printf '%s\n' "$out" | sed 's/^/      /' | head -8
}

# --- context
run "context: manual, main, attempt 1, suite D4"   pass "" -- env EVENT=workflow_dispatch REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/main RUN_ATTEMPT=1 SUITE=D4 bash -c ". '$CHECKS'; check_context"
for e in push pull_request schedule deployment; do
  run "context: event $e refused" fail "not by the Run workflow button" -- env EVENT=$e REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/main RUN_ATTEMPT=1 SUITE=D4 STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging bash -c ". '$CHECKS'; check_context"
done
run "context: wrong branch refused" fail "not refs/heads/main" -- env EVENT=workflow_dispatch REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/a4-hosted-verification RUN_ATTEMPT=1 SUITE=D4 STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging bash -c ". '$CHECKS'; check_context"
run "context: re-run refused" fail "re-runs are refused" -- env EVENT=workflow_dispatch REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/main RUN_ATTEMPT=2 SUITE=D4 STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging bash -c ". '$CHECKS'; check_context"
for s in ALL all "" "D4 D2" P3P4; do
  run "context: suite '$s' refused" fail "test suite must be exactly one of" -- env EVENT=workflow_dispatch REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/main RUN_ATTEMPT=1 "SUITE=$s" STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging bash -c ". '$CHECKS'; check_context"
done
for s in P1-P2 D1 D3 D4 D2 P3 P4; do
  run "context: suite $s accepted" pass "" -- env EVENT=workflow_dispatch REPO=highandmighty354-dev/gearhead-labs-staging GIT_REF=refs/heads/main RUN_ATTEMPT=1 SUITE=$s STAGING_REPOSITORY=highandmighty354-dev/gearhead-labs-staging bash -c ". '$CHECKS'; check_context"
done

# --- target (the reference function, unchanged)
tgt() { env EXPECTED_TEST_REF="$1" POOLER_URL="$2" CONFIRM_REF="$3" STAGING_REPOSITORY=x PRODUCTION_REF=$PROD bash -c ". '$CHECKS'; verify_target"; }
run "target: staging ref, pooler, typed ref and supabase-config.js agree" pass "^$REF$" -- tgt "$REF" "$POOL" "$REF"
run "target: wrong typed project refused" fail "typed ref does not match" -- tgt "$REF" "$POOL" "abcdefghijklmnopqrst"
run "target: production typed refused" fail "typed ref is the PRODUCTION" -- tgt "$REF" "$POOL" "$PROD"
run "target: production as EXPECTED_TEST_REF refused" fail "PRODUCTION" -- tgt "$PROD" "${POOL//$REF/$PROD}" "$PROD"
run "target: pooler URL for another project refused" fail "is for project" -- tgt "$REF" "${POOL//$REF/abcdefghijklmnopqrst}" "$REF"
run "target: pooler URL with a password refused" fail "unmodified Session pooler" -- tgt "$REF" "${POOL//\[YOUR-PASSWORD\]/hunter2}" "$REF"
run "target: missing EXPECTED_TEST_REF refused" fail "not set" -- tgt "" "$POOL" "$REF"

# --- origin
org() { env SUITE="$1" ANALYTICS_TEST_ORIGIN="$2" PRODUCTION_REF=$PROD bash -c ". '$CHECKS'; check_origin"; }
run "origin: not needed for P1-P2" pass "not needed" -- org P1-P2 ""
for s in D1 D3 D4 D2 P3 P4; do run "origin: missing refused for $s" fail "ANALYTICS_TEST_ORIGIN is not set" -- org $s ""; done
for o in http://a.example https://a.example/ https://a.example/x "https://A.example" https://localhost "https://a.example https://b.example"; do
  run "origin: malformed '$o' refused" fail "bare https origin" -- org D4 "$o"
done
for o in https://gearhead-labs.com https://www.gearhead-labs.com; do run "origin: production site $o refused" fail "PRODUCTION website" -- org D4 "$o"; done
run "origin: Supabase address refused" fail "Supabase address" -- org D4 "https://$REF.supabase.co"
run "origin: well-formed TEST origin accepted" pass "Origin OK" -- org D4 "https://highandmighty354-dev.github.io"

# --- prerequisites
pre() { env SUITE="$1" HV_PROBE_STATUS="$2" HV_RECEIVER_STATUS="$3" HV_TEST_WINDOW="$4" bash -c ". '$CHECKS'; check_prerequisites"; }
run "prereq: P1-P2 needs none" pass "none" -- pre P1-P2 "" "" ""
for s in D1 D3; do
  run "prereq: $s refused without the A9 probe" fail "HV_PROBE_STATUS" -- pre $s "" deployed-under-A7 open-under-A8
  run "prereq: $s refused with a loose value" fail "HV_PROBE_STATUS" -- pre $s deployed "" ""
  run "prereq: $s accepted with the A9 attestation" pass "A9" -- pre $s deployed-under-A9 "" ""
done
for s in D4 D2 P3 P4; do
  run "prereq: $s refused without the A7 receiver" fail "HV_RECEIVER_STATUS" -- pre $s deployed-under-A9 "" open-under-A8
  run "prereq: $s refused without the A8 window" fail "HV_TEST_WINDOW" -- pre $s "" deployed-under-A7 ""
  run "prereq: $s accepted with both attestations" pass "A7.*A8" -- pre $s "" deployed-under-A7 open-under-A8
done

# --- the approval gate and other runs, against a fake GitHub API
FAKE="$T/fake"; mkdir -p "$FAKE/bin"
cat > "$FAKE/bin/curl" <<'EOF'
#!/usr/bin/env bash
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -H) shift 2 ;; --proto) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac; done
echo "$url" >> "$FAKE_DIR/calls.log"
case "$url" in
  */environments/staging-db/deployment-branch-policies) f="$FAKE_DIR/branches.json" ;;
  */environments/staging-db) f="$FAKE_DIR/env.json" ;;
  */actions/workflows/*/runs*) wf="${url#*/actions/workflows/}"; wf="${wf%%/*}"; st="${url#*status=}"; st="${st%%&*}"; f="$FAKE_DIR/runs-$wf-$st.json"
    [ -f "$f" ] || f="$FAKE_DIR/runs-empty.json" ;;
  *) exit 22 ;;
esac
[ -f "$f" ] || exit 22
cp "$f" "$out"
EOF
chmod +x "$FAKE/bin/curl"
good_env='{"name":"staging-db","protection_rules":[{"type":"required_reviewers","prevent_self_review":false,"reviewers":[{"type":"User","reviewer":{"login":"highandmighty354-dev"}}]},{"type":"branch_policy"}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
good_br='{"total_count":1,"branch_policies":[{"name":"main","type":"branch"}]}'
gate() { env PATH="$FAKE/bin:$PATH" FAKE_DIR="$FAKE" GITHUB_API_URL=https://api.github.com GH_TOKEN=t REPO=highandmighty354-dev/gearhead-labs-staging \
             APPROVAL_ENVIRONMENT=staging-db APPROVER_LOGIN=highandmighty354-dev RUNNER_TEMP="$T" RUN_ID=777 bash -c ". '$CHECKS'; $1"; }
setenv() { printf '%s' "$1" > "$FAKE/env.json"; printf '%s' "$2" > "$FAKE/branches.json"; }
setenv "$good_env" "$good_br"
run "gate: staging-db requires the owner, main only" pass "Approval gate OK" -- gate check_environment
setenv "${good_env//highandmighty354-dev/someone-else}" "$good_br"
run "gate: another reviewer refused" fail "required reviewers must be exactly" -- gate check_environment
setenv '{"name":"staging-db","protection_rules":[{"type":"branch_policy"}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' "$good_br"
run "gate: no required reviewer refused" fail "no required-reviewers rule" -- gate check_environment
setenv "${good_env//\"prevent_self_review\":false/\"prevent_self_review\":true}" "$good_br"
run "gate: self-review prevention ON refused" fail "Prevent self-review" -- gate check_environment
setenv "$good_env" '{"total_count":2,"branch_policies":[{"name":"main","type":"branch"},{"name":"*","type":"branch"}]}'
run "gate: extra deployment branch refused" fail "only allowed deployment branch" -- gate check_environment
setenv "${good_env//\"custom_branch_policies\":true/\"custom_branch_policies\":false}" "$good_br"
run "gate: all branches allowed refused" fail "Selected branches" -- gate check_environment
rm -f "$FAKE/env.json"
run "gate: Environment missing refused" fail "could not read Environment" -- gate check_environment
setenv "$good_env" "$good_br"
echo '{"total_count":0,"workflow_runs":[]}' > "$FAKE/runs-empty.json"
run "runs: nothing else active" pass "No other staging" -- gate check_no_other_runs
echo '{"workflow_runs":[{"id":777,"run_number":9}]}' > "$FAKE/runs-hosted-verification.yml-in_progress.json"
run "runs: this run itself is not counted" pass "No other staging" -- gate check_no_other_runs
echo '{"workflow_runs":[{"id":555,"run_number":9}]}' > "$FAKE/runs-staging-migrations.yml-in_progress.json"
run "runs: an active migration run refused" fail "staging-migrations.yml" -- gate check_no_other_runs
rm -f "$FAKE/runs-staging-migrations.yml-in_progress.json"
echo '{"workflow_runs":[{"id":556,"run_number":3}]}' > "$FAKE/runs-staging-validation.yml-waiting.json"
run "runs: a validation run waiting for approval refused" fail "staging-validation.yml" -- gate check_no_other_runs
rm -f "$FAKE/runs-staging-validation.yml-waiting.json"
echo '{"workflow_runs":[{"id":778,"run_number":10}]}' > "$FAKE/runs-hosted-verification.yml-queued.json"
run "runs: another queued hosted-verification run refused" fail "hosted-verification.yml" -- gate check_no_other_runs

# --- the copied reference functions are byte-for-byte the migration workflow's
ref_src="$(git show HEAD:.github/workflows/staging-migrations.yml)"
want="$( { printf '%s\n' "$ref_src" | awk '/^    verify_target\(\) \{$/{on=1} on{print} on&&/^    \}$/{exit}'; echo
           printf '%s\n' "$ref_src" | awk '/^    # --- G\. TLS/{on=1} /^jobs:$/{exit} on{print}' | sed '${/^$/d}'; } | sed 's/^    //')"
have="$(sed -n '/^verify_target() {$/,$p' "$CHECKS")"
if [ "$(printf '%s\n' "$want" | sha256sum)" = "$(printf '%s\n' "$have" | sha256sum)" ]; then P=$((P+1)); echo "PASS  reference functions identical to staging-migrations.yml"
else F=$((F+1)); echo "FAIL  reference functions differ from staging-migrations.yml"; diff <(printf '%s\n' "$want") <(printf '%s\n' "$have") | head -20; fi

echo; echo "checks.sh tests: $P passed, $F failed"
[ "$F" = 0 ]
