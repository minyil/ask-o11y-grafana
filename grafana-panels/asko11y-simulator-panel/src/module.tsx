import React, { useEffect, useMemo, useState } from "react";
import { PanelPlugin } from "@grafana/data";
import { useTheme2 } from "@grafana/ui";
import {
  baselineInputs,
  baselinePrediction,
  curve,
  featureStats,
  inputVector,
  predict,
  validateSpec,
} from "./model.mjs";
import type { FeatureSpec, FeatureStats, Metric, SimulatorSpec, TargetSpec } from "./model.mjs";

type Options = { model?: SimulatorSpec };
type Theme = ReturnType<typeof useTheme2>;

const SPARK_W = 200;
const SPARK_H = 44;

function decimals(step: number): number {
  return Math.min(6, Math.max(0, -Math.floor(Math.log10(step))));
}

function fmt(value: number, digits: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function signed(value: number, digits: number): string {
  const text = fmt(Math.abs(value), digits);
  if (Number(text.replace(/,/g, "")) === 0) return `±${text}`;
  return value > 0 ? `▲ +${text}` : `▼ −${text}`;
}

const MODEL_LABELS: Record<string, string> = { linear: "線性 Ridge", trees: "梯度提升樹", forest: "隨機森林" };

const METRIC_LABELS: Array<[keyof TargetSpec["metrics"], string]> = [
  ["kfold", "隨機驗證"],
  ["by_day", "逐日驗證"],
  ["forward", "時間前推"],
];

function metricText(label: string, m: Metric, level: string | null): string {
  const own = level ? m.levels?.[level] : undefined;
  return `${label} R² ${m.r2.toFixed(2)} · MAE ${fmt(m.mae, 2)}${own ? `（${level} MAE ${fmt(own.mae, 2)}）` : ""}`;
}

function TargetCard({
  target,
  value,
  level,
  selected,
  onSelect,
  theme,
}: {
  target: TargetSpec;
  value: number;
  level: string | null;
  selected: boolean;
  onSelect: () => void;
  theme: Theme;
}) {
  const base = baselinePrediction(target, level);
  const delta = value - base;
  const metrics = METRIC_LABELS.flatMap(([key, label]) => {
    const m = target.metrics[key];
    return m ? [metricText(label, m, level)] : [];
  });
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      style={{
        flex: "1 1 220px",
        textAlign: "left",
        padding: "10px 14px",
        borderRadius: 6,
        cursor: "pointer",
        color: theme.colors.text.primary,
        background: theme.colors.background.secondary,
        border: `1px solid ${selected ? theme.colors.primary.main : theme.colors.border.weak}`,
        boxShadow: selected ? `inset 3px 0 0 ${theme.colors.primary.main}` : "none",
        font: "inherit",
      }}
    >
      <div style={{ fontSize: 13, color: theme.colors.text.secondary }}>{target.name}</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 28, fontWeight: 500, fontVariantNumeric: "tabular-nums" }}>{fmt(value, 2)}</span>
        <span style={{ fontSize: 15, color: theme.colors.primary.text, fontVariantNumeric: "tabular-nums" }}>
          {signed(delta, 2)}
        </span>
      </div>
      <div style={{ fontSize: 12, color: theme.colors.text.secondary }}>
        {level ? `${level} 典型設定下的預測` : "基準預測"} {fmt(base, 2)} · 實際範圍 {fmt(target.observed.min, 1)}–{fmt(target.observed.max, 1)}
      </div>
      <div style={{ fontSize: 12, color: theme.colors.text.secondary }}>
        {metrics.join(" ｜ ")}
      </div>
    </button>
  );
}

