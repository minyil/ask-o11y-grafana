package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"consensys-asko11y-app/pkg/mcp"
)

func TestMinimalRuntimeKeepsWriteApprovalAndArgumentBoundary(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/mcp/list-tools" {
			_, _ = w.Write([]byte(`{"tools":[{"name":"mutate","inputSchema":{"type":"object"}}]}`))
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
		if request.Params.Arguments["_server_session_id"] != "session-owned-by-host" {
			t.Error("model-controlled session was not replaced")
		}
		calls++
		_, _ = w.Write([]byte(`{"content":[{"type":"text","text":"saved"}]}`))
	}))
	defer server.Close()
	loop, _, cleanup := setupTestLoop(t, nil)
	defer cleanup()
	defer loop.mcpProxy.Close()
	servers := []mcp.ServerConfig{{ID: "writer", Type: "standard", URL: server.URL, Enabled: true}}
	loop.mcpProxy.UpdateConfig(servers)
	if _, err := loop.mcpProxy.ListTools(); err != nil {
		t.Fatal(err)
	}
	events := make(chan SSEEvent, 8)
	tc := ToolCall{ID: "same-model-id", Function: FunctionCall{Name: "writer_mutate", Arguments: `{"_server_session_id":"spoofed"}`}}
	req := LoopRequest{UserRole: "Admin", SessionID: "session-owned-by-host", MCPServers: servers, ApprovalPolicy: "approval-gated-writes"}
	if _, failed, kind := loop.executeToolWithApproval(context.Background(), events, tc, req); !failed || kind != "approval_required" || calls != 0 {
		t.Fatalf("unapproved write escaped: %v %s %d", failed, kind, calls)
	}
	var previousID string
	for _, approveMatchingID := range []bool{false, true} {
		req.RegisterApproval = func(_ context.Context, approval ApprovalRequestEvent) (ApprovalWaitFunc, error) {
			if approval.ApprovalID == tc.ID || approval.ApprovalID == previousID {
				t.Fatal("approval identity reused model-controlled id")
			}
			previousID = approval.ApprovalID
			return func(context.Context) (ApprovalResolvedEvent, error) {
				id := tc.ID
				if approveMatchingID {
					id = approval.ApprovalID
				}
				return ApprovalResolvedEvent{ApprovalID: id, Decision: "approved"}, nil
			}, nil
		}
		_, failed, _ := loop.executeToolWithApproval(context.Background(), events, tc, req)
		if failed == approveMatchingID {
			t.Fatalf("approval binding failed for matching=%v", approveMatchingID)
		}
	}
	if calls != 1 {
		t.Fatalf("expected exactly one authorized write, got %d", calls)
	}
	for _, arguments := range []string{"null", "[]", `"string"`} {
		tc.Function.Arguments = arguments
		if _, failed, _ := loop.executeTool(context.Background(), tc, req); !failed || calls != 1 {
			t.Fatalf("invalid arguments reached tool: %s", arguments)
		}
	}
}

func TestExecuteToolTreatsEmptyArgumentsAsNoParameterCall(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/mcp/list-tools" {
			_, _ = w.Write([]byte(`{"tools":[{"name":"list_things","inputSchema":{"type":"object"}}]}`))
			return
		}
		calls++
		_, _ = w.Write([]byte(`{"content":[{"type":"text","text":"listed"}]}`))
	}))
	defer server.Close()
	loop, _, cleanup := setupTestLoop(t, nil)
	defer cleanup()
	defer loop.mcpProxy.Close()
	servers := []mcp.ServerConfig{{ID: "reader", Type: "standard", URL: server.URL, Enabled: true}}
	loop.mcpProxy.UpdateConfig(servers)
	if _, err := loop.mcpProxy.ListTools(); err != nil {
		t.Fatal(err)
	}
	req := LoopRequest{UserRole: "Admin", SessionID: "s", MCPServers: servers}
	for _, arguments := range []string{"", "  "} {
		tc := ToolCall{ID: "tc", Function: FunctionCall{Name: "reader_list_things", Arguments: arguments}}
		if content, failed, _ := loop.executeTool(context.Background(), tc, req); failed {
			t.Fatalf("empty arguments %q rejected: %s", arguments, content)
		}
	}
	if calls != 2 {
		t.Fatalf("expected 2 tool calls, got %d", calls)
	}
}
