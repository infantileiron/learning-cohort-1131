"""Single-writer ledger guard.

Archiving, event edits and cohort snapshotting must not interleave: a cohort
snapshot has to see either the whole online set or the whole archived set, and
an archive job must not drop a revision that committed after its snapshot.

MySQL takes a transaction-scoped row lock on the seeded event_locks row
(released on commit/rollback).  The session's earlier read snapshot is closed
first, so reads taken under the lock observe everything committed by the
previous holder instead of a pre-lock REPEATABLE READ snapshot.  SQLite has no
row locks, so tests fall back to a process-wide re-entrant lock plus an identity
map reset.
"""
import threading
from contextlib import contextmanager
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

_SQLITE_LOCK = threading.RLock()
_LOCK_ID = 1
_DEADLOCK_ATTEMPTS = 3


def ensure_lock_row(connection):
    """Seed the singleton lock row; safe to call at startup and on demand."""
    if connection.dialect.name == 'sqlite':
        return
    connection.execute(text('INSERT IGNORE INTO event_locks (id) VALUES (:id)'), {'id': _LOCK_ID})


@contextmanager
def ledger_guard(session):
    dialect = session.get_bind().dialect.name
    if dialect == 'sqlite':
        with _SQLITE_LOCK:
            session.expire_all()
            yield
        return
    # End the snapshot opened by auth/session reads before waiting on the lock.
    session.rollback()
    for attempt in range(_DEADLOCK_ATTEMPTS):
        try:
            session.execute(text('INSERT IGNORE INTO event_locks (id) VALUES (:id)'), {'id': _LOCK_ID})
            session.execute(
                text('SELECT id FROM event_locks WHERE id = :id FOR UPDATE'), {'id': _LOCK_ID}
            )
            break
        except OperationalError:
            # MySQL may still choose the lock row as a deadlock victim; retry
            # on a fresh transaction rather than failing the user request.
            session.rollback()
            if attempt == _DEADLOCK_ATTEMPTS - 1:
                raise
    session.expire_all()
    yield
