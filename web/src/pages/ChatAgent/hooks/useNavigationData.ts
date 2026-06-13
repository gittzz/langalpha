import { useState, useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useWorkspaces } from '../../../hooks/useWorkspaces';
import { queryKeys } from '../../../lib/queryKeys';
import { getWorkspaces, getWorkspaceThreads, reorderWorkspaces, updateWorkspace } from '../utils/api';
import { useNavPrefs } from '../utils/navPrefs';

interface WorkspaceRecord {
  workspace_id: string;
  [key: string]: unknown;
}

interface ThreadRecord {
  thread_id: string;
  [key: string]: unknown;
}

interface ThreadsData {
  threads: ThreadRecord[];
  loading: boolean;
  total?: number;
}

interface WorkspacesResponse {
  workspaces: WorkspaceRecord[];
  total: number;
}

interface ThreadsResponse {
  threads: ThreadRecord[];
  total?: number;
  [key: string]: unknown;
}

const NAV_WS_PARAMS = { limit: 20, includeFlash: true };

// Session-stable nav ordering. The server sorts threads (and, within the
// 'custom' workspace sort, unpinned workspaces) by updated_at DESC, so the item
// being chatted in would hoist to the top on every refetch. We snapshot the
// order seen first in this page session and reorder later responses to match.
// The stores are module-level because ChatAgent caches one ChatView instance
// per thread — hook-local refs would re-snapshot on every thread switch,
// defeating the freeze. Reload starts a fresh session (fresh recency order).
const _frozenThreadOrders: Record<string, string[]> = {};
let _frozenWorkspaceOrder: string[] | null = null;

// Last-seen manual arrangement (sort_order + pin state) per workspace. The
// frozen order above neutralizes recency hoisting, but a reorder/pin made in
// the workspace gallery also lands as a refetch — we must NOT freeze that out.
// When an existing workspace's sort_order or pin state changes between
// refetches, we re-snapshot so the nav tracks the gallery's 'custom' sort.
// Recency (updated_at) bumps and newly paged-in workspaces don't change these
// values, so the active workspace still never hoists itself.
const _lastWorkspaceArrangement = new Map<string, { sortOrder: number | null; pinned: boolean }>();

export function resetStableNavOrder() {
  for (const key of Object.keys(_frozenThreadOrders)) delete _frozenThreadOrders[key];
  _frozenWorkspaceOrder = null;
  _lastWorkspaceArrangement.clear();
}

// Drop a deleted workspace from the frozen-order stores so they don't retain
// ghost ids for the rest of the session. Deleted ids are already filtered out
// of the rendered list (applyStableOrderBy drops map-misses), so this is
// housekeeping, not a correctness fix — call it from the workspace delete path.
export function forgetStableNavOrder(workspaceId: string) {
  delete _frozenThreadOrders[workspaceId];
  _lastWorkspaceArrangement.delete(workspaceId);
  if (_frozenWorkspaceOrder) {
    const idx = _frozenWorkspaceOrder.indexOf(workspaceId);
    if (idx !== -1) _frozenWorkspaceOrder.splice(idx, 1);
  }
}

// Bump notifications: chatting in a thread moves it to the top of its
// workspace's list (like normal chat apps), while clicking around never
// reorders. Subscribed hooks re-apply the frozen orders when the version ticks.
let _navOrderVersion = 0;
const _navOrderListeners = new Set<() => void>();

function subscribeNavOrder(fn: () => void): () => void {
  _navOrderListeners.add(fn);
  return () => _navOrderListeners.delete(fn);
}

/**
 * Move a thread to the top of its workspace's frozen order. Called when the
 * user sends a message (new turn, steering, edit/regenerate/retry). No-op if
 * the workspace's order hasn't been snapshotted yet — the initial snapshot is
 * recency-sorted, so the thread lands on top anyway.
 */
export function bumpThreadNavOrder(wsId: string, threadId: string | null | undefined) {
  if (!wsId || !threadId || threadId === '__default__') return;
  const frozen = _frozenThreadOrders[wsId];
  if (!frozen) return;
  const idx = frozen.indexOf(threadId);
  if (idx === 0) return;
  if (idx > 0) frozen.splice(idx, 1);
  frozen.unshift(threadId);
  _navOrderVersion++;
  _navOrderListeners.forEach((fn) => fn());
}

