import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ---------------------------------------------------------------------------
// Hoisted mutable state — lets us vary host mode / tier / preferences per test
// without needing one file per scenario. The getter form keeps the value live
// across re-renders even though vi.mock is hoisted once per file.
// ---------------------------------------------------------------------------
const h = vi.hoisted(() => ({
  platformMode: false,
  accessTier: 1 as number,
  otherPreference: {} as Record<string, unknown>,
  mutateAsync: vi.fn(async () => ({})),
  // Stable references rebuilt only between tests (in beforeEach). Settings has
  // effects keyed on the user / preferences / validModelNames identities; if a
  // mock returned a fresh object each render, those effects would fire every
  // render and (for the prefs sync effect) re-set state into a loop.
  user: null as Record<string, unknown> | null,
  preferences: null as Record<string, unknown> | null,
  validModelNames: new Set<string>(),
}));

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

// Build-time host mode flag — getter so the live value is read on each render.
vi.mock('@/config/hostMode', () => ({
  get isPlatformMode() {
    return h.platformMode;
  },
}));

// Auth — Settings only needs logout() from useAuth.
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ logout: vi.fn() }),
}));

// Current user — access_tier drives the paid-tier gating. Return the stable
// reference so the authUser effect doesn't fire on every render.
vi.mock('@/hooks/useUser', () => ({
  useUser: () => ({ user: h.user, isLoading: false }),
}));

// Preferences — search_provider is loaded from other_preference. Stable ref so
// the prefs-sync effect (setPreferences(prefsData)) doesn't loop.
vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({ preferences: h.preferences, isLoading: false }),
}));

// Update mutation — assert the saved payload here. Stable object so the
// saveModelPrefs useCallback identity stays put across renders.
const mutationStub = { mutateAsync: h.mutateAsync };
vi.mock('@/hooks/useUpdatePreferences', () => ({
  useUpdatePreferences: () => mutationStub,
}));

// Theme — Settings reads preference + setTheme.
vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', preference: 'dark', setTheme: vi.fn() }),
}));

// Models hook — supply the minimal shape Settings + the (stubbed) tier config read.
vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({
    models: {},
    modelAccessMap: {},
    systemDefaults: { fallback_models: [] },
    // Stable Set ref — the stale-model cleanup effect keys on its identity.
    validModelNames: h.validModelNames,
    compactionProfiles: null,
    isLoading: false,
  }),
}));

// Toast.
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// Debounced save — collapse the 500ms debounce to a 0ms macrotask instead of
// firing synchronously. The component updates modelStateRef.current during its
// render commit; saveModelPrefs reads that ref, so the save must run AFTER the
// setState's commit (a macrotask), not inside the same event handler tick — or
// it would read the pre-change value. Mirrors the real debounce ordering.
vi.mock('@/hooks/useDebouncedSave', () => ({
  useDebouncedSave: (saveFn: () => Promise<void>) => ({
    trigger: () => { setTimeout(() => { void saveFn(); }, 0); },
    flush: () => { setTimeout(() => { void saveFn(); }, 0); },
    status: 'idle',
  }),
}));

// Heavy model-tier widget — stub to keep the render light. The search-provider
// select lives outside this component, so a stub is safe. The button exposes
// onPrimaryModelChange so tests can fire an unrelated model-pref save.
vi.mock('@/components/model/ModelTierConfig', () => ({
  ModelTierConfig: (props: { onPrimaryModelChange?: (v: string) => void }) => (
    <div data-testid="model-tier-config-stub">
      <button onClick={() => props.onPrimaryModelChange?.('')}>stub-change-primary</button>
    </div>
  ),
}));

// Dashboard API surface Settings imports — model tab load calls three of these.
vi.mock('@/pages/Dashboard/utils/api', () => ({
  updateCurrentUser: vi.fn(async () => ({})),
  clearPreferences: vi.fn(async () => ({})),
  uploadAvatar: vi.fn(async () => ({ avatar_url: '' })),
  getUserApiKeys: vi.fn(async () => ({ providers: [] })),
  initiateCodexDevice: vi.fn(async () => ({})),
  pollCodexDevice: vi.fn(async () => ({})),
  getCodexOAuthStatus: vi.fn(async () => ({ connected: false })),
  disconnectCodexOAuth: vi.fn(async () => ({})),
  initiateClaudeOAuth: vi.fn(async () => ({})),
  submitClaudeCallback: vi.fn(async () => ({})),
  getClaudeOAuthStatus: vi.fn(async () => ({ connected: false })),
  disconnectClaudeOAuth: vi.fn(async () => ({})),
}));

