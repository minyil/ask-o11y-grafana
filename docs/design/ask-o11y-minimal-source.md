# Ask O11y 精簡 source：新手自然語言分析

2026-09-15 新增：使用者核准在 dashboard preview header 匯出單檔離線 HTML，並核准本機官方 Image Renderer／Grafana 設定及 Ask O11y 部署；維持 main-only、不更動既有 dashboard、不公開分享。設計、限制與驗證見 [Dashboard HTML 匯出](dashboard-html-export.md)。此授權只覆蓋該功能，不擴張下列歷史任務範圍。

## Upstream 同步（2026-09-18，本機，未推送）

`ask-o11y/` 由 upstream `v0.3.2`（`8395ae1`）改為 **`v0.3.16`（`f713d5f`）** 為基底，fork 差異以 3-way rebase 重新套上（`feature/ask-o11y-minimal-llm-upstream-0.3.16`）。合併時的取捨：

- **Actor/session header** 改走 upstream 的 request context（`mcp.WithUserID` / 新增 `mcp.WithSessionID`），由 `customRoundTripper` 依 `req.Context()` 設定 `X-Grafana-Actor-User-Id` / `X-Grafana-Session-Id`；移除 fork 自有的 `CallToolForRequest` / `connectMCPWithActorAndSessionContext` API。`_server_session_id` 仍由 loop 注入以防模型偽造，但送 MCP 前剝除。
- **保留 fork 安全語意**：每次 tool call 使用獨立 client（不共用 session）；transport error 只對 read-only 工具自動重試，`update_dashboard` / `execute_python_*` / `revise_python_analysis` 不重送；設定檔 headers 不能覆寫 host 決定的 org / actor / session header；非 read-only 一律 approval。
- **Tool call timeout**：fork 原本硬編 3600s；upstream 改為每個 server 的 `timeoutSeconds`（預設 30s）。`scripts/configure-ask-o11y-workflow-tools.py` 已對三個分析 MCP 設 `timeoutSeconds: 3600`；**既有已套用的 settings 需重新 apply 才會生效**。
- **Prompt / skills**：upstream 0.3.11 起有正式 bundled skills 機制（`pkg/skills/`）。fork 的 `skills/analysis/SKILL.md` 併入 `pkg/plugin/analyst_prompt.md`（維持一律載入，不改為 on-demand skill），並補上 upstream 的 `{{.CurrentTime}}` 區塊。Upstream 的 observability skills（investigating-alerts 等）仍在 load_skill 目錄中，可於 AppConfig 停用。
- **Subpath**：`uploadClient.ts` / `grafanaFetch.ts` 改用 upstream `utils/subpath` 以支援 `appSubUrl`。
- Upstream 新增的 OAuth per-user MCP、MCP provisioner、result cache、trace propagation、Redis 併發修正、CVE 修補等全部帶入。

驗證（本機 Go 1.26.6、Node 24）：`go vet` / `go test ./pkg/...` 全過；`tsc --noEmit`、`eslint`（0 errors / 9 既有 deprecation warnings）、Jest 43 suites / 576 tests、`validate:openapi`、webpack production build 全過。未做 live Grafana / MCP / OpenSandbox 驗收。

## 授權與回退

- 2026-09-12 使用者批准建立 branch、按直接維護 source 的方向重新設計並開始實作。
- Branch：`feature/ask-o11y-minimal-llm`；原 `master` 保持 `762c262`。
- 開始修改前 checkpoint：`87e03b532e50e758fcf0ae268ccf6a4d96abaead`，包含之前未提交的 source/patch 修正，不含 `.env`、資料、session 或執行產物。
- Main-only，本機開發與提交；不 push、不部署、不更動正式設定或既有 Dashboard。真 live 驗收於部署/測試授權確認後執行。
- 回退 source 可在乾淨 checkout 另開 rollback branch 指向 checkpoint；不用 `reset --hard`。Git 回退不會自動回退已部署 image、設定或 Dashboard；本階段不改這些資源。

## 使用者體驗

使用者可以直接說「這份資料有什麼值得注意？」、「哪些因素與熱耗有關？」、「能否預測這個結果？幫我做圖」。使用者不必知道模型、特徵工程、切分、MCP、Python、artifact refs 或 contract。

系統從問題和可用資料自主決定探索、比較、統計或機器學習；不強迫每個問題先跑 profile/ML。只對會改變答案的業務歧義、新資料權限或外部寫入範圍提出簡單問題。技術參數由 LLM 選擇，結果先解釋「發現什麼、代表什麼、不能推論什麼」，方法細節按需要提供；不加固定報告章節 gate。

分析授權後不逐次詢問 Query/Python。生成錯誤由 LLM 根據已確定失敗且去敏的錯誤修正；逾時/未知結果不盲目重送。負面結果亦可交付，不為通過 gate 改模型。

Dashboard Preview 目前仍是 Grafana 中已儲存的 Dashboard，建立前確認寫入與分享範圍；正式發布另有授權。分析結果不可因原始 session 私有而誤認為 Dashboard 也私有。

