import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import React from 'react';
import { McpServerModal } from '../McpServerModal';

// VaultSecretPicker pulls in the api module for inline-create; stub it so the
// modal renders without a real backend.
vi.mock('../../../utils/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, createVaultSecret: vi.fn() };
});

const baseProps = {
  workspaceId: 'ws-1',
  secretNames: ['EXISTING_TOKEN'],
  onClose: vi.fn(),
  onSubmit: vi.fn().mockResolvedValue(undefined),
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('McpServerModal — conditional fields per transport', () => {
  it('shows stdio fields (command, args, env) by default', () => {
    render(<McpServerModal {...baseProps} />);
    expect(screen.getByText('Command')).toBeInTheDocument();
    expect(screen.getByText('Arguments')).toBeInTheDocument();
    expect(screen.getByText('Environment variables')).toBeInTheDocument();
    // No URL/Headers fields for stdio.
    expect(screen.queryByText('URL')).not.toBeInTheDocument();
    expect(screen.queryByText('Headers')).not.toBeInTheDocument();
  });

  it('switches to URL + Headers when transport is http', () => {
    render(<McpServerModal {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'http' }));
    expect(screen.getByText('URL')).toBeInTheDocument();
    expect(screen.getByText('Headers')).toBeInTheDocument();
    // Command/Args/Env gone for remote transports.
    expect(screen.queryByText('Command')).not.toBeInTheDocument();
    expect(screen.queryByText('Arguments')).not.toBeInTheDocument();
    expect(screen.queryByText('Environment variables')).not.toBeInTheDocument();
  });

  it('switches to URL + Headers when transport is sse', () => {
    render(<McpServerModal {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'sse' }));
    expect(screen.getByText('URL')).toBeInTheDocument();
    expect(screen.getByText('Headers')).toBeInTheDocument();
  });

  it('renders the untrusted-context helper text on description + instruction', () => {
    render(<McpServerModal {...baseProps} />);
    const hints = screen.getAllByText(/shown to the agent as untrusted, user-provided context/i);
    expect(hints.length).toBe(2);
  });

  it('exposes the summary/detailed exposure toggle', () => {
    render(<McpServerModal {...baseProps} />);
    expect(screen.getByRole('button', { name: 'summary' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'detailed' })).toBeInTheDocument();
  });
});

describe('McpServerModal — validation gating', () => {
  it('disables Add until a valid name is entered', () => {
    render(<McpServerModal {...baseProps} />);
    const addBtn = screen.getByRole('button', { name: /^add$/i });
    expect(addBtn).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText('my_server'), { target: { value: 'good_name' } });
    expect(addBtn).not.toBeDisabled();
  });

  it('keeps Add disabled for an http server with a private-IP url (SSRF policy)', () => {
    render(<McpServerModal {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText('my_server'), { target: { value: 'remote' } });
    fireEvent.click(screen.getByRole('button', { name: 'http' }));
    fireEvent.change(screen.getByPlaceholderText('https://example.com/mcp'), {
      target: { value: 'https://169.254.169.254/' },
    });
    expect(screen.getByRole('button', { name: /^add$/i })).toBeDisabled();
  });

  it('submits the built payload on Add', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<McpServerModal {...baseProps} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByPlaceholderText('my_server'), { target: { value: 'good_name' } });
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'good_name', transport: 'stdio', command: 'npx' }),
    );
  });

  it('locks the name field when editing', () => {
    const initial = {
      name: 'locked_name',
      origin: 'workspace' as const,
      transport: 'stdio',
      enabled: true,
      editable: true,
      deletable: true,
      status: 'connected' as const,
      error: '',
      tool_count: 0,
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
    };
    render(<McpServerModal {...baseProps} initial={initial} />);
    const nameInput = screen.getByDisplayValue('locked_name') as HTMLInputElement;
    expect(nameInput).toBeDisabled();
  });
});
