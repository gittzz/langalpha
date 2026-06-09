import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '../lib/queryKeys';
import {
  getWorkspaceMcpServers,
  addWorkspaceMcpServer,
  updateWorkspaceMcpServer,
  setWorkspaceMcpServerEnabled,
  deleteWorkspaceMcpServer,
  discoverWorkspaceMcpServer,
  getMcpCatalog,
  createMcpCatalogServer,
  updateMcpCatalogServer,
  deleteMcpCatalogServer,
  type EffectiveServerList,
  type McpServerInput,
} from '../pages/ChatAgent/utils/api';

/**
 * React Query hooks for MCP server config — mirror `useWorkspaces` /
 * `useApiKeys` patterns. Mutations are DB-write-only on the backend (the change
 * applies on the next agent run within ~30s), so consumers show a transient
 * "not synced" hint themselves; here we just invalidate the relevant caches.
 *
 * The enabled toggle is OPTIMISTIC with rollback on error (plan requirement):
 * the row flips instantly, and reverts if the PATCH fails.
 */

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

/** Effective per-workspace MCP list (builtins + workspace servers + status). */
export function useWorkspaceMcpServers(workspaceId: string | null | undefined, enabled = true) {
  return useQuery({
    queryKey: queryKeys.mcp.workspace(workspaceId ?? ''),
    queryFn: () => getWorkspaceMcpServers(workspaceId!),
    enabled: enabled && !!workspaceId,
    staleTime: 15_000,
  });
}

/** The user's MCP template catalog. */
export function useMcpCatalog(enabled = true) {
  return useQuery({
    queryKey: queryKeys.mcp.catalog(),
    queryFn: getMcpCatalog,
    enabled,
    staleTime: 60_000,
  });
}

// ---------------------------------------------------------------------------
// Per-workspace mutations
// ---------------------------------------------------------------------------

/** Add a workspace server — a full def OR `{ from_template }`. */
export function useAddWorkspaceMcpServer(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: McpServerInput | { from_template: string }) =>
      addWorkspaceMcpServer(workspaceId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.workspace(workspaceId) });
    },
  });
}

export function useUpdateWorkspaceMcpServer(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: McpServerInput }) =>
      updateWorkspaceMcpServer(workspaceId, name, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.workspace(workspaceId) });
    },
  });
}

/** Optimistic enabled toggle with rollback on error. */
export function useToggleWorkspaceMcpServer(workspaceId: string) {
  const queryClient = useQueryClient();
  const key = queryKeys.mcp.workspace(workspaceId);
  return useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      setWorkspaceMcpServerEnabled(workspaceId, name, enabled),
    onMutate: async ({ name, enabled }) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<EffectiveServerList>(key);
      if (previous) {
        queryClient.setQueryData<EffectiveServerList>(key, {
          ...previous,
          servers: previous.servers.map((s) =>
            s.name === name ? { ...s, enabled } : s,
          ),
        });
      }
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: key });
    },
  });
}

export function useDeleteWorkspaceMcpServer(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteWorkspaceMcpServer(workspaceId, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.workspace(workspaceId) });
    },
  });
}

/**
 * Discovery probe. Does NOT auto-invalidate — callers display the returned
 * result inline (the effective list refetches on its own staleTime), and we
 * avoid clobbering a row the user is actively inspecting.
 */
export function useDiscoverWorkspaceMcpServer(workspaceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => discoverWorkspaceMcpServer(workspaceId, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.workspace(workspaceId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Catalog mutations
// ---------------------------------------------------------------------------

export function useCreateMcpCatalogServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: McpServerInput) => createMcpCatalogServer(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.catalog() });
    },
  });
}

export function useUpdateMcpCatalogServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: McpServerInput }) =>
      updateMcpCatalogServer(name, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.catalog() });
    },
  });
}

export function useDeleteMcpCatalogServer() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteMcpCatalogServer(name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.mcp.catalog() });
    },
  });
}