## 最小實作

- `ask-o11y/`：從固定 upstream `8395ae10c3e38beae56329e4174a14a9a6d4c680` 匯入並直接維護，保留 LICENSE。不是 nested Git repository，也不在 `.scratch` 編輯正式 source。
- 沿用原生 Go LLM/MCP loop、登入/RBAC、session/run、錯誤呈現、原生寫入 approval。移除自訂 selector、分析/報告狀態機、品質裁判。
- 移入必要 UI/上傳後端、actor/session 傳遞、tool history、timeout；保留少量安全產物接線。原有知識以短 prompt/skills 真正載入，不重建 skill selector。
- MCP 資料入口驗真正權限/只讀查詢/回應上限；不驗模型、split、feature policy。移除 mandatory analysis plan 票券，不開 Python 直連資料庫。
- OpenSandbox 接受授權資料和 Python，回傳真實結果/圖表與執行狀態。保留既有 `df`/`emit`，不要求 ML/report-source contract；沿用隔離、無任意網路/secret、CPU/RAM/timeout、取消與安全輸出。
- Plotly 接收保存的 native figure；LLM 只寫 layout/敘述與引用，不抄大量數值。Bridge 只做安全讀取/解析，不編排 report。
- 建置從 source 出發，不 clone/apply/reset/source rm；建置與安裝分開，預設不部署。舊外部 provider timeout/execd completion 屬其他套件修正，不因刪 Ask O11y patch 一併撤掉。
- 使用者最新要求乾淨版本、不保留歷史相容：移除舊 Dashboard/report/Plotly 格式的 readers/adapters 及專用依賴，不做舊格式遷移。必要的當前執行授權、完整性與未知效果 receipts 保護仍保留。不新增 runtime framework、datastore 或通用 recovery schema。
- 不刪正式 Dashboard/artifacts/sessions/receipts 或 Git 歷史；不再支援舊格式是 source 決策，不是正式資料清除授權。

## TODO（TODO-aab6376c）

- [x] M0：branch 與回退 checkpoint。
- [x] M1：正式 source／無 patch 本機建置可用；原生 loop + 必要 UI/上傳/session/timeout；沒有移入自訂 analysis/delivery 控制。Go/TS/前端建置通過；不是現行 MCP 整合或 live 驗收。
- [x] M2：資料 MCP 與通用 Python 執行鏈精簡；取消分析 contract/plan 准入；保留安全查詢、正確 completion/cancel/status、已完成錯誤可修。真 UI 證明 query→OpenSandbox、Python KeyError 依同一 frame 修復、取消後 reconcile/`redispatch_allowed:false`。
- [x] M3：native figure 直達新 Preview，移除 report workflow 與歷史相容分支；原圖/來源不變、授權/UID/version/readback。真 UI 建立唯一測試 UID、Plotly browser render、readback version 1，stale `version=0`/`overwrite=false` 收到 HTTP 409 且內容未變。
- [x] M4：新手 prompt/skills 實際載入、設定與工具目錄精簡；custom prompt 明確遷移，不靜默覆寫；移除退役 patch/source/test 接線。設定 readback/真 UI tool selection 與 default prompt、三 MCP、部署後 prompt context 均已核對；替換 default prompt 由明確旗標授權。
- [ ] M5：實際 Ask O11y UI 上傳→LLM 自選方法/碼→Sandbox→圖表/Preview；reload續談與重用目前格式的結果、已完成錯誤修碼、負面結果、長任務/取消/未知不重送、跨使用者拒絕/寫入授權。舊 Dashboard 相容驗收依最新要求取消。除本機只有 `admin`、未建立第二使用者而未測的 authenticated cross-user ownership rejection 外，其餘上述真 UI 證據已完成。

M1/M4 可以先完成部分，但不得宣稱 MCP 或 live E2E 完成。先前 California 是 main 手工 MCP/REST 驗證，不可當作 M5 證據。舊業務分析與模型品質 backlog 暫停，沒有新增預測品質門檻。

## 驗證與交付記錄

以下為各時點的歷史證據，不覆蓋上方目前範圍。使用者後續明確要求「不用保留歷史相容,我要一個乾淨的版本」；因此下列先前保留 legacy readers／舊 Dashboard 驗收的決定已被取代，不再是實作或接受條件。

### M1 本機 source/build checkpoint（2026-09-12）

