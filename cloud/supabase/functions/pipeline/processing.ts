// Section 3 processing logic: a line-for-line port of pipeline/processing.py.
// Both must reproduce tests/golden_cases.json (see tests/golden.test.ts).

export const W = 400;
const LOOKBACK = 800;
const REFRACTORY = 100;

export type Row = number[];
export type Samples = Map<number, Row>;
export type State = { next_window: number; scan_next: number; last_peak: number | null; peaks: number[] };
export type Loader = (sensor: number, lo: number, hi: number) => Samples;

export function r1(x: number | null): number | null {
  return x === null ? null : Math.floor(x * 10 + 0.5) / 10;
}

export function newState(): State {
  return { next_window: 0, scan_next: 0, last_peak: null, peaks: [] };
}

// deno-lint-ignore no-explicit-any
export function windowStats(s1: Samples, s2: Samples, w: number): Record<string, any> {
  // deno-lint-ignore no-explicit-any
  const out: Record<string, any> = { mean: {}, min: {}, max: {} };
  const lo = w * W;
  for (const [sensor, data, nch] of [[1, s1, 3], [2, s2, 2]] as [number, Samples, number][]) {
    const rows: Row[] = [];
    for (let i = lo; i < lo + W; i++) {
      const r = data.get(i);
      if (r !== undefined) rows.push(r);
    }
    out["missing_" + sensor] = W - rows.length;
    const key = "sensor" + sensor;
    if (rows.length) {
      const mean: (number | null)[] = [], mn: number[] = [], mx: number[] = [];
      for (let c = 0; c < nch; c++) {
        let s = 0, a = Infinity, b = -Infinity;
        for (const r of rows) {
          s += r[c];
          if (r[c] < a) a = r[c];
          if (r[c] > b) b = r[c];
        }
        mean.push(r1(s / rows.length));
        mn.push(a);
        mx.push(b);
      }
      out.mean[key] = mean;
      out.min[key] = mn;
      out.max[key] = mx;
    } else {
      out.mean[key] = new Array(nch).fill(null);
      out.min[key] = new Array(nch).fill(null);
      out.max[key] = new Array(nch).fill(null);
    }
    if (sensor === 2) {
      let acc = 0;
      for (const r of rows) acc += Math.sqrt(r[0] * r[0] + r[1] * r[1]);
      out.magnitude = rows.length ? r1(acc / rows.length) : null;
    }
  }
  out.complete = out.missing_1 === 0 && out.missing_2 === 0;
  return out;
}

export function scanPeaks(state: State, ecg: Map<number, number>, upto: number): void {
  const start = state.scan_next;
  if (start >= upto) return;
  const dq: number[] = [];
  let head = 0;
  let missing = 0;
  for (let j = start - LOOKBACK; j < start; j++) {
    if (j < 0 || !ecg.has(j)) missing++;
    else {
      while (dq.length > head && ecg.get(dq[dq.length - 1])! <= ecg.get(j)!) dq.pop();
      dq.push(j);
    }
  }
  let last = state.last_peak;
  for (let i = start; i < upto; i++) {
    if (missing === 0 && ecg.has(i) && ecg.has(i + 1)) {
      const v = ecg.get(i)!;
      if (v > ecg.get(i - 1)! && v > ecg.get(i + 1)! && v > ecg.get(dq[head])! / 2 &&
          (last === null || i - last >= REFRACTORY)) {
        state.peaks.push(i);
        last = i;
      }
    }
    const outJ = i - LOOKBACK;
    if (outJ < 0 || !ecg.has(outJ)) missing--;
    if (dq.length > head && dq[head] === outJ) head++;
    if (ecg.has(i)) {
      while (dq.length > head && ecg.get(dq[dq.length - 1])! <= ecg.get(i)!) dq.pop();
      dq.push(i);
    } else missing++;
  }
  state.last_peak = last;
  state.scan_next = upto;
}

export function heartRate(state: State, s1Avail: number, w: number): number | null {
  if (w < 4) return null;
  const lo = W * (w - 4), hi = W * (w + 1);
  if (s1Avail !== hi - lo) return null;
  const pk = state.peaks.filter((p) => p >= lo && p < hi);
  if (pk.length < 2) return null;
  const meanGapS = (pk[pk.length - 1] - pk[0]) / (pk.length - 1) / W;
  return r1(60 / meanGapS);
}

export function ready(w: number, known: Record<number, number>, ended: Record<number, boolean>): boolean {
  const need: Record<number, number> = { 1: W * (w + 1) + 1, 2: W * (w + 1) };
  for (const s of [1, 2]) if (known[s] < need[s] && !ended[s]) return false;
  if (ended[1] && ended[2] && W * w >= Math.max(known[1], known[2])) return false;
  return true;
}

// deno-lint-ignore no-explicit-any
export function processWindow(state: State, w: number, load: Loader): Record<string, any> {
  const end = W * (w + 1);
  const scanLo = Math.max(0, state.scan_next - LOOKBACK);
  const s1 = load(1, scanLo, end + 1);
  const ecg = new Map<number, number>();
  for (const [i, v] of s1) ecg.set(i, v[1]);
  scanPeaks(state, ecg, end);
  const s2 = load(2, W * w, end);
  const s1w: Samples = new Map();
  for (const [i, v] of s1) if (i >= W * w && i < end) s1w.set(i, v);
  const out = windowStats(s1w, s2, w);
  const hrLo = W * (w - 4);
  let avail = 0;
  if (w >= 4 && scanLo > hrLo) avail = load(1, hrLo, end).size;
  else for (const i of s1.keys()) if (i >= hrLo && i < end) avail++;
  out.heart_rate = heartRate(state, avail, w);
  state.peaks = state.peaks.filter((p) => p >= W * (w - 3));
  state.next_window = w + 1;
  return out;
}
