"""Cold-event archival.

Events whose Shanghai business day is strictly before a cutoff are written to
an immutable batch file on the shared artifacts volume, read back and verified,
and only then moved out of the online events table.  The batch file is the
receipt for the move; archived_events remains queryable/editable from the
ledger.

Guarantees:
* a batch file is written with temp-file + fsync + os.replace, so a reader can
  never observe a half-written batch;
* a resumed run re-validates an existing batch file and reuses it instead of
  rewriting/moving rows twice;
* candidate selection, the move and the job state change commit in one
  transaction under the ledger guard, so a cohort snapshot sees either the whole
  online set or the whole archived set, never half a batch;
* an archived event's revision stays in the archive tier and never resurrects
  online (which would double count it in a later snapshot).
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from src.models.entities import ArchiveJob, ArchivedEvent, Event
from src.services.locking import ledger_guard

TZ_NAME = 'Asia/Shanghai'
_EVENT_FIELDS = ('code', 'learner', 'occurred_at', 'course', 'note')


class ArchiveError(Exception):
    """A batch could not be written or verified; the move was not committed."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def shanghai_day(stamp):
    value = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ArchiveError('事件时间缺少时区，无法按上海业务日归档')
    return value.astimezone(ZoneInfo(TZ_NAME)).date()


def archive_dir():
    path = Path(os.getenv('ARCHIVE_DIR', '/artifacts/archive'))
    path.mkdir(parents=True, exist_ok=True)
    return path


def batch_path(job_id):
    return archive_dir() / f'batch-{int(job_id)}.json'


def canonical_events(events):
    """Stable batch payload: content plus the online source id, sorted by code."""
    rows = [
        {'source_id': event.id, **{key: (getattr(event, key) or '') for key in _EVENT_FIELDS}}
        for event in events
    ]
    return sorted(rows, key=lambda row: row['code'])


def payload_checksum(events):
    blob = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def before_cutoff(event, cutoff):
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff).date()
    return shanghai_day(event.occurred_at) < cutoff


def candidates(session, cutoff):
    return [
        event
        for event in session.query(Event).populate_existing().order_by(Event.id).all()
        if before_cutoff(event, cutoff)
    ]


def _payload(job, events, checksum):
    return {
        'job_id': job.id,
        'cutoff': job.cutoff,
        'timezone': TZ_NAME,
        'count': len(events),
        'checksum': checksum,
        'events': events,
        'created_at': now_iso(),
    }


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ArchiveError(f'归档批次文件缺失: {path}')
    except (OSError, ValueError) as exc:
        raise ArchiveError(f'归档批次文件损坏: {path} ({exc})')

def validate_payload(payload, job, *, require_job_checksum=True):
    if not isinstance(payload, dict) or not isinstance(payload.get('events'), list):
        raise ArchiveError(f'归档批次 #{job.id} 结构损坏')
    if payload.get('job_id') != job.id:
        raise ArchiveError(f'归档批次 #{job.id} 文件批次号不符: {payload.get("job_id")!r}')
    if payload.get('cutoff') != job.cutoff:
        raise ArchiveError(f'归档批次 #{job.id} 截至日不符: {payload.get("cutoff")!r}')
    rows = payload['events']
    if any(
        not isinstance(row, dict)
        or any(key not in row for key in _EVENT_FIELDS)
        or not isinstance(row.get('source_id'), int)
        for row in rows
    ):
        raise ArchiveError(f'归档批次 #{job.id} 事件内容损坏')
    canonical = sorted(
        [{'source_id': row['source_id'], **{key: row[key] for key in _EVENT_FIELDS}} for row in rows],
        key=lambda row: row['code'],
    )
    if len({row['code'] for row in canonical}) != len(canonical):
        raise ArchiveError(f'归档批次 #{job.id} 存在重复事件编号')
    checksum = payload_checksum(canonical)
    if payload.get('checksum') != checksum:
        raise ArchiveError(f'归档批次 #{job.id} 校验和不符')
    if payload.get('count') != len(canonical):
        raise ArchiveError(f'归档批次 #{job.id} 条数不符')
    if require_job_checksum and job.batch_checksum and job.batch_checksum != checksum:
        raise ArchiveError(f'归档批次 #{job.id} 与任务记录校验和不符')
    return canonical, checksum


