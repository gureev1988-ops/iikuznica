"""Record the local work actually performed, in the same timeline as model attempts."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from backend.db import connect

_steps = ContextVar("assembly_local_steps", default=None)


@contextmanager
def record_steps(dsn, request):
    rows = []
    token = _steps.set(rows)
    try:
        yield
    finally:
        _steps.reset(token)
        if rows and request.attempts is not None and dsn:
            with connect(dsn) as conn, conn.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO assembly_local_steps(turn_id, role, started_at, state, elapsed_ms)
                    VALUES (%s,%s,%s,%s,%s)
                """, [(request.turn_id, *row) for row in rows])


def record(role, started, success=True):
    rows = _steps.get()
    if rows is not None:
        elapsed_ms = max(0, (datetime.now(UTC) - started).total_seconds() * 1000)
        rows.append((role, started, "succeeded" if success else "failed", elapsed_ms))


@contextmanager
def local_step(role):
    started = datetime.now(UTC)
    try:
        yield
    except Exception:
        record(role, started, False)
        raise
    else:
        record(role, started)
