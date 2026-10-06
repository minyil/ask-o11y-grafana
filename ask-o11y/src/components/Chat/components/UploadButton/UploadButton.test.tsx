import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { UploadButton } from './UploadButton';
import { testIds } from '../../../testIds';
import { createSession, deleteSession } from '../../../../services/backendSessionClient';
import { uploadDataset, uploadedDatasetsMessage, type UploadedDataset } from '../../../../services/uploadClient';

jest.mock('@grafana/ui', () => ({
  Button: ({ icon, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { icon?: string }) => (
    <button {...props} />
  ),
}));

jest.mock('@grafana/runtime', () => ({
  config: { appSubUrl: '', bootData: { user: { orgId: 1 } } },
}));

jest.mock('../../../../services/backendSessionClient', () => ({
  createSession: jest.fn(),
  deleteSession: jest.fn(),
}));

jest.mock('../../../../services/uploadClient', () => ({
  ...jest.requireActual('../../../../services/uploadClient'),
  uploadDataset: jest.fn(),
}));

const mockCreateSession = createSession as jest.MockedFunction<typeof createSession>;
const mockDeleteSession = deleteSession as jest.MockedFunction<typeof deleteSession>;
const mockUploadDataset = uploadDataset as jest.MockedFunction<typeof uploadDataset>;

function dataset(id: string, filename: string): UploadedDataset {
  return { dataset_id: id, filename, rows: 3, columns: 2, fields: [], expires_at: 0 };
}

function selectFiles(...names: string[]): void {
  const files = names.map((name) => new File(['a,b\n1,2\n'], name, { type: 'text/csv' }));
  fireEvent.change(screen.getByTestId(testIds.chat.uploadFileInput), { target: { files } });
}

describe('UploadButton', () => {
  let alertSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    alertSpy = jest.spyOn(window, 'alert').mockImplementation(() => undefined);
    mockCreateSession.mockResolvedValue({ id: 'new-session' } as Awaited<ReturnType<typeof createSession>>);
    mockDeleteSession.mockResolvedValue(undefined as never);
  });

  afterEach(() => alertSpy.mockRestore());

  it('uploads several selected files into one new session', async () => {
    mockUploadDataset.mockImplementation((file) => ({
      promise: Promise.resolve(dataset(`upload_${file.name}`, file.name)),
      cancel: jest.fn(),
    }));
    const onUploaded = jest.fn();
    render(<UploadButton remaining={5} onUploaded={onUploaded} />);

    selectFiles('a.csv', 'b.csv');

    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
    expect(mockCreateSession).toHaveBeenCalledTimes(1);
    expect(mockCreateSession).toHaveBeenCalledWith('Upload: a.csv +1', []);
    expect(mockUploadDataset.mock.calls.map((call) => call[1])).toEqual(['new-session', 'new-session']);
    expect(onUploaded).toHaveBeenCalledWith('new-session', [
      dataset('upload_a.csv', 'a.csv'),
      dataset('upload_b.csv', 'b.csv'),
    ]);
  });

  it('adds files to the target session without creating a new one', async () => {
    mockUploadDataset.mockImplementation((file) => ({
      promise: Promise.resolve(dataset(`upload_${file.name}`, file.name)),
      cancel: jest.fn(),
    }));
    const onUploaded = jest.fn();
    render(<UploadButton targetSessionId="existing" remaining={4} onUploaded={onUploaded} />);

    selectFiles('c.csv');

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith('existing', [dataset('upload_c.csv', 'c.csv')]));
    expect(mockCreateSession).not.toHaveBeenCalled();
  });

  it('refuses a selection larger than the remaining slots', () => {
    const onUploaded = jest.fn();
    render(<UploadButton targetSessionId="existing" remaining={1} onUploaded={onUploaded} />);

    selectFiles('a.csv', 'b.csv');

    expect(alertSpy).toHaveBeenCalled();
    expect(mockUploadDataset).not.toHaveBeenCalled();
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it('keeps successful files and reports the failed ones', async () => {
    mockUploadDataset.mockImplementation((file) => ({
      promise:
        file.name === 'bad.csv'
          ? Promise.reject(new Error('broken'))
          : Promise.resolve(dataset('upload_ok', file.name)),
      cancel: jest.fn(),
    }));
    const onUploaded = jest.fn();
    render(<UploadButton remaining={5} onUploaded={onUploaded} />);

    selectFiles('ok.csv', 'bad.csv');

    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith('new-session', [dataset('upload_ok', 'ok.csv')]));
    expect(alertSpy).toHaveBeenCalledWith('bad.csv: broken');
    expect(mockDeleteSession).not.toHaveBeenCalled();
  });

  it('deletes the new session when every file fails', async () => {
    mockUploadDataset.mockReturnValue({ promise: Promise.reject(new Error('broken')), cancel: jest.fn() });
    const onUploaded = jest.fn();
    render(<UploadButton remaining={5} onUploaded={onUploaded} />);

    selectFiles('bad.csv');

    await waitFor(() => expect(mockDeleteSession).toHaveBeenCalledWith('new-session'));
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it('disables the button when the session is full', () => {
    render(<UploadButton targetSessionId="existing" remaining={0} onUploaded={jest.fn()} />);
    expect(screen.getByRole('button')).toBeDisabled();
  });
});

describe('uploadedDatasetsMessage', () => {
  it('keeps the single-file wording and lists every file otherwise', () => {
    expect(uploadedDatasetsMessage([dataset('upload_a', 'a.csv')])).toBe(
      'Use uploaded dataset `upload_a` (a.csv, 3 rows, 2 columns). '
    );
    expect(
      uploadedDatasetsMessage([dataset('upload_a', 'a.csv'), { ...dataset('upload_b', 'b.xlsx'), sheet: 'Data' }])
    ).toBe(
      'Use uploaded datasets `upload_a` (a.csv, 3 rows, 2 columns), `upload_b` (b.xlsx, sheet: Data, 3 rows, 2 columns). '
    );
  });
});
