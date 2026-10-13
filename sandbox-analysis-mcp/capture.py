"""Trusted in-sandbox bootstrap for authorized inputs and rich output capture."""
from __future__ import annotations

import ast
import contextlib
import importlib
import json
import os
import random
import shutil
import threading
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path("/tmp/sandbox-output")
MAX_ITEM_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_FRAME_FIELDS = 200
MAX_FRAME_ROWS = 50_000
MAX_FRAME_BYTES = 16 * 1024 * 1024
DERIVED_FRAME_MIME = "application/vnd.ask-o11y.dataframe+json"


class BoundedLog:
    def __init__(self) -> None:
        self.data = bytearray()
        self.overflow = False
        self.lock = threading.Lock()

    def append(self, value: bytes) -> None:
        with self.lock:
            remaining = MAX_LOG_BYTES - len(self.data)
            self.data.extend(value[:remaining])
            self.overflow = self.overflow or len(value) > remaining

    def write(self, value: str) -> int:
        self.append(value.encode("utf-8", errors="replace"))
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def pipe_output(fd: int, sink: BoundedLog) -> tuple[int, threading.Thread]:
    read_fd, write_fd = os.pipe()
    original_fd = os.dup(fd)
    os.dup2(write_fd, fd)
    os.close(write_fd)

    def drain() -> None:
        with os.fdopen(read_fd, "rb", closefd=True) as reader:
            while chunk := reader.read(65536):
                sink.append(chunk)

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return original_fd, thread


def load_dataframe(bundle_path: str, pd: Any) -> tuple[Any, dict[str, Any]]:
    try:
        bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authorized frame bundle is invalid") from exc
    frame = bundle.get("frame") if isinstance(bundle, dict) else None
    rules = bundle.get("validity_rules", []) if isinstance(bundle, dict) else None
    fields = frame.get("schema", {}).get("fields") if isinstance(frame, dict) else None
    values = frame.get("data", {}).get("values") if isinstance(frame, dict) else None
    if not isinstance(fields, list) or not isinstance(values, list) or len(fields) != len(values):
        raise ValueError("columnar frame fields/values must be equal-length arrays")
    names = [field.get("name") if isinstance(field, dict) else None for field in fields]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("columnar frame field names must be unique non-empty strings")
    if any(not isinstance(column, list) for column in values):
        raise ValueError("columnar frame values must be arrays")
    lengths = {len(column) for column in values}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ValueError("columnar frame columns must have one shared non-zero row count")
    data = pd.DataFrame(dict(zip(names, values, strict=True)))
    for field in fields:
        name = field.get("name")
        if field.get("type") == "time":
            numeric = pd.to_numeric(data[name], errors="coerce")
            data[name] = pd.to_datetime(numeric, unit="ms", utc=True, errors="coerce") if numeric.notna().any() else pd.to_datetime(data[name], utc=True, errors="coerce")
    if not isinstance(rules, list):
        raise ValueError("validity_rules must be an array")
    mask = pd.Series(True, index=data.index)
    applied = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("validity rule must be an object")
        field = rule.get("field")
        accepted = rule.get("accepted_values")
        if not isinstance(field, str) or field not in data.columns or not isinstance(accepted, list) or not accepted:
            raise ValueError("validity rule is incomplete")
        normalized = {str(value).strip().lower() for value in accepted}
        mask &= data[field].map(lambda value: str(value).strip().lower() in normalized)
        applied.append({"field": field, "accepted_values": accepted, "applies_to": rule.get("applies_to", [])})
    filtered = data.loc[mask].copy()
    if rules and filtered.empty:
        raise ValueError("validity rules exclude every row")
    return filtered, {"input_rows": len(data), "valid_rows": len(filtered), "excluded_rows": len(data) - len(filtered), "rules": applied}


