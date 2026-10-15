// Cloud side of the pipeline (Supabase Edge Function + Postgres).
//   GET  /health                       link check used by the device
//   POST /ingest                       chunks + losses, in index order per sensor; idempotent
//   GET  /status?session=              counts held in the cloud
//   GET  /export?session=&sensor=&from=&to=   samples page (raw chunks) + losses
//   GET  /results?session=             online results
// Every request except /health must carry x-device-token.
import postgres from "npm:postgres@3.4.5";
import { type Loader, newState, processWindow, ready, type Samples, type State, W } from "./processing.ts";

const sql = postgres(Deno.env.get("SUPABASE_DB_URL")!, { max: 1, prepare: false, idle_timeout: 3, connect_timeout: 10 });
const TOKEN = Deno.env.get("DEVICE_TOKEN") ?? "";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS hb2_chunks (
  session_id text NOT NULL, sensor int NOT NULL, first_index bigint NOT NULL, count int NOT NULL,
  data text NOT NULL, rx_time double precision, received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, sensor, first_index));
CREATE TABLE IF NOT EXISTS hb2_losses (
  session_id text NOT NULL, sensor int NOT NULL, from_index bigint NOT NULL, to_index bigint,
  reason text NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, sensor, from_index));
CREATE TABLE IF NOT EXISTS hb2_progress (
  session_id text NOT NULL, sensor int NOT NULL, known_next bigint NOT NULL DEFAULT 0,
  ended boolean NOT NULL DEFAULT false, PRIMARY KEY (session_id, sensor));
CREATE TABLE IF NOT EXISTS hb2_proc_state (session_id text PRIMARY KEY, state text NOT NULL);
CREATE TABLE IF NOT EXISTS hb2_results (
  session_id text NOT NULL, "window" int NOT NULL, mode text NOT NULL, payload text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (session_id, "window", mode));
