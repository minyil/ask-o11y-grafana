package mcp

import (
	"context"
	"errors"
	"fmt"
	"net/http"

	"go.opentelemetry.io/otel/trace"
)

// PerUserTokenProvider supplies the Authorization bearer value for a given
// (request-context, serverID) pair. Implementations read the user identity
// from the context via UserIDFromContext.
type PerUserTokenProvider interface {
	BearerFor(ctx context.Context, serverID string) (string, error)
}

// ErrPerUserTokenUnavailable is returned by BearerFor when the user has not
// connected the server yet. The round tripper turns it into a user-visible
// "please connect" error instead of a silent 401.
var ErrPerUserTokenUnavailable = errors.New("per-user bearer token unavailable")

type userIDCtxKey struct{}

// WithUserID returns a context carrying the Grafana user ID for downstream
// per-user token injection. A zero ID is treated as absent.
func WithUserID(ctx context.Context, userID int64) context.Context {
	if userID == 0 {
		return ctx
	}
	return context.WithValue(ctx, userIDCtxKey{}, userID)
}

// UserIDFromContext returns the Grafana user ID previously stored via
// WithUserID. The returned bool is true only when a non-zero ID is present.
func UserIDFromContext(ctx context.Context) (int64, bool) {
	v, ok := ctx.Value(userIDCtxKey{}).(int64)
	return v, ok && v != 0
}

type sessionIDCtxKey struct{}

// WithSessionID returns a context carrying the chat session ID so MCP servers
// that scope artifacts per session receive it as a host-owned header.
func WithSessionID(ctx context.Context, sessionID string) context.Context {
	if sessionID == "" {
		return ctx
	}
	return context.WithValue(ctx, sessionIDCtxKey{}, sessionID)
}

// SessionIDFromContext returns the chat session ID stored via WithSessionID.
func SessionIDFromContext(ctx context.Context) (string, bool) {
	v, ok := ctx.Value(sessionIDCtxKey{}).(string)
	return v, ok && v != ""
}

// mergeUserCtx returns a context rooted at base (for cancellation lifetime)
// that additionally carries the user and session IDs from caller. Rooting at
// base keeps long-lived client state alive across short-lived caller contexts.
func mergeUserCtx(base, caller context.Context) context.Context {
	ctx := base
	if userID, ok := UserIDFromContext(caller); ok {
		ctx = WithUserID(ctx, userID)
	}
	if sessionID, ok := SessionIDFromContext(caller); ok {
		ctx = WithSessionID(ctx, sessionID)
	}
	return ctx
}

// withCallerSpan re-attaches the caller's active span onto a context rebuilt
// by mergeUserCtx, which copies only known values and would otherwise drop
// it. Trace propagation then parents the MCP server's span onto the
// caller's trace instead of rooting a disjoint one.
func withCallerSpan(ctx, caller context.Context) context.Context {
	if sc := trace.SpanContextFromContext(caller); sc.IsValid() {
		return WithCallerSpanContext(ctx, sc)
	}
	return ctx
}

// userTokenRoundTripper wraps an http.RoundTripper for servers with an OAuth
// block. On every request it resolves the current user's bearer token and
// injects it as the Authorization header, overriding any static value.
//
// Requests without a user in context (health probes, connection handshakes
// initiated by the plugin itself) pass through without an Authorization
// header so system-level plumbing keeps working; the MCP server decides
// whether to accept them.
type userTokenRoundTripper struct {
	base     http.RoundTripper
	serverID string
	provider PerUserTokenProvider
}

func (rt *userTokenRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	if _, ok := UserIDFromContext(req.Context()); !ok {
		return rt.base.RoundTrip(req)
	}
	token, err := rt.provider.BearerFor(req.Context(), rt.serverID)
	if errors.Is(err, ErrPerUserTokenUnavailable) {
		return nil, fmt.Errorf("MCP server %q requires you to connect your account first (open MCP connections and click Connect): %w", rt.serverID, err)
	}
	if err != nil {
		return nil, fmt.Errorf("resolve per-user token for MCP server %q: %w", rt.serverID, err)
	}
	req = req.Clone(req.Context())
	req.Header.Set("Authorization", "Bearer "+token)
	return rt.base.RoundTrip(req)
}
