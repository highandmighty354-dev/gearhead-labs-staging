// LOCAL TEST ONLY: serves the REAL receiver handler (supabase/functions/_shared/analytics-ingest.js from gearhead-labs, passed by
// path and checked against its pinned SHA-256) on 127.0.0.1, wired to a LOCAL PostgreSQL as service_role, so hv.py can be
// exercised against the unmodified handler code. Needs the npm package "pg" (resolved from NODE_PATH). Never used on staging.
//   node real_receiver.mjs <analytics-ingest.js> <postgres url> <allowed origin>      -> prints "PORT <n>"
import http from 'node:http';
import { Readable } from 'node:stream';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const [file, dbUrl, origin] = process.argv.slice(2);
const PINNED = '5687519b0a81b30864b9a2d92a1c90f28beadcd080144b11a1098156fd22dd10';
if (createHash('sha256').update(readFileSync(file)).digest('hex') !== PINNED) { console.error('receiver file hash mismatch'); process.exit(1); }
const { makeAnalyticsIngestHandler } = await import(pathToFileURL(file).href);
const pg = createRequire(process.env.NODE_PATH + '/')('pg');
const handler = makeAnalyticsIngestHandler({
  enabled: process.env.RR_ENABLED !== 'false',
  config: { env: 'test', allowedOrigins: [origin], addressSource: 'none' },
  log: (m) => console.log('LOG ' + m),
  rpc: async (fn, args) => {
    const c = new pg.Client({ connectionString: dbUrl, application_name: 'real-receiver' });
    await c.connect();
    try {
      await c.query('SET ROLE service_role');
      await c.query(`SELECT public.${fn}($1::jsonb, $2::jsonb)`, [JSON.stringify(args.p_batch), JSON.stringify(args.p_meta)]);
      return { error: null };
    } catch (e) { return { error: { code: e.code } }; } finally { await c.end(); }
  },
});
const srv = http.createServer(async (req, res) => {
  if (req.url !== '/functions/v1/analytics-ingest') { res.writeHead(404); res.end(); return; }
  const hasBody = !['GET', 'HEAD', 'OPTIONS'].includes(req.method);
  const request = new Request('http://127.0.0.1' + req.url, { method: req.method, headers: req.headers,
    ...(hasBody ? { body: Readable.toWeb(req), duplex: 'half' } : {}) });
  const r = await handler(request);
  res.writeHead(r.status, Object.fromEntries(r.headers));
  res.end(() => { if (hasBody) req.destroy(); });   // after the answer is sent, an unread remainder is abandoned
});
srv.listen(0, '127.0.0.1', () => console.log('PORT ' + srv.address().port));
