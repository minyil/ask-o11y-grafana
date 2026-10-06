import React, { useRef, useState } from 'react';
import { Button } from '@grafana/ui';

import { createSession, deleteSession } from '../../../../services/backendSessionClient';
import { testIds } from '../../../testIds';
import {
  MAX_UPLOADS_PER_SESSION,
  uploadDataset,
  type UploadRequest,
  type UploadedDataset,
} from '../../../../services/uploadClient';

interface UploadButtonProps {
  disabled?: boolean;
  /** Session that already holds uploads; new files join it instead of a new session. */
  targetSessionId?: string;
  /** How many more files the target (or a new) session can take. */
  remaining: number;
  onUploaded: (sessionId: string, uploaded: UploadedDataset[]) => void;
}

export function UploadButton({ disabled, targetSessionId, remaining, onUploaded }: UploadButtonProps): React.ReactElement {
  const inputRef = useRef<HTMLInputElement>(null);
  const requestRef = useRef<UploadRequest | undefined>(undefined);
  const cancelledRef = useRef(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [fileLabel, setFileLabel] = useState('');

  async function uploadOne(file: File, sessionId: string): Promise<UploadedDataset> {
    try {
      requestRef.current = uploadDataset(file, sessionId, undefined, setProgress);
      return await requestRef.current.promise;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Upload failed';
      if (!message.startsWith('SHEET_SELECTION_REQUIRED:')) {
        throw error;
      }
      const sheets = JSON.parse(message.slice('SHEET_SELECTION_REQUIRED:'.length)) as string[];
      const selected = window.prompt(`${file.name}: choose a sheet:\n${sheets.join('\n')}`, sheets[0]);
      if (!selected || !sheets.includes(selected)) {
        throw new Error(`${file.name}: upload cancelled, no valid sheet selected`);
      }
      requestRef.current = uploadDataset(file, sessionId, selected, setProgress);
      return await requestRef.current.promise;
    }
  }

  async function submit(files: File[]): Promise<void> {
    if (files.length > remaining) {
      window.alert(`每個對話最多 ${MAX_UPLOADS_PER_SESSION} 個檔案，這次最多只能再選 ${remaining} 個`);
      return;
    }
    setUploading(true);
    cancelledRef.current = false;
    let createdSessionId: string | undefined;
    const uploaded: UploadedDataset[] = [];
    const failures: string[] = [];
    try {
      let sessionId = targetSessionId;
      if (!sessionId) {
        const title = files.length === 1 ? `Upload: ${files[0].name}` : `Upload: ${files[0].name} +${files.length - 1}`;
        sessionId = (await createSession(title, [])).id;
        createdSessionId = sessionId;
      }
      for (const [index, file] of files.entries()) {
        if (cancelledRef.current) {
          break;
        }
        setFileLabel(files.length > 1 ? `${index + 1}/${files.length}` : '');
        setProgress(0);
        try {
          uploaded.push(await uploadOne(file, sessionId));
        } catch (error) {
          failures.push(error instanceof Error ? `${file.name}: ${error.message}` : `${file.name}: Upload failed`);
        }
      }
      if (uploaded.length === 0) {
        throw new Error(failures.join('\n') || 'Upload cancelled');
      }
      createdSessionId = undefined;
      onUploaded(sessionId, uploaded);
      if (failures.length > 0) {
        window.alert(failures.join('\n'));
      }
    } catch (error) {
      if (createdSessionId) {
        await deleteSession(createdSessionId).catch(() => undefined);
      }
      window.alert(error instanceof Error ? error.message : 'Upload failed');
    } finally {
      requestRef.current = undefined;
      setUploading(false);
      setProgress(0);
      setFileLabel('');
      if (inputRef.current) {
        inputRef.current.value = '';
      }
    }
  }

  const full = remaining < 1;
  return (
    <>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        icon={uploading ? 'times' : 'upload'}
        disabled={disabled || (full && !uploading)}
        onClick={() => {
          if (uploading) {
            cancelledRef.current = true;
            requestRef.current?.cancel();
          } else {
            inputRef.current?.click();
          }
        }}
        aria-label={uploading ? `Cancel upload (${progress}%)` : 'Upload CSV or Excel'}
        title={
          uploading
            ? `取消上傳 (${progress}%)`
            : full
            ? '這個對話的檔案數已達上限'
            : targetSessionId
            ? `加入更多 CSV / Excel 到這個對話（還可加 ${remaining} 個）`
            : '上傳 CSV / Excel（可一次選多個）'
        }
      />
      {uploading && (
        <span className="self-center text-xs text-secondary">
          {fileLabel && `${fileLabel} · `}
          {progress}%
        </span>
      )}
      <input
        ref={inputRef}
        type="file"
        multiple
        accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        hidden
        data-testid={testIds.chat.uploadFileInput}
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          if (files.length > 0) {
            void submit(files);
          }
        }}
      />
    </>
  );
}
