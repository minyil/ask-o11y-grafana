import { config } from '@grafana/runtime';
import { pluginUrl } from '../utils/subpath';

const UPLOAD_URL = pluginUrl('/api/uploads');
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

function orgHeaders(): Record<string, string> {
  return { 'X-Grafana-Org-Id': String(config.bootData.user.orgId || '1') };
}

export interface UploadedDataset {
  dataset_id: string;
  filename: string;
  sheet?: string;
  rows: number;
  columns: number;
  fields: Array<{ name: string; type: string }>;
  expires_at: number;
}

export interface UploadRequest {
  promise: Promise<UploadedDataset>;
  cancel: () => void;
}

export function uploadDataset(file: File, sessionId: string, sheet?: string, onProgress?: (percent: number) => void): UploadRequest {
  if (file.size < 1 || file.size > MAX_UPLOAD_BYTES) {
    return { promise: Promise.reject(new Error('File must be between 1 byte and 50 MB')), cancel: () => undefined };
  }
  const form = new FormData();
  form.append('session_id', sessionId);
  if (sheet) {
    form.append('sheet', sheet);
  }
  form.append('file', file, file.name);
  const request = new XMLHttpRequest();
  const promise = new Promise<UploadedDataset>((resolve, reject) => {
    request.open('POST', UPLOAD_URL);
    for (const [key, value] of Object.entries(orgHeaders())) {
      request.setRequestHeader(key, value);
    }
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(Math.round((event.loaded / event.total) * 100));
      }
    };
    request.onerror = () => reject(new Error('Upload request failed'));
    request.onabort = () => reject(new Error('Upload cancelled'));
    request.onload = () => {
      let payload: Partial<UploadedDataset> & { error?: string } = {};
      try {
        payload = JSON.parse(request.responseText);
      } catch {
        // The status-based fallback below is enough.
      }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(String(payload.error || `Upload failed (${request.status})`)));
        return;
      }
      resolve(payload as UploadedDataset);
    };
    request.send(form);
  });
  return { promise, cancel: () => request.abort() };
}

export async function removeUploadedDataset(datasetId: string, sessionId: string): Promise<void> {
  const query = new URLSearchParams({ dataset_id: datasetId, session_id: sessionId });
  const response = await fetch(`${UPLOAD_URL}?${query}`, { method: 'DELETE', headers: orgHeaders() });
  if (!response.ok) {
    throw new Error(`Remove failed (${response.status})`);
  }
}
