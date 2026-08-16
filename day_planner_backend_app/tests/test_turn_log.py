"""Coverage of turn_log.py's own logging setup (A1.2), separate from
test_agent_client.py's coverage of how AgentClient.send_message populates a
TurnRecorder.

Deliberately not stdout-capture-based (capsys/capfd): turn_log's handler
binds sys.stdout once at import time, matching real process behaviour —
but that's *earlier* than any given test's own capture window starts, so
the write lands in pytest's outer/session-level capture instead of the
per-test buffer capsys/capfd expose, making both unreliable here. Testing
the formatter and handler directly is deterministic and exercises exactly
what production relies on, without fighting pytest's capture-timing
internals.
"""

import logging

from app.services import turn_log


def test_turn_logger_formatter_emits_bare_message_no_prefix():
    """Cloud Logging on Cloud Run only auto-parses a stdout line into
    structured jsonPayload when the *entire* line is valid JSON. The
    default logging.basicConfig() formatter (see main.py) would prefix
    this with "INFO:day_planner.turn:", which silently breaks that and,
    with it, every query in turn_log_queries.sql.

    Reaches for turn_log._handler directly rather than
    logger.handlers[0] — pytest's own log-capture machinery attaches
    additional handlers to this logger over the course of a session (via
    other tests' caplog.at_level calls), so index 0 isn't reliably "the
    real one" once other tests have run first."""
    formatter = turn_log._handler.formatter
    record = logging.LogRecord(
        name="day_planner.turn",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='{"turn_id": "abc"}',
        args=None,
        exc_info=None,
    )
    assert formatter.format(record) == '{"turn_id": "abc"}'


def test_turn_logger_does_not_propagate_to_root():
    """If this ever regressed to propagate=True, main.py's
    logging.basicConfig() root handler would print the same record a
    second time, prefixed — doubling every turn record in Cloud Logging."""
    assert turn_log.logger.propagate is False
