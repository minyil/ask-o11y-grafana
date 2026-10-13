"""What-if simulator models: training, export, reference evaluation and validation.

One source serves three places:
- scripts/train-simulator-model.py imports it for local CLI training;
- sandbox-analysis-mcp sends this file's source into OpenSandbox for build_simulator_model,
  so the model is trained by fixed code rather than model-written Python;
- artifact-bridge-mcp calls validate_spec before a simulator panel reaches Grafana.

Module scope stays stdlib-only so the bridge can import it; numpy/pandas/scikit-learn are
imported inside the training functions. grafana-panels/asko11y-simulator-panel/src/model.mjs
evaluates the exported model in the browser and must agree with evaluate().
"""
from __future__ import annotations

import datetime as dt
import math
import struct
import warnings
from typing import Any

MODEL_FORMAT = "asko11y-simulator-v1"
PANEL_ID = "asko11y-simulator-panel"
REPORT_FORMAT = "asko11y-simulator-report-v1"
CURVE_POINTS = 41
MODEL_KINDS = ("linear", "trees", "forest")
MAX_FEATURES = 40
MAX_TARGETS = 8
MAX_LEVELS = 20
MAX_TREES = 1000
MAX_TREE_NODES = 4096
MAX_TEXT = 200
MIN_ROWS = 30

# pandas 2.3 parsing "2h" under numpy 2.4 warns from inside pandas; harmless here.
warnings.filterwarnings("ignore", message="The 'generic' unit for NumPy timedelta", category=DeprecationWarning)


class SimulatorError(ValueError):
    """A user-correctable problem with the data, the requested columns or a spec."""


def nice_step(span: float) -> float:
    if span <= 0:
        return 1.0
    raw = span / 200
    magnitude = 10 ** math.floor(math.log10(raw))
    return min((m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw), default=raw)


def float32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def evaluate(model: dict[str, Any], x: list[float]) -> float:
    """Reference evaluator; src/model.mjs in the panel must agree with it."""
    if model["kind"] == "linear":
        return model["intercept"] + sum(c * v for c, v in zip(model["coef"], x))
    leaves = []
    for t in model["trees"]:
        node = 0
        while t["left"][node] != -1:
            # sklearn compares float32 inputs against float64 thresholds.
            node = t["left"][node] if float32(x[t["feature"][node]]) <= t["threshold"][node] else t["right"][node]
        leaves.append(t["value"][node])
    if model["kind"] == "forest":
        return sum(leaves) / len(leaves)
    total = model["init"]
    for value in leaves:
        total += model["learning_rate"] * value
    return total


# ---------------------------------------------------------------------------------------------
# Validation (stdlib only)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SimulatorError(f"{where} must be a finite number")
    return float(value)


