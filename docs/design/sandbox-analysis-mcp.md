# Sandboxed Python analysis MCP

Status: implementation design; capability-driven report manifest TDD slices implemented, localhost/browser acceptance pending

Branch: `feature/sandbox-analysis-mcp`

Runtime: OpenSandbox (`opensandbox==0.1.15`, Apache-2.0)

## Architecture

Ask O11y is the only runtime LLM planner. It dynamically selects live tools and up to two embedded Agent Skills. Query-only, query-plus-Dashboard, query-plus-Sandbox, and combined requests are optional compositions; no component prescribes an order, method, target, feature set, panel type, or layout.

```text
Ask O11y LLM
  ├─ Ontology MCP (bounded read-only semantic declarations)
  ├─ Data Query Planner MCP (plan only; deterministic semantic gate)
  ├─ Grafana Query MCP (datasource read → opaque frame_ref)
  ├─ Sandbox Analysis MCP
  │    └─ fresh OpenSandbox Code Interpreter
  │         ├─ trusted validity filtering
  │         ├─ generated Python
  │         ├─ deny-all egress
  │         └─ bounded named outputs
  ├─ embedded Grafana dashboarding Skill (advisory)
  ├─ hidden Artifact Bridge MCP (opaque binding only)
  └─ built-in mcp-grafana_update_dashboard (sole Dashboard writer)
```

The external endpoints bind loopback, require the shared service bearer, and use server-configured org/user identity. Model-visible tools exclude Artifact Bridge. Ontology is read-only and has no datasource credentials; Planner remains the final trusted semantic enforcement point. Built-in Grafana mutation still passes Ask O11y's host approval gate.

## Sandbox contract

### Capability-driven report output amendment (2026-09-06)

A successful analysis may emit a bounded `report-source-v1` declaration alongside captured outputs. The Host, not the model, resolves it into an immutable `report-manifest-v1` and returns `refs.report_manifest_ref`. The declaration names artifact IDs and safe fact references; it never names a guessed output index as a public coordinate.

For each declared artifact the Host evaluates Plotly first: a valid sanitized figure produces `render.mode="plotly"`; only when no figure exists may a validated PNG produce `render.mode="image"`; an invalid figure fails closed even when a PNG is present. Ordinary summary JSON without the source format is not a report manifest. Existing execution/provenance refs remain readable and unauthorized or ambiguous refs fail closed.

Plotly mode does not require a PNG fallback. PNG is an explicit image capability when no figure was produced, not a silent substitute for an invalid figure.

Sandbox Analysis exposes six tools:

- `profile_dataset`: run the deterministic full-data profile over one authorized frame without model-authored Python.
- `execute_python_analysis`: execute a new revision against one authorized frame.
- `execute_python_preprocessing`: execute generated preprocessing against one authorized original uploaded CSV/XLSX document; `emit_frame` returns both a chained frame and derived session dataset.
- `list_python_analyses`: list retained revisions for the authenticated context.
- `inspect_python_analysis`: return retained source and compact metadata, never frame rows.
- `revise_python_analysis`: run complete replacement source against a prior revision's authorized frame.

A profile call is:

```json
{
  "frame_ref": "artifact://run_…/grafana-frame",
  "seed": 42
}
```

`profile_dataset` computes missingness, numeric distribution, categorical concentration, bounded Spearman relationships, and a supported temporal trend over every input row and field. Its bins and time buckets are visual-only; the manifest records `full_data=true`, `sampling=false`, and `derived_dataset=false`. The trusted host composes this program from the opaque query plan, so it cannot become a model-method selector.

A model-authored Python call remains available only for an explicitly confirmed analysis that has a suitable contract:

```json
{
  "frame_ref": "artifact://run_…/grafana-frame",
  "python_code": "display(df.describe())",
  "seed": 42
}
```

Rules:

- `frame_ref` must be exactly one authorized `grafana-frame`; `document_ref` must be an owner/session-bound `uploaded-document` ref returned by upload inspection.
- The trusted input bundle carries native Grafana columnar JSON and query-plan validity rules.
- Trusted bootstrap constructs and filters `df` before generated code runs, then unlinks the input bundle.
- Source is UTF-8 and limited to 32 KiB. The MCP hashes and transfers it but never executes it locally.
- Frame execution receives `df`, `pd`, `np`, `display`, `emit`, and `emit_frame`. Document preprocessing receives `document_path`, `input_format`, `pd`, `np`, `emit`, and `emit_frame`; original host paths are never exposed. A `.json` name captures JSON; a `.csv` name captures a downloadable CSV.
- An analysis dashboard is a capability-driven report: generated Python may emit a sanitized Plotly figure, a PNG image, and bounded textual facts. A valid figure is rendered as Plotly; a PNG-only artifact is rendered explicitly as image; no Sandbox output becomes a native Grafana chart target.
- Output names are sanitized metadata and never filesystem paths. `emit_frame` is fail-closed for document preprocessing and analysis; derived datasets are not accepted as model inputs. The historical 200-field/5,000-row/4 MiB limits remain rejection bounds, not a persistence promise.
- Raw frames, query bodies, physical paths, credentials, full MIME payloads, stdout logs, and exception values are not returned to the model. Only explicitly emitted text/JSON is returned inline, bounded to 32 KiB total.

