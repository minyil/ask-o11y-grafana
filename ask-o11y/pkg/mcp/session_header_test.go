package mcp

import (
	"context"
	"net/http"
	"testing"
)

// The actor and session headers are derived from the request context, so an
// MCP server behind the shared service identity can scope work to the real
// Grafana user and chat session. Server-configured headers must not spoof them.
func TestCustomRoundTripperSetsActorAndSessionHeadersFromContext(t *testing.T) {
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		if got := req.Header.Get("X-Grafana-Actor-User-Id"); got != "42" {
			t.Fatalf("actor header = %q", got)
		}
		if got := req.Header.Get("X-Grafana-Session-Id"); got != "session-123" {
			t.Fatalf("session header = %q", got)
		}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: http.NoBody, Request: req}, nil
	})
	transport := &customRoundTripper{base: base, config: ServerConfig{Headers: map[string]string{
		"X-Grafana-Actor-User-Id": "spoofed",
		"X-Grafana-Session-Id":    "spoofed",
	}}}
	ctx := WithSessionID(WithUserID(context.Background(), 42), "session-123")
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://example.invalid/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := transport.RoundTrip(request); err != nil {
		t.Fatal(err)
	}
}

func TestCustomRoundTripperStripsActorHeadersWithoutContext(t *testing.T) {
	base := roundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.Header.Get("X-Grafana-Actor-User-Id") != "" || req.Header.Get("X-Grafana-Session-Id") != "" {
			t.Fatalf("configured headers must not impersonate an actor: %v", req.Header)
		}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: http.NoBody, Request: req}, nil
	})
	transport := &customRoundTripper{base: base, config: ServerConfig{Headers: map[string]string{"X-Grafana-Actor-User-Id": "7"}}}
	request, err := http.NewRequest(http.MethodPost, "http://example.invalid/mcp", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := transport.RoundTrip(request); err != nil {
		t.Fatal(err)
	}
}