def _text(value: Any, where: str, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise SimulatorError(f"{where} must be a non-empty string of at most {MAX_TEXT} characters")


def _stats(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise SimulatorError(f"{where} is missing")
    for key in ("min", "max", "p05", "p95", "baseline"):
        _number(value.get(key), f"{where}.{key}")
    if value["min"] > value["max"]:
        raise SimulatorError(f"{where} has min above max")


def _validate_model(model: Any, width: int, where: str) -> None:
    if not isinstance(model, dict) or model.get("kind") not in MODEL_KINDS:
        raise SimulatorError(f"{where}.kind must be one of {', '.join(MODEL_KINDS)}")
    if model["kind"] == "linear":
        _number(model.get("intercept"), f"{where}.intercept")
        coef = model.get("coef")
        if not isinstance(coef, list) or len(coef) != width:
            raise SimulatorError(f"{where}.coef must have one entry per model input ({width})")
        for i, c in enumerate(coef):
            _number(c, f"{where}.coef[{i}]")
        return
    if model["kind"] == "trees":
        _number(model.get("init"), f"{where}.init")
        _number(model.get("learning_rate"), f"{where}.learning_rate")
    trees = model.get("trees")
    if not isinstance(trees, list) or not 1 <= len(trees) <= MAX_TREES:
        raise SimulatorError(f"{where}.trees must hold 1..{MAX_TREES} trees")
    for ti, t in enumerate(trees):
        arrays = [t.get(key) if isinstance(t, dict) else None for key in ("feature", "threshold", "left", "right", "value")]
        if any(not isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1 or not 1 <= len(arrays[0]) <= MAX_TREE_NODES:
            raise SimulatorError(f"{where}.trees[{ti}] arrays must share a length of 1..{MAX_TREE_NODES}")
        feature, threshold, left, right, value = arrays
        n = len(feature)
        for node in range(n):
            leaf = left[node] == -1
            if leaf != (right[node] == -1):
                raise SimulatorError(f"{where}.trees[{ti}] node {node} must have both children or none")
            if not leaf:
                # Children after their parent keeps every walk finite.
                for child in (left[node], right[node]):
                    if isinstance(child, bool) or not isinstance(child, int) or not node < child < n:
                        raise SimulatorError(f"{where}.trees[{ti}] node {node} has an invalid child")
                if isinstance(feature[node], bool) or not isinstance(feature[node], int) or not 0 <= feature[node] < width:
                    raise SimulatorError(f"{where}.trees[{ti}] node {node} splits on an unknown input")
                _number(threshold[node], f"{where}.trees[{ti}].threshold[{node}]")
            _number(value[node], f"{where}.trees[{ti}].value[{node}]")


def validate_spec(spec: Any) -> None:
    """Raise SimulatorError unless spec is a well-formed model the panel can evaluate as trained."""
    if not isinstance(spec, dict) or spec.get("format") != MODEL_FORMAT:
        raise SimulatorError(f"simulator model format must be {MODEL_FORMAT}")
    _text(spec.get("title"), "title")
    features = spec.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= MAX_FEATURES:
        raise SimulatorError(f"features must hold 1..{MAX_FEATURES} entries")
    for i, f in enumerate(features):
        if not isinstance(f, dict):
            raise SimulatorError(f"features[{i}] is invalid")
        _text(f.get("name"), f"features[{i}].name")
        _stats(f, f"features[{i}]")
        if _number(f.get("step"), f"features[{i}].step") <= 0:
            raise SimulatorError(f"features[{i}].step must be positive")
    width = len(features)
    category = spec.get("category")
    if category is not None:
        levels = category.get("levels") if isinstance(category, dict) else None
        _text(category.get("name") if isinstance(category, dict) else None, "category.name")
        if not isinstance(levels, list) or not 1 <= len(levels) <= MAX_LEVELS:
            raise SimulatorError(f"category.levels must hold 1..{MAX_LEVELS} entries")
        for i, lv in enumerate(levels):
            if not isinstance(lv, dict) or not isinstance(lv.get("features"), list) or len(lv["features"]) != width:
                raise SimulatorError(f"category.levels[{i}] needs stats for every feature")
            _text(lv.get("name"), f"category.levels[{i}].name")
            for j, s in enumerate(lv["features"]):
                _stats(s, f"category.levels[{i}].features[{j}]")
        width += len(levels)
    targets = spec.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise SimulatorError(f"targets must hold 1..{MAX_TARGETS} entries")
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise SimulatorError(f"targets[{i}] is invalid")
        _text(t.get("name"), f"targets[{i}].name")
        _number(t.get("baseline_prediction"), f"targets[{i}].baseline_prediction")
        _validate_model(t.get("model"), width, f"targets[{i}].model")
        metrics = t.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            raise SimulatorError(f"targets[{i}].metrics are missing")
        parity = t.get("parity")
        if not isinstance(parity, list) or not parity:
            raise SimulatorError(f"targets[{i}].parity samples are missing")
        for sample in parity:
            x = sample.get("x") if isinstance(sample, dict) else None
            if not isinstance(x, list) or len(x) != width:
                raise SimulatorError(f"targets[{i}].parity sample width is invalid")
            expected = _number(sample.get("y"), f"targets[{i}].parity.y")
            got = evaluate(t["model"], [_number(v, f"targets[{i}].parity.x") for v in x])
            if abs(got - expected) > 1e-6 * max(1.0, abs(expected)):
                raise SimulatorError(f"targets[{i}] model does not reproduce its training predictions")


# ---------------------------------------------------------------------------------------------
# Training (numpy / pandas / scikit-learn imported lazily)


def build_estimator(kind: str):
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import RidgeCV
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "linear":
        return make_pipeline(StandardScaler(), RidgeCV(alphas=np.logspace(-3, 3, 40)))
    if kind == "forest":
        # Depth and leaf-size caps keep the exported JSON small enough for a dashboard panel.
        return RandomForestRegressor(n_estimators=100, max_depth=8, min_samples_leaf=5, max_features=0.5, random_state=0)
    return GradientBoostingRegressor(n_estimators=200, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)


def export_estimator(kind: str, est) -> dict[str, Any]:
    import numpy as np

    if kind == "linear":
        scaler, ridge = est.named_steps["standardscaler"], est.named_steps["ridgecv"]
        coef = ridge.coef_ / scaler.scale_
        intercept = float(ridge.intercept_ - np.dot(coef, scaler.mean_))
        return {"kind": "linear", "intercept": intercept, "coef": [float(c) for c in coef], "alpha": float(ridge.alpha_)}
    trees = []
    for tree in (est.estimators_ if kind == "forest" else (row[0] for row in est.estimators_)):
        t = tree.tree_
        trees.append({
            "feature": [int(v) for v in t.feature],
            "threshold": [float(v) for v in t.threshold],
            "left": [int(v) for v in t.children_left],
            "right": [int(v) for v in t.children_right],
            "value": [float(v) for v in t.value[:, 0, 0]],
        })
    if kind == "forest":
        return {"kind": "forest", "trees": trees}
    init = float(np.ravel(est.init_.predict(np.zeros((1, est.n_features_in_))))[0])
    return {"kind": "trees", "init": init, "learning_rate": float(est.learning_rate), "trees": trees}


def cv_metrics(kind: str, X, y, days, level) -> dict[str, Any]:
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import GroupKFold, KFold, TimeSeriesSplit, cross_val_predict

    def summarize(truth, pred, labels) -> dict[str, Any]:
        resid = truth - pred
        out: dict[str, Any] = {"r2": float(1 - np.sum(resid**2) / np.sum((truth - truth.mean()) ** 2)), "mae": float(np.mean(np.abs(resid)))}
        if labels is not None:
            out["levels"] = {str(v): {"mae": float(np.mean(np.abs(resid[labels == v]))), "n": int(np.sum(labels == v))} for v in pd.unique(labels)}
        return out

    labels = None if level is None else level.to_numpy()
    if days is None:
        pred = cross_val_predict(build_estimator(kind), X, y, cv=KFold(5, shuffle=True, random_state=0))
        return {"kfold": summarize(y.to_numpy(), pred, labels)}
    # Rows from the same day share slow drifts (thermal inertia, recipe), so whole days are held out together.
    n_days = days.nunique()
    if n_days < 3:
        raise SimulatorError(f"the time column spans only {n_days} day(s); day-grouped validation needs at least 3")
    pred = cross_val_predict(build_estimator(kind), X, y, groups=days.to_numpy(), cv=GroupKFold(min(10, n_days)))
    out = {"by_day": summarize(y.to_numpy(), pred, labels)}
    truth, fwd, fwd_labels = [], [], []
    for train, test in TimeSeriesSplit(5).split(X):
        est = build_estimator(kind).fit(X.iloc[train], y.iloc[train])
        truth.extend(y.iloc[test])
        fwd.extend(est.predict(X.iloc[test]))
        if labels is not None:
            fwd_labels.extend(labels[test])
    out["forward"] = summarize(np.array(truth), np.array(fwd), np.array(fwd_labels) if labels is not None else None)
    return out


def primary_metric(metrics: dict[str, Any]) -> dict[str, Any]:
    return metrics.get("by_day") or metrics.get("kfold") or next(iter(metrics.values()))


def feature_stats(s) -> dict[str, float]:
    return {
        "min": float(s.min()), "max": float(s.max()),
        "p05": float(s.quantile(0.05)), "p95": float(s.quantile(0.95)),
        "baseline": float(s.median()),
    }


def prepare_rows(raw, *, targets: list[str], features: list[str], category: str | None, time_column: str | None,
                 where: str | None, resample: str | None, hold: str | None):
    """Filter, time-align and clean the raw table into one row per training example."""
    import numpy as np
    import pandas as pd

    extra = [c for c in (time_column, category) if c]
    missing = [c for c in [*targets, *features, *extra] if c not in raw.columns]
    if missing:
        raise SimulatorError(f"columns not in the data: {missing}")
    if len({*targets, *features, *extra}) != len(targets) + len(features) + len(extra):
        raise SimulatorError("each column may be used only once (target, feature, category or time column)")
    if not targets or not features or len(targets) > MAX_TARGETS or len(features) > MAX_FEATURES:
        raise SimulatorError(f"need 1..{MAX_TARGETS} targets and 1..{MAX_FEATURES} features")
    if resample and not time_column:
        raise SimulatorError("resample needs a time column")
    if hold and not resample:
        raise SimulatorError("hold needs resample")
    if where:
        try:
            raw = raw.query(where, engine="python")
        except Exception as exc:  # pandas raises many types for a bad expression
            raise SimulatorError(f"row filter could not be applied: {type(exc).__name__}: {exc}") from exc
    if time_column:
        parsed = pd.to_datetime(raw[time_column], errors="coerce")
        if parsed.isna().mean() > 0.5:
            raise SimulatorError(f"time column {time_column} is mostly not parseable as dates")
        raw = raw.assign(**{time_column: parsed}).dropna(subset=[time_column]).sort_values(time_column, kind="stable")

    numeric = raw[[*features, *targets]].apply(pd.to_numeric, errors="coerce")
    labels = raw[category].astype("string") if category else None
    times = raw[time_column] if time_column else None
    if resample:
        ts = raw[time_column]
        by_time = numeric.set_axis(ts)
        # Windows with no kept rows (e.g. filtered-out downtime) become NaN and are dropped below.
        numeric = by_time.resample(resample).mean()
        if hold:
            # Features: trailing average over the hold span, read at each window's end. Spans missing >10% of
            # their rows (downtime, just restarted, gaps) are dropped rather than averaged over what is left.
            span = pd.Timedelta(hold)
            held = by_time[features].rolling(span).mean()
            present = pd.Series(1.0, index=ts).rolling(span).count()
            held[(present < 0.9 * (span / ts.diff().median())).to_numpy()] = np.nan
            numeric[features] = held.resample(resample).last()
        if labels is not None:
            # A window (or hold span) spanning a level change belongs to neither level.
            per_window = labels.set_axis(ts).resample(resample).agg(["nunique", "first"])
            pure = per_window["nunique"] == 1
            if hold:
                codes = pd.Series(pd.factorize(labels)[0], index=ts).astype(float)
                rolled = codes.rolling(pd.Timedelta(hold))
                pure &= (rolled.min() == rolled.max()).resample(resample).last().fillna(False).astype(bool)
            labels = per_window["first"].where(pure)
        times = numeric.index.to_series()
    if labels is not None:
        numeric = numeric.assign(**{category: labels.to_numpy()})
    rows = numeric.dropna()
    if len(rows) < MIN_ROWS:
        raise SimulatorError(f"only {len(rows)} complete rows after filtering; need at least {MIN_ROWS}")
    constant = [c for c in features if rows[c].nunique() < 2]
    if constant:
        raise SimulatorError(f"features constant in the data cannot be simulated: {constant}")
    days = None if times is None else pd.Series(pd.DatetimeIndex(rows.index if resample else times.loc[rows.index]).date, index=rows.index)
    level = rows[category].astype(str) if category else None
    if level is not None and level.nunique() > MAX_LEVELS:
        raise SimulatorError(f"category {category} has more than {MAX_LEVELS} values")
    return rows, len(numeric) - len(rows), days, level


def fit_targets(kind: str, rows, X, levels: list[str], level, days, targets: list[str], features: list[dict[str, Any]]):
    import numpy as np

    def baseline_input(lv: str | None) -> list[float]:
        if lv is None:
            return [f["baseline"] for f in features]
        sel = rows[level == lv]
        return [float(sel[f["name"]].median()) for f in features] + [float(v == lv) for v in levels]

    rng = np.random.default_rng(0)
    sample_idx = rng.choice(len(X), size=min(5, len(X)), replace=False)
    out = []
    for col in targets:
        y = rows[col].astype(float)
        est = build_estimator(kind).fit(X, y)
        model = export_estimator(kind, est)
        parity = [{"x": [float(v) for v in X.iloc[i]], "y": float(est.predict(X.iloc[[i]])[0])} for i in sample_idx]
        worst = max(abs(evaluate(model, s["x"]) - s["y"]) for s in parity)
        if worst > 1e-6 * max(1.0, float(y.abs().max())):
            raise SimulatorError(f"exported {col} model disagrees with scikit-learn by {worst}")
        entry = {
            "name": col,
            "observed": {"min": float(y.min()), "max": float(y.max()), "median": float(y.median())},
            "baseline_prediction": evaluate(model, baseline_input(levels[0] if levels else None)),
            "metrics": cv_metrics(kind, X, y, days, level),
            "model": model,
            "parity": parity,
        }
        if levels:
            entry["baseline_by_level"] = {lv: evaluate(model, baseline_input(lv)) for lv in levels}
        out.append(entry)
    return out


def effects(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Change of each target when one feature moves from its p05 to p95, others at typical values."""
    levels = spec.get("category", {}).get("levels", [])
    contexts = [(lv["name"], lv["features"]) for lv in levels] or [(None, spec["features"])]
    names = [lv["name"] for lv in levels]
    out = []
    for level_name, stats in contexts:
        base = [s["baseline"] for s in stats]
        onehot = [float(n == level_name) for n in names]
        for t in spec["targets"]:
            for i, f in enumerate(spec["features"]):
                lo, hi = base.copy(), base.copy()
                lo[i], hi[i] = stats[i]["p05"], stats[i]["p95"]
                delta = evaluate(t["model"], hi + onehot) - evaluate(t["model"], lo + onehot)
                out.append({"level": level_name, "target": t["name"], "feature": f["name"],
                            "from": stats[i]["p05"], "to": stats[i]["p95"], "delta": round(delta, 6)})
    return out


def build_spec(raw, *, targets: list[str], features: list[str], category: str | None = None,
               time_column: str | None = None, where: str | None = None, resample: str | None = None,
               hold: str | None = None, model: str = "auto", title: str | None = None,
               source_name: str = "data") -> tuple[dict[str, Any], dict[str, Any]]:
    """Train and export a simulator spec plus a compact report for the person asking."""
    import pandas as pd

    if model not in (*MODEL_KINDS, "auto"):
        raise SimulatorError(f"model must be one of {', '.join(MODEL_KINDS)} or auto")
    total_rows = len(raw)
    rows, dropped, days, level = prepare_rows(raw, targets=targets, features=features, category=category,
                                              time_column=time_column, where=where, resample=resample, hold=hold)
    feature_specs = [{"name": c, **feature_stats(rows[c]), "step": nice_step(float(rows[c].max() - rows[c].min()))} for c in features]
    X = rows[features].astype(float)
    levels: list[str] = []
    if level is not None:
        levels = [str(v) for v in level.value_counts().index]  # most common first; the panel opens on it
        X = pd.concat([X, pd.DataFrame({f"{category}={v}": (level == v).astype(float) for v in levels}, index=X.index)], axis=1)

    candidates = {}
    for kind in (MODEL_KINDS if model == "auto" else (model,)):
        fitted = fit_targets(kind, rows, X, levels, level, days, targets, feature_specs)
        candidates[kind] = (sum(primary_metric(t["metrics"])["r2"] for t in fitted) / len(fitted), fitted)
    chosen = max(candidates, key=lambda k: candidates[k][0])

    spec: dict[str, Any] = {
        "format": MODEL_FORMAT,
        "title": (title or f"{source_name} 參數模擬")[:MAX_TEXT],
        "source": {
            "file": source_name[:MAX_TEXT],
            "raw_rows": total_rows, "where": where, "resample": resample, "hold": hold,
            "rows": len(rows), "dropped_rows": dropped,
            "time_column": time_column,
            "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "model_kind": chosen,
        "curve_points": CURVE_POINTS,
        "features": feature_specs,
        "targets": candidates[chosen][1],
    }
    if levels:
        # Model input = features in order, then one-hot of category.levels in order.
        spec["category"] = {
            "name": category,
            "levels": [{
                "name": lv,
                "rows": int((level == lv).sum()),
                "days": int(days[level == lv].nunique()) if days is not None else None,
                "features": [feature_stats(rows.loc[level == lv, f["name"]]) for f in feature_specs],
            } for lv in levels],
        }
    validate_spec(spec)
    return spec, report(spec, {k: v[0] for k, v in candidates.items()})


def report(spec: dict[str, Any], scores: dict[str, float]) -> dict[str, Any]:
    warnings_out = []
    for lv in spec.get("category", {}).get("levels", []):
        if lv["days"] is not None and lv["days"] < 3:
            warnings_out.append(f"{lv['name']} has only {lv['days']} day(s) of data")
        fixed = [f["name"] for f, s in zip(spec["features"], lv["features"]) if s["max"] - s["min"] < f["step"]]
        if fixed:
            warnings_out.append(f"within {lv['name']} these never varied, so their sliders extrapolate: {fixed}")
    for t in spec["targets"]:
        if primary_metric(t["metrics"])["r2"] < 0.3:
            warnings_out.append(f"{t['name']}: validation R² below 0.3; predictions are weak for unseen days")
    return {
        "format": REPORT_FORMAT,
        "title": spec["title"],
        "model_kind": spec["model_kind"],
        "candidate_mean_r2": {k: round(v, 4) for k, v in scores.items()},
        "rows": spec["source"]["rows"], "dropped_rows": spec["source"]["dropped_rows"], "raw_rows": spec["source"]["raw_rows"],
        "targets": [{"name": t["name"], "metrics": t["metrics"], "baseline_prediction": t["baseline_prediction"],
                     "baseline_by_level": t.get("baseline_by_level")} for t in spec["targets"]],
        "levels": [{k: lv[k] for k in ("name", "rows", "days")} for lv in spec.get("category", {}).get("levels", [])],
        "effects_p05_to_p95": effects(spec),
        "warnings": warnings_out,
    }
