package plugin

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"consensys-asko11y-app/pkg/mcp"
	"github.com/grafana/grafana-plugin-sdk-go/backend/log"
)

func TestHandleUploadProxiesOwnedSession(t *testing.T) {
	const datasetID = "upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	var observedBody string
	var observedDelete bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Grafana-Org-Id") != "1" {
			t.Error("configured organization was not replaced with session owner organization")
		}
		if r.Method == http.MethodDelete {
			observedDelete = r.URL.Path == "/uploads/"+datasetID && r.Header.Get("X-Grafana-Actor-User-Id") == "7" && r.Header.Get("X-Upload-Session-Id") != ""
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		if r.Method != http.MethodPut || r.URL.Path != "/uploads" {
			t.Fatalf("unexpected upload request: %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer secret" || r.Header.Get("X-Grafana-Actor-User-Id") != "7" || r.Header.Get("X-Upload-Session-Id") == "" {
			t.Fatalf("missing trusted upstream headers: %v", r.Header)
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatal(err)
		}
		observedBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"ok":true,"dataset_id":"upload_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`))
	}))
	defer upstream.Close()

	store := NewSessionStore(log.DefaultLogger, time.Hour)
	session, err := store.CreateSession(7, 1, "upload", nil)
	if err != nil {
		t.Fatal(err)
	}
	plugin := &Plugin{
		sessionStore: store,
		settings: PluginSettings{MCPServers: []mcp.ServerConfig{{
			ID: "grafana-query", URL: upstream.URL + "/mcp", Enabled: true,
			Headers: map[string]string{"Authorization": "Bearer secret", "X-Grafana-Org-Id": "999"},
		}}},
	}
	var form bytes.Buffer
	writer := multipart.NewWriter(&form)
	_ = writer.WriteField("session_id", session.ID)
	part, err := writer.CreateFormFile("file", "測試.csv")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = part.Write([]byte("a,b\n1,2\n"))
	_ = writer.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/uploads", &form)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("X-Grafana-User-Id", "7")
	req.Header.Set("X-Grafana-Org-Id", "1")
	response := httptest.NewRecorder()
	plugin.handleUpload(response, req)
	if response.Code != http.StatusCreated || observedBody != "a,b\n1,2\n" {
		t.Fatalf("upload failed: status=%d body=%s observed=%q", response.Code, response.Body.String(), observedBody)
	}
	stored, err := store.GetSession(session.ID, 7, 1)
	if err != nil || !slices.Equal(stored.UploadDatasetIDs, []string{datasetID}) {
		t.Fatalf("uploaded dataset was not attached: session=%+v err=%v", stored, err)
	}
	remove := httptest.NewRequest(http.MethodDelete, "/api/uploads?dataset_id="+datasetID+"&session_id="+session.ID, nil)
	remove.Header.Set("X-Grafana-User-Id", "7")
	remove.Header.Set("X-Grafana-Org-Id", "1")
	removeResponse := httptest.NewRecorder()
	plugin.handleUpload(removeResponse, remove)
	if removeResponse.Code != http.StatusOK || !observedDelete {
		t.Fatalf("remove failed: status=%d observed=%v", removeResponse.Code, observedDelete)
	}
	stored, err = store.GetSession(session.ID, 7, 1)
	if err != nil || len(stored.UploadDatasetIDs) != 0 {
		t.Fatalf("uploaded dataset attachment was not cleared: session=%+v err=%v", stored, err)
	}
}

