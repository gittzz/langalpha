import React, { useState, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { motion, AnimatePresence } from 'framer-motion';
import { DndContext, DragOverlay, closestCenter, PointerSensor, MeasuringStrategy, useSensor, useSensors } from '@dnd-kit/core';
import type { DragStartEvent, DragEndEvent } from '@dnd-kit/core';
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  ChevronRight, Folder, FolderOpen, Zap, Pin, MessageSquareText,
  Check, Circle, Loader2, X, ChevronsDown, MoreHorizontal, SquarePen, Pencil,
} from 'lucide-react';
import { ScrollArea } from '../../../components/ui/scroll-area';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu';
import { useIsMobile } from '@/hooks/useIsMobile';
import './NavigationPanel.css';

interface WorkspaceEntry {
  workspace_id: string;
  name?: string;
  status?: string;
  is_pinned?: boolean;
  [key: string]: unknown;
}

interface ThreadEntry {
  thread_id: string;
  title?: string;
  first_query_content?: string;
  [key: string]: unknown;
}

interface ThreadsData {
  threads: ThreadEntry[];
  loading?: boolean;
  total?: number;
}

interface AgentMessage {
  role: string;
  isStreaming?: boolean;
  toolCallProcesses?: Record<string, { isInProgress?: boolean }>;
  [key: string]: unknown;
}

interface AgentEntry {
  id: string;
  name: string;
  description?: string;
  isMainAgent?: boolean;
  status?: string;
  messages?: AgentMessage[];
  [key: string]: unknown;
}

interface NavigationPanelProps {
  workspaces: WorkspaceEntry[];
  workspaceThreads: Record<string, ThreadsData>;
  currentWorkspaceId?: string | null;
  currentThreadId?: string | null;
  agents?: AgentEntry[];
  activeAgentId?: string | null;
  expandWorkspace: (wsId: string) => void;
  onSelectAgent: (agentId: string) => void;
  onRemoveAgent?: (agentId: string) => void;
  onNavigateThread: (wsId: string, threadId: string) => void;
  hasMore?: boolean;
  onLoadMore?: () => void;
  /** Fetch the next page of threads for a workspace ("Show more" row). */
  onLoadMoreThreads?: (wsId: string) => void;
  /** Drag-reorder handler: persist `activeId` dropped onto `overId`'s slot. */
  onReorderWorkspace?: (activeId: string, overId: string) => void;
  /** Pin/unpin a workspace to the top of the list (options menu). */
  onPinWorkspace?: (wsId: string, pinned: boolean) => void;
  /** Persist a workspace rename (options menu → inline edit). */
  onRenameWorkspace?: (wsId: string, name: string) => void;
  /** Open a fresh thread in the given workspace. */
  onNewThread?: (wsId: string) => void;
  /** Right-aligned controls (pin, minimize) rendered in a header row above the list. */
  headerActions?: React.ReactNode;
}

/**
 * Sortable wrapper for one workspace section (header row + thread sub-list).
 * The header row receives the drag listeners via the render prop, which also
 * gets `isDragging` so the section can collapse to header height while lifted.
 *
 * Translate-only (not Transform) so displaced siblings never pick up the
 * scaleX/scaleY that distorts variable-height rows; the lifted item itself is
 * hidden here and shown as a fixed-size DragOverlay chip instead.
 */
function SortableWorkspace({ wsId, disabled, children }: {
  wsId: string;
  disabled: boolean;
  children: (args: { dragHandleProps: Record<string, unknown>; isDragging: boolean }) => React.ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: wsId, disabled });
  const style: React.CSSProperties = {
    transform: CSS.Translate.toString(transform),
    transition,
    opacity: isDragging ? 0 : 1,
    position: 'relative',
    zIndex: isDragging ? 5 : undefined,
  };
  return (
    <div ref={setNodeRef} style={style}>
      {children({ dragHandleProps: disabled ? {} : { ...attributes, ...listeners }, isDragging })}
    </div>
  );
}

/**
 * NavigationPanel -- hover-triggered overlay sidebar showing
 * Workspace -> Thread -> Agent hierarchy.
 *
 * Follows the collapsible tree pattern from FilePanel's DirectoryNode:
 * ChevronRight/Down toggles, indented rows, Lucide icons throughout.
 */