// Flash workspace — used by preference-modify navigation, never in these tests.
vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getFlashWorkspace: vi.fn(async () => ({ workspace_id: 'ws-flash' })),
}));

// Import after mocks are registered.
import Settings from '../Settings';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderModelTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings?tab=model']}>
        <Settings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The search-provider select is the one with the "Web Search Provider" aria-label. */
function getSearchProviderSelect() {
  return screen.getByRole('combobox', { name: 'Web Search Provider' }) as HTMLSelectElement;
}

beforeEach(() => {
  h.platformMode = false;
  h.accessTier = 1;
  h.otherPreference = {};
  h.validModelNames = new Set<string>();
  h.mutateAsync.mockClear();
  h.mutateAsync.mockResolvedValue({});
});

/** Build the stable user/preferences refs for the current scenario, then render. */
function setupAndRenderModelTab() {
  h.user = {
    id: 'u-1',
    email: 'tester@example.com',
    name: 'Tester',
    access_tier: h.accessTier,
    onboarding_completed: true,
  };
  h.preferences = { other_preference: h.otherPreference };
  return renderModelTab();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Settings — Web Search Provider', () => {
  it('OSS mode: renders enabled with Default + 3 providers and no upgrade hint', async () => {
    h.platformMode = false;
    h.accessTier = 0; // tier is irrelevant in OSS mode

    setupAndRenderModelTab();

    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    expect(select).toBeEnabled();

    const options = Array.from(
      (select as HTMLSelectElement).querySelectorAll('option'),
    ).map(o => o.textContent);
    expect(options).toEqual(['Default', 'Tavily', 'Serper', 'Bocha']);

    expect(
      screen.queryByText('Choosing a search provider is available on paid plans.'),
    ).not.toBeInTheDocument();
  });

  it('platform mode, access_tier 0: select is disabled and upgrade hint is shown', async () => {
    h.platformMode = true;
    h.accessTier = 0;

    setupAndRenderModelTab();

    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    expect(select).toBeDisabled();
    expect(
      screen.getByText('Choosing a search provider is available on paid plans.'),
    ).toBeInTheDocument();
  });

  it('platform mode, access_tier 1: select is enabled and no upgrade hint', async () => {
    h.platformMode = true;
    h.accessTier = 1;

    setupAndRenderModelTab();

    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    expect(select).toBeEnabled();
    expect(
      screen.queryByText('Choosing a search provider is available on paid plans.'),
    ).not.toBeInTheDocument();
  });

  it('loads the saved value from other_preference.search_provider', async () => {
    h.otherPreference = { search_provider: 'serper' };

    setupAndRenderModelTab();

    await waitFor(() => {
      expect(getSearchProviderSelect().value).toBe('serper');
    });
  });

  it('normalizes an unknown saved search_provider to Default', async () => {
    h.otherPreference = { search_provider: 'not-an-engine' };

    setupAndRenderModelTab();

    // The load path validates against SEARCH_PROVIDERS and falls back to ''.
    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    await waitFor(() => {
      expect(select).toHaveValue('');
    });
  });

  it('changing the select saves search_provider through updatePreferences', async () => {
    h.platformMode = false;

    setupAndRenderModelTab();

    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    // Wait for the async model-tab load to settle (it sets state from prefs).
    await waitFor(() => expect(select).toHaveValue(''));

    fireEvent.change(select, { target: { value: 'serper' } });

    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalled();
    });
    const payload = h.mutateAsync.mock.calls.at(-1)![0] as {
      other_preference: Record<string, unknown>;
    };
    expect(payload.other_preference).toMatchObject({ search_provider: 'serper' });
  });

  it('platform mode below tier: unrelated saves omit search_provider entirely', async () => {
    h.platformMode = true;
    h.accessTier = 0;
    h.otherPreference = { search_provider: 'serper' };

    setupAndRenderModelTab();

    const select = await screen.findByRole('combobox', { name: 'Web Search Provider' });
    expect(select).toBeDisabled();

    // Fire a save through an unrelated control; the gated key must be omitted
    // (not nulled) so the stored pref is neither re-persisted nor deleted.
    fireEvent.click(screen.getByText('stub-change-primary'));

    await waitFor(() => {
      expect(h.mutateAsync).toHaveBeenCalled();
    });
    const payload = h.mutateAsync.mock.calls.at(-1)![0] as {
      other_preference: Record<string, unknown>;
    };
    expect('search_provider' in payload.other_preference).toBe(false);
  });
});