function Spark({
  feature,
  stats,
  points,
  value,
  yMin,
  yMax,
  theme,
}: {
  feature: FeatureSpec;
  stats: FeatureStats;
  points: Array<[number, number]>;
  value: number;
  yMin: number;
  yMax: number;
  theme: Theme;
}) {
  const span = feature.max - feature.min || 1;
  const sx = (v: number) => ((v - feature.min) / span) * SPARK_W;
  const sy = (v: number) => SPARK_H - 3 - ((v - yMin) / (yMax - yMin || 1)) * (SPARK_H - 6);
  const path = points.map(([px, py], i) => `${i ? "L" : "M"}${sx(px).toFixed(1)},${sy(py).toFixed(1)}`).join("");
  const cx = sx(Math.min(feature.max, Math.max(feature.min, value)));
  // Current prediction is the curve evaluated at the slider value; interpolate between sampled points.
  const idx = Math.min(points.length - 2, Math.max(0, Math.floor((cx / SPARK_W) * (points.length - 1))));
  const [x0, y0] = points[idx];
  const [x1, y1] = points[idx + 1];
  const cy = sy(y0 + ((value - x0) / (x1 - x0 || 1)) * (y1 - y0));
  return (
    <svg width={SPARK_W} height={SPARK_H} role="img" aria-label={`${feature.name} 的反應曲線`} style={{ display: "block" }}>
      <rect x={0} y={0} width={sx(stats.p05)} height={SPARK_H} fill={theme.colors.border.weak} opacity={0.5} />
      <rect x={sx(stats.p95)} y={0} width={SPARK_W - sx(stats.p95)} height={SPARK_H} fill={theme.colors.border.weak} opacity={0.5} />
      <path d={path} fill="none" stroke={theme.colors.primary.main} strokeWidth={2} strokeLinejoin="round" />
      <line x1={cx} x2={cx} y1={0} y2={SPARK_H} stroke={theme.colors.text.secondary} strokeDasharray="2 3" />
      <circle cx={cx} cy={Math.max(3, Math.min(SPARK_H - 3, cy))} r={4} fill={theme.colors.primary.main} stroke={theme.colors.background.primary} strokeWidth={2} />
    </svg>
  );
}

function FeatureRow({
  feature,
  hold,
  stats,
  value,
  points,
  yMin,
  yMax,
  wide,
  theme,
  onChange,
}: {
  feature: FeatureSpec;
  hold: string | null | undefined;
  stats: FeatureStats;
  value: number;
  points: Array<[number, number]>;
  yMin: number;
  yMax: number;
  wide: boolean;
  theme: Theme;
  onChange: (v: number) => void;
}) {
  const digits = decimals(feature.step);
  const ys = points.map((p) => p[1]);
  const impact = Math.max(...ys) - Math.min(...ys);
  const sparse = isSparse(stats, value, feature.step);
  const changed = Math.abs(value - stats.baseline) > feature.step / 2;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: wide ? "minmax(150px,1.2fr) minmax(160px,2fr) 104px 200px 92px" : "1fr 104px",
        alignItems: "center",
        gap: "6px 14px",
        padding: "8px 0",
        borderTop: `1px solid ${theme.colors.border.weak}`,
      }}
    >
      <div style={{ gridColumn: wide ? undefined : "1 / -1" }}>
        <div style={{ fontWeight: 500 }}>{feature.name}</div>
        {hold ? <div style={{ fontSize: 12, color: theme.colors.text.secondary }}>過去 {hold} 平均</div> : null}
        <div style={{ fontSize: 12, color: theme.colors.text.secondary }}>
          基準 {fmt(stats.baseline, digits)}
          {stats.max - stats.min < feature.step ? "（此條件下資料中固定）" : ""}
          {changed ? (
            <button
              type="button"
              onClick={() => onChange(stats.baseline)}
              style={{ marginLeft: 8, padding: 0, border: 0, background: "none", color: theme.colors.primary.text, cursor: "pointer", font: "inherit" }}
            >
              重設
            </button>
          ) : null}
        </div>
      </div>
      <input
        type="range"
        min={feature.min}
        max={feature.max}
        step={feature.step}
        value={value}
        aria-label={feature.name}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: theme.colors.primary.main }}
      />
      <input
        type="number"
        step={feature.step}
        value={Number(value.toFixed(digits))}
        aria-label={`${feature.name} 數值`}
        onChange={(e) => {
          const v = Number(e.target.value);
          if (e.target.value !== "" && Number.isFinite(v)) onChange(v);
        }}
        style={{
          width: "100%",
          padding: "4px 6px",
          fontVariantNumeric: "tabular-nums",
          color: theme.colors.text.primary,
          background: theme.colors.background.primary,
          border: `1px solid ${sparse ? theme.colors.warning.main : theme.colors.border.medium}`,
          borderRadius: 4,
          font: "inherit",
        }}
      />
      {wide ? (
        <>
          <Spark feature={feature} stats={stats} points={points} value={value} yMin={yMin} yMax={yMax} theme={theme} />
          <div style={{ fontSize: 12, color: theme.colors.text.secondary, fontVariantNumeric: "tabular-nums" }}>
            影響幅度
            <div style={{ fontSize: 14, color: theme.colors.text.primary }}>{fmt(impact, 2)}</div>
          </div>
        </>
      ) : null}
    </div>
  );
}

