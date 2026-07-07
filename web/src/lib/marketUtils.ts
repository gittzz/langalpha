/**
 * Shared market utilities used across Dashboard and MarketView.
 */
import { api } from '@/api/client';

interface MarketStatusData {
  market?: string;
  afterHours?: boolean;
  earlyHours?: boolean;
  [key: string]: unknown;
}

interface ExtendedHoursRow {
  earlyTradingChangePercent?: number | null;
  lateTradingChangePercent?: number | null;
  early_trading_change_percent?: number | null;
  late_trading_change_percent?: number | null;
  previousClose?: number | null;
  previous_close?: number | null;
  [key: string]: unknown;
}

interface ExtendedHoursInfo {
  extPct: number | null;
  extLabel: string | null;
  extType: 'pre' | 'post' | null;
  extPrice: number | null;
  extChange: number | null;
  prevClose: number | null;
}

interface StockSearchResult {
  query: string;
  results: unknown[];
  count: number;
}

/**
 * Normalize a symbol to its cache/lookup key spelling: trim, strip a leading
 * `^` index caret, uppercase. The single source for the caret-stripping
 * normalizer that MarketView/Dashboard/quote-layer all need.
 */
export function normalizeIndexKey(symbol: string): string {
  return String(symbol ?? '').trim().replace(/^\^/, '').toUpperCase();
}

/**
 * Compute extended-hours display info from market status and a data row.
 * Accepts both camelCase (snapshot-enriched rows) and snake_case (raw snapshot) field names.
 */
export function getExtendedHoursInfo(
  marketStatus: MarketStatusData | null,
  data: ExtendedHoursRow | null,
  { shortLabels = false } = {},
): ExtendedHoursInfo {
  const isRegularOpen = marketStatus?.market === 'open' && !marketStatus?.afterHours && !marketStatus?.earlyHours;
  const isPreMarket = marketStatus?.earlyHours === true;

  const earlyPct = data?.earlyTradingChangePercent ?? data?.early_trading_change_percent ?? null;
  const latePct = data?.lateTradingChangePercent ?? data?.late_trading_change_percent ?? null;

  const extPct = isPreMarket && earlyPct != null
    ? earlyPct
    : !isRegularOpen && latePct != null
      ? latePct
      : null;

  const extLabel = isPreMarket && earlyPct != null
    ? (shortLabels ? 'PM' : 'Pre-Market')
    : !isRegularOpen && latePct != null
      ? (shortLabels ? 'AH' : 'After-Hours')
      : null;

  const extType: 'pre' | 'post' | null = extLabel ? (isPreMarket && earlyPct != null ? 'pre' : 'post') : null;

  // Compute extended-hours price from previousClose + extPct when available
  const prevClose = data?.previousClose ?? data?.previous_close ?? null;
  const extPrice = extPct != null && prevClose != null
    ? Math.round(prevClose * (1 + extPct / 100) * 100) / 100
    : null;
  const extChange = extPrice != null && prevClose != null
    ? Math.round((extPrice - prevClose) * 100) / 100
    : null;

  return { extPct, extLabel, extType, extPrice, extChange, prevClose };
}

/**
 * Search for stocks by keyword (symbol or company name).
 * GET /api/v1/market-data/search/stocks
 */
export async function searchStocks(query: string, limit = 50): Promise<StockSearchResult> {
  if (!query || !query.trim()) {
    return { query: '', results: [], count: 0 };
  }
  try {
    const params = new URLSearchParams();
    params.append('query', query.trim());
    params.append('limit', String(Math.min(Math.max(1, limit), 100)));
    const { data } = await api.get('/api/v1/market-data/search/stocks', { params });
    return data || { query: query.trim(), results: [], count: 0 };
  } catch (e: unknown) {
    const err = e as { response?: { status?: number; data?: unknown }; message?: string };
    console.error('Search stocks failed:', err?.response?.status, err?.response?.data, err?.message);
    return { query: query.trim(), results: [], count: 0 };
  }
}

/**
 * GET /api/v1/market-data/market-status
 * Returns { market, afterHours, earlyHours, serverTime, exchanges }
 */
export async function fetchMarketStatus({ signal }: { signal?: AbortSignal } = {}): Promise<MarketStatusData> {
  try {
    const { data } = await api.get('/api/v1/market-data/market-status', { signal });
    return data || {};
  } catch (e: unknown) {
    const err = e as { name?: string; message?: string };
    if (err?.name === 'CanceledError' || err?.name === 'AbortError') throw e;
    console.error('[API] fetchMarketStatus failed:', err?.message);
    return {};
  }
}
