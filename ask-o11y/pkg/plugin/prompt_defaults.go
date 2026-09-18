package plugin

import _ "embed"

// DefaultSystemPrompt is the always-on analyst persona for this fork. Upstream
// observability workflows live in bundled skills under pkg/skills/bundled/ and
// remain loadable on demand; the analysis guidance below is intentionally
// always present rather than a skill because every conversation is analysis.
//
//go:embed analyst_prompt.md
var DefaultSystemPrompt string

const ToolInstructionsFragment = `{{if .AvailableTools}}
## Available MCP Tools

The following tools are currently enabled and ready to use:

{{range .AvailableTools}}
### {{.Name}}
{{.Description}}
{{if .Instructions}}

**Usage Instructions:**
{{.Instructions}}
{{end}}
{{end}}
{{end}}

{{if .DisabledTools}}
## Disabled MCP Tools

The following tools are disabled and not available:

{{range .DisabledTools}}
* **{{.Name}}**: {{.Description}}
  {{if .DocsURL}}- Setup instructions: {{.DocsURL}}{{end}}
{{end}}

If you need a disabled tool, inform the user and ask them to configure it.
{{end}}

{{if .FailedTools}}
## Failed MCP Tools

The following tools failed to initialize:

{{range .FailedTools}}
* **{{.Name}}**: {{.Description}}
  - Status: FAILED
  {{if .Error}}- Error: {{.Error}}{{end}}
  {{if .DocsURL}}- Setup instructions: {{.DocsURL}}{{end}}
{{end}}

If you need a failed tool, inform the user and include the error message.
{{end}}`
