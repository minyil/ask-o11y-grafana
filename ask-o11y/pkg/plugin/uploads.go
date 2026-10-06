package plugin

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/grafana/grafana-plugin-sdk-go/backend/httpclient"
)

const (
	maxUploadBytes        int64 = 50 * 1024 * 1024
	maxUploadRequestBytes       = maxUploadBytes + 1024*1024
)

func (p *Plugin) grafanaQueryServer() (string, map[string]string, bool) {
	for _, server := range p.settings.MCPServers {
		if server.ID != "grafana-query" || !server.Enabled {
			continue
		}
		parsed, err := url.Parse(server.URL)
		if err != nil || parsed.Scheme != "http" || (parsed.Hostname() != "127.0.0.1" && parsed.Hostname() != "localhost") {
			return "", nil, false
		}
		return strings.TrimSuffix(server.URL, "/mcp"), server.Headers, true
	}
	return "", nil, false
}

func (p *Plugin) handleUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodDelete {
		p.handleDeleteUpload(w, r)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if r.ContentLength < 1 || r.ContentLength > maxUploadRequestBytes {
		http.Error(w, "File must be between 1 byte and 50 MB", http.StatusRequestEntityTooLarge)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUploadRequestBytes)
	if err := r.ParseMultipartForm(1024 * 1024); err != nil {
		http.Error(w, "Upload must be valid multipart form data", http.StatusBadRequest)
		return
	}
	if r.MultipartForm != nil {
		defer r.MultipartForm.RemoveAll()
	}
	file, fileHeader, err := r.FormFile("file")
	if err != nil {
		http.Error(w, "One upload file is required", http.StatusBadRequest)
		return
	}
	defer file.Close()
	filename := fileHeader.Filename
	sessionID := r.FormValue("session_id")
	if filename == "" || fileHeader.Size < 1 || fileHeader.Size > maxUploadBytes || sessionID == "" || !isValidSecureID(sessionID) {
		http.Error(w, "Upload filename and valid session id are required", http.StatusBadRequest)
		return
	}
	userID, orgID := getUserID(r), getOrgID(r)
	session, err := p.sessionStore.GetSession(sessionID, userID, orgID)
	if err != nil {
		http.Error(w, "Session not found", http.StatusNotFound)
		return
	}
	if len(session.UploadDatasetIDs) >= maxUploadsPerSession {
		writeUploadLimitError(w)
		return
	}
	baseURL, headers, ok := p.grafanaQueryServer()
	if !ok {
		http.Error(w, "Grafana Query upload service is unavailable", http.StatusServiceUnavailable)
		return
	}
	upstream, err := http.NewRequestWithContext(r.Context(), http.MethodPut, baseURL+"/uploads", io.LimitReader(file, maxUploadBytes+1))
	if err != nil {
		http.Error(w, "Could not create upload request", http.StatusInternalServerError)
		return
	}
	upstream.ContentLength = fileHeader.Size
	for key, value := range headers {
		upstream.Header.Set(key, value)
	}
	upstream.Header.Set("Content-Type", "application/octet-stream")
	upstream.Header.Set("Content-Length", strconv.FormatInt(fileHeader.Size, 10))
	upstream.Header.Set("X-Grafana-Actor-User-Id", strconv.FormatInt(userID, 10))
	upstream.Header.Set("X-Grafana-Org-Id", strconv.FormatInt(orgID, 10))
	upstream.Header.Set("X-Upload-Filename", url.QueryEscape(filename))
	upstream.Header.Set("X-Upload-Session-Id", sessionID)
	if sheet := r.FormValue("sheet"); sheet != "" {
		upstream.Header.Set("X-Upload-Sheet", url.QueryEscape(sheet))
	}
	client, err := httpclient.New(httpclient.Options{Timeouts: &httpclient.TimeoutOptions{Timeout: 5 * time.Minute}})
	if err != nil {
		http.Error(w, "Upload client unavailable", http.StatusServiceUnavailable)
		return
	}
	response, err := client.Do(upstream)
	if err != nil {
		p.logger.Error("Upload proxy failed", "error", err)
		http.Error(w, "Upload service request failed", http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024))
	if err != nil {
		http.Error(w, "Upload service response failed", http.StatusBadGateway)
		return
	}
	if response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices {
		var uploaded struct {
			DatasetID string `json:"dataset_id"`
		}
		if err := json.Unmarshal(body, &uploaded); err != nil || !isValidUploadDatasetID(uploaded.DatasetID) {
			http.Error(w, "Upload service returned an invalid dataset", http.StatusBadGateway)
			return
		}
		if err := p.sessionStore.AddUploadDatasetID(sessionID, userID, orgID, uploaded.DatasetID); err != nil {
			// A concurrent upload can fill the session after the pre-check; the
			// stored file is then unattached and expires with the upload TTL.
			if errors.Is(err, ErrUploadLimit) {
				writeUploadLimitError(w)
				return
			}
			p.logger.Error("Failed to attach uploaded dataset to session", "error", err)
			http.Error(w, "Could not attach uploaded dataset to session", http.StatusInternalServerError)
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(response.StatusCode)
	if len(body) == 0 {
		_ = json.NewEncoder(w).Encode(map[string]string{"error": fmt.Sprintf("upload service returned %d", response.StatusCode)})
		return
	}
	_, _ = w.Write(body)
}

func writeUploadLimitError(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusConflict)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": ErrUploadLimit.Error()})
}

func isValidUploadDatasetID(value string) bool {
	if len(value) != len("upload_")+32 || !strings.HasPrefix(value, "upload_") {
		return false
	}
	for _, char := range value[len("upload_"):] {
		if !(char >= '0' && char <= '9') && !(char >= 'a' && char <= 'f') {
			return false
		}
	}
	return true
}

func (p *Plugin) handleDeleteUpload(w http.ResponseWriter, r *http.Request) {
	datasetID := r.URL.Query().Get("dataset_id")
	sessionID := r.URL.Query().Get("session_id")
	if !isValidUploadDatasetID(datasetID) || !isValidSecureID(sessionID) {
		http.Error(w, "Valid dataset and session ids are required", http.StatusBadRequest)
		return
	}
	userID, orgID := getUserID(r), getOrgID(r)
	if _, err := p.sessionStore.GetSession(sessionID, userID, orgID); err != nil {
		http.Error(w, "Session not found", http.StatusNotFound)
		return
	}
	baseURL, headers, ok := p.grafanaQueryServer()
	if !ok {
		http.Error(w, "Grafana Query upload service is unavailable", http.StatusServiceUnavailable)
		return
	}
	upstream, err := http.NewRequestWithContext(r.Context(), http.MethodDelete, baseURL+"/uploads/"+url.PathEscape(datasetID), nil)
	if err != nil {
		http.Error(w, "Could not create remove request", http.StatusInternalServerError)
		return
	}
	for key, value := range headers {
		upstream.Header.Set(key, value)
	}
	upstream.Header.Set("X-Grafana-Actor-User-Id", strconv.FormatInt(userID, 10))
	upstream.Header.Set("X-Grafana-Org-Id", strconv.FormatInt(orgID, 10))
	upstream.Header.Set("X-Upload-Session-Id", sessionID)
	client, err := httpclient.New(httpclient.Options{Timeouts: &httpclient.TimeoutOptions{Timeout: 30 * time.Second}})
	if err != nil {
		http.Error(w, "Upload client unavailable", http.StatusServiceUnavailable)
		return
	}
	response, err := client.Do(upstream)
	if err != nil {
		http.Error(w, "Upload remove request failed", http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024))
	if err != nil {
		http.Error(w, "Upload remove response unavailable; outcome is unknown", http.StatusBadGateway)
		return
	}
	if response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices {
		if session, err := p.sessionStore.GetSession(sessionID, userID, orgID); err == nil && slices.Contains(session.UploadDatasetIDs, datasetID) {
			if err := p.sessionStore.RemoveUploadDatasetID(sessionID, userID, orgID, datasetID); err != nil {
				p.logger.Error("Failed to clear uploaded dataset session attachment", "error", err)
				http.Error(w, "Could not clear uploaded dataset session attachment", http.StatusInternalServerError)
				return
			}
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(body)
}
