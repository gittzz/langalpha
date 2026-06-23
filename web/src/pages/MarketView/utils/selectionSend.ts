/**
 * Builds the chart-selection payload for a MarketView send: the backend
 * `additional_context` items, the message-bubble snapshots, and the outgoing
 * message text (a lone selection's note is promoted when the user typed
 * nothing). Shared by both send paths — the desktop `MarketChatPanel` and the
 * mobile / FAB path in `MarketView` — so the build is defined once. Clearing
 * the selections after send stays the caller's job (the two paths clear at
 * different points in their flow).
 */
import { chartSelectionToContext } from '../../ChatAgent/utils/fileUpload';
import {
  chartSelectionStore,
  promoteSelectionComment,
  toSelectionSnapshot,
  type ChartSelectionSnapshot,
} from '../stores/chartSelectionStore';

export interface ChartSelectionSend {
  /** `additional_context` items to append to the send (empty when none confirmed). */
  contexts: Record<string, unknown>[];
  /** Per-selection snapshots so the sent message renders selection cards. */
  snapshots: ChartSelectionSnapshot[];
  /** Message to send — the lone selection's note when the user typed nothing. */
  outgoingMessage: string;
}

export function buildChartSelectionSend(
  symbol: string,
  timeframe: string,
  message: string,
): ChartSelectionSend {
  const confirmed = chartSelectionStore.getConfirmedFor(symbol, timeframe);
  const contexts: Record<string, unknown>[] = [];
  for (const selection of confirmed) {
    contexts.push(
      ...(chartSelectionToContext(selection, { symbol, timeframe }) as unknown as Record<
        string,
        unknown
      >[]),
    );
  }
  return {
    contexts,
    snapshots: confirmed.map(toSelectionSnapshot),
    outgoingMessage: promoteSelectionComment(message, confirmed),
  };
}