// Reorder `items` to a frozen id sequence. Unseen ids appearing before any
// known id are genuinely new (the server lists them first) and surface on top;
// unseen ids after a known id are paginated-in older entries and stay below
// the stable block. Ids missing from `items` (deleted) drop out via map-miss.
export function applyStableOrderBy<T>(
  frozen: string[] | null | undefined,
  items: T[],
  getId: (item: T) => string,
): { order: string[]; items: T[] } {
  const ids = items.map(getId);
  if (!frozen) return { order: ids, items };
  const byId = new Map(items.map((item) => [getId(item), item]));
  const frozenSet = new Set(frozen);
  const firstKnownIdx = ids.findIndex((id) => frozenSet.has(id));
  const newIds: string[] = [];
  const trailingIds: string[] = [];
  ids.forEach((id, idx) => {
    if (frozenSet.has(id)) return;
    if (firstKnownIdx === -1 || idx < firstKnownIdx) newIds.push(id);
    else trailingIds.push(id);
  });
  const order = [...newIds, ...frozen, ...trailingIds];
  const merged = order
    .map((id) => byId.get(id))
    .filter((item): item is T => item !== undefined);
  return { order, items: merged };
}

export function applyStableOrder(
  frozen: string[] | undefined,
  serverThreads: ThreadRecord[],
): { order: string[]; threads: ThreadRecord[] } {
  const { order, items } = applyStableOrderBy(frozen, serverThreads, (thread) => thread.thread_id);
  return { order, threads: items };
}

