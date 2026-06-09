import React, { useState } from 'react';
import { Plus, Loader2, ServerCog } from 'lucide-react';
import {
  useWorkspaceMcpServers,
  useAddWorkspaceMcpServer,
  useUpdateWorkspaceMcpServer,
  useToggleWorkspaceMcpServer,
  useDeleteWorkspaceMcpServer,
  useDiscoverWorkspaceMcpServer,
} from '@/hooks/useMcpServers';
import { getVaultSecrets, type EffectiveServer, type McpServerInput } from '../../utils/api';
import { McpServerRow } from './McpServerRow';
import { McpServerModal } from './McpServerModal';
import { TemplatesView } from './TemplatesView';

/**
 * The "MCP" tab in the workspace settings panel. Segmented control switches
 * between the effective per-workspace list and the user's template catalog.
 *
 * After any successful mutation (add/edit/toggle/delete/discover-driven change)
 * the affected row shows a transient "not synced — applies shortly" hint, since
 * the backend applies the change on the next agent run (≤30s), not live.
 *
 * `onOpenVaultTab` deep-links to the Vault tab (optionally prefilling a secret
 * name) for the needs_secret "Set up NAME" affordance.
 */

type SubView = 'workspace' | 'templates';

const NOT_SYNCED_MS = 30_000;

interface McpTabProps {
  workspaceId: string;
  /** Deep-link into the Vault tab, optionally with a prefilled secret name. */
  onOpenVaultTab?: (prefillSecretName?: string) => void;
}

