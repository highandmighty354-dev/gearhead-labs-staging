"""Structural tests for .github/workflows/hosted-verification.yml. LOCAL ONLY (needs PyYAML).
  python3 .github/hosted-verification/tests/test_workflow.py        (from the repository root)
"""
import os
import re
import subprocess
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
WF = os.path.join(ROOT, ".github", "workflows", "hosted-verification.yml")
REF_WF = os.path.join(ROOT, ".github", "workflows", "staging-migrations.yml")


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WF) as f:
            cls.text = f.read()
        cls.wf = yaml.safe_load(cls.text)
        cls.jobs = cls.wf["jobs"]

    def steps(self, job):
        return self.jobs[job]["steps"]

    def test_manual_dispatch_only(self):
        on = self.wf[True]                      # PyYAML reads the key "on" as True
        self.assertEqual(list(on), ["workflow_dispatch"], "no push, pull_request, schedule or deployment trigger")

    def test_inputs_one_suite_no_all(self):
        inp = self.wf[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inp), ["confirm_project_ref", "suite"])
        self.assertEqual(inp["suite"]["type"], "choice")
        self.assertEqual(inp["suite"]["options"], ["P1-P2", "D1", "D3", "D4", "D2", "P3", "P4"])
        self.assertTrue(inp["suite"]["required"] and inp["confirm_project_ref"]["required"])
        self.assertNotIn("default", inp["confirm_project_ref"])

    def test_permissions_and_concurrency(self):
        self.assertEqual(self.wf["permissions"], {})
        for j in self.jobs.values():
            self.assertEqual(j["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(self.wf["concurrency"], {"group": "staging-migrations", "cancel-in-progress": False})
        for other in ("staging-migrations.yml", "staging-validation.yml"):
            with open(os.path.join(ROOT, ".github", "workflows", other)) as f:
                self.assertEqual(yaml.safe_load(f)["concurrency"], self.wf["concurrency"], f"same repository-wide group as {other}")

    def test_only_the_approved_job_has_the_environment_and_secret(self):
        self.assertNotIn("environment", self.jobs["preflight"])
        self.assertEqual(self.jobs["verify"]["environment"], "staging-db")
        self.assertEqual(self.jobs["verify"]["needs"], "preflight")
        secret_steps = [(job, s.get("name")) for job in self.jobs for s in self.steps(job) if "secrets." in yaml.safe_dump(s)]
        self.assertEqual(secret_steps, [("verify", "Run suite ${{ inputs.suite }} (the only step that receives the password)")])
        self.assertEqual(self.text.count("secrets."), 1)
        self.assertNotRegex(self.text, r"(?i)service_role|SERVICE_ROLE_KEY|apikey")

    def test_no_request_before_approval(self):
        pre = yaml.safe_dump(self.jobs["preflight"])
        self.assertNotIn("hv.py", pre, "the runner never runs in preflight")
        self.assertNotRegex(pre, r"supabase\.co|functions/v1", "preflight never contacts the TEST project's HTTP endpoints")
        runner_steps = [(job, s["name"]) for job in self.jobs for s in self.steps(job) if "hv.py" in s.get("run", "")]
        self.assertEqual(runner_steps, [("verify", "Run suite ${{ inputs.suite }} (the only step that receives the password)")])
        names = [s.get("name", "") for s in self.steps("verify")]
        i_run = [n.startswith("Run suite") for n in names].index(True)
        self.assertLess(names.index("Repeat every check after approval (no secret in this step)"), i_run)

    def test_checks_repeated_after_approval(self):
        rep = [s for s in self.steps("verify") if s.get("name") == "Repeat every check after approval (no secret in this step)"][0]["run"]
        for fn in ("check_context", "verify_target", "check_origin", "check_prerequisites", "check_environment", "check_no_other_runs"):
            self.assertIn(fn, rep)
        tls = [s for s in self.steps("verify") if "TLS" in s.get("name", "")]
        self.assertTrue(tls and "tls_probe" in tls[0]["run"])

    def test_actions_pinned_by_sha(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^[\w-]+/[\w-]+@[0-9a-f]{40}$", u)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", uses)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", uses)

    def test_no_input_expressions_inside_scripts(self):
        for job in self.jobs:
            for s in self.steps(job):
                self.assertNotRegex(s.get("run", ""), r"\$\{\{", f"{job}/{s.get('name')}: inputs reach scripts only through env")

    def test_password_handling_mirrors_reference(self):
        run = [s for s in self.steps("verify") if s.get("name", "").startswith("Run suite")][0]["run"]
        for needle in ['echo "::add-mask::$enc"', 'echo "::add-mask::$DB_URL"', "unset DB_PASSWORD enc",
                       "sslmode=verify-full&sslrootcert=$CA_FILE", "unset DB_URL"]:
            self.assertIn(needle, run)
        self.assertLess(run.index('echo "::add-mask::$DB_URL"'), run.index("hv.py"))
        self.assertNotRegex(run, r"\$DB_URL[^\n]*(>>?|\|\s*tee)", "the connection string is never written to a file or the summary")

    def test_pinned_roots_identical_to_reference(self):
        with open(REF_WF) as f:
            ref = yaml.safe_load(f)["env"]
        env = self.wf["env"]
        self.assertEqual(env["SUPABASE_ROOT_CA_PEM"], ref["SUPABASE_ROOT_CA_PEM"])
        self.assertEqual(env["SUPABASE_ROOT_CA_SHA256_FINGERPRINTS"], ref["SUPABASE_ROOT_CA_SHA256_FINGERPRINTS"])
        for k in ("PRODUCTION_REF", "STAGING_REPOSITORY", "APPROVAL_ENVIRONMENT", "APPROVER_LOGIN"):
            self.assertEqual(env[k], ref[k], k)

    def test_evidence_uploaded_even_on_failure(self):
        up = [s for s in self.steps("verify") if "upload-artifact" in s.get("uses", "")][0]
        self.assertEqual(up["if"], "always()")
        self.assertEqual(self.steps("verify")[-1], up)

    def test_existing_workflows_untouched(self):
        out = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", "origin/main", "--", ".github/workflows/staging-migrations.yml",
                              ".github/workflows/staging-validation.yml", ".github/validation", "supabase", "supabase-config.js"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            self.skipTest("origin/main not available")
        self.assertEqual(out.stdout.strip(), "", "existing workflows, validation files and migrations are unchanged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
