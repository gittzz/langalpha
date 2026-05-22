import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useSyncUserLocale } from '../useSyncUserLocale';

vi.mock('../useUser', () => ({
  useUser: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: vi.fn(),
}));

import { useUser } from '../useUser';
import { useTranslation } from 'react-i18next';

const mockUseUser = useUser as Mock;
const mockUseTranslation = useTranslation as unknown as Mock;

type I18nStub = { language: string; changeLanguage: Mock };

function makeI18n(language = 'en-US'): I18nStub {
  const stub: I18nStub = {
    language,
    changeLanguage: vi.fn((lang: string) => {
      // Mirror real i18next behavior so subsequent reads see the new language.
      stub.language = lang;
      return Promise.resolve();
    }),
  };
  return stub;
}

let mockI18n: I18nStub;

describe('useSyncUserLocale', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockI18n = makeI18n('en-US');
    mockUseTranslation.mockReturnValue({ i18n: mockI18n });
  });

  it('applies the server locale on first render', () => {
    mockUseUser.mockReturnValue({ user: { locale: 'zh-CN' } });

    renderHook(() => useSyncUserLocale());

    expect(mockI18n.changeLanguage).toHaveBeenCalledTimes(1);
    expect(mockI18n.changeLanguage).toHaveBeenCalledWith('zh-CN');
    expect(localStorage.getItem('locale')).toBe('zh-CN');
  });

  it('does not re-apply when user.locale changes after the first sync (regression: locale race)', () => {
    mockUseUser.mockReturnValue({ user: { locale: 'zh-CN' } });

    const { rerender } = renderHook(() => useSyncUserLocale());

    expect(mockI18n.changeLanguage).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('locale')).toBe('zh-CN');

    // Simulate a stale /users/me refetch returning the prior server value while
    // the user has just picked a different locale locally.
    mockUseUser.mockReturnValue({ user: { locale: 'en-US' } });
    rerender();

    // Latch must hold: no second changeLanguage, localStorage untouched.
    expect(mockI18n.changeLanguage).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem('locale')).toBe('zh-CN');
  });

  it('latches even when the first render is a no-op (user.locale already matches i18n.language)', () => {
    mockI18n = makeI18n('en-US');
    mockUseTranslation.mockReturnValue({ i18n: mockI18n });
    mockUseUser.mockReturnValue({ user: { locale: 'en-US' } });

    const { rerender } = renderHook(() => useSyncUserLocale());

    // First render: locale matches i18n.language, so changeLanguage is not
    // called, but the latch must still trip.
    expect(mockI18n.changeLanguage).not.toHaveBeenCalled();
    expect(localStorage.getItem('locale')).toBeNull();

    // Later refetch returns a different locale: latch must block the sync.
    mockUseUser.mockReturnValue({ user: { locale: 'zh-CN' } });
    rerender();

    expect(mockI18n.changeLanguage).not.toHaveBeenCalled();
    expect(localStorage.getItem('locale')).toBeNull();
  });
});
