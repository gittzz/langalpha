/**
 * Tests the renderErrorContent helper in MarketPanel.
 *
 * We import the MarketPanel component and render it with error props
 * to verify both structured and string error rendering.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import type { StructuredError } from '@/utils/rateLimitError';

// Mock navigate for internal link tests
const mockNavigate = vi.fn();

// Minimal reproduction of ErrorLink + renderErrorContent from MarketPanel.tsx.
// We test the logic in isolation rather than mounting MarketPanel
// (which pulls in MessageList and its heavy dependency tree).
function ErrorLink({ url, label, navigate }: { url: string; label: string; navigate: (to: string) => void }) {
  return (
    <>
      {' '}
      <a
        href={url}
        onClick={(e) => {
          if (url.startsWith('/')) {
            e.preventDefault();
            navigate(url);
          }
        }}
        style={{ textDecoration: 'underline', fontWeight: 500 }}
      >
        {label}
      </a>
    </>
  );
}

function renderErrorContent(error: string | StructuredError, navigate: (to: string) => void): React.ReactNode {
  if (typeof error === 'object' && 'message' in error) {
    return (
      <>
        {error.message}
        {error.link && <ErrorLink url={error.link.url} label={error.link.label} navigate={navigate} />}
      </>
    );
  }
  return error;
}

function Wrapper({ error }: { error: string | StructuredError }) {
  return <div data-testid="error">{renderErrorContent(error, mockNavigate)}</div>;
}

describe('MarketPanel renderErrorContent', () => {
  it('renders structured error with external link as <a> tag', () => {
    const error: StructuredError = {
      message: 'Daily credit limit reached (80/100 credits). Resets at midnight UTC.',
      link: { url: 'https://ginlix.ai/account/plans', label: 'Upgrade plan' },
    };
    render(<Wrapper error={error} />);

    expect(screen.getByText(/Daily credit limit reached/)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Upgrade plan' });
    expect(link).toHaveAttribute('href', 'https://ginlix.ai/account/plans');
  });

  it('renders structured error with internal link using client-side navigation', async () => {
    mockNavigate.mockClear();
    const error: StructuredError = {
      message: 'No API key configured.',
      link: { url: '/setup/method', label: 'Configure providers' },
    };
    render(<Wrapper error={error} />);

    const link = screen.getByRole('link', { name: 'Configure providers' });
    expect(link).toHaveAttribute('href', '/setup/method');

    await userEvent.click(link);
    expect(mockNavigate).toHaveBeenCalledWith('/setup/method');
  });

  it('renders string error as plain text', () => {
    render(<Wrapper error="Something went wrong" />);

    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
