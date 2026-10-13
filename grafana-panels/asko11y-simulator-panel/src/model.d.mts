export type FeatureSpec = {
  name: string;
  min: number;
  max: number;
  p05: number;
  p95: number;
  baseline: number;
  step: number;
};

export type LinearModel = { kind: "linear"; intercept: number; coef: number[]; alpha?: number };
type Tree = { feature: number[]; threshold: number[]; left: number[]; right: number[]; value: number[] };
export type TreesModel = { kind: "trees"; init: number; learning_rate: number; trees: Tree[] };
export type ForestModel = { kind: "forest"; trees: Tree[] };

export type Metric = { r2: number; mae: number; levels?: Record<string, { mae: number; n: number }> };
export type FeatureStats = { min: number; max: number; p05: number; p95: number; baseline: number };

export type TargetSpec = {
  name: string;
  observed: { min: number; max: number; median: number };
  baseline_prediction: number;
  metrics: { kfold?: Metric; by_day?: Metric; forward?: Metric };
  baseline_by_level?: Record<string, number>;
  model: LinearModel | TreesModel | ForestModel;
  parity?: Array<{ x: number[]; y: number }>;
};

export type SimulatorSpec = {
  format: string;
  title: string;
  source: {
    file: string;
    rows: number;
    dropped_rows: number;
    time_column?: string | null;
    resample?: string | null;
    hold?: string | null;
    trained_at: string;
  };
  model_kind: "linear" | "trees" | "forest";
  curve_points?: number;
  features: FeatureSpec[];
  targets: TargetSpec[];
  category?: {
    name: string;
    levels: Array<{ name: string; rows: number; days: number | null; features: FeatureStats[] }>;
  };
};

export const MODEL_FORMAT: string;
export function predict(model: LinearModel | TreesModel | ForestModel, x: number[]): number;
export function inputVector(spec: SimulatorSpec, x: number[], level: string | null): number[];
export function featureStats(spec: SimulatorSpec, featureIndex: number, level: string | null): FeatureStats;
export function baselineInputs(spec: SimulatorSpec, level: string | null): number[];
export function baselinePrediction(target: TargetSpec, level: string | null): number;
export function curve(
  spec: SimulatorSpec,
  target: TargetSpec,
  featureIndex: number,
  x: number[],
  level: string | null,
): Array<[number, number]>;
export function validateSpec(spec: unknown): string | null;
