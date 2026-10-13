// Parity of the browser evaluator with sklearn, using specs exported by train-simulator-model.py.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { baselineInputs, curve, inputVector, predict, validateSpec } from "../src/model.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const dir = mkdtempSync(path.join(tmpdir(), "simulator-"));

for (const kind of ["linear", "trees", "forest"]) {
  const out = path.join(dir, `${kind}.json`);
  execFileSync("uv", [
    "run", "--quiet", path.join(root, "scripts/train-simulator-model.py"), path.join(root, "data/u1_by_date.csv"),
    "--target", "主蒸汽溫度_C", "--feature", "燃燒器角度_度", "--feature", "保證時段發電量_avg_MW",
    "--time-column", "日期", "--model", kind, "--out", out,
  ], { stdio: ["ignore", "ignore", "inherit"] });
  const spec = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(validateSpec(spec), null, kind);
  const [target] = spec.targets;
  assert.ok(target.parity.length > 0);
  const x = spec.features.map((f) => f.baseline);
  assert.ok(Math.abs(predict(target.model, x) - target.baseline_prediction) < 1e-6, `${kind} baseline`);
  const before = x.slice();
  const points = curve(spec, target, 0, x, null);
  assert.deepEqual(x, before, "curve must not mutate the slider values");
  assert.equal(points.length, spec.curve_points);
  assert.equal(points[0][0], spec.features[0].min);
  assert.equal(points.at(-1)[0], spec.features[0].max);

  const broken = structuredClone(spec);
  broken.targets[0].parity[0].y += 1;
  assert.match(validateSpec(broken), /不一致/);
}
// Categorical column: per-level baselines, one-hot inputs, 10-minute data resampled with a row filter.
const csv = ["時間,產品,運轉,a,b,y"];
for (let i = 0; i < 6 * 24 * 60; i += 10) {
  const t = new Date(Date.UTC(2026, 0, 1) + i * 60000).toISOString().replace("T", " ").slice(0, 19);
  const product = i < 3 * 24 * 60 ? "P1" : "P2";
  const a = Math.sin(i / 97) * 10 + (product === "P2" ? 20 : 0);
  const b = Math.cos(i / 53) * 5;
  csv.push(`${t},${product},${i % 70 === 0 ? 0 : 1},${a},${b},${2 * a - b + (product === "P2" ? 7 : 0)}`);
}
const csvPath = path.join(dir, "cat.csv");
writeFileSync(csvPath, csv.join("\n"));
for (const kind of ["linear", "trees", "forest"]) {
  const out = path.join(dir, `cat-${kind}.json`);
  execFileSync("uv", [
    "run", "--quiet", path.join(root, "scripts/train-simulator-model.py"), csvPath, "--target", "y",
    "--feature", "a", "--feature", "b", "--category", "產品", "--time-column", "時間",
    "--where", "運轉 == 1", "--resample", "30min", "--hold", "1h", "--model", kind, "--out", out,
  ], { stdio: ["ignore", "ignore", "inherit"] });
  const spec = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(validateSpec(spec), null, kind);
  assert.deepEqual(spec.category.levels.map((l) => l.name).sort(), ["P1", "P2"]);
  const [target] = spec.targets;
  for (const level of ["P1", "P2"]) {
    const x = baselineInputs(spec, level);
    assert.ok(Math.abs(predict(target.model, inputVector(spec, x, level)) - target.baseline_by_level[level]) < 1e-6, `${kind} ${level}`);
  }
  assert.ok(target.baseline_by_level.P2 > target.baseline_by_level.P1, `${kind} level effect`);
  assert.ok(target.metrics.by_day && target.metrics.forward && !target.metrics.kfold);
  assert.equal(spec.source.hold, "1h");
}

assert.match(validateSpec(undefined), /尚未設定模型/);
assert.match(validateSpec({ format: "other" }), /不支援/);
console.log("simulator model checks passed");