export function McpTab({ workspaceId, onOpenVaultTab }: McpTabProps) {
  const [view, setView] = useState<SubView>('workspace');

  const { data, isLoading, error } = useWorkspaceMcpServers(workspaceId);
  const addMutation = useAddWorkspaceMcpServer(workspaceId);
  const updateMutation = useUpdateWorkspaceMcpServer(workspaceId);
  const toggleMutation = useToggleWorkspaceMcpServer(workspaceId);
  const deleteMutation = useDeleteWorkspaceMcpServer(workspaceId);
  const discoverMutation = useDiscoverWorkspaceMcpServer(workspaceId);

  // Vault secret names for the picker (loaded lazily; failure is non-fatal).
  const [secretNames, setSecretNames] = useState<string[]>([]);
  React.useEffect(() => {
    let cancelled = false;
    getVaultSecrets(workspaceId)
      .then((secrets: Array<{ name: string }>) => {
        if (!cancelled) setSecretNames(secrets.map((s) => s.name));
      })
      .catch(() => { if (!cancelled) setSecretNames([]); });
    return () => { cancelled = true; };
  }, [workspaceId]);

  function refetchSecretNames() {
    getVaultSecrets(workspaceId)
      .then((secrets: Array<{ name: string }>) => setSecretNames(secrets.map((s) => s.name)))
      .catch(() => {});
  }

  // Transient "not synced" tracking — a set of server names recently mutated.
  const [notSynced, setNotSynced] = useState<Record<string, number>>({});
  const flagNotSynced = React.useCallback((name: string) => {
    setNotSynced((prev) => ({ ...prev, [name]: Date.now() }));
    setTimeout(() => {
      setNotSynced((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }, NOT_SYNCED_MS);
  }, []);

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<EffectiveServer | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [togglingName, setTogglingName] = useState<string | null>(null);
  const [deletingName, setDeletingName] = useState<string | null>(null);

  const servers = data?.servers ?? [];
  const sandboxRunning = data?.sandbox_running ?? false;
  const maxServers = data?.max_servers ?? 20;
  const workspaceCount = servers.filter((s) => s.origin === 'workspace').length;
  const atCap = workspaceCount >= maxServers;
  const workspaceServerNames = new Set(servers.map((s) => s.name));

  async function handleSubmit(body: McpServerInput) {
    setSubmitError(null);
    try {
      if (editing) {
        await updateMutation.mutateAsync({ name: editing.name, body });
      } else {
        await addMutation.mutateAsync(body);
      }
      flagNotSynced(body.name);
      setModalOpen(false);
      setEditing(null);
    } catch (err) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string };
      setSubmitError(e?.response?.data?.detail || e?.message || 'Failed to save server');
    }
  }

  async function handleToggle(server: EffectiveServer, enabled: boolean) {
    setTogglingName(server.name);
    try {
      await toggleMutation.mutateAsync({ name: server.name, enabled });
      flagNotSynced(server.name);
    } finally {
      setTogglingName(null);
    }
  }

  async function handleDelete(server: EffectiveServer) {
    setDeletingName(server.name);
    try {
      await deleteMutation.mutateAsync(server.name);
      flagNotSynced(server.name);
    } finally {
      setDeletingName(null);
    }
  }

  async function handleAddFromTemplate(templateName: string) {
    await addMutation.mutateAsync({ from_template: templateName });
    flagNotSynced(templateName);
    setView('workspace');
  }

  async function handleDiscoverFromModal(body: McpServerInput) {
    // The modal's "Test connection" needs the server persisted first. If it's a
    // new server, the row-level discover is the right entry point — here we run
    // discovery against the existing server name (edits) and surface results.
    return discoverMutation.mutateAsync(body.name);
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Segmented control */}
      <div
        className="inline-flex self-start gap-1 p-0.5 rounded-md"
        style={{ backgroundColor: 'var(--color-bg-card)' }}
      >
        {([['workspace', 'This Workspace'], ['templates', 'Templates']] as const).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setView(key)}
            className="px-3 py-1.5 text-xs font-medium rounded"
            style={{
              color: view === key ? 'var(--color-text-on-accent)' : 'var(--color-text-tertiary)',
              backgroundColor: view === key ? 'var(--color-accent-primary)' : 'transparent',
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {view === 'workspace' ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ServerCog className="h-4 w-4" style={{ color: 'var(--color-accent-primary)' }} />
              <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
                MCP servers
              </span>
              <span className="text-xs px-1.5 py-0.5 rounded" style={{ color: 'var(--color-text-tertiary)', backgroundColor: 'var(--color-bg-card)' }}>
                {workspaceCount} / {maxServers}
              </span>
            </div>
            <button
              type="button"
              onClick={() => { setEditing(null); setSubmitError(null); setModalOpen(true); }}
              disabled={atCap}
              title={atCap ? `At ${maxServers}/${maxServers} — remove one first` : undefined}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition-colors disabled:opacity-50"
              style={{ color: 'var(--color-text-on-accent)', backgroundColor: 'var(--color-accent-primary)' }}
            >
              <Plus className="h-3 w-3" />
              Add server
            </button>
          </div>

          {!sandboxRunning && (
            <div className="text-[11px] p-2 rounded" style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-text-tertiary)' }}>
              Workspace is stopped. New servers stay pending until the workspace starts and discovery runs.
            </div>
          )}

          {error ? (
            <div className="text-xs p-2 rounded" style={{ backgroundColor: 'var(--color-bg-card)', color: 'var(--color-loss)' }}>
              {(error as { message?: string })?.message || 'Failed to load MCP servers'}
            </div>
          ) : isLoading ? (
            <div className="flex flex-col gap-2">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-14 rounded-lg animate-pulse" style={{ backgroundColor: 'var(--color-bg-card)' }} />
              ))}
            </div>
          ) : servers.length === 0 ? (
            <div className="py-8 text-center text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
              No MCP servers. Add one or copy a template.
            </div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {servers.map((server) => (
                <McpServerRow
                  key={server.name}
                  server={server}
                  toggling={togglingName === server.name || deletingName === server.name}
                  showNotSynced={notSynced[server.name] !== undefined}
                  onToggle={(enabled) => handleToggle(server, enabled)}
                  onEdit={() => { setEditing(server); setSubmitError(null); setModalOpen(true); }}
                  onDiscover={async () => {
                    await discoverMutation.mutateAsync(server.name);
                    flagNotSynced(server.name);
                  }}
                  onDelete={() => handleDelete(server)}
                  onSetupSecret={(name) => onOpenVaultTab?.(name)}
                />
              ))}
            </div>
          )}
        </div>
      ) : (
        <TemplatesView
          workspaceId={workspaceId}
          secretNames={secretNames}
          onAddToWorkspace={handleAddFromTemplate}
          workspaceServerNames={workspaceServerNames}
        />
      )}

      {modalOpen && (
        <McpServerModal
          workspaceId={workspaceId}
          secretNames={secretNames}
          initial={editing}
          allowDiscover={!!editing && sandboxRunning}
          onClose={() => { setModalOpen(false); setEditing(null); }}
          onSubmit={handleSubmit}
          onDiscover={editing ? handleDiscoverFromModal : undefined}
          onSecretCreated={refetchSecretNames}
          saving={addMutation.isPending || updateMutation.isPending}
          submitError={submitError}
        />
      )}

      {/* Discovery loading hint (row kebab "Test connection") */}
      {discoverMutation.isPending && (
        <div className="inline-flex items-center gap-1.5 text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
          <Loader2 className="h-3 w-3 animate-spin" />
          Testing connection…
        </div>
      )}
    </div>
  );
}
