import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import { getWorkflowStatus } from '../utils/api';

/** UI-facing lifecycle of a dispatched PTC research run. */
export type PTCDispatchStatus =
  | 'starting'
  | 'running'
  | 'needs_input'
  | 'completed'
  | 'failed'
  | 'stopped';

const TERMINAL: ReadonlySet<PTCDispatchStatus> = new Set(['completed', 'failed', 'stopped']);

/** Map the backend WorkflowStatus enum onto the card's status. `unknown`/absent
 *  means the run hasn't registered yet, so we show "starting" and keep polling. */
function mapStatus(raw: unknown): PTCDispatchStatus {
  switch (raw) {
    case 'active': return 'running';
    case 'interrupted': return 'needs_input';
    case 'completed': return 'completed';
    case 'failed': return 'failed';
    case 'cancelled': return 'stopped';
    default: return 'starting';
  }
}

export interface DispatchStatusResult {
  status: PTCDispatchStatus;
  isTerminal: boolean;
  runId: string | null;
}

/**
 * Poll a dispatched thread's /status while it's live, mapping the backend
 * WorkflowStatus to the card's lifecycle. Polling stops once terminal; cards
 * watching the same thread share one cache entry.
 */
export function usePTCDispatchStatus(
  threadId: string | undefined,
  enabled: boolean,
): DispatchStatusResult {
  const { data } = useQuery({
    queryKey: queryKeys.threads.status(threadId ?? ''),
    queryFn: () => getWorkflowStatus(threadId as string),
    enabled: enabled && !!threadId,
    staleTime: 2_000,
    refetchInterval: (query) => {
      const s = mapStatus((query.state.data as { status?: string } | undefined)?.status);
      return TERMINAL.has(s) ? false : 4_000;
    },
  });

  const status = mapStatus((data as { status?: string } | undefined)?.status);
  return {
    status,
    isTerminal: TERMINAL.has(status),
    runId: (data as { run_id?: string | null } | undefined)?.run_id ?? null,
  };
}
