// Gearhead Labs staging validation: the hosted website and the public web API. No secret is used or needed:
// the only key involved is the publishable key that the website itself serves to every visitor.
//
//   node site_check.mjs integrity   A1 every published file served byte-identical to this commit; A2 served config names the
//                                   staging project only, and no served code names any other Supabase project or a secret key
//   node site_check.mjs live        A3 the live page boots, connects to the staging project only, with 0 page errors
//   node site_check.mjs anon        S3 with only the publishable key: every table read/insert/update/delete and every callable
//                                   function is refused; trigger functions are not exposed at all
//
// Inputs (environment): SITE_URL, STAGING_REF, EXPECTED_JSON, REPO_DIR, OUT_DIR, RUN_ID. Exit 0 = PASS, 1 = HARD FAIL.
// Requests that could change data are built so they cannot: inserts send an empty list, updates and deletes target a
// key that cannot exist. Nothing here retries a failed check or treats an unreachable site as a pass.
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const mode = process.argv[2];
const env = (k) => { const v = process.env[k]; if (!v) die(`environment value ${k} is missing`); return v; };
const problems = [];
const bad = (m) => problems.push(m);
function die(m) { console.error(`HARD FAIL: ${m}`); process.exit(1); }
function finish(label) {
  if (problems.length) { for (const p of problems) console.error(`HARD FAIL: ${p}`); console.log(`${label}: FAILED (${problems.length} problem(s))`); process.exit(1); }
  console.log(`${label}: PASS`); process.exit(0);
}

