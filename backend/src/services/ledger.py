"""Event ledger over the online table plus archived batches.

A code is unique across both tiers.  New input is matched against both, so a
re-entry of an archived code still obeys the original idempotent/409 rules.
Editing an archived event never resurrects it online (which would double count
it in a later cohort snapshot).
"""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from src.models.entities import ArchivedEvent, Event
from src.services.archive import _EVENT_FIELDS


def _same_payload(row, data):
    return all((getattr(row, key) or '') == (getattr(data, key) or '') for key in _EVENT_FIELDS)


def _online_dump(row):
    return {
        'id': row.id,
        'record_ref': str(row.id),
        'code': row.code,
        'learner': row.learner,
        'occurred_at': row.occurred_at,
        'course': row.course,
        'note': row.note,
        'origin': 'online',
        'origin_event_id': row.id,
        'archive_job_id': None,
        'archive_problem': None,
    }


def _archived_dump(row, problem=None):
    return {
        'id': row.id,
        'record_ref': f'a{row.id}',
        'code': row.code,
        'learner': row.learner,
        'occurred_at': row.occurred_at,
        'course': row.course,
        'note': row.note,
        'origin': 'archive',
        'origin_event_id': row.origin_event_id,
        'archive_job_id': row.archive_job_id,
        'archive_problem': problem,
    }


def parse_ref(ref):
    """Accept a bare id (online) or an a-prefixed id (archived)."""
    text = str(ref).strip()
    tier = 'online'
    if text[:1] in ('a', 'A'):
        tier, text = 'archive', text[1:]
    try:
        return tier, int(text)
    except ValueError:
        raise HTTPException(404, '事件不存在')


def find_code(session, code):
    online = session.query(Event).filter_by(code=code).first()
    if online:
        return 'online', online
    archived = session.query(ArchivedEvent).filter_by(code=code).first()
    if archived:
        return 'archive', archived
    return None, None


def create_event(session, data):
    tier, existing = find_code(session, data.code)
    if existing:
        if _same_payload(existing, data):
            return (_online_dump(existing) if tier == 'online' else _archived_dump(existing)), False
        raise HTTPException(409, '同事件编号的内容冲突，请修订原记录或使用新编号')
    row = Event(**data.model_dump())
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # A concurrent archive of this code can win the race; re-read both tiers.
        tier, other = find_code(session, data.code)
        if other and _same_payload(other, data):
            return (_online_dump(other) if tier == 'online' else _archived_dump(other)), False
        raise HTTPException(409, '同事件编号的内容冲突')
    return _online_dump(row), True


def edit_event(session, ref, data):
    tier, event_id = parse_ref(ref)
    if tier == 'online':
        row = session.get(Event, event_id)
        if row:
            # Uniqueness spans both tiers: taking an archived code here would make
            # the merged history collapse two events into one.
            if session.query(ArchivedEvent).filter_by(code=data.code).first():
                raise HTTPException(409, '事件编号重复')
            for key, value in data.model_dump().items():
                setattr(row, key, value)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise HTTPException(409, '事件编号重复')
            return _online_dump(row)
        # The id may have moved to the archive tier between the page load and the
        # save; follow origin_event_id instead of resurrecting a second row online.
        # A bare numeric id never addresses an archived row by its own primary key;
        # archived rows are addressed as a<id>.
        target = session.query(ArchivedEvent).filter_by(origin_event_id=event_id).first()
        if not target:
            raise HTTPException(404, '事件不存在')
    else:
        target = session.get(ArchivedEvent, event_id)
        if not target:
            raise HTTPException(404, '事件不存在')

    # The archived revision stays in the archive tier: re-inserting it online
    # would count the same event twice in a later cohort snapshot.
    if session.query(Event).filter_by(code=data.code).first():
        raise HTTPException(409, '事件编号重复')
    other = session.query(ArchivedEvent).filter(
        ArchivedEvent.code == data.code, ArchivedEvent.id != target.id
    ).first()
    if other:
        raise HTTPException(409, '事件编号重复')
    for key, value in data.model_dump().items():
        setattr(target, key, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, '事件编号重复')
    return _archived_dump(target)


def ledger(session, archive_problems=None):
    problems = {p['job_id']: p['reason'] for p in (archive_problems or [])}
    online = [_online_dump(row) for row in session.query(Event).order_by(Event.id.desc()).all()]
    archived = [
        _archived_dump(row, problems.get(row.archive_job_id))
        for row in session.query(ArchivedEvent).order_by(ArchivedEvent.id.desc()).all()
    ]
    # Both tiers are reachable from the same ledger; origin says where it lives.
    return online + archived


def _snapshot_event(row, archive_job_id):
    return {
        'id': row.id,
        'code': row.code,
        'learner': row.learner,
        'occurred_at': row.occurred_at,
        'course': row.course,
        'note': row.note,
        'archive_job_id': archive_job_id,
    }


def all_events(session):
    """Complete frozen history over both tiers, each event exactly once.

    Archived first so that on the (defensive) event of a code existing in both
    tiers the archived copy wins and the online copy never double counts.
    """
    merged = {}
    for row in session.query(ArchivedEvent).order_by(ArchivedEvent.id).all():
        merged[row.code] = _snapshot_event(row, row.archive_job_id)
    for row in session.query(Event).order_by(Event.id).all():
        merged.setdefault(row.code, _snapshot_event(row, None))
    return [merged[code] for code in sorted(merged)]
