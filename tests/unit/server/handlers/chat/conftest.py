"""Chat-handler test fixtures."""

import pytest

from src.server.handlers.chat import report_back


@pytest.fixture(autouse=True)
def _reset_rb_consumer_state():
    """Module-global report-back consumer registries must not leak across tests."""
    report_back._rb_consumers.clear()
    report_back._rb_terminal_events.clear()
    yield
    for task in list(report_back._rb_consumers.values()):
        task.cancel()
    report_back._rb_consumers.clear()
    report_back._rb_terminal_events.clear()