- `2d2c7d4` 匯入固定 upstream；後續修改只移入必要 UI/上傳、session tool history、actor/session headers、MCP timeout/retry 限制與 context sizing，未移入舊 analysis/delivery/selector 類別。
- `scripts/build-install-ask-o11y.sh` 保留原入口名稱，但**現在只建置** `ask-o11y/`，沒有 clone、patch、source reset/delete、安裝依賴、Docker 或重啟操作；舊 `ASK_O11Y_BUILD_DIR` 不再指定正式 source。不要用此名稱推斷它仍會安裝。
- 既有 `node_modules` 僅以 ignored symlink 重用。第一次建置發現共用 webpack cache 包含舊 scratch source，改建置入口使用原生 `--no-cache`；最後 source maps 查核沒有舊 scratch application source。沒有修改 scaffolded `.config`。
- 使用者明確批准本機使用已安裝 Node 24.18.0；未安裝 Node 22、未改正式 runtime。Go 1.26.5，`CGO_ENABLED=0`，不冒稱 race 檢查通過。
- 聚焦回歸發現 upstream 對未知名称且未標唯讀的工具不要求批准；改以非唯讀為批准預設（保留 operator overrides），以測試驗批准 ID 綁定、未批准不呼叫、null/array/string args 拒絕與 host session 覆寫。這不等於已完成 indeterminate operation 防重送。
- 上傳沿用 session ownership/容量上限，改用 Grafana SDK HTTP client，讓真實 org 覆寫配置中的 org，拒絕將讀取失敗當成成功刪除；補 OpenAPI 與 header 回歸。
- 修正移入 Chat 的 memo 缺少 attachment/session dependencies；不是增加新 UI 框架。

驗證證據在 `.scratch/minimal-source/`：

- `go-test-final.log`：`CGO_ENABLED=0 go test ./pkg/...` 全 package 通過。
- `go-vet.log`：`go vet ./pkg/...` 通過。
- `frontend-tests-final.log`：34 suites / 492 tests 通過；保留既有 React act 警告。
- `lint-final.log`：0 errors，9 個 deprecated API warnings。
- `openapi.log`：OpenAPI valid，2 warnings。
- `build-final.log`：`tsc --noEmit`、webpack production（1 asset-size warning）、Go backend、build stamp 通過。產物 `0.3.2+local.3546d78c8241a4ba` 只在本機 dist，沒有安裝。

診斷限制：舊四個 scripts 的五個告警已按當前 body + primary LSP=0 核對並 mark false-positive；turn-end 仍重報。新增目錄的 lens/TS LSP 亦以無 JSX/ES5/缺 React 的配置報錯，但同一 source 的專案 `tsc --noEmit`、Jest 與正式 webpack 都通過。`lens_diagnostics(mode=all)` 最後仍列 Chat.tsx 28 blocking + 其他 warnings，因此**不宣稱 lens 或全 repo 綠燈**；以 source-bound 專案命令作本階段替代證據，不為 stale/錯配設定改 source 或修工具。

M1 checkpoint 當時 M2–M5 未完成；下方記錄後續進展。既有 patches 暫留作 rollback 記錄但不再被本機 build 使用，待 consumers 同步完成再移除。source checkpoint 不能當成整體產品或部署驗收。

### M2–M4 本機整合進展（2026-09-12）

已接通的最短路徑：`query_dataset → authorized frame → execute_python_analysis → captured native figure → approved native Dashboard writer`。沒有新增 agent/skill selector、資料庫、流程引擎或分析方法裁判。

- Query 重新取得授權 metadata，重用 Grafana `/api/ds/query`；CSV 使用 server-owned datasource/URL/template，執行前驗證授權查詢與回應／時間／容量上限，不要求 analysis plan/ontology 准入。新 frame 必須有 authenticated session。
- 通用 Python 不再讀 plan、注入 validity/semantic contract、要求 report preflight/manifest。輸出保留原始 execution、provenance、derived frames 與 native figures；明示 `trusted_ml_contract=false`。標準 Python 錯誤只回去敏 class/generated-code 行號，已完成失敗可修碼。缺 completion/error 證據為 indeterminate；現有 receipts 擋相同操作重送。
- Bridge 與 Go writer 已接 native figure binding：未批准不解析/寫入，停用 bridge 或解析失敗不寫入，數值直接傳 writer、不回灌模型。沿用 sanitizer 拒絕外部網路圖像等不安全 figure，原始 execution 不刪。舊讀取分支保留，未聲稱做過舊 Dashboard live 驗證。
- 公開 tools/list **及 tools/call** 均移除 profile/fixed ML/reexport/repair 與 report prepare/inspection/compositor；舊 private helpers 尚待清理，不能稱整個 repo 已完成退役。
- `analyst_prompt.md` 是 Go embed 與 settings 候選的同一份預設；`skills/analysis/SKILL.md` 直接接進原生 `BuildSystemPrompt`，其結果經 `handleAgentRun` 傳入 `LoopRequest.SystemPrompt`。它是 advisory，不是新選擇器或 gate。
- 設定候選縮為 Grafana Query / Sandbox Analysis / Artifact Bridge 三個 MCP。resolver 在 operator config 啟用供 host 使用，但對模型隱藏並禁止直接呼叫。候選維持 `approval-gated-writes`；遷移保留非空 custom prompt、既有 built-in tool selections 及其他無關設定。沒有執行真 settings apply。

