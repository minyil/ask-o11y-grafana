import { act } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { useSessionManager } from '../useSessionManager';
import { listSessions, updateSession, type SessionMetadata } from '../../../../services/backendSessionClient';

jest.mock('../../../../services/backendSessionClient', () => ({
  listSessions: jest.fn(),
  getSession: jest.fn(),
  updateSession: jest.fn(),
  deleteSession: jest.fn(),
  deleteAllSessions: jest.fn(),
  setCurrentSessionId: jest.fn(),
}));

const sessions: SessionMetadata[] = [
  {
    id: 'a',
    title: 'Why is CPU high',
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-02T00:00:00Z',
    messageCount: 4,
  },
  { id: 'b', title: 'Untitled', createdAt: '2026-10-01T00:00:00Z', updatedAt: '2026-10-01T00:00:00Z', messageCount: 2 },
] as SessionMetadata[];

function renderManager() {
  return renderHook(() => useSessionManager('1', [], jest.fn(), null, jest.fn()));
}

describe('useSessionManager.renameSession', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (listSessions as jest.Mock).mockResolvedValue(sessions);
  });

  it('persists the trimmed title and updates the local list in place', async () => {
    (updateSession as jest.Mock).mockResolvedValue(undefined);
    const { result } = renderManager();
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    await act(async () => {
      await result.current.renameSession('b', '  Checkout latency spike  ');
    });

    expect(updateSession).toHaveBeenCalledWith('b', { title: 'Checkout latency spike' });
    expect(result.current.sessions.map((s) => s.title)).toEqual(['Why is CPU high', 'Checkout latency spike']);
  });

  it('rejects a blank title without calling the backend', async () => {
    const { result } = renderManager();
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    await expect(result.current.renameSession('b', '   ')).rejects.toThrow('Title must not be empty');
    expect(updateSession).not.toHaveBeenCalled();
  });

  it('propagates backend failures and keeps the old title', async () => {
    (updateSession as jest.Mock).mockRejectedValue(new Error('Failed to update session (400)'));
    const { result } = renderManager();
    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    await act(async () => {
      await expect(result.current.renameSession('b', 'New name')).rejects.toThrow('Failed to update session (400)');
    });
    expect(result.current.sessions[1].title).toBe('Untitled');
  });
});
