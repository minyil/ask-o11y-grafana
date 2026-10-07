package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"consensys-asko11y-app/pkg/mcp"
)

func TestDashboardWriterResolvesWithoutReturningFigureToModel(t *testing.T) {
	var bound, written int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bridge := strings.HasPrefix(r.URL.Path, "/bridge/")
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "list-tools") {
			name := "update_dashboard"
			if bridge {
				name = "resolve_dashboard_refs"
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"tools": []map[string]interface{}{{"name": name, "inputSchema": map[string]interface{}{"type": "object"}}}})
			return
		}
		var request struct {
			Params struct {
				Arguments map[string]interface{} `json:"arguments"`
			} `json:"params"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
		}
		args := request.Params.Arguments
		if args["_server_session_id"] != "owned-session" {
			t.Error("missing host session")
		}
		content := `{"uid":"new-preview","url":"/d/new-preview"}`
		if bridge {
			bound++
			content = `{"ok":true,"dashboard":{"title":"New preview","panels":[{"type":"asko11y-plotly-panel","options":{"figure":{"data":[{"type":"scatterpolar","r":[12345]}]}}}]}}`
		} else {
			written++
			encoded, _ := json.Marshal(args["dashboard"])
			if !strings.Contains(string(encoded), "12345") || strings.Contains(string(encoded), "$execution_ref") {
				t.Errorf("writer did not receive the resolved figure: %s", encoded)
			}
		}
		_ = json.NewEncoder(w).Encode(mcp.CallToolResult{Content: []mcp.ContentBlock{{Type: "text", Text: content}}})
	}))
	defer server.Close()
	loop, _, cleanup := setupTestLoop(t, nil)
	defer cleanup()
	defer loop.mcpProxy.Close()
	servers := []mcp.ServerConfig{
		{ID: "artifact-bridge", Type: "standard", URL: server.URL + "/bridge", Enabled: true},
		{ID: "mcp-grafana", Type: "standard", URL: server.URL + "/grafana", Enabled: true},
	}
	loop.mcpProxy.UpdateConfig(servers)
	if _, err := loop.mcpProxy.ListTools(); err != nil {
		t.Fatal(err)
	}
	req := LoopRequest{UserRole: "Admin", SessionID: "owned-session", MCPServers: servers, ApprovalPolicy: "approval-gated-writes"}
	tc := ToolCall{ID: "writer", Function: FunctionCall{Name: "mcp-grafana_update_dashboard", Arguments: `{"dashboard":{"title":"New preview","panels":[{"askO11yPlotlyBindings":[{"$execution_ref":"artifact://retained/sandbox-execution","output_index":0}]}]}}`}}
	events := make(chan SSEEvent, 4)
	if _, failed, _ := loop.executeToolWithApproval(context.Background(), events, tc, req); !failed || bound != 0 || written != 0 {
		t.Fatal("unapproved write or binding read executed")
	}
	req.RegisterApproval = func(_ context.Context, approval ApprovalRequestEvent) (ApprovalWaitFunc, error) {
		return func(context.Context) (ApprovalResolvedEvent, error) {
			return ApprovalResolvedEvent{ApprovalID: approval.ApprovalID, Decision: "approved"}, nil
		}, nil
	}
	content, failed, _ := loop.executeToolWithApproval(context.Background(), events, tc, req)
	if failed || written != 1 || bound != 1 || strings.Contains(content, "12345") || !strings.Contains(content, "new-preview") {
		t.Fatalf("resolution/write/result boundary failed: %s %v %d %d", content, failed, bound, written)
	}
	servers[0].Enabled = false
	if _, failed, _ := loop.executeTool(context.Background(), tc, req); !failed || written != 1 {
		t.Fatal("disabled bridge did not prevent write")
	}
	tc.Function.Name = artifactBridgeResolveTool
	if _, failed, _ := loop.executeTool(context.Background(), tc, req); !failed || bound != 1 {
		t.Fatal("model called the host-only resolver")
	}
}

func TestBindingResolutionErrorRelaysOnlyRecoverableContractErrors(t *testing.T) {
	err := bindingResolutionError(`{"ok":false,"error":"plotly binding must have exactly the keys placeholder, $execution_ref, output_index, plugin_id; got [execution_ref output_index placeholder plugin_id]","recoverable":true,"instruction":"Revise only the dashboard binding placeholders."}`)
	if !strings.Contains(err.Error(), "no dashboard was written: plotly binding must have exactly the keys placeholder, $execution_ref") || !strings.Contains(err.Error(), "Revise only") {
		t.Fatalf("contract error was not relayed: %v", err)
	}
	for _, text := range []string{
		`{"ok":false,"error":"unauthorized artifact access: secret detail","recoverable":false}`,
		`not json`,
		`{"ok":false,"recoverable":true}`,
	} {
		if got := bindingResolutionError(text).Error(); got != "artifact binding resolution failed; no dashboard was written" {
			t.Fatalf("non-recoverable detail leaked for %q: %s", text, got)
		}
	}
	long := strings.Repeat("錯", 400)
	if got := bindingResolutionError(`{"error":"` + long + `","recoverable":true}`).Error(); !strings.HasSuffix(got, "…") || !utf8.ValidString(got) {
		t.Fatalf("long detail was not truncated safely: %d bytes", len(got))
	}
}
