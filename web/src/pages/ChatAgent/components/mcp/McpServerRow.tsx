import React from 'react';
import { MoreVertical, Pencil, Zap, Trash2, Server, KeyRound, Loader2 } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import { McpStatusPill, NotSyncedHint } from './McpStatusPill';
import type { EffectiveServer } from '../../utils/api';

/**
 * One row in the effective per-workspace MCP list.
 *
 * - Origin badge (builtin / workspace).
 * - Enabled toggle (the only interactive control for builtins).
 * - Tool count + status pill.
 * - Kebab menu: Edit / Test connection / Delete — all disabled for builtins.
 * - needs_secret rows surface a "Set up NAME" affordance that deep-links to the
 *   Vault tab with the secret name prefilled.
 * - After a successful mutation the parent flips `showNotSynced`, and the row
 *   shows the transient "applies shortly" hint.
 */

interface McpServerRowProps {
  server: EffectiveServer;
  toggling?: boolean;
  showNotSynced?: boolean;
  onToggle: (enabled: boolean) => void;
  onEdit: () => void;
  onDiscover: () => void;
  onDelete: () => void;
  /** Deep-link to the Vault tab, optionally prefilling a secret name. */
  onSetupSecret: (secretName: string) => void;
}

export function McpServerRow({
  server,
  toggling = false,
  showNotSynced = false,
  onToggle,
  onEdit,
  onDiscover,
  onDelete,
  onSetupSecret,
}: McpServerRowProps) {
  const isBuiltin = server.origin === 'builtin';

  return (
    <div
      className="flex items-start justify-between gap-3 py-2.5 px-3 rounded-lg"
      style={{ backgroundColor: 'var(--color-bg-card)' }}
      data-testid={`mcp-row-${server.name}`}
    >
      <div className="min-w-0 flex flex-col gap-1">
        {/* Name + origin badge */}
        <div className="flex items-center gap-2 flex-wrap">
          <Server className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-accent-primary)' }} />
          <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
            {server.name}
          </span>
          <span
            className="text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide"
            style={{
              color: 'var(--color-text-tertiary)',
              backgroundColor: 'var(--color-bg-default)',
              border: '1px solid var(--color-border-muted)',
            }}
          >
            {isBuiltin ? 'built-in' : 'workspace'}
          </span>
        </div>

        {/* Status + tool count */}
        <div className="flex items-center gap-2 flex-wrap">
          <McpStatusPill status={server.status} enabled={server.enabled} />
          {server.enabled && server.tool_count > 0 && (
            <span className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>
              {server.tool_count} tool{server.tool_count === 1 ? '' : 's'}
            </span>
          )}
          {showNotSynced && <NotSyncedHint />}
        </div>

        {/* Error text */}
        {server.enabled && server.status === 'error' && server.error && (
          <p className="text-[11px] break-words" style={{ color: 'var(--color-loss)' }}>
            {server.error}
          </p>
        )}

        {/* needs_secret → "Set up NAME" affordance(s) */}
        {server.enabled && server.status === 'needs_secret' && server.missing_secrets.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {server.missing_secrets.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => onSetupSecret(name)}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded"
                style={{
                  color: 'var(--color-warning, #d97706)',
                  backgroundColor: 'var(--color-bg-default)',
                  border: '1px dashed var(--color-border-default)',
                }}
              >
                <KeyRound className="h-3 w-3" />
                Set up {name}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 flex-shrink-0">
        {/* Enabled toggle */}
        <button
          type="button"
          role="switch"
          aria-checked={server.enabled}
          aria-label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`}
          disabled={toggling}
          onClick={() => onToggle(!server.enabled)}
          className="relative inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50"
          style={{
            backgroundColor: server.enabled ? 'var(--color-accent-primary)' : 'var(--color-border-muted)',
          }}
        >
          <span
            className="inline-block h-4 w-4 transform rounded-full bg-white transition-transform"
            style={{ transform: server.enabled ? 'translateX(18px)' : 'translateX(2px)' }}
          />
        </button>

        {/* Kebab menu */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="p-1.5 rounded transition-colors hover:bg-foreground/10"
              style={{ color: 'var(--color-text-tertiary)' }}
              aria-label={`Actions for ${server.name}`}
            >
              {toggling ? <Loader2 className="h-4 w-4 animate-spin" /> : <MoreVertical className="h-4 w-4" />}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={!server.editable} onSelect={() => onEdit()}>
              <Pencil className="h-3.5 w-3.5 mr-2" />
              Edit
            </DropdownMenuItem>
            <DropdownMenuItem disabled={isBuiltin} onSelect={() => onDiscover()}>
              <Zap className="h-3.5 w-3.5 mr-2" />
              Test connection
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!server.deletable}
              onSelect={() => onDelete()}
              className="text-red-600 focus:text-red-600"
            >
              <Trash2 className="h-3.5 w-3.5 mr-2" />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