def _write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_or_reuse_batch(job, events, checksum):
    """Write the batch once; on a retry an already verified batch is reused."""
    path = batch_path(job.id)
    if path.exists():
        try:
            stored, stored_checksum = validate_payload(_read(path), job, require_job_checksum=False)
            if stored_checksum == checksum and stored == events:
                return path
        except ArchiveError:
            pass  # rewrite a damaged or outdated partial batch
    _write_atomic(path, _payload(job, events, checksum))
    # Read back and verify before the caller is allowed to delete online rows.
    stored, stored_checksum = validate_payload(_read(path), job)
    if stored_checksum != checksum or stored != events:
        raise ArchiveError(f'归档批次 #{job.id} 写后核验不一致')
    return path


def verify_archives(session):
    """Return one problem per missing/damaged batch, naming the batch file."""
    problems = []
    jobs = session.query(ArchiveJob).filter(ArchiveJob.state == 'archived').order_by(ArchiveJob.id).all()
    for job in jobs:
        path = batch_path(job.id)
        try:
            payload = _read(path)
            events, _checksum = validate_payload(payload, job)
            if job.archived_count != len(events):
                raise ArchiveError(
                    f'归档批次 #{job.id} 任务条数 {job.archived_count} 与文件 {len(events)} 不符'
                )
            archived_rows = session.query(ArchivedEvent).filter(ArchivedEvent.archive_job_id == job.id).count()
            if archived_rows != len(events):
                raise ArchiveError(
                    f'归档批次 #{job.id} 归档表条数 {archived_rows} 与文件 {len(events)} 不符'
                )
        except ArchiveError as exc:
            problems.append({'job_id': job.id, 'file': str(path), 'reason': str(exc)})
    return problems


def assert_archives_healthy(session):
    problems = verify_archives(session)
    if problems:
        detail = '；'.join(f'批次 #{p["job_id"]}: {p["reason"]} ({p["file"]})' for p in problems)
        raise HTTPException(409, f'归档文件缺失或损坏，已阻止新计算。{detail}')
    return problems


def enqueue(job_id):
    """Hand the job to the worker; False means the caller should run it inline."""
    import redis
    try:
        redis.Redis.from_url(os.environ['REDIS_URL']).rpush('archive:jobs', job_id)
        return True
    except Exception:
        return False


def _same_event(row, event):
    return all((getattr(row, key) or '') == (getattr(event, key) or '') for key in _EVENT_FIELDS)


def _move(session, job, selected, events):
    for event in selected:
        existing = session.query(ArchivedEvent).filter_by(code=event.code).first()
        if existing:
            if not _same_event(existing, event):
                raise ArchiveError(
                    f'事件编号 {event.code} 已归档且内容不同，为避免覆盖更正已保留双方，任务失败'
                )
            session.delete(event)
            continue
        session.add(ArchivedEvent(
            code=event.code,
            learner=event.learner,
            occurred_at=event.occurred_at,
            course=event.course,
            note=event.note,
            origin_event_id=event.id,
            archive_job_id=job.id,
        ))
        session.delete(event)


def process(session, job_id):
    """Run one archive job.  Safe to call again after any interruption."""
    job = session.get(ArchiveJob, job_id)
    if not job or job.state == 'archived':
        return job
    if job.state not in ('pending', 'processing', 'failed'):
        raise ArchiveError(f'归档任务 #{job_id} 状态不可执行: {job.state}')
    job.state = 'processing'
    job.started_at = now_iso()
    job.error = ''
    session.commit()
    try:
        with ledger_guard(session):
            job = session.get(ArchiveJob, job_id)
            if not job or job.state == 'archived':
                return job
            selected = candidates(session, job.cutoff)
            events = canonical_events(selected)
            checksum = payload_checksum(events)
            job.candidate_count = len(events)
            job.batch_checksum = checksum
            write_or_reuse_batch(job, events, checksum)
            _move(session, job, selected, events)
            job.archived_count = len(events)
            job.state = 'archived'
            job.finished_at = now_iso()
            job.error = ''
            session.commit()
        return job
    except Exception as exc:
        session.rollback()
        failed = session.get(ArchiveJob, job_id)
        if failed and failed.state != 'archived':
            failed.state = 'failed'
            failed.error = str(exc)[:1000]
            failed.finished_at = now_iso()
            session.commit()
        raise
