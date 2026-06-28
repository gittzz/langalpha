import { useTranslation } from 'react-i18next';
import { ArrowDown } from 'lucide-react';
import './JumpToLatestPill.css';

interface JumpToLatestPillProps {
  /** Whether the user is scrolled up far enough to warrant the affordance. */
  visible: boolean;
  /** New messages arrived below the fold while scrolled up. */
  hasNew: boolean;
  /** Count of new messages since the user last scrolled up (only shown when hasNew). */
  newCount?: number;
  onJump: () => void;
}

/**
 * Floating "jump to latest" affordance shown when the user has scrolled up.
 * Switches to a "N new" style when messages arrive below the fold. Purely
 * presentational — all scroll logic lives in ChatView.
 */
export default function JumpToLatestPill({ visible, hasNew, newCount = 0, onJump }: JumpToLatestPillProps) {
  const { t } = useTranslation();
  if (!visible) return null;

  const showCount = hasNew && newCount > 0;

  return (
    <button
      type="button"
      className="jump-to-latest-pill"
      onClick={onJump}
      aria-label={t('chat.jumpToLatest.aria', { defaultValue: 'Scroll to latest message' })}
    >
      {showCount && <span className="jump-to-latest-pill__count">{newCount}</span>}
      <span>
        {showCount
          ? t('chat.jumpToLatest.newSuffix', { defaultValue: 'new' })
          : t('chat.jumpToLatest.label', { defaultValue: 'Jump to latest' })}
      </span>
      <ArrowDown className="h-3.5 w-3.5" />
    </button>
  );
}
