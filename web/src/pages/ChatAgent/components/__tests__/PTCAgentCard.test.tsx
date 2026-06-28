/**
 * PTCAgentCard — pending approval flow + the live dispatch status indicator.
 *
 * The approved card polls the dispatched thread's /status (getWorkflowStatus)
 * and maps the backend WorkflowStatus enum onto a pill: active → Working,
 * interrupted → Needs input, completed → Completed, etc. The kicker shows the
 * workspace name. These tests mock the api module so the card's wiring is
 * exercised without a real backend.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Mock } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';

vi.mock('../../utils/api', () => ({
  getWorkflowStatus: vi.fn(),
}));

import { getWorkflowStatus } from '../../utils/api';
import PTCAgentCard from '../PTCAgentCard';

const mockStatus = getWorkflowStatus as unknown as Mock;

const APPROVED = {
  status: 'approved' as const,
  question: 'Is NVDA overvalued vs AMD on datacenter share?',
  workspace_name: 'Semiconductors',
  thread_id: 'thread-123',
  workspace_id: 'ws-1',
};

describe('PTCAgentCard — pending approval', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the workspace name as the kicker, question, and actions; never polls /status', () => {
    const onApprove = vi.fn();
    renderWithProviders(
      <PTCAgentCard
        proposalData={{ status: 'pending', question: 'Compare NVDA vs AMD', workspace_name: 'Semiconductors', report_back: true }}
        onApprove={onApprove}
        onReject={vi.fn()}
      />,
    );

    expect(screen.getByText('Semiconductors')).toBeInTheDocument();
    expect(screen.getByText('Awaiting approval')).toBeInTheDocument();
    expect(screen.getByText('Compare NVDA vs AMD')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument();
    expect(mockStatus).not.toHaveBeenCalled();
  });

  it('approves with the current report-back choice', () => {
    const onApprove = vi.fn();
    renderWithProviders(
      <PTCAgentCard
        proposalData={{ status: 'pending', question: 'Q', report_back: true }}
        onApprove={onApprove}
        onReject={vi.fn()}
      />,
    );

    // Toggle report-back off, then approve.
    fireEvent.click(screen.getByRole('button', { name: /report back/i }));
    fireEvent.click(screen.getByRole('button', { name: /approve/i }));
    expect(onApprove).toHaveBeenCalledWith({ report_back: false });
  });
});

describe('PTCAgentCard — live dispatch status', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows "Working" while the dispatched run is active', async () => {
    mockStatus.mockResolvedValue({ status: 'active', run_id: 'run-1', can_reconnect: true });
    renderWithProviders(<PTCAgentCard proposalData={APPROVED} onApprove={vi.fn()} onReject={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Working')).toBeInTheDocument());
    expect(mockStatus).toHaveBeenCalledWith('thread-123');
    expect(screen.getByText('Semiconductors')).toBeInTheDocument();
    expect(screen.getByText('Open thread')).toBeInTheDocument();
  });

  it('maps interrupted → "Needs input" with an answer affordance', async () => {
    mockStatus.mockResolvedValue({ status: 'interrupted' });
    renderWithProviders(<PTCAgentCard proposalData={APPROVED} onApprove={vi.fn()} onReject={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Needs input')).toBeInTheDocument());
    expect(screen.getByText('Answer & continue')).toBeInTheDocument();
  });

  it('maps completed → "Completed"', async () => {
    mockStatus.mockResolvedValue({ status: 'completed' });
    renderWithProviders(<PTCAgentCard proposalData={APPROVED} onApprove={vi.fn()} onReject={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Completed')).toBeInTheDocument());
  });

  it('maps failed → "Failed"', async () => {
    mockStatus.mockResolvedValue({ status: 'failed' });
    renderWithProviders(<PTCAgentCard proposalData={APPROVED} onApprove={vi.fn()} onReject={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Failed')).toBeInTheDocument());
  });
});

describe('PTCAgentCard — rejected', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders a quiet "Research declined" row and does not poll', () => {
    renderWithProviders(
      <PTCAgentCard proposalData={{ status: 'rejected', question: 'Q' }} onApprove={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText('Research declined')).toBeInTheDocument();
    expect(mockStatus).not.toHaveBeenCalled();
  });
});
