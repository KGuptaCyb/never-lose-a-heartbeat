// Checks the TypeScript port against the Python reference outputs.
// Run: node --experimental-strip-types tests/golden.test.ts
import { readFileSync } from "node:fs";
import { newState, processWindow, ready, type Samples } from "../cloud/supabase/functions/pipeline/processing.ts";

const cases = JSON.parse(readFileSync(new URL("./golden_cases.json", import.meta.url), "utf8"));
let failures = 0;
const canon = (x: unknown): string => JSON.stringify(x, (_k, v) =>
  v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v);
for (const c of cases) {
  const data: Record<number, Samples> = { 1: new Map(), 2: new Map() };
  for (const [i, ...v] of c.s1) data[1].set(i, v);
  for (const [i, ...v] of c.s2) data[2].set(i, v);
  const load = (s: number, lo: number, hi: number) => {
    const m: Samples = new Map();
    for (let i = Math.max(0, lo); i < hi; i++) { const v = data[s].get(i); if (v) m.set(i, v); }
    return m;
  };
  const st = newState();
  const got = [];
  while (ready(st.next_window, { 1: c.n, 2: c.n }, { 1: true, 2: true })) {
    const w = st.next_window;
    got.push({ window: w, ...processWindow(st, w, load) });
  }
  const a = canon(got), b = canon(c.expected);
  if (a !== b) {
    failures++;
    for (let k = 0; k < Math.max(got.length, c.expected.length); k++) {
      if (canon(got[k]) !== canon(c.expected[k])) {
        console.log(`${c.name} window ${k}\n got ${JSON.stringify(got[k])}\nwant ${JSON.stringify(c.expected[k])}`);
        break;
      }
    }
  } else console.log(`${c.name}: ${got.length} windows identical`);
}
process.exit(failures ? 1 : 0);
