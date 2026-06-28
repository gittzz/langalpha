/**
 * Reconnect guards: the post-await guard in `reconnectIfStaleRun` (D1) and the
 * per-instance isolation that the render-gate identity check leans on (D2).
 *
 * D1 — `reconnectIfStaleRun` checks /status across an await. A history reload
 * (driven by `reloadTrigger`, or a workspace-key change) can begin DURING that
 * await, flipping `historyLoadingRef` true. The post-await re-check must mirror
 * the pre-await check and bail, so the stale-run reconnect can't race the reload
 * for the message state.
 *
 * D2 — In production ChatView is multi-instance (useChatViewCache keys one hook
 * instance per workspace+thread with a stable React key, so a stable threadId
 * per instance). The `threadIdRef.current !== tid` render gate is therefore a
 * belt-and-suspenders identity check; the real isolation is per-instance hook
 * state + the currentRunIdRef dedup. This proves a run targeting thread A never
 * leaks into a separate thread-B instance.
 *
 * Harness mirrors the sibling reconnect/report-back suites: REAL hook internals,
 * mocked api module.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock('@/lib/supabase', () => ({ supabase: null }));

vi.mock('../utils/threadStorage', () => ({
  getStoredThreadId: vi.fn().mockReturnValue(null),
  setStoredThreadId: vi.fn(),
  removeStoredThreadId: vi.fn(),
}));

vi.mock('../../utils/api', () => ({
  sendChatMessageStream: vi.fn(),
  sendHitlResponse: vi.fn(),
  cancelWorkflow: vi.fn().mockResolvedValue({ success: true }),
  replayThreadHistory: vi.fn().mockResolvedValue(undefined),
  getWorkflowStatus: vi.fn().mockResolvedValue({ can_reconnect: false, status: 'completed' }),
  reconnectToWorkflowStream: vi.fn().mockResolvedValue({ disconnected: false, aborted: false }),
  streamSubagentTaskEvents: vi.fn(),
  fetchThreadTurns: vi.fn().mockResolvedValue({ turns: [], retry_checkpoint_id: null }),
  submitFeedback: vi.fn(),
  removeFeedback: vi.fn(),
  getThreadFeedback: vi.fn().mockResolvedValue([]),
  watchThread: vi.fn().mockReturnValue({ abort: new AbortController() }),
}));

import { getWorkflowStatus, reconnectToWorkflowStream, replayThreadHistory } from '../../utils/api';
import { useChatMessages } from '../useChatMessages';

const mockStatus = getWorkflowStatus as Mock;
const mockReconnect = reconnectToWorkflowStream as Mock;
const mockReplay = replayThreadHistory as Mock;

const IDLE = { can_reconnect: false, status: 'completed', pending_report_back: false, active_tasks: [] };

/** Flush the mount effect's status-fetch → history-load → branch decision. */
async function settleMountEffect() {
  for (let i = 0; i < 2; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

/** Externally-resolvable promise so a test can hold an await open then release it. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

describe('useChatMessages — reconnect guards (D1 post-await load guard, D2 instance isolation)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReplay.mockResolvedValue(undefined);
    mockReconnect.mockResolvedValue({ disconnected: false, aborted: false });
  });

  it('D1: bails (no reconnect) when a history reload starts DURING the /status await', async () => {
    // Mount idle: the view shows a completed turn, history load settles, no reconnect.
    mockStatus.mockResolvedValue(IDLE);

    let wsId = 'ws';
    const { result, rerender } = renderHookWithProviders(() => useChatMessages(wsId, 'th'));
    await waitFor(() => expect(mockReplay).toHaveBeenCalled());
    await settleMountEffect();
    expect(result.current.isLoadingHistory).toBe(false);
    expect(mockReconnect).not.toHaveBeenCalled();

    // A live run now exists on this thread — both the concurrent reload's /status
    // and reconnectIfStaleRun's /status report it.
    const live = { can_reconnect: true, status: 'running', run_id: 'run-2', pending_report_back: false, active_tasks: [] };

    // The NEXT history load (triggered by the workspace-key change below) hangs on
    // replay, so historyLoadingRef stays TRUE — a reload genuinely in flight.
    mockReplay.mockImplementation(() => new Promise(() => {}));

    // Default /status → live; but DEFER the very next call, which is
    // reconnectIfStaleRun's, so we can flip historyLoadingRef true while it awaits.
    mockStatus.mockResolvedValue(live);
    const d = deferred<typeof live>();
    mockStatus.mockImplementationOnce(() => d.promise);

    let staleRunPromise: Promise<unknown>;
    await act(async () => {
      // Passes the pre-await guard (not loading yet) and parks on the deferred /status.
      staleRunPromise = result.current.reconnectIfStaleRun();
      // Kick off a concurrent reload by changing the workspace key (threadId stays
      // 'th'): the load effect fires loadConversationHistory → historyLoadingRef=true
      // → parks on the hanging replay. Its /status uses the resolved default (live),
      // but it never reaches its own reconnect branch (parked inside the load).
      wsId = 'ws2';
      rerender();
      await new Promise((r) => setTimeout(r, 0));
    });

    // Reload in flight → historyLoadingRef is true (mirrored by isLoadingHistory).
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(true));

    // Now the stale-run /status resolves with a live run. The POST-await guard must
    // observe historyLoadingRef and BAIL. Without the historyLoadingRef clause,
    // reconnectToStream would fire here and race the reload for the message state.
    await act(async () => {
      d.resolve(live);
      await staleRunPromise;
    });

    expect(mockReconnect).not.toHaveBeenCalled();
  });

  it('D1 sanity: still reconnects to a live run when NO reload is in flight', async () => {
    // Guards against a false pass: prove the bail above is the load guard, not a
    // blanket "never reconnect". Same shape as the reconnect-on-reactivate suite.
    mockStatus.mockResolvedValue(IDLE);

    const { result } = renderHookWithProviders(() => useChatMessages('ws', 'th'));
    await waitFor(() => expect(mockReplay).toHaveBeenCalled());
    await settleMountEffect();
    expect(mockReconnect).not.toHaveBeenCalled();

    mockStatus.mockResolvedValue({ can_reconnect: true, status: 'running', run_id: 'run-2', pending_report_back: false, active_tasks: [] });
    await act(async () => {
      await result.current.reconnectIfStaleRun();
    });

    expect(mockReconnect).toHaveBeenCalledTimes(1);
    expect(mockReconnect.mock.calls[0][0]).toBe('th');
    expect(mockReconnect.mock.calls[0][1]).toBe('run-2');
  });

  it('D2: a run targeting thread A does not leak into a separate thread-B instance', async () => {
    // Two hook instances, one per stable threadId (mirrors useChatViewCache:
    // one ChatView/useChatMessages instance per workspace+thread).
    mockStatus.mockResolvedValue(IDLE);

    const a = renderHookWithProviders(() => useChatMessages('ws', 'A'));
    const b = renderHookWithProviders(() => useChatMessages('ws', 'B'));
    await waitFor(() => expect(mockReplay).toHaveBeenCalled());
    await settleMountEffect();
    expect(a.result.current.messages).toHaveLength(0);
    expect(b.result.current.messages).toHaveLength(0);
    expect(mockReconnect).not.toHaveBeenCalled();

    // A live run starts on thread A; its reconnect streams a text chunk into A.
    mockStatus.mockImplementation((tid: string) =>
      Promise.resolve(
        tid === 'A'
          ? { can_reconnect: true, status: 'running', run_id: 'run-A', pending_report_back: false, active_tasks: [] }
          : IDLE,
      ),
    );
    mockReconnect.mockImplementation(
      (tid: string, _runId: unknown, _lastId: unknown, onEvent: (e: Record<string, unknown>) => void) => {
        if (tid === 'A') {
          onEvent({ event: 'message_chunk', content_type: 'text', content: 'A-only content' });
        }
        return Promise.resolve({ disconnected: false, aborted: false });
      },
    );

    // Reactivate A → the identity gate passes for A's OWN threadId (it equals the
    // instance's stable threadId, so the gate is inert here, as in production) →
    // attaches run-A → streams the chunk into A's per-instance state.
    await act(async () => {
      await a.result.current.reconnectIfStaleRun();
    });

    // A processed its run and rendered the chunk.
    expect(mockReconnect).toHaveBeenCalledTimes(1);
    expect(mockReconnect.mock.calls[0][0]).toBe('A');
    expect(JSON.stringify(a.result.current.messages)).toContain('A-only content');

    // Nothing leaked into B: separate hook instance, separate state, never reconnected.
    expect(b.result.current.messages).toHaveLength(0);
    expect(mockReconnect.mock.calls.some((c) => c[0] === 'B')).toBe(false);
  });
});
