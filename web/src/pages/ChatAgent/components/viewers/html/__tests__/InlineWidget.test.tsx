import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';

import InlineWidget from '../../InlineWidget';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  toast: vi.fn(),
}));

/** Dispatch a postMessage as if it came from the widget iframe's contentWindow. */
function postFromIframe(iframe: HTMLIFrameElement, data: unknown) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data, source: iframe.contentWindow }));
  });
}

describe('InlineWidget — sandbox bridge regressions', () => {
  beforeEach(() => {
    vi.stubGlobal('open', vi.fn(() => ({ print: vi.fn(), addEventListener: vi.fn() })));
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('grows the iframe height on widget:resize', () => {
    const { container } = render(<InlineWidget html="<div>hi</div>" />);
    const iframe = container.querySelector('iframe.inline-widget-frame') as HTMLIFrameElement;
    // Before any resize, height is the 150px placeholder and opacity 0.
    expect(iframe.style.height).toBe('150px');
    postFromIframe(iframe, { type: 'widget:resize', height: 420 });
    expect(iframe.style.height).toBe('420px');
    expect(iframe.style.opacity).toBe('1');
  });

  it('calls onSendPrompt on widget:sendPrompt', () => {
    const onSendPrompt = vi.fn();
    const { container } = render(<InlineWidget html="<div>hi</div>" onSendPrompt={onSendPrompt} />);
    const iframe = container.querySelector('iframe.inline-widget-frame') as HTMLIFrameElement;
    postFromIframe(iframe, { type: 'widget:sendPrompt', text: '  fix it  ' });
    expect(onSendPrompt).toHaveBeenCalledWith('fix it');
  });

  it('ignores messages from a foreign source', () => {
    const onSendPrompt = vi.fn();
    render(<InlineWidget html="<div>hi</div>" onSendPrompt={onSendPrompt} />);
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', { data: { type: 'widget:sendPrompt', text: 'x' }, source: window }),
      );
    });
    expect(onSendPrompt).not.toHaveBeenCalled();
  });
});

describe('InlineWidget — hover action bar + fullscreen', () => {
  beforeEach(() => {
    vi.stubGlobal('open', vi.fn(() => ({ print: vi.fn(), addEventListener: vi.fn() })));
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the overlay action bar (fullscreen, open, more menu)', () => {
    render(<InlineWidget html="<div>hi</div>" title="My widget" />);
    expect(screen.getByLabelText('filePanel.fullscreen')).toBeInTheDocument();
    expect(screen.getByLabelText('filePanel.openInNewTab')).toBeInTheDocument();
    // Download/PDF are consolidated into the secondary "more" menu.
    expect(screen.getByLabelText('filePanel.moreActions')).toBeInTheDocument();
    expect(screen.queryByLabelText('filePanel.copySource')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('filePanel.downloadAsHtml')).not.toBeInTheDocument();
  });

  it('opens the fullscreen modal with a widget-fullscreen srcDoc iframe', () => {
    render(<InlineWidget html="<div>hi</div>" title="My widget" />);
    fireEvent.click(screen.getByLabelText('filePanel.fullscreen'));
    const frame = document.querySelector('iframe.html-fullscreen-frame') as HTMLIFrameElement;
    expect(frame).toBeTruthy();
    // Fullscreen variant uses a srcDoc (not a served src), scrollable body.
    expect(frame.getAttribute('src')).toBeNull();
    expect(frame.getAttribute('srcdoc')).toContain('overflow: auto; height: 100%;');
  });

  it('opens a blob tab when open-in-new-tab is clicked', () => {
    render(<InlineWidget html="<div>hi</div>" />);
    fireEvent.click(screen.getByLabelText('filePanel.openInNewTab'));
    expect(window.open).toHaveBeenCalledWith('blob:x', '_blank', 'noopener,noreferrer');
  });
});