def runtime_modules() -> tuple[Any, Any, Any, Any]:
    matplotlib = importlib.import_module("matplotlib")
    matplotlib.use("Agg")

    def apply_cjk_defaults() -> None:
        matplotlib.rcParams["font.sans-serif"] = ["Noto Sans CJK TC", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False

    apply_cjk_defaults()
    plt = importlib.import_module("matplotlib.pyplot")
    original_style_use = plt.style.use

    def style_use_with_cjk(style: Any) -> Any:
        result = original_style_use(style)
        apply_cjk_defaults()
        return result

    plt.style.use = style_use_with_cjk

    try:
        sns = importlib.import_module("seaborn")
    except ModuleNotFoundError:
        pass
    else:
        original_set_theme = sns.set_theme

        def set_theme_with_cjk(*args: Any, **kwargs: Any) -> Any:
            result = original_set_theme(*args, **kwargs)
            apply_cjk_defaults()
            return result

        setattr(sns, "set_theme", set_theme_with_cjk)

    np = importlib.import_module("numpy")
    pd = importlib.import_module("pandas")
    Figure = importlib.import_module("matplotlib.figure").Figure
    return plt, np, pd, Figure


def execute(code: str, seed: int, namespace: dict[str, Any], audit: dict[str, Any], plt: Any, np: Any, pd: Any, Figure: Any) -> None:
    try:
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        OUTPUT_DIR.mkdir(mode=0o700)
    except OSError as exc:
        raise RuntimeError("sandbox output directory is unavailable") from exc
    random.seed(seed)
    np.random.seed(seed)
    manifest: list[dict[str, str]] = []
    captured_figures: set[int] = set()
    emitted_frame = False

    def label(value: str | None, index: int, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        cleaned = " ".join(value.replace("\x00", "").split()).strip()
        return cleaned[:120] or fallback

    def write(name: str, mime_type: str, value: str | bytes, display_name: str, limit: int = MAX_ITEM_BYTES) -> None:
        payload = value.encode() if isinstance(value, str) else value
        if len(payload) > limit:
            raise ValueError(f"captured output {name} exceeds {limit} bytes")
        path = OUTPUT_DIR / name
        path.write_bytes(payload)
        manifest.append({"path": str(path), "mime_type": mime_type, "display_name": display_name})

    def emit_frame(value: Any, name: str | None = None) -> None:
        nonlocal emitted_frame
        if emitted_frame:
            raise ValueError("only one derived frame may be emitted")
        if not isinstance(value, pd.DataFrame) or value.empty:
            raise ValueError("emit_frame requires a non-empty pandas DataFrame")
        if not 1 <= len(value.columns) <= MAX_FRAME_FIELDS or len(value) > MAX_FRAME_ROWS:
            raise ValueError(f"derived frame must contain 1..{MAX_FRAME_FIELDS} fields and at most {MAX_FRAME_ROWS} rows")
        columns = [str(column).strip() for column in value.columns]
        if any(not column for column in columns) or len(set(columns)) != len(columns):
            raise ValueError("derived frame field names must be unique non-empty strings")
        logical_types = []
        for column in value.columns:
            series = value[column]
            if pd.api.types.is_datetime64_any_dtype(series.dtype):
                logical_types.append("time")
            elif pd.api.types.is_bool_dtype(series.dtype):
                logical_types.append("boolean")
            elif pd.api.types.is_numeric_dtype(series.dtype):
                logical_types.append("number")
            else:
                logical_types.append("string")
        try:
            split = json.loads(value.set_axis(columns, axis=1).to_json(orient="split", date_format="iso", date_unit="ms", force_ascii=False))
            payload = json.dumps({"format": "ask-o11y-dataframe-v1", "columns": columns, "types": logical_types, "data": split["data"]}, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("derived frame contains unsupported values") from exc
        index = len(manifest) + 1
        display_name = label(name, index, f"Derived frame {index}")
        if len(payload.encode()) > MAX_FRAME_BYTES:
            raise ValueError(f"derived frame exceeds {MAX_FRAME_BYTES} bytes")
        write(f"derived-frame-{index}.json", DERIVED_FRAME_MIME, payload, display_name, MAX_FRAME_BYTES)
        emitted_frame = True

    def emit(value: Any, name: str | None = None) -> None:
        if value is None:
            return
        index = len(manifest) + 1
        display_name = label(name, index, f"Output {index}")
        csv_requested = display_name.lower().endswith(".csv")
        json_requested = display_name.lower().endswith(".json")
        if hasattr(value, "to_plotly_json") and hasattr(value, "to_json"):
            try:
                rendered = value.to_json()
            except (TypeError, ValueError) as exc:
                raise ValueError("Plotly figure JSON is not serializable") from exc
            write(f"plotly-{index}.json", "application/vnd.plotly.v1+json", rendered, display_name)
            return
        if json_requested:
            try:
                rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("JSON output must contain JSON-serializable values") from exc
            mime_type = "application/vnd.plotly.v1+json" if isinstance(value, dict) and set(value) == {"data", "layout", "config"} else "application/json"
            write(f"output-{index}.json", mime_type, rendered, display_name)
            return
        if isinstance(value, Figure):
            path = OUTPUT_DIR / f"figure-{index}.png"
            value.savefig(path, format="png", bbox_inches="tight")
            if path.stat().st_size > MAX_ITEM_BYTES:
                path.unlink()
                raise ValueError(f"captured output {path.name} exceeds {MAX_ITEM_BYTES} bytes")
            manifest.append({"path": str(path), "mime_type": "image/png", "display_name": display_name})
            captured_figures.add(value.number)
            return
        if hasattr(value, "to_plotly_json") and hasattr(value, "to_json"):
            write(f"plotly-{index}.json", "application/vnd.plotly.v1+json", value.to_json(), display_name)
            return
        if isinstance(value, pd.DataFrame):
            if csv_requested:
                write(f"table-{index}.csv", "text/csv", value.to_csv(index=False), display_name)
            else:
                write(f"table-{index}.html", "text/html", value.to_html(index=False), display_name)
            return
        html = getattr(value, "_repr_html_", None)
        if callable(html) and (rendered := html()) is not None:
            write(f"output-{index}.html", "text/html", str(rendered), display_name)
            return
        if isinstance(value, str):
            write(f"output-{index}.csv" if csv_requested else f"output-{index}.txt", "text/csv" if csv_requested else "text/plain", value, display_name)
            return
        write(f"output-{index}.txt", "text/plain", repr(value), display_name)

    namespace.update({"pd": pd, "np": np, "display": emit, "emit": emit, "emit_frame": emit_frame})
    setattr(plt, "show", lambda *args, **kwargs: None)
    stdout_log, stderr_log = BoundedLog(), BoundedLog()
    stdout_fd, stdout_thread = pipe_output(1, stdout_log)
    stderr_fd, stderr_thread = pipe_output(2, stderr_log)
    try:
        with contextlib.redirect_stdout(stdout_log), contextlib.redirect_stderr(stderr_log):
            tree = ast.parse(code, filename="<generated-analysis>", mode="exec")
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                body = ast.Module(body=tree.body[:-1], type_ignores=[])
                # pi-lens-ignore: ast-grep:no-compile-call
                exec(compile(body, "<generated-analysis>", "exec"), namespace)
                # pi-lens-ignore: ast-grep:no-compile-call
                emit(eval(compile(ast.Expression(tree.body[-1].value), "<generated-analysis>", "eval"), namespace))
            else:
                # pi-lens-ignore: ast-grep:no-compile-call
                exec(compile(tree, "<generated-analysis>", "exec"), namespace)
    finally:
        for fd, original in ((1, stdout_fd), (2, stderr_fd)):
            os.dup2(original, fd)
            os.close(original)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        OUTPUT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        for number in plt.get_fignums():
            if number not in captured_figures:
                emit(plt.figure(number))
        for output_name, log in (("stdout.txt", stdout_log), ("stderr.txt", stderr_log)):
            path = OUTPUT_DIR / output_name
            path.write_bytes(log.data)
            path.chmod(0o600)
        (OUTPUT_DIR / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
        (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if stdout_log.overflow or stderr_log.overflow:
            raise RuntimeError("generated output exceeded the sandbox log limit")


def run(code: str, bundle_path: str, seed: int) -> None:
    plt, np, pd, Figure = runtime_modules()
    data, audit = load_dataframe(bundle_path, pd)
    Path(bundle_path).unlink(missing_ok=True)
    execute(code, seed, {"df": data}, audit, plt, np, pd, Figure)


def run_document(code: str, document_path: str, input_format: str, seed: int) -> None:
    if input_format not in {"csv", "xlsx"}:
        raise ValueError("unsupported document format")
    plt, np, pd, Figure = runtime_modules()
    audit = {"input_rows": 0, "valid_rows": 0, "excluded_rows": 0, "rules": []}
    execute(code, seed, {"document_path": document_path, "input_format": input_format}, audit, plt, np, pd, Figure)
