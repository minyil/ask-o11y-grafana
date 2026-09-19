package mcp

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/grafana/grafana-plugin-sdk-go/backend/log"
)

// TestClient_ToolCallTimeout covers the per-server tool-call budget: unset
// falls back to the historical 30s default, a configured value overrides it.
// Prod traces (2026-09-03..07) showed 47 calls pinned at the 30s ceiling
// (1,423s, 34% of tool time) because heavy Prometheus/Loki scans could not
// finish inside the old hard-coded budget.
func TestClient_ToolCallTimeout(t *testing.T) {
	c := &Client{config: ServerConfig{ID: "test"}, logger: log.DefaultLogger, ctx: context.Background()}
	if got := c.toolCallTimeout(); got != defaultToolCallTimeout {
		t.Fatalf("unset TimeoutSeconds should fall back to default; got %v", got)
	}

	c.config.TimeoutSeconds = 90
	if got := c.toolCallTimeout(); got != 90*time.Second {
		t.Fatalf("TimeoutSeconds=90 should yield 90s; got %v", got)
	}

	// Negative values are treated as unset (zero-value contract in
	// ServerConfig docs) rather than an immediately-expired context.
	c.config.TimeoutSeconds = -5
	if got := c.toolCallTimeout(); got != defaultToolCallTimeout {
		t.Fatalf("negative TimeoutSeconds should fall back to default; got %v", got)
	}
}

// TestClient_TransportHTTPClientTimeout guards against a regression of the
// Bugbot finding: the shared SDK http.Client carries its own 30s Timeout that
// bounds the whole exchange, so for streamable-http servers the transport
// client must be clamped to the tool-call budget — a provisioned 90s budget
// never took effect when the CallTool context was extended but the client
// deadline stayed at 30s. SSE keeps its zero-timeout semantics (stream must
// not be severed), and other transports are passed through untouched.
func TestClient_TransportHTTPClientTimeout(t *testing.T) {
	newClient := func(serverType string, timeoutSeconds int) *Client {
		return &Client{
			config: ServerConfig{ID: "test", Type: serverType, TimeoutSeconds: timeoutSeconds},
			logger: log.DefaultLogger,
			ctx:    context.Background(),
		}
	}
	shared := &http.Client{Timeout: 30 * time.Second}

	// streamable-http: clamped up to the configured budget.
	c := newClient("streamable-http", 90)
	got := c.transportHTTPClient(shared)
	if got.Timeout != 90*time.Second {
		t.Fatalf("streamable-http client timeout should be clamped to the 90s budget; got %v", got.Timeout)
	}
	if got == shared {
		t.Fatal("streamable-http should not mutate the shared client")
	}

	// streamable-http with unset budget: clamped to the 30s default (same
	// value, but as a copy so future budget changes cannot leak in).
	c = newClient("streamable-http", 0)
	if got := c.transportHTTPClient(shared); got.Timeout != defaultToolCallTimeout {
		t.Fatalf("streamable-http default should be %v; got %v", defaultToolCallTimeout, got.Timeout)
	}

	// sse: zero timeout so long-lived streams are never severed.
	c = newClient("sse", 90)
	if got := c.transportHTTPClient(shared); got.Timeout != 0 {
		t.Fatalf("sse client timeout should be 0; got %v", got.Timeout)
	}

	// unknown transport: pass-through.
	c = newClient("openapi", 90)
	if got := c.transportHTTPClient(shared); got != shared {
		t.Fatal("non-MCP transports should get the shared client unchanged")
	}
}

// A synchronous MCP tool sends no response headers until it finishes. The
// SDK copies its Timeout into the transport's ResponseHeaderTimeout, so the
// SDK client must be built from the server's tool-call budget; otherwise a
// configured timeoutSeconds above the default is silently ineffective.
func TestSDKClient_ResponseHeaderTimeoutFollowsServerBudget(t *testing.T) {
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
		w.WriteHeader(http.StatusOK)
	}))
	defer slow.Close()
	p := NewProxy(context.Background(), log.DefaultLogger)
	defer p.Close()

	short, err := p.sdkClient(ServerConfig{ID: "s", TimeoutSeconds: 1})
	if err != nil {
		t.Fatal(err)
	}
	if resp, err := short.Get(slow.URL); err == nil {
		resp.Body.Close()
		t.Fatal("a 1s budget must time out awaiting headers from a 2s tool")
	}

	long, err := p.sdkClient(ServerConfig{ID: "l", TimeoutSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := long.Get(slow.URL)
	if err != nil {
		t.Fatalf("a 10s budget must wait for the slow tool's headers: %v", err)
	}
	resp.Body.Close()
}
