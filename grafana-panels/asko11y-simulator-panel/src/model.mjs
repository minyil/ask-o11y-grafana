// Evaluates models exported by scripts/train-simulator-model.py; must agree with its `evaluate`.
export const MODEL_FORMAT = "asko11y-simulator-v1";

export function predict(model, x) {
  if (model.kind === "linear") {
    let total = model.intercept;
    for (let i = 0; i < model.coef.length; i++) total += model.coef[i] * x[i];
    return total;
  }
  const forest = model.kind === "forest";
  let total = forest ? 0 : model.init;
  for (const t of model.trees) {
    let node = 0;
    while (t.left[node] !== -1) {
      // sklearn compares float32 inputs against float64 thresholds.
      node = Math.fround(x[t.feature[node]]) <= t.threshold[node] ? t.left[node] : t.right[node];
    }
    total += forest ? t.value[node] : model.learning_rate * t.value[node];
  }
  return forest ? total / model.trees.length : total;
}

// Model input (a fresh array): features in order, then a one-hot of spec.category.levels (when present).
export function inputVector(spec, x, level) {
  return [...x, ...(spec.category?.levels ?? []).map((l) => (l.name === level ? 1 : 0))];
}

// Range and typical values of a feature, within the chosen category level when the spec has one.
export function featureStats(spec, featureIndex, level) {
  const lv = spec.category?.levels.find((l) => l.name === level);
  return lv ? lv.features[featureIndex] : spec.features[featureIndex];
}

export function baselineInputs(spec, level) {
  return spec.features.map((_, i) => featureStats(spec, i, level).baseline);
}

export function baselinePrediction(target, level) {
  return target.baseline_by_level?.[level] ?? target.baseline_prediction;
}

// Response of one target while a single feature sweeps its full observed range, others held at x.
export function curve(spec, target, featureIndex, x, level) {
  const f = spec.features[featureIndex];
  const n = spec.curve_points ?? 41;
  const probe = inputVector(spec, x, level);
  const points = [];
  for (let i = 0; i < n; i++) {
    const v = f.min + ((f.max - f.min) * i) / (n - 1);
    probe[featureIndex] = v;
    points.push([v, predict(target.model, probe)]);
  }
  return points;
}

// Returns a user-facing problem description, or null when the spec is usable.
export function validateSpec(spec) {
  if (!spec || typeof spec !== "object") return "尚未設定模型；請用 scripts/train-simulator-model.py --publish 產生。";
  if (spec.format !== MODEL_FORMAT) return `模型格式 ${spec.format ?? "(缺少)"} 不支援，需要 ${MODEL_FORMAT}。`;
  if (!Array.isArray(spec.features) || spec.features.length === 0) return "模型沒有可調參數。";
  if (!Array.isArray(spec.targets) || spec.targets.length === 0) return "模型沒有預測目標。";
  const width = spec.features.length + (spec.category?.levels.length ?? 0);
  for (const t of spec.targets) {
    if (t.model.kind === "linear" && t.model.coef.length !== width) return `${t.name} 的模型輸入數量與參數不符。`;
  }
  for (const t of spec.targets) {
    for (const sample of t.parity ?? []) {
      const got = predict(t.model, sample.x);
      if (!(Math.abs(got - sample.y) <= 1e-6 * Math.max(1, Math.abs(sample.y)))) {
        return `${t.name} 的模型在瀏覽器計算結果與訓練時不一致（${got} ≠ ${sample.y}）。`;
      }
    }
  }
  return null;
}