// Expansion state shared across panel instances (one mounts per cached
// ChatView), so user-opened folders survive thread switches within a session.
const _expandedWorkspaces = new Set<string>();
const _expandedThreads = new Set<string>();

export function resetNavPanelExpansion() {
  _expandedWorkspaces.clear();
  _expandedThreads.clear();
}

// Forget a deleted workspace so the mount-effect doesn't re-expand it (and fire
// a spurious threads 404) on the next panel remount. Only deletion is a safe
// prune signal — `workspaces` is a paged/limited slice, so absence there can
// mean "scrolled out", not "gone".
export function forgetNavPanelExpansion(workspaceId: string) {
  _expandedWorkspaces.delete(workspaceId);
}

function NavigationPanel({
  workspaces,
  workspaceThreads,
  currentWorkspaceId,
  currentThreadId,
  agents,
  activeAgentId,
  expandWorkspace,
  onSelectAgent,
  onRemoveAgent,
  onNavigateThread,
  hasMore,
  onLoadMore,
  onLoadMoreThreads,
  onReorderWorkspace,
  onPinWorkspace,
  onRenameWorkspace,
  onNewThread,
  headerActions,
}: NavigationPanelProps) {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  // 8px activation distance (same as the gallery's reorder mode) keeps plain
  // clicks toggling expand/collapse instead of starting a drag.
  const dndSensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }));
  // Id of the workspace currently being dragged — drives the DragOverlay chip.
  const [activeDragId, setActiveDragId] = useState<string | null>(null);
  // Expanded workspaces/threads. Backed by module-level sets because one
  // NavigationPanel mounts per cached ChatView instance — without them, every
  // thread switch would remount the panel and collapse the folders the user
  // opened. Current workspace/thread are expanded by default. Resets on reload.
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<Set<string>>(() => {
    if (currentWorkspaceId) _expandedWorkspaces.add(currentWorkspaceId);
    return new Set(_expandedWorkspaces);
  });
  const [expandedThreads, setExpandedThreads] = useState<Set<string>>(() => {
    if (currentThreadId && currentThreadId !== '__default__') _expandedThreads.add(currentThreadId);
    return new Set(_expandedThreads);
  });

  // Repopulate thread lists for folders that were already open — the thread
  // data lives in per-instance hook state, so a fresh mount must re-request it
  // (served from the React Query cache when warm).
  React.useEffect(() => {
    _expandedWorkspaces.forEach((wsId) => expandWorkspace(wsId));
    // Mount-only: expandWorkspace is stable and re-running on its identity churn would be redundant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep current workspace and thread expanded when they change
  React.useEffect(() => {
    if (currentWorkspaceId) {
      _expandedWorkspaces.add(currentWorkspaceId);
      setExpandedWorkspaces((prev) => {
        if (prev.has(currentWorkspaceId)) return prev;
        const next = new Set(prev);
        next.add(currentWorkspaceId);
        return next;
      });
      expandWorkspace(currentWorkspaceId);
    }
  }, [currentWorkspaceId, expandWorkspace]);

  React.useEffect(() => {
    if (currentThreadId && currentThreadId !== '__default__') {
      _expandedThreads.add(currentThreadId);
      setExpandedThreads((prev) => {
        if (prev.has(currentThreadId)) return prev;
        const next = new Set(prev);
        next.add(currentThreadId);
        return next;
      });
    }
  }, [currentThreadId]);

  const toggleWorkspace = useCallback((wsId: string) => {
    if (_expandedWorkspaces.has(wsId)) {
      _expandedWorkspaces.delete(wsId);
    } else {
      _expandedWorkspaces.add(wsId);
    }
    setExpandedWorkspaces(new Set(_expandedWorkspaces));
    // Lazy-load threads when expanding -- called outside updater to avoid setState-during-render warning.
    // expandWorkspace is a no-op when data is already cached, so calling unconditionally is safe.
    expandWorkspace(wsId);
  }, [expandWorkspace]);

  const toggleThread = useCallback((threadId: string) => {
    if (_expandedThreads.has(threadId)) {
      _expandedThreads.delete(threadId);
    } else {
      _expandedThreads.add(threadId);
    }
    setExpandedThreads(new Set(_expandedThreads));
  }, []);

  // Inline workspace rename — the name span becomes a text input while editing.
  // Refs shadow the editing state so the blur/Enter commit reads fresh values and
  // stays idempotent: Enter clears the id, the trailing blur then no-ops.
  const [renamingWsId, setRenamingWsId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const renamingWsIdRef = useRef<string | null>(null);
  const renameValueRef = useRef('');
  const renameOriginalRef = useRef('');
  const renameInputRef = useRef<HTMLInputElement>(null);

  const startRename = useCallback((wsId: string, currentName: string) => {
    renamingWsIdRef.current = wsId;
    renameOriginalRef.current = currentName;
    renameValueRef.current = currentName;
    setRenameValue(currentName);
    setRenamingWsId(wsId);
  }, []);

  const cancelRename = useCallback(() => {
    renamingWsIdRef.current = null;
    setRenamingWsId(null);
  }, []);

  const commitRename = useCallback(() => {
    const wsId = renamingWsIdRef.current;
    if (!wsId) return; // already committed — the trailing blur after Enter is a no-op
    renamingWsIdRef.current = null;
    setRenamingWsId(null);
    const name = renameValueRef.current.trim();
    if (name && name !== renameOriginalRef.current) onRenameWorkspace?.(wsId, name);
  }, [onRenameWorkspace]);

  // Focus + select the rename input once it mounts. rAF defers past Radix's
  // focus-restore-to-trigger when the rename is launched from the options menu.
  React.useEffect(() => {
    if (!renamingWsId) return;
    const raf = requestAnimationFrame(() => {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    });
    return () => cancelAnimationFrame(raf);
  }, [renamingWsId]);

  const handleWorkspaceDragStart = useCallback((event: DragStartEvent) => {
    setActiveDragId(String(event.active.id));
  }, []);

  const handleWorkspaceDragEnd = useCallback((event: DragEndEvent) => {
    setActiveDragId(null);
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    onReorderWorkspace?.(String(active.id), String(over.id));
  }, [onReorderWorkspace]);

  const handleWorkspaceDragCancel = useCallback(() => setActiveDragId(null), []);

  const activeDragWs = activeDragId ? workspaces.find((ws) => ws.workspace_id === activeDragId) : null;

  // Derive agent status for display
  const getAgentStatus = useCallback((agent: AgentEntry): string => {
    if (agent.isMainAgent) return 'active';
    const messages = agent.messages || [];
    if (agent.status === 'completed') return 'completed';
    if (messages.length === 0) return 'initializing';
    const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
    const isStreaming = lastAssistant?.isStreaming === true;
    const hasInProgressTool = lastAssistant?.toolCallProcesses
      ? Object.values(lastAssistant.toolCallProcesses).some((p) => p.isInProgress)
      : false;
    if (isStreaming || hasInProgressTool) return 'active';
    if (lastAssistant && lastAssistant.isStreaming === false) return 'completed';
    return agent.status || 'pending';
  }, []);

  return (
    <div
      className="nav-panel h-full flex flex-col"
    >
      {headerActions && (
        <div className="nav-panel-header">
          {headerActions}
        </div>
      )}
      <ScrollArea className="flex-1">
        <div className="py-2">
          <DndContext
            sensors={dndSensors}
            collisionDetection={closestCenter}
            measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
            onDragStart={handleWorkspaceDragStart}
            onDragEnd={handleWorkspaceDragEnd}
            onDragCancel={handleWorkspaceDragCancel}
          >
          <SortableContext items={workspaces.map((ws) => ws.workspace_id)} strategy={verticalListSortingStrategy}>
          {workspaces.map((ws) => {
            const wsId = ws.workspace_id;
            const isExpanded = expandedWorkspaces.has(wsId);
            const isFlash = ws.status === 'flash';
            const isPinned = ws.is_pinned;
            const isCurrent = wsId === currentWorkspaceId;
            const threadsData = workspaceThreads[wsId];
            const allThreads = threadsData?.threads || [];
            const threads = isFlash ? allThreads.slice(0, 3) : allThreads;
            const threadsLoading = threadsData?.loading || false;
            const isRenaming = renamingWsId === wsId;
            // Pin / rename / new-thread aren't offered on the flash workspace
            // (shared, immutable) — mirrors the gallery hiding its card menu there.
            const showWsActions = !isFlash && (onNewThread || onPinWorkspace || onRenameWorkspace);

            return (
              <SortableWorkspace key={wsId} wsId={wsId} disabled={isFlash || isMobile || !onReorderWorkspace}>
                {({ dragHandleProps, isDragging: isThisDragging }) => (<>
                {/* Workspace row — doubles as the drag handle for reordering.
                    While renaming, click-to-toggle and drag are suppressed so the
                    inline input owns the row. */}
                <div
                  className="nav-panel-row group"
                  style={{ paddingLeft: 10 }}
                  onClick={() => { if (!isRenaming) toggleWorkspace(wsId); }}
                  {...(isRenaming ? {} : dragHandleProps)}
                >
                  {isFlash
                    ? <Zap className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                    : isPinned
                      ? <Pin className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                      : isExpanded
                        ? <FolderOpen className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                        : <Folder className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                  }
                  {isRenaming ? (
                    <input
                      ref={renameInputRef}
                      className="text-sm font-medium bg-transparent outline-none border-b flex-1 min-w-0"
                      style={{ color: 'var(--color-text-primary)', borderColor: 'var(--color-border-muted)' }}
                      value={renameValue}
                      onChange={(e) => { setRenameValue(e.target.value); renameValueRef.current = e.target.value; }}
                      onClick={(e) => e.stopPropagation()}
                      onPointerDown={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
                        else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
                      }}
                      onBlur={commitRename}
                      aria-label={t('workspace.rename')}
                    />
                  ) : (
                    <>
                      <span
                        className="text-sm font-medium truncate"
                        style={{ color: isCurrent ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
                      >
                        {ws.name || 'Workspace'}
                      </span>
                      {/* initial={false}: thread switches remount the panel; the
                          chevron must render at its resting angle, not animate to it.
                          Hidden until the row is hovered (always visible on touch). */}
                      <motion.span
                        className={`flex-shrink-0 flex items-center ${isMobile ? '' : 'opacity-0 group-hover:opacity-100 transition-opacity'}`}
                        initial={false}
                        animate={{ rotate: isExpanded ? 90 : 0 }}
                        transition={{ duration: 0.15, ease: 'easeOut' }}
                      >
                        <ChevronRight className="h-4 w-4" style={{ color: 'var(--color-text-tertiary)' }} />
                      </motion.span>
                      {/* Right-aligned row actions: new thread + options (pin / rename).
                          Hover-revealed on desktop, always shown on touch. */}
                      {showWsActions && (
                        <div className={`flex items-center gap-0.5 ml-auto flex-shrink-0 ${isMobile ? '' : 'opacity-0 group-hover:opacity-100 transition-opacity'}`}>
                          {onNewThread && (
                            <button
                              type="button"
                              onPointerDown={(e) => e.stopPropagation()}
                              onClick={(e) => { e.stopPropagation(); onNewThread(wsId); }}
                              className="flex items-center justify-center p-0.5 rounded bg-transparent border-none cursor-pointer hover:bg-[var(--color-border-muted)]"
                              title={t('nav.newThread')}
                              aria-label={t('nav.newThread')}
                            >
                              <SquarePen className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
                            </button>
                          )}
                          {(onPinWorkspace || onRenameWorkspace) && (
                            <DropdownMenu modal={false}>
                              <DropdownMenuTrigger asChild>
                                <button
                                  type="button"
                                  onPointerDown={(e) => e.stopPropagation()}
                                  onClick={(e) => e.stopPropagation()}
                                  className="flex items-center justify-center p-0.5 rounded bg-transparent border-none cursor-pointer hover:bg-[var(--color-border-muted)]"
                                  title={t('workspace.options')}
                                  aria-label={t('workspace.options')}
                                >
                                  <MoreHorizontal className="h-3.5 w-3.5" style={{ color: 'var(--color-text-tertiary)' }} />
                                </button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end" sideOffset={4} onClick={(e) => e.stopPropagation()}>
                                {onPinWorkspace && (
                                  <DropdownMenuItem onSelect={() => onPinWorkspace(wsId, !isPinned)}>
                                    <Pin className="h-4 w-4" />
                                    {isPinned ? t('workspace.unpin') : t('workspace.pinToTop')}
                                  </DropdownMenuItem>
                                )}
                                {onRenameWorkspace && (
                                  <DropdownMenuItem onSelect={() => startRename(wsId, ws.name || '')}>
                                    <Pencil className="h-4 w-4" />
                                    {t('workspace.rename')}
                                  </DropdownMenuItem>
                                )}
                              </DropdownMenuContent>
                            </DropdownMenu>
                          )}
                        </div>
                      )}
                      {threadsLoading && (
                        <Loader2 className={`h-3.5 w-3.5 animate-spin flex-shrink-0 ${showWsActions ? '' : 'ml-auto'}`} style={{ color: 'var(--color-text-tertiary)' }} />
                      )}
                    </>
                  )}
                </div>

                {/* Threads under this workspace — animated expand/collapse.
                    Hidden while this section is the one being dragged so the
                    lifted item shrinks to header height (clean gap), and the
                    DragOverlay chip below carries the visual instead. */}
                <AnimatePresence initial={false}>
                  {isExpanded && !isThisDragging && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2, ease: 'easeInOut' }}
                      style={{ overflow: 'hidden' }}
                    >
                    {!threadsLoading && threads.length === 0 && (
                      <div
                        className="text-xs px-2 py-1"
                        style={{ paddingLeft: 44, color: 'var(--color-icon-muted)' }}
                      >
                        No conversations yet
                      </div>
                    )}
                    {threads.map((thread) => {
                      const tid = thread.thread_id;
                      const isCurrentThread = tid === currentThreadId;
                      const isThreadExpanded = expandedThreads.has(tid);
                      const subagents = agents?.filter((a) => !a.isMainAgent) || [];
                      const hasSubagents = isCurrentThread && subagents.length > 0;
                      const title = thread.title || thread.first_query_content?.slice(0, 40) || 'Untitled';

                      return (
                        <div key={tid}>
                          {/* Thread row */}
                          <div
                            className={`nav-panel-row group ${isCurrentThread ? 'nav-panel-row-active' : ''}`}
                            style={{ paddingLeft: 28 }}
                            onClick={() => {
                              if (isCurrentThread) {
                                // Toggle agents expand for current thread
                                if (hasSubagents) toggleThread(tid);
                              } else {
                                onNavigateThread(wsId, tid);
                              }
                            }}
                          >
                            <MessageSquareText className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                            <span
                              className="text-sm truncate"
                              style={{ color: isCurrentThread ? 'var(--color-text-primary)' : 'var(--color-text-secondary)' }}
                              title={title}
                            >
                              {title}
                            </span>
                            {/* Expand-agents chevron — immediately right of the thread
                                name, mirroring the workspace row. Only on the current
                                thread when it has subagents. Hover-revealed (always shown
                                on touch); rotates 90° when expanded. initial={false}:
                                thread switches remount the panel, so the chevron renders
                                at its resting angle. */}
                            {hasSubagents && (
                              <motion.button
                                type="button"
                                onClick={(e) => { e.stopPropagation(); toggleThread(tid); }}
                                className={`flex-shrink-0 flex items-center p-0 bg-transparent border-none cursor-pointer ${isMobile ? '' : 'opacity-0 group-hover:opacity-100 transition-opacity'}`}
                                initial={false}
                                animate={{ rotate: isThreadExpanded ? 90 : 0 }}
                                transition={{ duration: 0.15, ease: 'easeOut' }}
                                aria-label={isThreadExpanded ? 'Collapse agents' : 'Expand agents'}
                              >
                                <ChevronRight className="h-4 w-4" style={{ color: 'var(--color-text-tertiary)' }} />
                              </motion.button>
                            )}
                          </div>

                          {/* Agent rows -- only when subagents exist, for current thread when expanded */}
                          {hasSubagents && isThreadExpanded && (
                            <div className="nav-panel-agent-group">
                              {agents!.map((agent) => {
                                const isMainAgent = agent.isMainAgent;
                                const isSelected = activeAgentId === agent.id;
                                const status = getAgentStatus(agent);
                                const isActive = status === 'active';
                                const isCompleted = status === 'completed';

                                const trimmedDescription = typeof agent.description === 'string' ? agent.description.trim() : '';
                                const rowLabel = !isMainAgent && trimmedDescription
                                  ? trimmedDescription
                                  : agent.name;

                                return (
                                  <div
                                    key={agent.id}
                                    data-testid="agent-row"
                                    data-agent-role={isMainAgent ? 'main' : 'sub'}
                                    className={`nav-panel-agent-row group ${isActive && !isMainAgent ? 'nav-panel-agent-pulse' : ''}${isSelected ? ' is-selected' : ''}`}
                                    style={{
                                      backgroundColor: isSelected ? 'var(--color-border-muted)' : undefined,
                                    }}
                                    onClick={() => onSelectAgent(agent.id)}
                                  >
                                    {/* Hierarchy indicator: subagents render `└─` to descend visually under the main agent's text column */}
                                    {!isMainAgent && (
                                      <span aria-hidden="true" className="nav-panel-agent-glyph text-xs">
                                        └─
                                      </span>
                                    )}
                                    {/* Agent label: subagent description when available, else fallback name */}
                                    <span
                                      className="text-xs truncate"
                                      style={{ color: isSelected ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)' }}
                                      title={rowLabel}
                                    >
                                      {rowLabel}
                                    </span>
                                    {/* Status badge */}
                                    {!isMainAgent && (
                                      <span className="flex-shrink-0 ml-auto flex items-center">
                                        {isCompleted ? (
                                          <Check className="h-3 w-3" style={{ color: 'var(--color-text-tertiary)' }} />
                                        ) : isActive ? (
                                          <Loader2 className="h-3 w-3 animate-spin" style={{ color: 'var(--color-text-tertiary)' }} />
                                        ) : (
                                          <Circle className="h-3 w-3" style={{ color: 'var(--color-icon-muted)' }} />
                                        )}
                                      </span>
                                    )}
                                    {/* Remove button -- non-main agents only, on hover */}
                                    {!isMainAgent && (
                                      <button
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          onRemoveAgent?.(agent.id);
                                        }}
                                        className={`flex-shrink-0 p-0 bg-transparent border-none cursor-pointer transition-opacity ${isMobile ? 'opacity-60' : 'opacity-0 group-hover:opacity-100'}`}
                                        title="Remove agent"
                                      >
                                        <X className="h-3 w-3" style={{ color: 'var(--color-text-tertiary)' }} />
                                      </button>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {/* Show more — next page of threads for this workspace */}
                    {!isFlash && onLoadMoreThreads && typeof threadsData?.total === 'number'
                      && allThreads.length < threadsData.total && !threadsLoading && (
                      <div
                        className="nav-panel-row"
                        style={{ paddingLeft: 44 }}
                        onClick={(e) => { e.stopPropagation(); onLoadMoreThreads(wsId); }}
                      >
                        <ChevronsDown className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                        <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                          {t('nav.showMore')}
                        </span>
                      </div>
                    )}
                    </motion.div>
                  )}
                </AnimatePresence>
                </>)}
              </SortableWorkspace>
            );
          })}
          </SortableContext>
          {/* Fixed-size lift preview — a clean header chip that follows the
              cursor, so the dragged section's real height never distorts. */}
          <DragOverlay dropAnimation={null}>
            {activeDragWs ? (
              <div className="nav-panel nav-panel-drag-chip">
                <div className="nav-panel-row" style={{ paddingLeft: 10 }}>
                  {activeDragWs.is_pinned
                    ? <Pin className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                    : <Folder className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
                  }
                  <span className="text-sm font-medium truncate" style={{ color: 'var(--color-text-primary)' }}>
                    {activeDragWs.name || 'Workspace'}
                  </span>
                </div>
              </div>
            ) : null}
          </DragOverlay>
          </DndContext>
          {hasMore && (
            <div
              className="nav-panel-row"
              style={{ paddingLeft: 10, justifyContent: 'center' }}
              onClick={onLoadMore}
            >
              <ChevronsDown className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
              <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                {t('nav.loadAll')}
              </span>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}

export default NavigationPanel;