驗證：`.scratch/minimal-source/mcp/` 的 `settings-python.log` 是真 MCP handlers + 隔離 fixtures（不在 host 執行生成 Python）：涵蓋 CSV 與安全 SQL→frame、無 plan 計算、actor/org/session 拒絕、receipt 重用、已完成錯誤修碼、相同未知操作不重送、native polar figure/不安全 figure 拒絕、退役工具拒絕、工具目錄一致，以及 mock settings migration 保留既有值。`settings-go.log` 全 `go test ./pkg/...` 通過，`settings-vet.log` 通過；含批准/bridge 邊界與 actual system prompt skill 載入測試。`settings-build.log` 的 tsc/webpack/Go/build stamp 通過，最終產物 `0.3.2+local.5421aa3ba73b1e22`，僅本機 dist；skill 補標題後的 `prompt-final.log` 亦通過。最後 source hashes 在 `source-sha256.txt`。`settings-candidate.log` 是離線 CLI self-check（包含修正原本相對/外部 `--out` 顯示路徑會失敗的小 bug），不是 settings apply。

本輪 3 個 Go prompt 檔及 4 個 Python production/config 檔 primary LSP 無 errors。既有 TS/JSX 診斷錯配沒有藉改 source/tsconfig 迴避，project tsc/build 是當前 source 的替代證據；fixture JSON decode 故意讓錯誤使測試失敗，`is False` 檢查真正 bool，非 production 未處理輸入。prompt 的 text/template 只產 LLM 純文字，非 HTML/XSS sink，已標 false-positive。最後 lens all 仍有歷史 ETL 檔案的 blocking 及既有 warnings，本輪不擴到該 backlog；不宣稱全 repo/lens 綠燈。曾誤執行既有 live completion check，因缺 `EXPECTED_EXECD_SHA256` 在初始化停止，`completion.log` 保留且不計入通過。

仍未完成（原 M2–M5 範圍，不增新功能）：

- 真正的 MCP→OpenSandbox 取消／狀態收斂驗收：下方已接 agent run context、原生取消及 owned sandbox 停止；仍未以真 UI/OpenSandbox 驗證。
- 跨 run/改碼後的 indeterminate 拒重送驗收：下方已沿用 journals 加入 session 防重送，尚未完成真實驗證。更正先前判斷：原 MCP 呼叫本就使用 per-call isolated client，不需另建 actor/session 隔離層。
- 退役 private helpers、patch/source/test consumers 清理；完整 writer UID/version/readback 與舊 Dashboard 相容驗收。
- 取得另行部署/live 授權後的真 Ask O11y UI/LLM E2E（非 main 手工 RPC）。以上 runtime 保護完成前不可部署。

設定遷移時先審閱三 MCP 候選與寫入 policy，另確認 operator 是否要以新預設取代既有非空 prompt；未批准時保留原值並明示可能仍帶舊流程文字。不可靜默 reset custom prompt。舊 Dashboard、資料、artifacts、sessions、receipts 與 live services 本輪均未修改。

### 取消接線與 Sandbox 固定產生器退役（2026-09-12；source checkpoint，未真實驗收）

使用者明確禁止 hardcoded 分析流程／Python、fixture 與造假。本輪不執行 fixture/mock，不以靜態檢查或手工 RPC 結案，也沒有部署、設定套用、真資料或 Dashboard 寫入。

- Client/Proxy 的 request context 接到 agent 工具與內部 figure resolver，保留既有 timeout/client shutdown；沿用原有 per-call isolated client。沒有新隔離框架。
- Sandbox HTTP 接原生 MCP session/request cancellation，以 authenticated org/user/application-session/MCP-session/request ID 對到 owned sandbox；SDK kill 未確認時保留 indeterminate，不宣稱取消。建立前收到取消不建立 sandbox；已取得 ID 即綁定取消，不再等 SDK readiness 完成。使用 SDK 公開 `skip_health_check`、`is_healthy()` 等待真實 readiness，因原 SDK 的 blocking readiness loop 會吞掉 cancellation exceptions；不偽造 ready。仍須真實驗證 create/ready/execute/kill 的時序與收斂。
- 既有 journal + session file lock 擋正在執行或未知工作的換碼重送；可省略 operation ID 列出最近 owned statuses，再依 ID 查 receipt。沒有新資料庫、通用 recovery schema 或自動重播。這是 source 接線，不是已通過跨 run 真實驗收。
- 取消 API 改回 `cancellation_requested`，無本機 cancel handle 時明確拒絕；run 收尾在 agent 結束後依真正 context 判斷取消，不再用 cancel map／事件尾端猜測。`cancelled` 表示 agent 停止，並提醒遠端工作可能仍在執行、須先對帳；不能當 Sandbox 終止證據。OpenAPI 僅定點更新此回覆，保留原有未提交格式變動。
- 移除 Sandbox 的 32 個退役定義／常數：約 1,485 行固定分類／迴歸／profile、plan/ML contract、report preflight、repair/reexport 產生器與舊 self-check。通用分析／文件處理／revision 不再接受替換 executor，也不再有 `runtime_class=fake` 分支。實際分析碼仍只來自工具的 `python_code`，直接交 OpenSandbox。
- 原有授權 frame/document、execution/provenance/history 與 native output readers 保留；source AST 比對確認 14 個既有讀取／capture/output 函式未變，Bridge source 未變。這不是舊 Dashboard live 相容性驗收。
- 本輪及前一 checkpoint 的 minimal fixture 測試撤出產品樹，原始內容／失敗紀錄保留在 ignored `.scratch/minimal-source/cancellation/retired-fixtures/`，不作驗收。README/Local Development 移除舊 self-check／fixture 驗收指引。其他退役 consumers 盤點於 `retired-consumers.txt`；Bridge／Query 私有舊流程、其餘 source/test/patch consumers 仍待清理。