const STAGING_REF = env('STAGING_REF');
if (!/^[a-z0-9]{20}$/.test(STAGING_REF)) die('STAGING_REF is not a 20-character project ref');
const API = `https://${STAGING_REF}.supabase.co`;
const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');
const REF_RE = /([a-z0-9]{20})\.supabase\.co/g;
const SECRET_RE = /sb_secret_[A-Za-z0-9_-]{8,}|eyJhbGciOi[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/;

function publishableKey(repoDir) {
  const cfg = fs.readFileSync(path.join(repoDir, 'supabase-config.js'), 'utf8');
  const urls = [...cfg.matchAll(/https:\/\/([a-z0-9]{20})\.supabase\.co/g)].map((m) => m[1]);
  if (urls.length !== 1 || urls[0] !== STAGING_REF) die(`supabase-config.js must name exactly the staging project ${STAGING_REF}, found ${JSON.stringify(urls)}`);
  const keys = [...cfg.matchAll(/(sb_publishable_[A-Za-z0-9_-]+)/g)].map((m) => m[1]);
  if (keys.length !== 1) die('supabase-config.js must hold exactly one publishable key (sb_publishable_...)');
  if (SECRET_RE.test(cfg)) die('supabase-config.js holds something that looks like a secret key');
  return keys[0];
}

async function get(url) {
  const r = await fetch(url, { redirect: 'error', headers: { 'cache-control': 'no-cache' } });
  return { status: r.status, body: Buffer.from(await r.arrayBuffer()) };
}

// GitHub Pages publishes the branch through Jekyll: paths with a dot-folder or dot-file segment are not published and
// Markdown is rendered to HTML. Everything else must be served byte-for-byte. Nothing else is excluded.
function publishedFiles(repoDir) {
  const all = execFileSync('git', ['-C', repoDir, 'ls-files', '-z'], { maxBuffer: 1 << 26 }).toString().split('\0').filter(Boolean);
  const dot = (p) => p.split('/').some((s) => s.startsWith('.') || s.startsWith('_'));
  const md = (p) => /\.(md|markdown)$/i.test(p);
  return { published: all.filter((p) => !dot(p) && !md(p)), skippedDot: all.filter(dot), skippedMd: all.filter((p) => !dot(p) && md(p)) };
}

async function integrity() {
  const SITE = env('SITE_URL'), REPO = env('REPO_DIR'), OUT = env('OUT_DIR'), RUN = env('RUN_ID');
  if (!SITE.endsWith('/')) die('SITE_URL must end with /');
  publishableKey(REPO);
  const { published, skippedDot, skippedMd } = publishedFiles(REPO);
  if (published.length < 100) die(`only ${published.length} published files found: wrong checkout?`);
  fs.mkdirSync(OUT, { recursive: true });
  let same = 0;
  for (const rel of published) {
    const url = SITE + rel.split('/').map(encodeURIComponent).join('/') + `?gh_validation=${RUN}`;
    let r;
    try { r = await get(url); } catch (e) { bad(`${rel}: request failed (${e.message})`); continue; }
    if (r.status !== 200) { bad(`${rel}: served with HTTP ${r.status}`); continue; }
    const local = fs.readFileSync(path.join(REPO, rel));
    if (sha256(r.body) !== sha256(local)) { bad(`${rel}: served bytes differ from this commit`); continue; }
    same++;
    if (/\.(js|mjs|html|json|css)$/i.test(rel)) {
      const text = r.body.toString('utf8');
      const refs = new Set([...text.matchAll(REF_RE)].map((m) => m[1]));
      for (const ref of refs) if (ref !== STAGING_REF) bad(`${rel}: names Supabase project ${ref}, which is not the staging project`);
      if (SECRET_RE.test(text)) bad(`${rel}: holds something that looks like a secret key`);
    }
    if (rel.startsWith('F1_12_4_') && rel.endsWith('.html')) fs.writeFileSync(path.join(OUT, rel), r.body);   // engine for B2
  }
  console.log(`A1: ${same} of ${published.length} published files served byte-identical; not published by design: ` +
              `${skippedDot.length} dot-path file(s), ${skippedMd.length} Markdown file(s) (rendered to HTML by Pages)`);
  const cfg = await get(SITE + 'supabase-config.js?gh_validation=' + RUN);
  const served = cfg.status === 200 ? [...cfg.body.toString('utf8').matchAll(/https:\/\/([a-z0-9]{20})\.supabase\.co/g)].map((m) => m[1]) : [];
  if (served.length !== 1 || served[0] !== STAGING_REF) bad(`A2: served supabase-config.js names ${JSON.stringify(served)}, not exactly ${STAGING_REF}`);
  else console.log(`A2: served supabase-config.js names only ${API}`);
  finish('deployment integrity (A1, A2)');
}

async function live() {
  const SITE = env('SITE_URL'), RUN = env('RUN_ID');
  let playwright;
  try { playwright = await import('playwright'); } catch (e) {
    const root = execFileSync('npm', ['root', '-g']).toString().trim();
    playwright = await import(path.join(root, 'playwright', 'index.mjs'));
  }
  const browser = await playwright.chromium.launch();
  const page = await browser.newPage();
  const pageErrors = [], consoleErrors = [], hosts = new Set();
  let connected = false;
  page.on('pageerror', (e) => pageErrors.push(String(e && e.message || e)));
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text());
    if (m.text().includes('Gearhead Labs: Supabase connected.')) connected = true;
  });
  page.on('request', (q) => { try { const h = new URL(q.url()).hostname; if (h.endsWith('.supabase.co') || h.endsWith('.supabase.in')) hosts.add(h); } catch (e) { /* ignore */ } });
  const resp = await page.goto(SITE + `?gh_validation=${RUN}`, { waitUntil: 'load', timeout: 60000 });
  if (!resp || resp.status() !== 200) bad(`A3: the staging page answered HTTP ${resp && resp.status()}`);
  const t0 = Date.now();
  while (!connected && Date.now() - t0 < 30000) await page.waitForTimeout(250);
  if (!connected) bad('A3: the page never logged "Gearhead Labs: Supabase connected." within 30 s');
  const clientUrl = await page.evaluate(() => (window.GH_SUPABASE && (window.GH_SUPABASE.supabaseUrl || (window.GH_SUPABASE.rest && window.GH_SUPABASE.rest.url))) || null);
  if (!clientUrl || !String(clientUrl).replace(/\/+$/, '').startsWith(API)) bad(`A3: the live client points at ${clientUrl}, not ${API}`);
  await page.waitForTimeout(3000);   // let late frame scripts report errors
  for (const h of hosts) if (h !== `${STAGING_REF}.supabase.co`) bad(`A3: the page contacted ${h}, which is not the staging project`);
  if (!hosts.has(`${STAGING_REF}.supabase.co`)) bad('A3: the page never contacted the staging project');
  for (const e of pageErrors) bad(`A3: page error: ${e}`);
  for (const e of consoleErrors) bad(`A3: console error: ${e}`);
  console.log(`A3: connected=${connected}, client ${clientUrl}, Supabase hosts contacted ${JSON.stringify([...hosts])}, page errors ${pageErrors.length}, console errors ${consoleErrors.length}`);
  await browser.close();
  finish('live page (A3)');
}