function isSparse(stats: FeatureStats, value: number, step: number): boolean {
  return value < stats.p05 - step / 2 || value > stats.p95 + step / 2;
}

function SimulatorPanel({ options, width }: { options: Options; width: number; height: number }) {
  const theme = useTheme2();
  const spec = options.model;
  const problem = useMemo(() => validateSpec(spec), [spec]);
  const ok = !problem && !!spec;
  const defaultLevel = ok ? (spec.category?.levels[0]?.name ?? null) : null;
  const [level, setLevel] = useState<string | null>(defaultLevel);
  const [x, setX] = useState<number[]>(() => (ok ? baselineInputs(spec, defaultLevel) : []));
  const [targetIndex, setTargetIndex] = useState(0);
  useEffect(() => {
    setLevel(defaultLevel);
    setX(ok ? baselineInputs(spec, defaultLevel) : []);
    setTargetIndex(0);
  }, [ok, spec, defaultLevel]);

  const ready = ok && x.length === spec.features.length;
  const target = ready ? spec.targets[Math.min(targetIndex, spec.targets.length - 1)] : undefined;
  const curves = useMemo(
    () => (ready && target ? spec.features.map((_, i) => curve(spec, target, i, x, level)) : []),
    [ready, spec, target, x, level],
  );

  if (!ready || !target) {
    return (
      <div role="alert" style={{ padding: 16 }}>
        {problem ?? "模型載入中…"}
      </div>
    );
  }

  const stats = spec.features.map((_, i) => featureStats(spec, i, level));
  const baseline = baselineInputs(spec, level);
  const input = inputVector(spec, x, level);
  const levelInfo = spec.category?.levels.find((l) => l.name === level);
  const ys = curves.flat().map((p) => p[1]);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const wide = width >= 760;
  const sparse = spec.features.filter((f, i) => isSparse(stats[i], x[i], f.step)).map((f) => f.name);
  const changed = spec.features.some((f, i) => Math.abs(x[i] - baseline[i]) > f.step / 2);
  const weak = spec.targets.some((t) => (t.metrics.by_day ?? t.metrics.forward ?? t.metrics.kfold)!.r2 < 0.3);

  return (
    <div style={{ height: "100%", overflow: "auto", padding: "4px 4px 12px", fontVariantNumeric: "tabular-nums" }}>
      {spec.category ? (
        <div
          role="radiogroup"
          aria-label={spec.category.name}
          style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 12 }}
        >
          <span style={{ fontSize: 15, fontWeight: 500, marginRight: 4 }}>{spec.category.name}</span>
          {spec.category.levels.map((l) => {
            const active = l.name === level;
            return (
              <button
                key={l.name}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => {
                  setLevel(l.name);
                  setX(baselineInputs(spec, l.name));
                }}
                style={{
                  padding: "6px 14px",
                  borderRadius: 6,
                  cursor: "pointer",
                  font: "inherit",
                  textAlign: "left",
                  color: active ? theme.colors.primary.contrastText : theme.colors.text.primary,
                  background: active ? theme.colors.primary.main : theme.colors.background.secondary,
                  border: `1px solid ${active ? theme.colors.primary.main : theme.colors.border.medium}`,
                }}
              >
                <div style={{ fontSize: 16, fontWeight: 500 }}>{l.name}</div>
                <div style={{ fontSize: 11, opacity: 0.85 }}>
                  {l.days != null ? `${l.days} 天 · ` : ""}
                  {l.rows} 筆
                </div>
              </button>
            );
          })}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        {spec.targets.map((t, i) => (
          <TargetCard
            key={t.name}
            target={t}
            value={predict(t.model, input)}
            level={level}
            selected={t === target}
            onSelect={() => setTargetIndex(i)}
            theme={theme}
          />
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", margin: "14px 0 4px" }}>
        <div style={{ fontSize: 13, color: theme.colors.text.secondary, flex: "1 1 300px" }}>
          拖動滑桿調整參數{spec.source.hold ? `（每個參數代表「維持 ${spec.source.hold}」的平均值，反映溫度的延遲反應）` : ""}；右側曲線是「只改該參數時，{target.name} 的變化」，其他參數維持目前設定。灰色區域是{levelInfo ? `${levelInfo.name} ` : ""}資料中少見（低於 5% 或高於 95% 分位）的範圍。
        </div>
        <button
          type="button"
          disabled={!changed}
          onClick={() => setX(baseline)}
          style={{
            padding: "4px 12px",
            borderRadius: 4,
            border: `1px solid ${theme.colors.border.medium}`,
            background: theme.colors.background.secondary,
            color: theme.colors.text.primary,
            cursor: changed ? "pointer" : "default",
            opacity: changed ? 1 : 0.5,
            font: "inherit",
          }}
        >
          全部重設為基準
        </button>
      </div>

      {levelInfo && levelInfo.days != null && levelInfo.days < 3 ? (
        <div role="status" style={{ fontSize: 13, color: theme.colors.warning.text, margin: "6px 0" }}>
          ⚠ {levelInfo.name} 只有 {levelInfo.days} 天的資料，預測與曲線僅供參考。
        </div>
      ) : null}
      {sparse.length ? (
        <div role="status" style={{ fontSize: 13, color: theme.colors.warning.text, margin: "6px 0" }}>
          ⚠ {sparse.join("、")} 位於{levelInfo ? ` ${levelInfo.name} ` : ""}資料少見的範圍，預測可信度較低。
        </div>
      ) : null}

      <div>
        {spec.features.map((f, i) => (
          <FeatureRow
            key={f.name}
            feature={f}
            hold={spec.source.hold}
            stats={stats[i]}
            value={x[i]}
            points={curves[i]}
            yMin={yMin}
            yMax={yMax}
            wide={wide}
            theme={theme}
            onChange={(v) => setX((prev) => prev.map((old, j) => (j === i ? v : old)))}
          />
        ))}
      </div>

      <div style={{ fontSize: 12, color: theme.colors.text.secondary, marginTop: 10, lineHeight: 1.6 }}>
        模型從 {spec.source.file} 的 {spec.source.rows} 筆{spec.source.resample ? `（${spec.source.resample} 平均）` : ""}資料學出（{MODEL_LABELS[spec.model_kind] ?? spec.model_kind}，
        {spec.source.trained_at.slice(0, 10)}）。這是資料中的關聯，不等於「實際去調整就會得到這個結果」；未納入模型的因素也可能同時在變化。
        {weak ? " 驗證分數（R²）偏低：模型對沒看過的日子預測不準，運轉狀況改變後不一定適用，曲線的方向比數字本身更值得參考。" : ""}
      </div>
    </div>
  );
}

export const plugin = new PanelPlugin<Options>(SimulatorPanel);