當前證據在 `.scratch/minimal-source/cancellation/`：`source-build.log`、`source-vet.log` 是 Go build/vet；`openapi.log` valid（原 2 warnings）；`build-final.log` 通過 tsc/webpack/Go/stamp（1 asset-size warning），僅本機產物 `0.3.2+local.6f4035e59073595c`。最後 Python `py_compile` 與 primary LSP 通過；`source-checks.log` 是對實際 source 的 AST/handler/保留 readers 檢查，沒有 import 服務、合成資料或執行分析。`source-sha256.txt` 綁定最後 source。

前一 checkpoint 的 Scoped lens all 無 blocking、client.go 有 13 個 warnings；不是全 repo 綠燈。當時自動重播的五個舊 script/agentClient 警告已確認檔案與 `ef847ca` 未變，未為錯配診斷改碼；當時 project typecheck/build 通過。M2–M5 仍不勾選：required live evidence 未取得，source/build 不替代真 UI／LLM／OpenSandbox 驗收。

### UI 取消狀態續接（source-only，未完成驗收）

源碼追蹤確認：`useChat.stopGeneration` 原先先 abort SSE 並清除 run ID；sendMessage 的 AbortError 路徑卻顯示「Generation stopped」。後端取消收尾關閉 stream、没有 done event，`reconnectToAgentRun` 也未讀 run status 就重連。這些是源碼可見的責任錯置，並非真 UI 復現證據。

- 停止按鈕只送取消、清掉待送佇列，保留既有 SSE/run ID 直到狀態收斂。只有 detached POST 尚未回 run ID 的時窗需要一個 controller-bound ref 記住停止請求，拿到 ID 即送同一 run 的取消；沒有新增 controller、排程或分析流程。
- SSE EOF 後沿用既有 owned run status API，辨別 cancelled/failed/completed 與仍 running；不把 EOF、HTTP acknowledgement 或瀏覽器 AbortError 當遠端已停止。不產生假 done event／iterations。取消訊息明示 Agent 停止不等於遠端已終止，須對帳 receipts。
- session 切換／離開仍中斷舊觀察；舊 sendMessage 的 catch/finally 不再清掉新 run 或改寫新 session 訊息。保留原有未提交 Chat.tsx/OpenAPI 內容。

本次 `node node_modules/typescript/bin/tsc --noEmit` 與兩檔 ESLint 均 exit 0（`.scratch/minimal-source/cancellation/ui-{typecheck,lint}.log`，成功時無輸出）。完整 build 未跑成：shell 發生 `fork: Resource temporarily unavailable`（exit 254），一次同入口重試為 `spawn /bin/bash EAGAIN`；第一次未建立 build log，不能沿用舊 build 冒稱最終 source 通過。Primary LSP 兩檔逾時未確認；scoped lens all 無 blocking、8 warnings，包含 runner `pthread_create: Resource temporarily unavailable` 造成 ESLint JSON parse failure，以及既有大 hook 的 complexity/fanout 警告。

當時未執行 fixture/mock、預設 Python 或 live；沒有部署/設定/資料/Dashboard 寫入、push 或新 commit。主機程序／thread 資源曾阻擋完整 source build，未擅自終止他人程序。以下為恢復建置後的最新進展；M2–M5 仍未完成，真 UI/LLM/OpenSandbox 驗收仍須另行授權。

### Build 補驗與 Bridge 退役 source 清理

