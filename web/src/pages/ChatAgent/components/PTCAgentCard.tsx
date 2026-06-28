import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion';
import { Check, X, ChevronRight, Loader2, ArrowRight, AlertTriangle, Square } from 'lucide-react';
import { usePTCDispatchStatus, type PTCDispatchStatus } from '../hooks/usePTCDispatchStatus';

interface ProposalData {
  workspace_name?: string;
  question: string;
  status: 'pending' | 'approved' | 'rejected';
  thread_id?: string;
  workspace_id?: string;
  report_back?: boolean;
}

interface FlashContext {
  threadId: string;
  workspaceId: string;
}

interface PTCAgentCardProps {
  proposalData: ProposalData | null;
  onApprove?: (overrides?: { report_back?: boolean }) => void;
  onReject?: () => void;
  flashContext?: FlashContext | null;
}

// Featured-surface visual language (matches ConversationWidget / AIDailyBriefCard).
const PANEL_BG =
  'linear-gradient(135deg, var(--color-bg-card) 0%, var(--color-bg-card) 46%, color-mix(in srgb, var(--color-accent-primary) 10%, var(--color-bg-card)) 100%)';
// Tight 1px accent ring for a live run. We deliberately avoid a wide outer
// box-shadow halo: the card spans the full chat column, which clips horizontal
// overflow, so any outer glow bleeds past / hard-clips at the right margin. The
// "alive" breathing is a contained inset layer instead (clipped by the card).
const RING_LIVE = '0 0 0 1px var(--color-accent-overlay)';

/** Status states that mean the run is still in flight. */
const LIVE: ReadonlySet<PTCDispatchStatus> = new Set(['starting', 'running', 'needs_input']);

function borderColor(status: PTCDispatchStatus): string {
  if (status === 'needs_input') return 'rgba(234,179,8,0.45)';
  if (LIVE.has(status)) return 'var(--color-accent-overlay)';
  return 'var(--color-border-muted)';
}

function fmtElapsed(secs: number): string {
  const m = Math.floor(secs / 60);
  return `${m}:${String(secs % 60).padStart(2, '0')}`;
}

/** Elapsed seconds since `active` first turned true on this mount (best-effort —
 *  a card mounted mid-run counts from mount, not from the true run start). */