ALTER TABLE hb2_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE hb2_losses ENABLE ROW LEVEL SECURITY;
ALTER TABLE hb2_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE hb2_proc_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE hb2_results ENABLE ROW LEVEL SECURITY;`;

let schemaReady: Promise<unknown> | null = null;
const ensureSchema = () => (schemaReady ??= sql.unsafe(SCHEMA).catch((e) => { schemaReady = null; throw e; }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Item =
  | { type: "chunk"; sensor: number; first_index: number; data: number[][]; rx_time?: number }
  | { type: "loss"; sensor: number; from_index: number; to_index: number | null; reason: string }
  | { type: "end"; sensor: number; at: number };

async function readBody(req: Request): Promise<{ session_id: string; items: Item[] }> {
  if (req.headers.get("x-body-encoding") === "gzip") {
    const stream = req.body!.pipeThrough(new DecompressionStream("gzip"));
    return JSON.parse(await new Response(stream).text());
  }
  return await req.json();
}

async function ingest(req: Request) {
  const { session_id: sid, items } = await readBody(req);
  const t0 = performance.now();
  const results = await sql.begin(async (tx) => {
    // serialise work per session so retries/overlaps cannot race on processing state
    await tx`SELECT pg_advisory_xact_lock(hashtext(${sid}))`;
    await tx`INSERT INTO hb2_progress (session_id, sensor) VALUES (${sid}, 1), (${sid}, 2) ON CONFLICT DO NOTHING`;
    const prog = await tx`SELECT sensor, known_next, ended FROM hb2_progress WHERE session_id = ${sid}`;
    const known: Record<number, number> = {}, ended: Record<number, boolean> = {};
    for (const p of prog) { known[p.sensor] = Number(p.known_next); ended[p.sensor] = p.ended; }

    const chunkRows = [], lossRows = [];
    const pos = (x: Item) => x.type === "chunk" ? x.first_index : x.type === "loss" ? x.from_index : x.at;
    const sorted = [...items].sort((a, b) => a.sensor - b.sensor || pos(a) - pos(b));
    for (const it of sorted) {
      if (it.type === "end") {
        // session ended cleanly for this sensor at index `at`
        if (it.at === known[it.sensor]) ended[it.sensor] = true;
      } else if (it.type === "chunk") {
        chunkRows.push({ session_id: sid, sensor: it.sensor, first_index: it.first_index, count: it.data.length,
          data: JSON.stringify(it.data), rx_time: it.rx_time ?? null });
        if (it.first_index === known[it.sensor]) known[it.sensor] += it.data.length;
      } else {
        lossRows.push({ session_id: sid, sensor: it.sensor, from_index: it.from_index, to_index: it.to_index, reason: it.reason });
        if (it.from_index === known[it.sensor]) {
          if (it.to_index === null) ended[it.sensor] = true;
          else known[it.sensor] = it.to_index + 1;
        }
      }
    }
    for (let k = 0; k < chunkRows.length; k += 200) {
      const part = chunkRows.slice(k, k + 200);
      await tx`INSERT INTO hb2_chunks ${tx(part, "session_id", "sensor", "first_index", "count", "data", "rx_time")} ON CONFLICT DO NOTHING`;
    }
    if (lossRows.length) {
      await tx`INSERT INTO hb2_losses ${tx(lossRows, "session_id", "sensor", "from_index", "to_index", "reason")} ON CONFLICT DO NOTHING`;
    }
    for (const s of [1, 2]) {
      await tx`UPDATE hb2_progress SET known_next = GREATEST(known_next, ${known[s]}), ended = ended OR ${ended[s]}
               WHERE session_id = ${sid} AND sensor = ${s}`;
    }
    return await processReady(tx, sid, known, ended);
  });
  return json({ ok: true, items: items.length, windows: results, ms: Math.round(performance.now() - t0) });
}

// deno-lint-ignore no-explicit-any
async function processReady(tx: any, sid: string, known: Record<number, number>, ended: Record<number, boolean>) {
  const st = await tx`SELECT state FROM hb2_proc_state WHERE session_id = ${sid}`;
  const state: State = st.length ? JSON.parse(st[0].state) : newState();
  if (!ready(state.next_window, known, ended)) return 0;

  // load everything the ready windows can need in one query
  const lo = Math.max(0, Math.min(state.scan_next - 800, W * (state.next_window - 4)));
  const hi = Math.max(known[1], known[2]) + 1;
  const rows = await tx`SELECT sensor, first_index, data, rx_time FROM hb2_chunks
    WHERE session_id = ${sid} AND first_index < ${hi} AND first_index + count > ${lo}`;
  const data: Record<number, Samples> = { 1: new Map(), 2: new Map() };
  const rx: { sensor: number; end: number; t: number }[] = [];
  for (const r of rows) {
    const arr = JSON.parse(r.data);
    const f = Number(r.first_index);
    arr.forEach((v: number[], k: number) => data[r.sensor].set(f + k, v));
    if (r.rx_time) rx.push({ sensor: r.sensor, end: f + arr.length, t: r.rx_time });
  }
  const load: Loader = (s, a, b) => {
    const m: Samples = new Map();
    for (let i = Math.max(0, a); i < b; i++) { const v = data[s].get(i); if (v) m.set(i, v); }
    return m;
  };

  const out = [];
  while (ready(state.next_window, known, ended)) {
    const w = state.next_window;
    const res = processWindow(state, w, load);
    // when the device received the last sample this window depends on (device clock, informational)
    const needEnd = W * (w + 1);
    const ts = rx.filter((x) => x.end >= needEnd && x.end - needEnd < W * 2).map((x) => x.t);
    out.push({ session_id: sid, window: w, mode: "online",
      payload: JSON.stringify({ session: sid, window: w, mode: "online", ...res,
        device_received_at: ts.length ? Math.min(...ts) : null }) });
  }
  for (let k = 0; k < out.length; k += 200) {
    await tx`INSERT INTO hb2_results ${tx(out.slice(k, k + 200), "session_id", "window", "mode", "payload")} ON CONFLICT DO NOTHING`;
  }
  await tx`INSERT INTO hb2_proc_state (session_id, state) VALUES (${sid}, ${JSON.stringify(state)})
           ON CONFLICT (session_id) DO UPDATE SET state = excluded.state`;
  return out.length;
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const path = url.pathname.replace(/^.*\/pipeline/, "") || "/";
  try {
    if (path === "/health") return json({ ok: true, t: Date.now() / 1000 });
    if (!TOKEN || req.headers.get("x-device-token") !== TOKEN) return json({ error: "unauthorized" }, 401);
    await ensureSchema();
    const sid = url.searchParams.get("session") ?? "";
    if (path === "/ingest" && req.method === "POST") return await ingest(req);
    if (path === "/status") {
      const [c] = await sql`SELECT COALESCE(SUM(count),0)::bigint AS n FROM hb2_chunks WHERE session_id = ${sid}`;
      const p = await sql`SELECT sensor, known_next, ended FROM hb2_progress WHERE session_id = ${sid}`;
      return json({ samples: Number(c.n), progress: p });
    }
    if (path === "/export") {
      const sensor = Number(url.searchParams.get("sensor"));
      const from = Number(url.searchParams.get("from") ?? 0), to = Number(url.searchParams.get("to") ?? 2 ** 40);
      const chunks = await sql`SELECT first_index, data FROM hb2_chunks WHERE session_id = ${sid} AND sensor = ${sensor}
        AND first_index >= ${from} AND first_index < ${to} ORDER BY first_index`;
      const losses = await sql`SELECT sensor, from_index, to_index, reason FROM hb2_losses WHERE session_id = ${sid}
        ORDER BY sensor, from_index`;
      const [p] = await sql`SELECT known_next FROM hb2_progress WHERE session_id = ${sid} AND sensor = ${sensor}`;
      return json({ chunks: chunks.map((c) => [Number(c.first_index), c.data]), losses, known_next: p ? Number(p.known_next) : 0 });
    }
    if (path === "/results") {
      const r = await sql`SELECT payload, created_at FROM hb2_results WHERE session_id = ${sid} ORDER BY "window"`;
      return json(r.map((x) => ({ ...JSON.parse(x.payload), processed_at: new Date(x.created_at).getTime() / 1000 })));
    }
    return json({ error: "not found" }, 404);
  } catch (e) {
    console.error(e);
    return json({ error: String(e) }, 500);
  }
});
