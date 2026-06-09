import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import React from 'react';
import { McpServerRow } from '../McpServerRow';
import type { EffectiveServer } from '../../../utils/api';

// Mirror the repo convention (FileHeaderActions.test): render the Radix
// dropdown inline so items are queryable without portal/pointer machinery. A
// disabled item must NOT fire onSelect, mirroring real Radix behaviour.
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DropdownMenuTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  DropdownMenuContent: ({ children }: { children: React.ReactNode }) => (
    <div role="menu">{children}</div>
  ),
  DropdownMenuItem: ({
    children,
    onSelect,
    disabled,
    className,
  }: {
    children: React.ReactNode;
    onSelect?: () => void;
    disabled?: boolean;
    className?: string;
  }) => (
    <button
      role="menuitem"
      aria-disabled={disabled ? 'true' : undefined}
      className={className}
      onClick={() => { if (!disabled) onSelect?.(); }}
    >
      {children}
    </button>
  ),
}));

function makeServer(overrides: Partial<EffectiveServer> = {}): EffectiveServer {
  return {
    name: 'placeholder_server',
    origin: 'workspace',
    transport: 'stdio',
    enabled: true,
    editable: true,
    deletable: true,
    status: 'connected',
    error: '',
    tool_count: 3,
    tools: [],
    missing_secrets: [],
    env_refs: [],
    header_refs: [],
    description: '',
    instruction: '',
    tool_exposure_mode: 'summary',
    command: 'npx',
    args: [],
    url: null,
    config_version: 1,
    ...overrides,
  };
}

const handlers = () => ({
  onToggle: vi.fn(),
  onEdit: vi.fn(),
  onDiscover: vi.fn(),
  onDelete: vi.fn(),
  onSetupSecret: vi.fn(),
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe('McpServerRow — origin badge + base render', () => {
  it('shows the workspace badge, tool count, and status pill', () => {
    render(<McpServerRow server={makeServer()} {...handlers()} />);
    expect(screen.getByText('workspace')).toBeInTheDocument();
    expect(screen.getByText('3 tools')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-status-connected')).toBeInTheDocument();
  });

  it('shows the built-in badge for builtins', () => {
    render(<McpServerRow server={makeServer({ origin: 'builtin', editable: false, deletable: false })} {...handlers()} />);
    expect(screen.getByText('built-in')).toBeInTheDocument();
  });
});

describe('McpServerRow — enabled toggle', () => {
  it('toggles via the switch (interactive for builtins too)', () => {
    const h = handlers();
    render(<McpServerRow server={makeServer({ origin: 'builtin', editable: false, deletable: false })} {...h} />);
    fireEvent.click(screen.getByRole('switch'));
    expect(h.onToggle).toHaveBeenCalledWith(false);
  });
});

describe('McpServerRow — kebab menu (builtins restricted)', () => {
  it('disables Edit/Test/Delete for a built-in server', () => {
    const h = handlers();
    render(<McpServerRow server={makeServer({ origin: 'builtin', editable: false, deletable: false })} {...h} />);

    for (const label of ['Edit', 'Test connection', 'Delete']) {
      const item = screen.getByText(label).closest('[role="menuitem"]')!;
      expect(item).toHaveAttribute('aria-disabled', 'true');
    }

    // Clicking a disabled item is a no-op.
    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByText('Delete'));
    expect(h.onEdit).not.toHaveBeenCalled();
    expect(h.onDelete).not.toHaveBeenCalled();
  });

  it('enables Edit/Test/Delete for a workspace server and fires handlers', () => {
    const h = handlers();
    render(<McpServerRow server={makeServer()} {...h} />);

    fireEvent.click(screen.getByText('Edit'));
    expect(h.onEdit).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('Test connection'));
    expect(h.onDiscover).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('Delete'));
    expect(h.onDelete).toHaveBeenCalledTimes(1);
  });
});

describe('McpServerRow — status-specific affordances', () => {
  it('surfaces the error text on an error row', () => {
    render(<McpServerRow server={makeServer({ status: 'error', error: 'could not start' })} {...handlers()} />);
    expect(screen.getByText('could not start')).toBeInTheDocument();
  });

  it('renders a "Set up NAME" affordance for needs_secret rows', () => {
    const h = handlers();
    render(
      <McpServerRow
        server={makeServer({ status: 'needs_secret', missing_secrets: ['MY_API_KEY'] })}
        {...h}
      />,
    );
    const setup = screen.getByText('Set up MY_API_KEY');
    fireEvent.click(setup);
    expect(h.onSetupSecret).toHaveBeenCalledWith('MY_API_KEY');
  });

  it('shows the transient not-synced hint when flagged', () => {
    render(<McpServerRow server={makeServer()} showNotSynced {...handlers()} />);
    expect(screen.getByTestId('mcp-not-synced')).toBeInTheDocument();
  });

  it('hides the not-synced hint when not flagged', () => {
    render(<McpServerRow server={makeServer()} {...handlers()} />);
    expect(screen.queryByTestId('mcp-not-synced')).not.toBeInTheDocument();
  });
});