async function anon() {
  const REPO = env('REPO_DIR');
  const exp = JSON.parse(fs.readFileSync(env('EXPECTED_JSON'), 'utf8'));
  if (exp.staging_ref !== STAGING_REF) die('expected.json is for a different project');
  const key = publishableKey(REPO);
  const H = { apikey: key, 'content-type': 'application/json', prefer: 'return=minimal' };
  const base = `${API}/rest/v1/`;
  const call = async (method, url, body) => {
    const r = await fetch(url, { method, headers: method === 'GET' ? { apikey: key } : H, body, redirect: 'error' });
    const text = await r.text(); let code = '';
    try { code = JSON.parse(text).code || ''; } catch (e) { /* not JSON */ }
    return { status: r.status, code };
  };
  const tables = exp.exposed_tables;
  if (JSON.stringify(Object.keys(exp.primary_keys).sort()) !== JSON.stringify([...tables].sort())) die('expected.json: primary_keys does not cover exactly the exposed tables');
  let n = 0;
  for (const t of tables) {
    const [col, typ] = exp.primary_keys[t].split(':');
    const none = typ === 'uuid' ? '00000000-0000-0000-0000-000000000000' : 'gh_validation_no_such_row';
    const filter = `?${col}=eq.${encodeURIComponent(none)}`;
    const probes = [
      ['read', 'GET', `${base}${t}?select=*&limit=5`],
      ['insert', 'POST', `${base}${t}`, '[]'],
      ['update', 'PATCH', `${base}${t}${filter}`, JSON.stringify({ [col]: none })],
      ['delete', 'DELETE', `${base}${t}${filter}`],
    ];
    for (const [what, m, url, body] of probes) {
      n++;
      const r = await call(m, url, body);
      if (!(r.status === 401 && r.code === '42501')) bad(`S3 ${t} ${what}: expected 401 permission denied (42501), got ${r.status} ${r.code}`);
    }
  }
  for (const [fn, args] of exp.anon_rpc_probes) {
    n++;
    const r = await call('POST', `${base}rpc/${fn}`, JSON.stringify(args));
    if (!(r.status === 401 && r.code === '42501')) bad(`S3 function ${fn}: expected 401 permission denied (42501), got ${r.status} ${r.code}`);
  }
  for (const fn of exp.anon_rpc_not_exposed) {
    n++;
    const r = await call('POST', `${base}rpc/${fn}`, '{}');
    if (!(r.status === 404 && r.code === 'PGRST202')) bad(`S3 function ${fn}: expected not exposed (404 PGRST202), got ${r.status} ${r.code}`);
  }
  const root = await call('GET', base);
  n++;
  if (root.status !== 401) bad(`S3 API description: anonymous callers must not be able to list the API, got HTTP ${root.status}`);
  console.log(`S3: ${n} anonymous request(s) sent to ${API} with the publishable key only`);
  finish('anonymous web API (S3)');
}

if (mode === 'integrity') await integrity();
else if (mode === 'live') await live();
else if (mode === 'anon') await anon();
else die('usage: node site_check.mjs integrity|live|anon');