- 同一 `scripts/build-install-ask-o11y.sh` 入口補跑：第一次 `npm run typecheck` Aborted（exit 134，`ui-build.log`），隨後 shell 再次 EAGAIN。只在建置命令設定 `GOMAXPROCS=2 UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1` 後成功完成 tsc、no-cache webpack、Go build 與 stamp（`ui-build-bounded.log`，exit 0，1 asset-size warning），產物 `0.3.2+local.498255f693086e3f`，未部署。這僅證明低併行建置成功，不宣稱查明主機資源耗用根因；沒有工具／OS／產品設定變更。
- `artifact-bridge-mcp/server.py` 刪除 18 個退役定義、report synthesis schemas、兩個無再使用的 module loads 與 self-check；淨減 607 行。移除 prepare/inspect/compose 與其無 caller 的私有 report/lineage helpers，不新增替代工具。RPC image-content 分支只服務已退役 inspection，亦移除。
- 保留 `_read_report_binding`、`_canonical_report_material` 及其舊格式依賴：舊 Dashboard 的 presentation-error binding 仍需驗 retained manifest；不能因退役新報告產生器便移除舊 reader 的安全檢查。native output resolver、image/query bindings、reuse grant、TOOLS catalog 均保留。
- `bridge-source-checks.log` 只對真 source 做 AST 比對及 py_compile，沒有 import MCP、假資料或執行分析：23 個 retained definitions（含 resolver、舊 reader、授權與 HTTP handler）與 HEAD AST 相同；TOOLS AST 相同；18 個刪除 symbol 無殘留引用。`main` 與 RPC text packaging 是另兩個有意修改的函式。Python primary LSP confirmed clean。
- `ui-source-sha256.txt` 兩 UI 檔 hashes 重查一致，build 可繼續適用；`ui-pending-source.diff` 是本次 Bridge 修改前恢復取得的完整差異。最終差異另存 `bridge-pending-source.diff`。lens all 無 blocking、仍有 8 warnings（7 個 hook complexity/style，1 個先前資源不足 runner parse error）；不宣稱全專案綠燈。既有未提交 Chat.tsx/OpenAPI 與 staged source 均保留，未新 commit。

以上不是 runtime/E2E 或舊 Dashboard live 相容性驗收。當時 Query 私有舊流程、剩餘 patch/source/test consumers、writer UID/version/readback 與真取消／未知不重送驗收仍未完成；以下記錄後續 source 清理，未以靜態檢查或刪測試勾選 M2–M5。

### Query 舊入口與 Ask O11y patch stack 退役

- `grafana-query-mcp/server.py` 移除已不在 HANDLERS 的 `tool_execute_planned_query`、其專用 `verify_authorized_plan` 與固定 self-check；移除無用 ontology/hash/ref imports。`query_dataset`、SQL policy/metadata validation、frame shape/resource limits、time bounds、dataset/upload 授權與 HTTP handlers 不改。
- `query-source-checks.log`：18 個 retained definitions 與 HEAD AST 相同，TOOLS/HANDLERS 相同；3 個刪除 symbol 無引用。只做 source AST／py_compile／manifest JSON parsing，無服務 import、fixture、資料讀取或 query execution。Query Python 與修改的 CJS primary LSP confirmed clean。
- 退役 33 份 `patches/ask-o11y-*.patch` 與 3 支僅驗舊 patch/skill 流程的 scripts：`check-ask-o11y-autonomous-analyst.py`、`check-data-understanding-skill.py`、`check-ml-no-hardcoded-report.py`。刪除前確認這些檔案與 HEAD 相同，存 Git archive `retired-ask-o11y-patches.tar`，Git history 可還原。`retired-patch-inventory.txt` 記錄 patch 目標範圍；未動 OpenSandbox completion 與 Grafana LLM timeout patches。
- `check-ml-plotly-readability.cjs` 保留現有舊 panel SSR 檢查，依賴位置改為 `ask-o11y/`，只刪不再成立的 patch/prompt 文字斷言。没有新增 fixtures，也沒有執行其原有 fixtures 作驗收。NOTICE、reuse manifest、SBOM 的 Ask O11y 項改指直接維護的 source，保留 MIT／upstream／版權資訊；不是發布新的 SBOM 或部署聲明。scripts／目前 notices/manifests 已無舊 Ask O11y patch 路徑引用；歷史設計紀錄不改寫。
- `query-patch-free-build.log`：在 patches 已刪除後，既有 build-only 入口完成 tsc、no-cache webpack、Go、stamp（同低 thread 建置參數），exit 0、1 asset-size warning，產物 `0.3.2+local.498255f693086e3f`。本輪開始發現 agentClient.ts 較上次 hash 多了排版換行；未覆蓋，已用目前 bytes 重跑 build／兩 UI 檔 ESLint。CJS `node --check` 通過，`query-ui-lint.log` 無輸出；未跑 fixture tests。

尚未完成：其他直接依賴退役 tool/private API 的舊 source/test consumers（清單 `query-retired-consumers.txt`）、writer UID/version/readback、真 UI/LLM/OpenSandbox 取消／未知不重送／分析／舊 Dashboard 驗收。真實驗收仍需要另行部署/live 授權；不得用 main RPC 或 fixture 替代。沒有新架構、工具、模型、部署、資料/Dashboard 改写、push 或新 commit。

### 乾淨版本：歷史相容與固定產生器退役（最新 source checkpoint）

使用者明確取消歷史相容；本節及上方 TODO 取代先前保留 legacy readers／舊 Dashboard 驗收的決定。不刪正式資料，不作遷移，也不恢復舊格式。

