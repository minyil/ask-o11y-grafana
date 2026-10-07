package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"consensys-asko11y-app/pkg/mcp"
)

const artifactBridgeResolveTool = "artifact-bridge_resolve_dashboard_refs"

// toolContext carries the acting user and chat session into MCP calls so the
// client forwards them as host-owned headers.
func (req LoopRequest) toolContext(ctx context.Context) context.Context {
	return mcp.WithSessionID(mcp.WithUserID(ctx, req.UserID), req.SessionID)
}

// Reuse opaque bindings: the model supplies layout/references, not figure arrays.
func (a *AgentLoop) resolveDashboardBindings(ctx context.Context, args map[string]interface{}, req LoopRequest) error {
	encoded, err := json.Marshal(args)
	if err != nil {
		return fmt.Errorf("invalid dashboard arguments")
	}
	opaque := false
	for _, marker := range []string{`"$execution_ref"`, `"askO11yPlotlyBindings"`, `"askO11yAssetBindings"`} {
		opaque = opaque || strings.Contains(string(encoded), marker)
	}
	if !opaque {
		return nil
	}
	dashboard, ok := args["dashboard"].(map[string]interface{})
	if !ok || args["operations"] != nil {
		return fmt.Errorf("artifact bindings require a complete dashboard; do not embed them in patch operations")
	}
	if !mcp.IsToolEnabled(artifactBridgeResolveTool, req.MCPServers) {
		return fmt.Errorf("artifact bridge is disabled")
	}
	result, err := a.mcpProxy.CallToolWithContext(req.toolContext(ctx), artifactBridgeResolveTool,
		map[string]interface{}{"dashboard": dashboard, "_server_session_id": req.SessionID},
		req.OrgID, req.OrgName, req.ScopeOrgID)
	if err != nil || result == nil {
		return fmt.Errorf("artifact binding resolution failed; no dashboard was written")
	}
	if result.IsError {
		return bindingResolutionError(extractText(result))
	}
	var resolved struct {
		OK        bool                   `json:"ok"`
		Dashboard map[string]interface{} `json:"dashboard"`
	}
	if err := json.Unmarshal([]byte(extractText(result)), &resolved); err != nil || !resolved.OK || resolved.Dashboard == nil {
		return fmt.Errorf("artifact bridge returned an invalid dashboard; no write was dispatched")
	}
	args["dashboard"] = resolved.Dashboard
	return nil
}

const maxBindingErrorDetail = 500

// bindingResolutionError relays the bridge's recoverable contract error so the
// model can correct its bindings; anything else stays generic.
func bindingResolutionError(text string) error {
	var failure struct {
		Error       string `json:"error"`
		Recoverable bool   `json:"recoverable"`
		Instruction string `json:"instruction"`
	}
	if err := json.Unmarshal([]byte(text), &failure); err != nil || !failure.Recoverable || failure.Error == "" {
		return fmt.Errorf("artifact binding resolution failed; no dashboard was written")
	}
	detail := failure.Error
	if failure.Instruction != "" {
		detail += ". " + failure.Instruction
	}
	if len(detail) > maxBindingErrorDetail {
		detail = strings.ToValidUTF8(detail[:maxBindingErrorDetail], "") + "…"
	}
	return fmt.Errorf("artifact binding resolution failed; no dashboard was written: %s", detail)
}