func TestHandleUploadRejectsUnownedAndOversized(t *testing.T) {
	store := NewSessionStore(log.DefaultLogger, time.Hour)
	session, err := store.CreateSession(8, 1, "foreign", nil)
	if err != nil {
		t.Fatal(err)
	}
	plugin := &Plugin{sessionStore: store}
	var form bytes.Buffer
	writer := multipart.NewWriter(&form)
	_ = writer.WriteField("session_id", session.ID)
	part, err := writer.CreateFormFile("file", "x.csv")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = part.Write([]byte("x"))
	_ = writer.Close()
	foreign := httptest.NewRequest(http.MethodPost, "/api/uploads", &form)
	foreign.Header.Set("Content-Type", writer.FormDataContentType())
	foreign.Header.Set("X-Grafana-User-Id", "7")
	foreign.Header.Set("X-Grafana-Org-Id", "1")
	foreignResponse := httptest.NewRecorder()
	plugin.handleUpload(foreignResponse, foreign)
	if foreignResponse.Code != http.StatusNotFound {
		t.Fatalf("foreign upload status=%d", foreignResponse.Code)
	}

	oversized := httptest.NewRequest(http.MethodPost, "/api/uploads", strings.NewReader("x"))
	oversized.ContentLength = maxUploadRequestBytes + 1
	oversizedResponse := httptest.NewRecorder()
	plugin.handleUpload(oversizedResponse, oversized)
	if oversizedResponse.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized upload status=%d", oversizedResponse.Code)
	}
}

func TestSessionStoreHoldsSeveralUploadsUpToLimit(t *testing.T) {
	store := NewSessionStore(log.DefaultLogger, time.Hour)
	session, err := store.CreateSession(7, 1, "upload", nil)
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for i := 0; i < maxUploadsPerSession; i++ {
		id := fmt.Sprintf("upload_%032x", i)
		want = append(want, id)
		if err := store.AddUploadDatasetID(session.ID, 7, 1, id); err != nil {
			t.Fatalf("attach %d: %v", i, err)
		}
	}
	if err := store.AddUploadDatasetID(session.ID, 7, 1, want[0]); err != nil {
		t.Fatalf("re-attaching an existing upload should be a no-op: %v", err)
	}
	if err := store.AddUploadDatasetID(session.ID, 7, 1, "upload_ffffffffffffffffffffffffffffffff"); !errors.Is(err, ErrUploadLimit) {
		t.Fatalf("expected upload limit error, got %v", err)
	}
	if err := store.AddUploadDatasetID(session.ID, 8, 1, "upload_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"); err == nil {
		t.Fatal("foreign user attached an upload")
	}
	if err := store.RemoveUploadDatasetID(session.ID, 7, 1, want[1]); err != nil {
		t.Fatal(err)
	}
	stored, _ := store.GetSession(session.ID, 7, 1)
	if !slices.Equal(stored.UploadDatasetIDs, append([]string{want[0]}, want[2:]...)) {
		t.Fatalf("unexpected uploads after removal: %v", stored.UploadDatasetIDs)
	}
}

func TestHandleUploadRejectsFullSessionBeforeProxying(t *testing.T) {
	proxied := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		proxied = true
	}))
	defer upstream.Close()
	store := NewSessionStore(log.DefaultLogger, time.Hour)
	session, err := store.CreateSession(7, 1, "upload", nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < maxUploadsPerSession; i++ {
		if err := store.AddUploadDatasetID(session.ID, 7, 1, fmt.Sprintf("upload_%032x", i)); err != nil {
			t.Fatal(err)
		}
	}
	plugin := &Plugin{
		sessionStore: store,
		settings:     PluginSettings{MCPServers: []mcp.ServerConfig{{ID: "grafana-query", URL: upstream.URL + "/mcp", Enabled: true}}},
	}
	var form bytes.Buffer
	writer := multipart.NewWriter(&form)
	_ = writer.WriteField("session_id", session.ID)
	part, err := writer.CreateFormFile("file", "extra.csv")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = part.Write([]byte("a\n1\n"))
	_ = writer.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/uploads", &form)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("X-Grafana-User-Id", "7")
	req.Header.Set("X-Grafana-Org-Id", "1")
	response := httptest.NewRecorder()
	plugin.handleUpload(response, req)
	if response.Code != http.StatusConflict || proxied || !strings.Contains(response.Body.String(), "at most") {
		t.Fatalf("full session upload: status=%d proxied=%v body=%s", response.Code, proxied, response.Body.String())
	}
}
