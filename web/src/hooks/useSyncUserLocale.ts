import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useUser } from './useUser';

const SUPPORTED = new Set(['en-US', 'zh-CN']);

/**
 * Apply the user's DB-stored locale once, on the first user payload we see.
 * Subsequent refetches must not override the local value — otherwise a stale
 * /users/me response can clobber a locale the user just picked in Settings.
 */
export function useSyncUserLocale() {
  const { user } = useUser();
  const { i18n } = useTranslation();
  const synced = useRef(false);

  useEffect(() => {
    if (synced.current) return;
    const stored = user?.locale as string | undefined;
    if (!stored || !SUPPORTED.has(stored)) return;
    synced.current = true; // latch even if no-op, so a later refetch can't trigger sync
    if (i18n.language === stored) return;
    i18n.changeLanguage(stored);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('locale', stored);
    }
  }, [user?.locale, i18n]);
}