- Bridge 移除 report-manifest、composed-dashboard、query-plan readers／resolution，Plotly 與 PNG 僅接受現在的 execution/output binding。新分析 Dashboard 保持空 targets；一般 Grafana query Dashboard 仍由 native writer 直接處理，不再繞 plan adapter。移除舊 Plotly→PNG 相容要求，只接受明確的 renderMode。
- Host／Sandbox 兩份 `ml_plotly_contract.py` 同步移除 legacy dispatch；native schema、typed-array／precision／缺值、外部資源／active content 與容量檢查未改。刪除兩份 `_plotly_legacy.py`、`ml_report_contract.py`、`ml_figure_inspection.py`、`ml_dashboard_compositor.py`。`ml_dashboard_contract.py` 只保留原樣的 text-panel HTML 安全檢查，不保留報告結構 gate。
- Plotly panel 直接呈現完整 native figure，不再依 v1/v2 選 decoder、拆 subplot、套固定 narrative/evidence 欄位；保留 Grafana theme、resize、ARIA/alt、輸入 clone、cleanup 與可見錯誤。`figureViews` source/types 已刪除。無 source-data fixture 或預設分析碼補位。
- 移除無現行 runtime caller 的 6 個固定 ML/profile 模組（preprocessing/execution/autoresearch/regression/presentation/data_profile），同步清理 Docker COPY／`.dockerignore`；code-update recipe 只清掉 dependency image 繼承的對應舊 application files。没有執行 Docker build／部署或更換已運行 image。Sandbox listing/inspection 亦不再回傳舊 report status/ref 欄位。
- 退役依賴已刪 API、固定模板、report/legacy decoder 的舊 scripts，包含先前曾保留的歷史 recovery receipt reader 與其專用 consumers。清單在 `retired-script-paths.txt`、`clean-version-retired-paths.txt`、`fixed-ml-retired-paths.txt`；原有 Git history 不改寫。刪除舊測試是去除退役路徑，不代表其安全條件已重新驗收。

實際證據在 `.scratch/minimal-source/cancellation/`：

- `clean-native-source-checks.log`：native sanitizer 定義與 HEAD 比對，僅移除 legacy 參數及分支；兩份 sanitizer bytes 相同；文字安全兩定義及 Bridge ownership/reuse/HTTP/placeholder primitives AST 不變；變更 Python py_compile、manifest JSON、Docker COPY source 存在性通過。不 import MCP、不造資料、不執行分析。
- `clean-native-panel-build.log`：panel tsc／webpack／dynamic-code 檢查成功；完整 native Plotly runtime，3 個 bundle/performance warnings。不是 browser/render QA。
- `clean-native-ask-build.log`：無 patch 的 Ask O11y tsc/no-cache webpack/Go/stamp 成功，`0.3.2+local.c87a30bf4347a5fe`，1 asset-size warning；只本機 dist。
- 9 檔 primary LSP batch 為 8 confirmed clean／1 timeout，不能稱 9 檔全部確認；Sandbox server 另一次 primary confirmed clean。L766 的 int 告警已核當前來源：輸入僅為 regex 擷取的 1–6 位十進位數字，已記 false-positive，不加多餘 try/except。最後 lens all 18 檔無 blocking、7 個既有 useChat complexity/style warnings；不是全專案掃描。

Native writer 的 source 調查：先前本機 Grafana LLM source `c72f0da` 的 go.mod 固定 `mcp-grafana v0.11.3`；其 `tools/dashboard.go` full-JSON 路徑直接傳 dashboard（含 UID/version）、folderUid/overwrite，save 只回 Grafana response，沒有自動 readback；patch 路徑先讀 dashboard，但保存時固定 overwrite=true。這只是對本機 dependency source 的觀察，不是目前服務 binary attestation，也不把 native approval／欄位透傳當作完整 version-conflict/readback 驗收。沒有為此增加 writer controller、state 或修改 upstream dependency。

M2–M5 仍未勾完成：剩真 UI/LLM/OpenSandbox 分析／修碼／取消／未知不重送、native writer UID/version/readback 與跨使用者／外部寫入驗收。舊 Dashboard 相容已取消，不能重新列為缺口。既有 staged/unstaged 工作保留，未新 commit/push/deploy/正式資料寫入；最終 source diff/hashes 另存 `clean-native-pending-source.diff`／`clean-native-source-sha256.txt`。

### Completion journal 收斂與真實驗收授權邊界

