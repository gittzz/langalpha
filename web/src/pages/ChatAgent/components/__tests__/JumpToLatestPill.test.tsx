import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import JumpToLatestPill from '../JumpToLatestPill';

// i18n in tests returns the defaultValue (with {{count}} interpolation) — assert
// on stable substrings rather than exact translated copy.
describe('JumpToLatestPill', () => {
  it('renders nothing when not visible', () => {
    const { container } = render(<JumpToLatestPill visible={false} hasNew={false} onJump={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('shows the default label when visible without new messages', () => {
    render(<JumpToLatestPill visible hasNew={false} onJump={() => {}} />);
    expect(screen.getByRole('button')).toHaveTextContent(/jump to latest/i);
  });

  it('shows the new-message count when hasNew', () => {
    render(<JumpToLatestPill visible hasNew newCount={3} onJump={() => {}} />);
    const btn = screen.getByRole('button');
    expect(btn).toHaveTextContent('3');
    expect(btn).toHaveTextContent(/new/i);
  });

  it('falls back to the default label when hasNew but count is 0', () => {
    render(<JumpToLatestPill visible hasNew newCount={0} onJump={() => {}} />);
    expect(screen.getByRole('button')).toHaveTextContent(/jump to latest/i);
  });

  it('calls onJump when clicked', () => {
    const onJump = vi.fn();
    render(<JumpToLatestPill visible hasNew={false} onJump={onJump} />);
    fireEvent.click(screen.getByRole('button'));
    expect(onJump).toHaveBeenCalledTimes(1);
  });

  it('exposes an accessible label', () => {
    render(<JumpToLatestPill visible hasNew={false} onJump={() => {}} />);
    expect(screen.getByRole('button')).toHaveAttribute('aria-label');
  });
});
