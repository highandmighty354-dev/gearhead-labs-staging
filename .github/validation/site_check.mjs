// Gearhead Labs staging validation: the hosted website and the public web API. No secret is used or needed:
// the only key involved is the publishable key that the website itself serves to every visitor.
//
//   node site_check.mjs integrity   A1 every published file served byte-identical to this commit; A2 served config names the
//                                   staging project only, and no served code names any other Supabase project or a secret key;
//                                   A4 every published HTML page carries exactly one robots directive, "noindex", and every
//                                   Markdown file is kept off the site (_config.yml lists exactly them; each answers 404)
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

// The staging site and nothing else. SITE_URL must be exactly this address: HTTPS, this host (no port, no credentials),
// this path, no query and no fragment. Production is served from the same host under another path, and any other host is
// refused even with the same path. Anything else stops before a single request is sent.
const STAGING_SITE = 'https://highandmighty354-dev.github.io/gearhead-labs-staging/';
function stagingSite() {
  const s = env('SITE_URL');
  let u;
  try { u = new URL(s); } catch (e) { die(`SITE_URL ${s} is not a valid address`); }
  const why = [];
  if (u.protocol !== 'https:') why.push(`protocol is ${u.protocol}, not https:`);
  if (u.hostname !== 'highandmighty354-dev.github.io' || u.port !== '') why.push(`host is ${u.host}, not highandmighty354-dev.github.io`);
  if (u.username || u.password) why.push('it carries credentials');
  if (u.pathname !== '/gearhead-labs-staging/') why.push(`path is ${u.pathname}, not /gearhead-labs-staging/`);
  if (u.search || u.hash || s.includes('?') || s.includes('#')) why.push('it has a query or fragment');
  if (s !== STAGING_SITE && !why.length) why.push(`it is not written exactly as ${STAGING_SITE}`);
  if (why.length) die(`SITE_URL ${s} is not the staging site: ${why.join('; ')}`);
  return s;
}

// A4: the robots directives a crawler would read from a page. Every <meta> named "robots" or naming a crawler ("...bot",
// e.g. googlebot, bingbot) is counted, wherever it sits. A page passes only with exactly one such tag, inside <head>,
// named "robots", whose content is exactly "noindex"; a second or crawler-specific tag could re-allow indexing.
function robotsProblems(html) {
  const attr = (tag, a) => {
    const m = new RegExp(`\\s${a}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'>]+))`, 'i').exec(tag);
    return m ? (m[1] ?? m[2] ?? m[3]) : null;
  };
  const isRobots = (tag) => /^(robots|[a-z0-9_-]*bot[a-z0-9_-]*)$/i.test((attr(tag, 'name') || '').trim());
  const head = /<head(?:\s[^>]*)?>([\s\S]*?)<\/head\s*>/i.exec(html);
  if (!head) return ['no <head> element'];
  const all = [...html.matchAll(/<meta\b[^>]*>/gi)].map((m) => m[0]).filter(isRobots);
  const inHead = [...head[1].matchAll(/<meta\b[^>]*>/gi)].map((m) => m[0]).filter(isRobots);
  if (all.length === 0) return ['no robots meta tag'];
  const out = [];
  if (all.length !== 1) out.push(`${all.length} robots-type meta tags (${all.map((t) => attr(t, 'name')).join(', ')}), not exactly 1`);
  if (inHead.length !== all.length) out.push(`${all.length - inHead.length} robots-type meta tag(s) outside <head>`);
  for (const t of all) {
    const name = (attr(t, 'name') || '').trim(), content = (attr(t, 'content') || '').replace(/\s+/g, '').toLowerCase();
    if (name.toLowerCase() !== 'robots') out.push(`crawler-specific tag name="${name}"`);
    if (content !== 'noindex') out.push(`name="${name}" content="${attr(t, 'content')}", not "noindex"`);
  }
  return out;
}

// A4: the exclusions in _config.yml, read strictly. Only comments, blank lines, one "exclude:" key and its list of quoted
// paths are allowed, so the file cannot quietly change anything else about how Pages builds the site.
function configExcludes(repoDir) {
  const p = path.join(repoDir, '_config.yml');
  if (!fs.existsSync(p)) return { error: '_config.yml is missing' };
  const items = [];
  let key = false;
  for (const [i, line] of fs.readFileSync(p, 'utf8').split('\n').entries()) {
    if (/^\s*(#.*)?$/.test(line)) continue;
    if (line === 'exclude:' && !key) { key = true; continue; }
    const m = /^  - "([^"\\]+)"$/.exec(line);
    if (key && m) { items.push(m[1]); continue; }
    return { error: `_config.yml line ${i + 1} is not a comment, the single "exclude:" key or a quoted list entry: ${JSON.stringify(line)}` };
  }
  if (!key) return { error: '_config.yml has no "exclude:" list' };
  return { items };
}