Success returns opaque refs, validity evidence, provenance, and compact output metadata:

```json
{
  "ok": true,
  "refs": {
    "execution_ref": "artifact://run_…/sandbox-execution",
    "provenance_ref": "artifact://run_…/sandbox-provenance",
    "report_manifest_ref": "artifact://run_…/report-manifest"
  },
  "output_summary": {
    "result_count": 1,
    "inline_results": [
      {
        "output_index": 0,
        "display_name": "result.json",
        "mime_type": "application/json",
        "value": {"rmse": 12.3}
      }
    ],
    "downloads": [
      {
        "output_index": 1,
        "display_name": "result.csv",
        "mime_type": "text/csv",
        "url": "https://…/assets/<signed-token>",
        "expires_at": 1780000000
      }
    ]
  }
}
```

A failure returns a bounded classification and authorized diagnostic refs. It never falls back to host execution.

## Data transport and isolation

Grafana DataFrames remain columnar JSON (`schema.fields` plus `data.values`). CSV and SQLite are not input intermediaries because they lose typing or duplicate decoding and lifecycle work. Arrow IPC or Parquet is deferred until measurement proves JSON material to performance.

Every call creates and destroys one sandbox. No kernel persists across turns.

| Control | Limit |
| --- | ---: |
| sandbox lifetime | 3600 seconds |
| code start (one automatic retry) | 60 seconds |
| code execution | 900 seconds |
| CPU | 4 |
| memory | 4 GiB |
| source | 32 KiB |
| input bundle | 16 MiB |
| original uploaded document | 50 MiB, CSV/XLSX only |
| captured execution | 32 MiB |
| derived frame output | 200 fields, 50,000 rows, 16 MiB |
| inline text/JSON | 32 KiB total |
| signed CSV download | 4 MiB each, artifact-retention expiry |
| output field summary | 200 fields |
| egress | deny all |
| credentials | none |
| host volumes | none |

Production rejects unpinned images and unapproved runtime classes. The MCP verifies the OpenSandbox TOML network/runtime settings and records its hash. `runc` requires explicit local-development opt-in. No regex or import blacklist is used; containment belongs to the sandbox boundary.

## Dashboard authoring and opaque binding

Sandbox output is evidence, not a Dashboard. When Grafana output is requested:

1. The Ask O11y LLM selects the embedded dashboarding Skill and reads live built-in tool schemas.
2. The LLM authors the complete Dashboard JSON, including panel types, options, layout, and opaque bindings.
3. The host invokes hidden `artifact-bridge_resolve_dashboard_refs`.
4. The bridge validates artifact ownership and replaces only trusted data/asset placeholders.
5. The host dispatches the resolved Dashboard to approved built-in `mcp-grafana_update_dashboard`.

A native query target is model-authored as:

```json
{
  "$plan_ref": "artifact://run_…/query-plan",
  "fields": ["date", "value"],
  "refId": "A"
}
```

An analysis dashboard may not contain Grafana data targets. It uses only PNG asset bindings and optional text panels. A dashboard that did not call Sandbox may use `$plan_ref` query targets; the bridge rejects a dashboard that mixes an analysis asset with a data target.

For an image, the LLM authors its chosen panel and an opaque URL placeholder:

```json
{
  "type": "text",
  "options": {
    "mode": "html",
    "content": "<img src=\"$asset_url_shap\" alt=\"SHAP output\">"
  },
  "askO11yAssetBindings": [
    {
      "placeholder": "$asset_url_shap",
      "$execution_ref": "artifact://run_…/sandbox-execution",
      "output_index": 1
    }
  ]
}
```

The bridge validates the output and replaces the placeholder with a signed URL. It does not generate the `<img>`, select the text panel, transform PNG into HTML, or write Grafana. The Sandbox asset endpoint validates the signature, retention deadline, and artifact authorization, then streams the stored bytes with their trusted MIME type. Sandbox returns signed URLs only for captured CSV downloads; PNG Dashboard URLs remain hidden Artifact Bridge outputs.

