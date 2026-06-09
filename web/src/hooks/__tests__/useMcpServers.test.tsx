import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import React, { type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { queryKeys } from '../../lib/queryKeys';
import {
  useWorkspaceMcpServers,
  useToggleWorkspaceMcpServer,
  useAddWorkspaceMcpServer,
  useDeleteWorkspaceMcpServer,
  useCreateMcpCatalogServer,
} from '../useMcpServers';
import type { EffectiveServerList } from '../../pages/ChatAgent/utils/api';

vi.mock('../../pages/ChatAgent/utils/api', () => ({
  getWorkspaceMcpServers: vi.fn(),
  addWorkspaceMcpServer: vi.fn(),
  updateWorkspaceMcpServer: vi.fn(),
  setWorkspaceMcpServerEnabled: vi.fn(),
  deleteWorkspaceMcpServer: vi.fn(),
  discoverWorkspaceMcpServer: vi.fn(),
  getMcpCatalog: vi.fn(),
  createMcpCatalogServer: vi.fn(),
  updateMcpCatalogServer: vi.fn(),
  deleteMcpCatalogServer: vi.fn(),
}));

import {
  getWorkspaceMcpServers,
  setWorkspaceMcpServerEnabled,
  addWorkspaceMcpServer,
  deleteWorkspaceMcpServer,
  createMcpCatalogServer,
} from '../../pages/ChatAgent/utils/api';

const WS = 'ws-1';

function makeServer(name: string, enabled: boolean): EffectiveServerList['servers'][number] {
  return {
    name,
    origin: 'workspace',
    transport: 'stdio',
    enabled,
    editable: true,
    deletable: true,
    status: 'connected',
    error: '',
    tool_count: 2,
    tools: [],
    missing_secrets: [],
    env_refs: [],
    header_refs: [],
    description: '',
    instruction: '',
    tool_exposure_mode: 'summary',
    command: 'npx',
    args: [],
    url: null,
    config_version: 1,
  };
}

function makeList(servers: EffectiveServerList['servers']): EffectiveServerList {
  return { servers, sandbox_running: true, max_servers: 20, config_version: 1 };
}

function makeClient() {
  // gcTime kept non-zero so an unobserved query's cache survives the optimistic
  // setQueryData / rollback assertions (no query hook mounts in these tests).
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('useWorkspaceMcpServers', () => {
  it('fetches the effective list and is disabled without a workspace id', async () => {
    (getWorkspaceMcpServers as Mock).mockResolvedValue(makeList([makeServer('s1', true)]));
    const client = makeClient();
    const { result } = renderHook(() => useWorkspaceMcpServers(WS), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data?.servers).toHaveLength(1);

    const disabled = renderHook(() => useWorkspaceMcpServers(null), { wrapper: wrapperFor(makeClient()) });
    expect(disabled.result.current.fetchStatus).toBe('idle');
  });
});

describe('useToggleWorkspaceMcpServer — optimistic with rollback', () => {
  it('optimistically flips enabled, then settles', async () => {
    const client = makeClient();
    client.setQueryData(queryKeys.mcp.workspace(WS), makeList([makeServer('s1', true)]));
    (setWorkspaceMcpServerEnabled as Mock).mockResolvedValue({ name: 's1', enabled: false });

    const { result } = renderHook(() => useToggleWorkspaceMcpServer(WS), { wrapper: wrapperFor(client) });

    act(() => {
      result.current.mutate({ name: 's1', enabled: false });
    });

    // Optimistic update applies synchronously in onMutate.
    await waitFor(() => {
      const cached = client.getQueryData<EffectiveServerList>(queryKeys.mcp.workspace(WS));
      expect(cached?.servers[0].enabled).toBe(false);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it('rolls back the optimistic update on error', async () => {
    const client = makeClient();
    client.setQueryData(queryKeys.mcp.workspace(WS), makeList([makeServer('s1', true)]));
    (setWorkspaceMcpServerEnabled as Mock).mockRejectedValue(new Error('boom'));

    const { result } = renderHook(() => useToggleWorkspaceMcpServer(WS), { wrapper: wrapperFor(client) });

    act(() => {
      result.current.mutate({ name: 's1', enabled: false });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    // After rollback the cached row is back to enabled=true.
    const cached = client.getQueryData<EffectiveServerList>(queryKeys.mcp.workspace(WS));
    expect(cached?.servers[0].enabled).toBe(true);
  });
});

describe('mcp mutations — invalidation', () => {
  it('add invalidates the workspace list', async () => {
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries');
    (addWorkspaceMcpServer as Mock).mockResolvedValue({ name: 's2', source: 'workspace', enabled: true });

    const { result } = renderHook(() => useAddWorkspaceMcpServer(WS), { wrapper: wrapperFor(client) });
    await act(async () => {
      await result.current.mutateAsync({ from_template: 'tmpl' });
    });

    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.mcp.workspace(WS) });
  });

  it('delete invalidates the workspace list', async () => {
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries');
    (deleteWorkspaceMcpServer as Mock).mockResolvedValue({ ok: true });

    const { result } = renderHook(() => useDeleteWorkspaceMcpServer(WS), { wrapper: wrapperFor(client) });
    await act(async () => {
      await result.current.mutateAsync('s1');
    });

    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.mcp.workspace(WS) });
  });

  it('catalog create invalidates the catalog list', async () => {
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries');
    (createMcpCatalogServer as Mock).mockResolvedValue({ name: 't1' });

    const { result } = renderHook(() => useCreateMcpCatalogServer(), { wrapper: wrapperFor(client) });
    await act(async () => {
      await result.current.mutateAsync({ name: 't1', transport: 'stdio', command: 'npx' });
    });

    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.mcp.catalog() });
  });
});