// GitHub Pages publishes the branch through Jekyll: paths with a dot-folder or dot-file segment are not published, and
// Markdown is kept off the site by _config.yml (A4). Everything else must be served byte-for-byte. Nothing else is excluded.
function publishedFiles(repoDir) {
  const all = execFileSync('git', ['-C', repoDir, 'ls-files', '-z'], { maxBuffer: 1 << 26 }).toString().split('\0').filter(Boolean);
  const dot = (p) => p.split('/').some((s) => s.startsWith('.') || s.startsWith('_'));
  const md = (p) => /\.(md|markdown)$/i.test(p);
  return { published: all.filter((p) => !dot(p) && !md(p)), skippedDot: all.filter(dot), skippedMd: all.filter((p) => !dot(p) && md(p)) };
}

async function integrity() {
  const SITE = stagingSite(), REPO = env('REPO_DIR'), OUT = env('OUT_DIR'), RUN = env('RUN_ID');
  publishableKey(REPO);
  const { published, skippedDot, skippedMd } = publishedFiles(REPO);
  if (published.length < 100) die(`only ${published.length} published files found: wrong checkout?`);
  const htmlPages = published.filter((p) => /\.html?$/i.test(p));
  if (!htmlPages.includes('index.html')) die('index.html is not among the published files: wrong checkout?');
  fs.mkdirSync(OUT, { recursive: true });
  let same = 0, noindexOk = 0;
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
    if (/\.html?$/i.test(rel)) {   // A4 on the bytes actually served (already proven identical to this commit)
      const issues = robotsProblems(r.body.toString('utf8'));
      if (issues.length) for (const m of issues) bad(`A4: ${rel}: ${m}`); else noindexOk++;
    }
    if (rel.startsWith('F1_12_4_') && rel.endsWith('.html')) fs.writeFileSync(path.join(OUT, rel), r.body);   // engine for B2
  }
  console.log(`A1: ${same} of ${published.length} published files served byte-identical; not published by design: ` +
              `${skippedDot.length} dot-path file(s), ${skippedMd.length} Markdown file(s) (excluded by _config.yml; see A4)`);
  // A4: every published HTML page was served and carries the noindex directive. A page that could not be fetched, or was
  // served with other bytes, was not checked and counts as a failure here as well as in A1.
  if (noindexOk !== htmlPages.length) bad(`A4: ${htmlPages.length - noindexOk} of ${htmlPages.length} published HTML page(s) not confirmed noindex`);
  // A4: Markdown is kept off the site. _config.yml must list exactly the Markdown files, and for each one the raw file, the
  // page Jekyll would render from it, and that page's extension-less address must all answer exactly 404. Any other status,
  // a redirect or a failed request is a failure.
  const cfgEx = configExcludes(REPO);
  if (cfgEx.error) bad(`A4: ${cfgEx.error}`);
  else {
    const want = [...skippedMd].sort(), got = [...cfgEx.items].sort();
    const missing = want.filter((p) => !got.includes(p)), extra = got.filter((p) => !want.includes(p));
    if (new Set(got).size !== got.length) bad('A4: _config.yml lists a path more than once');
    if (missing.length) bad(`A4: _config.yml does not exclude ${missing.length} Markdown file(s): ${missing.join(', ')}`);
    if (extra.length) bad(`A4: _config.yml excludes ${extra.length} path(s) that are not tracked Markdown files: ${extra.join(', ')}`);
  }
  let gone = 0, probes = 0;
  for (const rel of skippedMd) {
    const base = rel.replace(/\.(md|markdown)$/i, '');
    for (const variant of [rel, `${base}.html`, base]) {
      probes++;
      const url = SITE + variant.split('/').map(encodeURIComponent).join('/') + `?gh_validation=${RUN}`;
      let r;
      try { r = await get(url); } catch (e) { bad(`A4: ${variant} (from ${rel}): request failed or was redirected (${e.message})`); continue; }
      if (r.status === 404) gone++;
      else bad(`A4: ${variant} (from ${rel}) answered HTTP ${r.status}, not 404${r.status < 300 ? ': it is published' : ''}`);
    }
  }
  console.log(`A4: ${noindexOk} of ${htmlPages.length} published HTML page(s) carry exactly one robots directive, "noindex"; ` +
              `${skippedMd.length} Markdown file(s) excluded by _config.yml, ${gone} of ${probes} address(es) answer 404`);
  const cfg = await get(SITE + 'supabase-config.js?gh_validation=' + RUN);
  const served = cfg.status === 200 ? [...cfg.body.toString('utf8').matchAll(/https:\/\/([a-z0-9]{20})\.supabase\.co/g)].map((m) => m[1]) : [];
  if (served.length !== 1 || served[0] !== STAGING_REF) bad(`A2: served supabase-config.js names ${JSON.stringify(served)}, not exactly ${STAGING_REF}`);
  else console.log(`A2: served supabase-config.js names only ${API}`);
  finish('deployment integrity (A1, A2, A4)');
}

async function live() {
  const SITE = stagingSite(), RUN = env('RUN_ID');
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