- 續接時重驗上次 31 個 source/log hashes：只有使用者通知的 panel `module.tsx` 格式化不同，其他全部一致。讀取目前 bytes 後重新跑既有 panel build，`current-panel-build.log` 通過 tsc／webpack／dynamic-code 檢查，仍 3 個 bundle warnings；沒有還原 formatter 變動。Ask O11y source 未變，沿用上節 `c87a30bf4347a5fe` build，不重跑無關檢查。
- 發現 `ArtifactStore.reconcile_operation` 尚有 response-only legacy reader，而且新 completion journal 已保存完整 result，`response.json` 為重複寫入。依乾淨版本要求移除該 fallback／重複 publication；目前只讀 `completion.json`，缺少它仍為 indeterminate 並禁止重送。狀態查詢不再重寫舊 response cache；原始 reserve、actor/session 驗證、file lock、未知工作阻擋與 durable completion 保持。沒有刪磁碟既有 receipts、加新 state 或 migration。
- `check-effect-reconciliation.py` 只驗「completion 已寫、response publication 前」的舊雙檔故障點，隨該機制退役，不新增或執行 fixture。`completion-only-source-checks.log` 對實際 staged source 作 AST 差異比對，确认上述移除之外的 methods／run_once 執行次序與 checks 不變，py_compile 通過。這不是 crash/取消 runtime 驗收。
- 本輪兩檔 primary LSP confirmed clean；最後 lens all 28 檔無 blocking、仍 7 個既有 hook warnings。L766 helper bytes 未變，先前已證 regex 數字轉換誤報，不因自動重播加防呆。
- 只讀 preflight 確認既有 Compose 的 Grafana／CSV server／Redis 為 running，現有 app dependencies 已有 Playwright 與本機 Chromium；沒有安裝能力或啟動 browser／分析。接下來實際驗收需要明確授權部署／重啟本機相關服務、套用精簡 Ask O11y 設定，以及使用既有登入與真資料。驗收限新測試 session／本次建立的 Dashboard，不覆寫原有資源；未取得授權前不執行。部署前仍需確認沒有其他活動工作，不能用 source-only 證據結案。

### 真 UI／OpenSandbox／native Dashboard E2E checkpoint（2026-09-12；本機授權範圍）

使用者已授權本機 Ask O11y、Plotly、三個 MCP、Sandbox code-only image 的部署／重啟與真 UI 驗收；限制為新測試 session 與本次新建 Dashboard，不覆寫既有資料/Dashboard、不 push/正式發布。部署完成：Ask O11y `0.3.2+local.c87a30bf4347a5fe`、Plotly `0.3.1`、Sandbox image `sha256:287a42e7467063b284226401baddb845165ba208827e031848305880f5b59210`。設定 readback 僅 `grafana-query`、`sandbox-analysis`、`artifact-bridge`；default prompt hash 與 `ask-o11y/pkg/plugin/analyst_prompt.md` 相同；`approval-gated-writes` 保留。

真 UI session `Kil7VX6vToXNa1a3DUe3LrKQNiEr40bkDdDXdrFn2O8` 完成：LLM 自主選既有授權資料，discover→inspect→query；兩次 time-bound query error 後自主修正，成功 query→真 OpenSandbox `execute_python_analysis`→native Plotly→繁中結果與限制。UI expanded tool evidence 顯示 6 calls/4 success/2 failed/4 evidence。真取消 session `kcIMfWY6PM6HB5b_id9itRbJIttIJvDiiFe7V7pngsY` 在 Sandbox tool 已開始後由 UI Stop；UI 明示 agent stopped 不等 remote termination；後續同 session 只用 UI 要求 reconcile，receipt 回 `cancelled`、`redispatch_allowed:false`，沒有 query/Python 重送。raw run 與 browser evidence 位於 `.scratch/minimal-source/live/browser/`。

首次 native writer 真 UI approval 以新 UID 404 預覽後安全失敗：LLM 使用 `$u1_operating_eda`，bridge 原 validator 過度限制 `$plotly_` prefix；ownership/session 與實際 execution figure 均正常，Grafana 仍 404、未寫入。修正 `artifact-bridge-mcp/server.py` 只放寬到 `$` 開頭安全 identifier（字母、數字、`_`、`-`，且須為完整 option value），未恢復歷史格式；保留 exact replacement、plugin/render mode、execution/output ref、sanitizer、ownership/session 與 unresolved checks。原失敗 payload source probe 修正後解析 1 binding，figure 為 `config/data/layout`；py_compile、primary LSP、lens edited files、diff check pass。

修正後第二次真 UI preview＋一次性 approval 成功建立唯一測試 Dashboard `ask-o11y-live-mtyk71rt`：readback HTTP 200、version 1、General、3 panels；Plotly panel stored `options.figure` 含 sanitized `data/layout/config`，UI 確認 execution/output ref、plugin、format/render mode，無 overwrite/後續修改/外部發布。瀏覽器直接開 Dashboard 成功 render，Plotly DOM 5 個，摘要、圖表、限制 panel 可見；證據 `dashboard-write-final-report.json`、`dashboard-rendered-report.json`、`dashboard-rendered.png`。

未登入開 session 導向 `/login` 且不曝露分析。Grafana 本機只有 `admin` 一個使用者；未新增 user data，因此跨另一個已登入使用者的 ownership rejection 未實測，不冒稱完成。所有 raw runs 已 completed/cancelled、無 running；OpenSandbox 列表為空。瀏覽器既有 `/api/health` abort、日期格式 warning、外部 Storyblok abort 僅列環境噪音。M2/M3/M4 仍不勾完成；M5 仍 open，尚未有真 generated-Python failure→repair、第二個已登入 user、或 version-conflict 競爭場景。
