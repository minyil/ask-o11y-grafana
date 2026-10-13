// Regression: Plotly WebGL traces draw nothing on an axis with rangebreaks.
import assert from "node:assert/strict";
import { withRangebreakSafeTraces } from "../grafana-panels/asko11y-plotly-panel/src/rangebreaks.mjs";

const gl = { type: "scattergl", x: [1, 2], y: [3, 4], mode: "lines" };
const bar = { type: "bar", x: [1], y: [2] };
const breaks = { xaxis: { rangebreaks: [{ bounds: ["2026-08-14", "2026-09-09"] }] } };

const converted = withRangebreakSafeTraces([gl, bar], breaks);
assert.deepEqual(converted[0], { ...gl, type: "scatter" });
assert.equal(converted[1], bar);
assert.equal(gl.type, "scattergl", "input trace is not mutated");

const data = [gl];
assert.equal(withRangebreakSafeTraces(data, {}), data);
assert.equal(withRangebreakSafeTraces(data, { xaxis: { rangebreaks: [] } }), data);
assert.equal(withRangebreakSafeTraces(data, { xaxis: { rangebreaks: [{ enabled: false, bounds: [1, 2] }] } }), data);
assert.equal(withRangebreakSafeTraces(data, { xaxis2: { rangebreaks: [{ pattern: "day of week", bounds: [6, 1] }] } })[0].type, "scatter");
assert.equal(withRangebreakSafeTraces(data, { title: { rangebreaks: [{}] } }), data);
console.log("plotly rangebreak checks passed");