function useElapsedSeconds(active: boolean): number {
  const [secs, setSecs] = useState(0);
  const startRef = useRef<number | null>(null);
  useEffect(() => {
    if (!active) {
      startRef.current = null;
      setSecs(0);
      return;
    }
    if (startRef.current === null) startRef.current = Date.now();
    const tick = () => setSecs(Math.floor((Date.now() - (startRef.current as number)) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [active]);
  return secs;
}

const PILL_BASE =
  'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[12px] font-semibold whitespace-nowrap flex-shrink-0';

function StatusPill({ status, elapsed }: { status: PTCDispatchStatus; elapsed: string | null }) {
  if (status === 'running') {
    return (
      <span
        className={PILL_BASE}
        style={{ color: 'var(--color-accent-light)', background: 'var(--color-accent-soft)', border: '1px solid var(--color-accent-overlay)' }}
      >
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full" style={{ background: 'var(--color-accent-light)', opacity: 0.75 }} />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full" style={{ background: 'var(--color-accent-light)' }} />
        </span>
        Working
        {elapsed && <span className="font-mono text-[11px] opacity-80">· {elapsed}</span>}
      </span>
    );
  }
  if (status === 'starting') {
    return (
      <span className={PILL_BASE} style={{ color: 'var(--color-text-tertiary)', background: 'rgba(255,255,255,0.04)', border: '1px solid var(--color-border-muted)' }}>
        <Loader2 className="h-3 w-3 animate-spin" />
        Starting…
      </span>
    );
  }
  if (status === 'needs_input') {
    return (
      <span className={PILL_BASE} style={{ color: 'var(--color-warning)', background: 'var(--color-warning-soft)', border: '1px solid rgba(234,179,8,0.3)' }}>
        <AlertTriangle className="h-3 w-3" />
        Needs input
      </span>
    );
  }
  if (status === 'completed') {
    return (
      <span className={PILL_BASE} style={{ color: 'var(--color-success)', background: 'var(--color-success-soft)', border: '1px solid rgba(34,197,94,0.3)' }}>
        <Check className="h-3 w-3 stroke-[2.5]" />
        Completed
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <span className={PILL_BASE} style={{ color: 'var(--color-loss)', background: 'var(--color-loss-soft)', border: '1px solid rgba(255,56,60,0.3)' }}>
        <AlertTriangle className="h-3 w-3" />
        Failed
      </span>
    );
  }
  return (
    <span className={PILL_BASE} style={{ color: 'var(--color-text-secondary)', background: 'rgba(255,255,255,0.04)', border: '1px solid var(--color-border-muted)' }}>
      <Square className="h-3 w-3" />
      Stopped
    </span>
  );
}

/** Footer hint + open-thread affordance, varying by lifecycle. */
function footerFor(status: PTCDispatchStatus): { hint: string | null; cta: string; ctaWarn?: boolean } {
  switch (status) {
    case 'starting': return { hint: 'Provisioning sandbox…', cta: 'Open thread' };
    case 'running': return { hint: 'Working through the analysis…', cta: 'Open thread' };
    case 'needs_input': return { hint: null, cta: 'Answer & continue', ctaWarn: true };
    case 'completed': return { hint: null, cta: 'Open thread' };
    case 'failed': return { hint: 'Run stopped before finishing', cta: 'View thread' };
    case 'stopped': return { hint: 'Run was stopped', cta: 'View thread' };
  }
}

/**
 * PTCAgentCard — inline HITL card for dispatching a background PTC run.
 *
 *   pending  — question headline + report-back toggle + Approve/Decline
 *   approved — live "mission panel" that tracks the dispatched thread's /status
 *              (starting → running → completed/needs-input/failed/stopped)
 *   rejected — quiet collapsed "Research declined" row
 */
function PTCAgentCard({ proposalData, onApprove, onReject, flashContext }: PTCAgentCardProps) {
  const [collapsed, setCollapsed] = useState(true);
  const [reportBack, setReportBack] = useState(proposalData?.report_back ?? true);
  const navigate = useNavigate();
  const reduceMotion = useReducedMotion();

  const status = proposalData?.status;
  const isApproved = status === 'approved';
  const threadId = proposalData?.thread_id;

  const { status: dispatchStatus } = usePTCDispatchStatus(threadId, isApproved && !!threadId);
  const elapsedSecs = useElapsedSeconds(isApproved && dispatchStatus === 'running');

  if (!proposalData) return null;

  const { workspace_name, question, workspace_id } = proposalData;
  // The kicker names which workspace the run belongs to — PTC runs aren't only
  // "deep research", so we surface the workspace's real name (resolved by the
  // backend). Empty when unknown rather than a fixed placeholder string.
  const eyebrow = workspace_name?.trim() || '';

  const openThread = () => {
    if (!threadId) return;
    navigate(`/chat/t/${threadId}`, {
      state: {
        ...(workspace_id ? { workspaceId: workspace_id } : {}),
        ...(flashContext ? { fromThreadId: flashContext.threadId, fromWorkspaceId: flashContext.workspaceId } : {}),
      },
    });
  };

  // ---------------- Rejected: quiet collapsible row ----------------
  if (status === 'rejected') {
    return (
      <div>
        <button onClick={() => setCollapsed((v) => !v)} className="flex w-full cursor-pointer items-center gap-2 py-1 text-left">
          <motion.div animate={{ rotate: collapsed ? 0 : 90 }} transition={{ duration: 0.2 }}>
            <ChevronRight className="h-3.5 w-3.5 flex-shrink-0" style={{ color: 'var(--color-icon-muted)' }} />
          </motion.div>
          <X className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--color-text-tertiary)' }} />
          <span className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>Research declined</span>
        </button>
        <AnimatePresence initial={false}>
          {!collapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              className="overflow-hidden"
            >
              <div className="pb-1 pl-6 pt-2">
                <div className="rounded-lg px-4 py-3" style={{ border: '1px solid var(--color-border-muted)', opacity: 0.6 }}>
                  {workspace_name && <div className="mb-1 text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>{workspace_name}</div>}
                  <div className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>{question}</div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  // ---------------- Approved: live mission panel ----------------
  if (isApproved) {
    const breathing = dispatchStatus === 'running' && !reduceMotion;
    const live = LIVE.has(dispatchStatus);
    const foot = footerFor(dispatchStatus);
    const elapsed = dispatchStatus === 'running' ? fmtElapsed(elapsedSecs) : null;

    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{
          opacity: 1,
          y: 0,
          boxShadow: live ? RING_LIVE : 'none',
        }}
        transition={{
          opacity: { duration: 0.4, ease: [0.22, 1, 0.36, 1] },
          y: { duration: 0.4, ease: [0.22, 1, 0.36, 1] },
          boxShadow: { duration: 0.3 },
        }}
        className="relative overflow-hidden rounded-xl"
        style={{ border: `1px solid ${borderColor(dispatchStatus)}`, background: PANEL_BG }}
      >
        {/* dot-grid texture */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage: 'radial-gradient(circle at 1px 1px, var(--color-dot-grid) 1px, transparent 0)',
            backgroundSize: '22px 22px',
            opacity: 0.22,
            WebkitMaskImage: 'linear-gradient(180deg, transparent, #000 40%, #000 75%, transparent)',
            maskImage: 'linear-gradient(180deg, transparent, #000 40%, #000 75%, transparent)',
          }}
        />
        {/* corner accent wash */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0"
          style={{ background: 'radial-gradient(ellipse 70% 55% at 96% -8%, rgba(65,97,164,0.30), transparent 60%)' }}
        />
        {/* Breathing accent light while the run is live — kept INSIDE the card so
            it's clipped by overflow-hidden and can't bleed past the chat margin. */}
        {live && (
          <motion.div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{ background: 'radial-gradient(125% 80% at 50% 0%, rgba(65,97,164,0.32), transparent 62%)' }}
            initial={{ opacity: breathing ? 0.45 : 0.65 }}
            animate={{ opacity: breathing ? [0.4, 0.92, 0.4] : 0.65 }}
            transition={breathing ? { duration: 3.4, repeat: Infinity, ease: 'easeInOut' } : { duration: 0.3 }}
          />
        )}

        <div className="relative px-[18px] pb-[14px] pt-[15px]">
          <div className="mb-2.5 flex items-center justify-between gap-3">
            <span className="min-w-0 truncate text-[11px] font-bold uppercase" style={{ letterSpacing: '0.16em', color: 'var(--color-accent-light)' }}>
              {eyebrow}
            </span>
            <StatusPill status={dispatchStatus} elapsed={elapsed} />
          </div>

          <div className="text-[16px] font-semibold leading-snug" style={{ color: 'var(--color-text-primary)', letterSpacing: '-0.005em' }}>
            {question}
          </div>

          {threadId && (
            <div className="mt-3 flex items-center justify-between gap-3 pt-3" style={{ borderTop: '1px solid var(--color-border-muted)' }}>
              <span className="text-[12.5px]" style={{ color: 'var(--color-text-tertiary)' }}>{foot.hint ?? ''}</span>
              <button
                onClick={openThread}
                className="group inline-flex flex-shrink-0 items-center gap-1 text-[13px] font-medium transition-colors"
                style={{ color: foot.ctaWarn ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}
              >
                {foot.cta}
                <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
              </button>
            </div>
          )}
        </div>
      </motion.div>
    );
  }

  // ---------------- Pending: quieter version of the panel ----------------
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
      className="relative overflow-hidden rounded-xl"
      style={{ border: '1px solid var(--color-border-muted)', background: PANEL_BG }}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          backgroundImage: 'radial-gradient(circle at 1px 1px, var(--color-dot-grid) 1px, transparent 0)',
          backgroundSize: '22px 22px',
          opacity: 0.18,
          WebkitMaskImage: 'linear-gradient(180deg, transparent, #000 40%, #000 75%, transparent)',
          maskImage: 'linear-gradient(180deg, transparent, #000 40%, #000 75%, transparent)',
        }}
      />
      <div className="relative px-[18px] pb-[14px] pt-[15px]">
        <div className="mb-2.5 flex items-center justify-between gap-3">
          <span className="min-w-0 truncate text-[11px] font-bold uppercase" style={{ letterSpacing: '0.16em', color: 'var(--color-accent-light)' }}>
            {eyebrow}
          </span>
          <span className="flex-shrink-0 text-[11px] font-medium" style={{ color: 'var(--color-text-quaternary)' }}>Awaiting approval</span>
        </div>

        <div className="text-[16px] font-semibold leading-snug" style={{ color: 'var(--color-text-primary)', letterSpacing: '-0.005em' }}>
          {question}
        </div>

        {/* Report-back toggle */}
        <button
          type="button"
          className="mt-3 flex w-full cursor-pointer items-center justify-between pt-3"
          style={{ borderTop: '1px solid var(--color-border-muted)' }}
          onClick={(e: React.MouseEvent) => { e.stopPropagation(); setReportBack((v) => !v); }}
        >
          <span className="text-[13px]" style={{ color: 'var(--color-text-tertiary)' }}>Report back with a summary</span>
          <div className="relative h-[18px] w-8 rounded-full transition-colors" style={{ background: reportBack ? 'var(--color-accent-light)' : 'rgba(255,255,255,0.12)' }}>
            <div className="absolute left-[3px] top-[3px] h-3 w-3 rounded-full bg-white transition-transform" style={{ transform: reportBack ? 'translateX(14px)' : 'translateX(0)' }} />
          </div>
        </button>

        {/* Actions */}
        <div className="flex items-center gap-2 pt-3">
          <motion.button
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onApprove?.({ report_back: reportBack }); }}
            className="flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors hover:brightness-110"
            style={{ backgroundColor: 'var(--color-btn-primary-bg)', color: 'var(--color-btn-primary-text)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <Check className="h-3.5 w-3.5 stroke-[2.5]" />
            Approve
          </motion.button>
          <motion.button
            onClick={(e: React.MouseEvent) => { e.stopPropagation(); onReject?.(); }}
            className="flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors"
            style={{ backgroundColor: 'var(--color-border-muted)', color: 'var(--color-text-tertiary)' }}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <X className="h-3.5 w-3.5" />
            Decline
          </motion.button>
        </div>
      </div>
    </motion.div>
  );
}

export default PTCAgentCard;