The bridge rejects every nonempty target without an opaque binding, including mixed dashboards that combine authorized and raw datasource targets. It also rejects model-authored datasource/query/URL bodies, physical artifact URLs, unsupported fields, foreign refs, unresolved placeholders, excessive total nested panels/targets/assets, and oversized dashboards.

## Preview and publication lifecycle

The execution turn must create one complete Dashboard JSON. The host normalizes `ask-o11y-preview` to the first tag, records the writer-returned UID under org/user/session identity, disables all further tools, and lets the final response return the real URL and request confirmation. The intended final title is used; visible Preview state is only the tag.

After explicit confirmation, the capability selector exposes only built-in Dashboard read/update tools. The host ignores any model-selected UID, derives the reviewed UID from its lifecycle record, reads the Dashboard, verifies both UID and first tag, and only then removes `$.tags[0]`. Missing state, selector uncertainty, UID mismatch, or missing Preview tag fails closed. A successful publication consumes the lifecycle record. This prevents query, Python, Skill selection, or panel regeneration in the publication turn.

Only a successful built-in Grafana write and returned URL prove a Dashboard exists. A Sandbox output alone does not.

## Cross-conversation recovery

Artifacts persist for the configured retention period. Ask O11y compacts only successful refs, output schemas, and Dashboard identities into later turns. List/inspect/revise recover analysis source and provenance without restoring raw frames, MIME bodies, complete tool responses, or kernels.

The reproducible Ask O11y v0.3.2 integration patch is `patches/ask-o11y-dynamic-tools-and-timeout.patch`. It contains dynamic capability/Skill selection, embedded dashboarding references, opaque binding middleware, Preview/publication enforcement, successful-state compaction, and the 3600-second external MCP and Sandbox execution timeout.

## Acceptance criteria

1. Runtime config contains Ontology, Planner, Grafana Query, Sandbox Analysis, and hidden Artifact Bridge endpoints.
2. Ontology exposes bounded read-only declarations only; Planner independently enforces the pinned snapshot and analysis contract before query execution.
3. Engineering/Finance Analysis, `analysis_core`, external Renderer tools, and method-specific runtime paths are absent.
4. Grafana remains the only datasource executor; Ontology, Planner, and Sandbox have no datasource credentials.
5. Missing identity, foreign refs, raw frames, unsupported arguments, and oversized inputs fail closed.
6. `profile_dataset` profiles every returned row and field and records the full-data/visual-only invariants before any optional ML path.
7. Trusted validity filtering runs before generated code and is verified against the source row count.
8. Sandbox execution has deny-all egress, bounded resources/output, no credentials, no volumes, and unconditional cleanup.
9. Analysis PNG outputs remain behind authorized refs; signed URLs are host-resolved and never authored by the model.
10. Artifact Bridge preserves model-authored panels/options, resolves only authorized bindings, exposes no Grafana write tool, and consumes `report_manifest_ref` rather than model-guessed output indexes.
11. Preview is a real tagged Dashboard; publication removes the tag on the same UID without rerunning query or Python.
12. E2E proves SHAP PNG visibility without an analysis data target, query-only dynamic XY authoring, built-in-only publication, and absence of model-visible bridge calls.
13. Production deployment adds OpenSandbox authentication and gVisor, Kata, or Firecracker; local `runc` evidence is not production attestation.

## TDD implementation slices

1. `ml_plotly_contract.py`: red aggregate point-budget case, then count all bounded trace arrays and retain idempotent sanitizer behavior.
2. `ml_report_contract.py`: red Plotly-only/PNG-only/invalid-figure/summary-index cases, then add bounded source → Host manifest normalization.
3. Sandbox execution seam: red missing `report_manifest_ref`, then persist the manifest beside immutable execution/provenance and return the opaque ref.
4. Bridge/compositor seam: red figure-only and explicit image bindings, then remove mandatory fallback while preserving legacy readers.
5. Profile seam: red conditional Plotly candidate emission, then reuse existing profile aggregates without fixed chart selection.
6. Tool schema/prompt and gates: remove new index guessing, run scoped contract/security checks, then defer localhost/manual acceptance to the user.

## Deferred

- Multiple input frames.
- Pre-warmed sandbox pools.
- Persistent kernels.
- Arrow/Parquet transport before measured need.
- Persistent download service beyond artifact retention.
- Grafana Image Renderer for screenshots or PDF exports.