export function useNavigationData(currentWorkspaceId: string) {
  const queryClient = useQueryClient();
  const { workspaceLimit, threadPageSize, orderBy } = useNavPrefs();
  const orderVersion = useSyncExternalStore(subscribeNavOrder, () => _navOrderVersion);

  const orderThreads = useCallback((wsId: string, serverThreads: ThreadRecord[]): ThreadRecord[] => {
    const { order, threads } = applyStableOrder(_frozenThreadOrders[wsId], serverThreads);
    _frozenThreadOrders[wsId] = order;
    return threads;
    // orderVersion isn't read in the body, but a bump rewrites the frozen
    // orders this callback closes over — its identity must change so the
    // memos downstream re-apply the new order.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderVersion]);

  // Workspace list via React Query. sortBy mirrors the gallery's order-by
  // selection; changing it swaps the query key and refetches in the new order.
  const wsParams = useMemo(() => ({ ...NAV_WS_PARAMS, sortBy: orderBy }), [orderBy]);
  const { data: wsData, isLoading } = useWorkspaces(wsParams);
  const allFetched: WorkspaceRecord[] = (wsData as WorkspacesResponse | undefined)?.workspaces || [];
  const totalCount = (wsData as WorkspacesResponse | undefined)?.total || 0;

  const [workspaceThreads, setWorkspaceThreads] = useState<Record<string, ThreadsData>>({});
  // "Load all" clicked this session — overrides a numeric workspaceLimit pref.
  const [showAllWorkspaces, setShowAllWorkspaces] = useState(false);
  const showAll = workspaceLimit === 'all' || showAllWorkspaces;

  // When showing all workspaces, page in the remainder beyond the first fetch.
  // Each completed page grows allFetched, re-running the effect until total is
  // reached. A failure stops the loop for the session (avoids a retry storm).
  const wsFetchRef = useRef({ inflight: false, failed: false });
  // Per-workspace single-flight for "Show more": the page offset is snapshotted
  // before the await, so two rapid taps would otherwise fetch the same page.
  const loadMoreInflightRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!showAll || isLoading) return;
    if (!totalCount || allFetched.length >= totalCount) return;
    if (wsFetchRef.current.inflight || wsFetchRef.current.failed) return;
    wsFetchRef.current.inflight = true;
    getWorkspaces(100, allFetched.length, orderBy, true)
      .then((data: unknown) => {
        const page = data as WorkspacesResponse;
        queryClient.setQueryData(queryKeys.workspaces.list({ ...NAV_WS_PARAMS, sortBy: orderBy, offset: 0 }), (old: unknown) => {
          const oldData = old as WorkspacesResponse | undefined;
          if (!oldData) return page;
          const existingIds = new Set(oldData.workspaces.map(w => w.workspace_id));
          const unique = (page.workspaces || []).filter(w => !existingIds.has(w.workspace_id));
          return { ...oldData, workspaces: [...oldData.workspaces, ...unique], total: page.total || oldData.total };
        });
      })
      .catch((e: unknown) => {
        wsFetchRef.current.failed = true;
        console.warn('[useNavigationData] Failed to load all workspaces:', e);
      })
      .finally(() => {
        wsFetchRef.current.inflight = false;
      });
  }, [showAll, isLoading, allFetched.length, totalCount, queryClient, orderBy]);

  // Workspace list in server order (pinned first, then manual sort_order, then
  // recency), frozen to its first-session arrangement so the active workspace's
  // updated_at bumps (which reorder the server response) don't hoist it.
  //
  // NOTE: this memo intentionally reads AND writes the module-level frozen-order
  // stores during render (an impure useMemo). That is safe ONLY because this
  // tree renders synchronously — no StrictMode, no useTransition/Suspense on the
  // nav path — so the factory runs once per render and can't interleave or be
  // discarded. If concurrent features are ever adopted on this path, rework the
  // ordering subsystem (this memo + orderThreads + reorderWorkspace) to
  // pure-compute + an effect-commit before they can tear the snapshot.
  const workspaces = useMemo(() => {
    if (!allFetched.length) return [];

    let ordered: WorkspaceRecord[];
    if (orderBy === 'custom') {
      // If an existing workspace's manual arrangement (sort_order or pin state)
      // changed since the last render, a reorder/pin happened — here or in the
      // workspace gallery — so drop the frozen order and re-snapshot from the
      // fresh server order, keeping the nav in sync with the gallery's 'custom'
      // sort. Recency bumps and paged-in workspaces don't change these fields.
      let manualOrderChanged = false;
      for (const ws of allFetched) {
        const sortOrder = (ws.sort_order as number | null | undefined) ?? null;
        const pinned = Boolean(ws.is_pinned);
        const prev = _lastWorkspaceArrangement.get(ws.workspace_id);
        if (prev && (prev.sortOrder !== sortOrder || prev.pinned !== pinned)) manualOrderChanged = true;
        _lastWorkspaceArrangement.set(ws.workspace_id, { sortOrder, pinned });
      }
      if (manualOrderChanged) _frozenWorkspaceOrder = null;

      const { order, items: stable } = applyStableOrderBy(_frozenWorkspaceOrder, allFetched, (ws) => ws.workspace_id);
      _frozenWorkspaceOrder = order;
      ordered = stable;
    } else {
      // 'activity' / 'name': the server already returned the list in this order
      // (the query's sortBy). Trust it and don't freeze — recency hoisting is
      // the point of 'activity'. Drop any custom snapshot so switching back to
      // 'custom' re-snapshots fresh from the server's manual order.
      _frozenWorkspaceOrder = null;
      ordered = allFetched;
    }

    if (showAll) return ordered;

    const sliced = ordered.slice(0, workspaceLimit as number);
    if (currentWorkspaceId && !sliced.some((ws) => ws.workspace_id === currentWorkspaceId)) {
      const currentWs = allFetched.find((ws) => ws.workspace_id === currentWorkspaceId);
      // Keep the current workspace in view without hoisting it to the top —
      // it joins at the bottom of the visible slice, holding the list stable.
      if (currentWs) sliced.push(currentWs);
    }
    return sliced;
    // orderVersion isn't read in the body, but drag-reorder rewrites the frozen
    // workspace order this memo reads — it must recompute when the version ticks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allFetched, showAll, workspaceLimit, currentWorkspaceId, orderVersion, orderBy]);

  // Drag-reorder a workspace in the nav list. Optimistically rewrites the
  // frozen nav order, then persists sequential sort_order values so the
  // workspace gallery's 'custom' sort shows the same arrangement. Mirrors the
  // gallery's reorder-mode rules: flash is immovable, and crossing the
  // pinned/unpinned boundary is refused (the server sort would undo it —
  // is_pinned DESC dominates sort_order).
  const reorderWorkspace = useCallback(async (activeId: string, overId: string) => {
    const frozen = _frozenWorkspaceOrder;
    if (!frozen || !activeId || !overId || activeId === overId) return;
    const byId = new Map(allFetched.map((ws) => [ws.workspace_id, ws]));
    const active = byId.get(activeId);
    const over = byId.get(overId);
    if (!active || !over) return;
    if (active.status === 'flash' || over.status === 'flash') return;
    if (Boolean(active.is_pinned) !== Boolean(over.is_pinned)) return;
    const fromIdx = frozen.indexOf(activeId);
    const toIdx = frozen.indexOf(overId);
    if (fromIdx === -1 || toIdx === -1) return;

    const snapshot = [...frozen];
    frozen.splice(fromIdx, 1);
    frozen.splice(toIdx, 0, activeId);
    _navOrderVersion++;
    _navOrderListeners.forEach((fn) => fn());

    const items = frozen
      .map((id) => byId.get(id))
      .filter((ws): ws is WorkspaceRecord => !!ws && ws.status !== 'flash')
      .map((ws, i) => ({ workspace_id: ws.workspace_id, sort_order: i }));
    try {
      await reorderWorkspaces(items);
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.lists() });
    } catch (e) {
      console.warn('[useNavigationData] Failed to persist workspace order:', e);
      _frozenWorkspaceOrder = snapshot;
      _navOrderVersion++;
      _navOrderListeners.forEach((fn) => fn());
    }
  }, [allFetched, queryClient]);

  // Optimistically patch one workspace across every cached list, persist via
  // `updateWorkspace`, then invalidate so the server's re-sort lands. Rolls the
  // caches back on failure. Shared by pin and rename — the only difference is
  // the patch payload and the persisted field.
  const patchWorkspace = useCallback(async (wsId: string, patch: Partial<WorkspaceRecord>) => {
    const previous = queryClient.getQueriesData({ queryKey: queryKeys.workspaces.lists() });
    previous.forEach(([key, data]: [unknown, unknown]) => {
      const d = data as WorkspacesResponse | undefined;
      if (!d?.workspaces) return;
      queryClient.setQueryData(key as readonly unknown[], {
        ...d,
        workspaces: d.workspaces.map((ws) => (ws.workspace_id === wsId ? { ...ws, ...patch } : ws)),
      });
    });
    try {
      await updateWorkspace(wsId, patch);
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.lists() });
      // The detail cache (useWorkspace → FilePanel header) carries the name, so
      // a rename must refresh it too; harmless for a pin-only patch.
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces.detail(wsId) });
    } catch (e) {
      previous.forEach(([key, data]: [unknown, unknown]) => queryClient.setQueryData(key as readonly unknown[], data));
      console.warn('[useNavigationData] Failed to update workspace:', e);
    }
  }, [queryClient]);

  // Pin/unpin a workspace. The server sorts pinned-first, so the invalidate
  // refetch reorders the list; the 'custom' freeze re-snapshots on the pin-state
  // change (manualOrderChanged detection above).
  const pinWorkspace = useCallback((wsId: string, pinned: boolean) => {
    return patchWorkspace(wsId, { is_pinned: pinned });
  }, [patchWorkspace]);

  // Rename a workspace. No-op on blank/unchanged names is enforced by the caller.
  const renameWorkspace = useCallback((wsId: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed) return Promise.resolve();
    return patchWorkspace(wsId, { name: trimmed });
  }, [patchWorkspace]);

  const hasMore = useMemo(() => {
    if (showAll) return false;
    if ((workspaceLimit as number) < allFetched.length) return true;
    if (allFetched.length < totalCount) return true;
    return false;
  }, [showAll, workspaceLimit, allFetched.length, totalCount]);

  const { data: currentWsThreadData, isLoading: currentWsThreadsLoading } = useQuery({
    // Page size is part of the key (mirrors the dashboard widgets' thread keys):
    // the queryFn fetches `threadPageSize` rows, so changing that pref must miss
    // the cache and refetch instead of replaying the previous page size.
    queryKey: [...queryKeys.threads.byWorkspace(currentWorkspaceId), threadPageSize, 0],
    queryFn: () => getWorkspaceThreads(currentWorkspaceId, threadPageSize, 0),
    enabled: !!currentWorkspaceId,
    staleTime: 30_000,
  });

  const mergedThreads = useMemo(() => {
    if (!currentWorkspaceId) return workspaceThreads;
    const stored = workspaceThreads[currentWorkspaceId];
    if (currentWsThreadData === undefined) {
      return {
        ...workspaceThreads,
        [currentWorkspaceId]: {
          threads: stored?.threads || [],
          loading: true,
          total: stored?.total,
        },
      };
    }
    // The query holds the first page; "Show more" pages land in workspaceThreads.
    // Union them (query page wins on overlap) so paging the current workspace
    // survives the query refetching its first page.
    const page = (currentWsThreadData as ThreadsResponse)?.threads || [];
    const pageIds = new Set(page.map((t) => t.thread_id));
    const extras = (stored?.threads || []).filter((t) => !pageIds.has(t.thread_id));
    return {
      ...workspaceThreads,
      [currentWorkspaceId]: {
        threads: orderThreads(currentWorkspaceId, [...page, ...extras]),
        loading: currentWsThreadsLoading || stored?.loading || false,
        total: (currentWsThreadData as ThreadsResponse)?.total ?? stored?.total,
      },
    };
  }, [workspaceThreads, currentWorkspaceId, currentWsThreadData, currentWsThreadsLoading, orderThreads]);

  const expandWorkspace = useCallback((wsId: string) => {
    const mergeFetched = (data: ThreadsResponse) => {
      setWorkspaceThreads(prev => {
        const have = prev[wsId]?.threads || [];
        // Keep already-paged-in threads; the fetched first page wins on overlap.
        const pageIds = new Set((data.threads || []).map((t) => t.thread_id));
        const extras = have.filter((t) => !pageIds.has(t.thread_id));
        return {
          ...prev,
          [wsId]: {
            threads: orderThreads(wsId, [...(data.threads || []), ...extras]),
            loading: false,
            total: data.total ?? prev[wsId]?.total,
          },
        };
      });
    };

    const cached = queryClient.getQueryData([...queryKeys.threads.byWorkspace(wsId), threadPageSize, 0]) as ThreadsResponse | undefined;
    if (cached) {
      mergeFetched(cached);
      return;
    }

    setWorkspaceThreads(prev => ({
      ...prev,
      [wsId]: { threads: prev[wsId]?.threads || [], loading: true, total: prev[wsId]?.total },
    }));

    queryClient.fetchQuery({
      queryKey: [...queryKeys.threads.byWorkspace(wsId), threadPageSize, 0],
      queryFn: () => getWorkspaceThreads(wsId, threadPageSize, 0),
      staleTime: 30_000,
    }).then((data: unknown) => {
      mergeFetched(data as ThreadsResponse);
    }).catch(() => {
      setWorkspaceThreads(prev => ({
        ...prev,
        [wsId]: { threads: prev[wsId]?.threads || [], loading: false, total: prev[wsId]?.total },
      }));
    });
  }, [queryClient, orderThreads, threadPageSize]);

  // Fetch the next page of threads for a workspace and append it below the
  // already-shown ones (the stable order keeps paginated-in ids at the bottom).
  const loadMoreThreads = useCallback(async (wsId: string) => {
    if (loadMoreInflightRef.current.has(wsId)) return;
    loadMoreInflightRef.current.add(wsId);
    const shown = mergedThreads[wsId]?.threads || [];
    setWorkspaceThreads(prev => ({
      ...prev,
      [wsId]: {
        threads: prev[wsId]?.threads || shown,
        loading: true,
        total: prev[wsId]?.total ?? mergedThreads[wsId]?.total,
      },
    }));
    try {
      const data = await getWorkspaceThreads(wsId, threadPageSize, shown.length) as ThreadsResponse;
      setWorkspaceThreads(prev => {
        const have = prev[wsId]?.threads?.length ? prev[wsId].threads : shown;
        const haveIds = new Set(have.map((t) => t.thread_id));
        const fresh = (data.threads || []).filter((t) => !haveIds.has(t.thread_id));
        return {
          ...prev,
          [wsId]: {
            threads: orderThreads(wsId, [...have, ...fresh]),
            loading: false,
            total: data.total ?? prev[wsId]?.total,
          },
        };
      });
    } catch (e) {
      console.warn('[useNavigationData] Failed to load more threads:', e);
      setWorkspaceThreads(prev => ({
        ...prev,
        [wsId]: {
          threads: prev[wsId]?.threads || shown,
          loading: false,
          total: prev[wsId]?.total,
        },
      }));
    } finally {
      loadMoreInflightRef.current.delete(wsId);
    }
  }, [mergedThreads, threadPageSize, orderThreads]);

  const loadAll = useCallback(() => {
    // The page-in effect above fetches the remainder once this flips.
    setShowAllWorkspaces(true);
    wsFetchRef.current.failed = false;
  }, []);

  // `canReorderWorkspaces` is false under activity/name: drag-reorder is a
  // 'custom'-order action (a drop persists sort_order the view wouldn't
  // reflect), so the consumer withholds the handler to disable the affordance.
  // Mirrors the gallery, where reordering only applies to the custom arrangement.
  return { workspaces, workspaceThreads: mergedThreads, loading: isLoading, hasMore, loadAll, expandWorkspace, loadMoreThreads, reorderWorkspace, canReorderWorkspaces: orderBy === 'custom', pinWorkspace, renameWorkspace };
}
