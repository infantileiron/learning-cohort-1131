import json
import logging
import os
import time
from pathlib import Path

import redis
from src.config.database import Session
from src.models.entities import ArchiveJob, Cohort
from src.services import archive as archive_service
from src.services.analysis import calculate


def recover():
    """Requeue unfinished work after a restart.  Both handlers are idempotent."""
    with Session() as session:
        for pending in session.query(ArchiveJob).filter(ArchiveJob.state.in_(['pending', 'processing'])).all():
            redis.Redis.from_url(os.environ['REDIS_URL']).rpush('archive:jobs', pending.id)
        for pending in session.query(Cohort).filter(Cohort.state.in_(['queued', 'running'])).all():
            pending.state = 'queued'
            session.commit()
            redis.Redis.from_url(os.environ['REDIS_URL']).rpush('cohort:jobs', pending.id)


def handle_archive(job_id):
    """A duplicate wake-up re-verifies and reuses the batch instead of moving twice."""
    with Session() as session:
        archive_service.process(session, int(job_id))


def artifact_dir():
    return Path(os.getenv('ARTIFACTS_DIR', '/artifacts'))


def handle_cohort(cohort_id):
    with Session() as session:
        row = session.get(Cohort, int(cohort_id))
        if not row or row.state != 'queued':
            return
        snapshot = row.result['snapshot']
        row.state = 'running'
        session.commit()
        try:
            row.result = {**calculate(snapshot), 'snapshot': snapshot}
            row.state = 'done'
            target = artifact_dir() / f'{row.id}.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(row.result, ensure_ascii=False))
        except Exception as exc:
            row.state = 'failed'
            row.error = str(exc)
        session.commit()


def handle(queue, value):
    if queue == 'archive:jobs':
        handle_archive(value)
    else:
        handle_cohort(value)


def main():
    r = redis.Redis.from_url(os.environ['REDIS_URL'])
    recover()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop(['cohort:jobs', 'archive:jobs'], timeout=3)
            if not item:
                continue
            queue, value = item
            handle(queue.decode(), value)
        except Exception:
            logging.exception('worker')
            time.sleep(2)


if __name__ == '__main__':
    main()
