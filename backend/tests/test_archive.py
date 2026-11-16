import json
import threading
import time

import src.services.archive as archive_service
from src.config.database import Session
from src.models.entities import ArchiveJob, ArchivedEvent, Cohort, Event
from src.services.analysis import calculate
from tests.conftest import login


def add(client, headers, code, learner, stamp, course='数据素养', note='normal'):
    body = {'code': code, 'learner': learner, 'occurred_at': stamp, 'course': course, 'note': note}
    return client.post('/events', json=body, headers=headers)


def archive(client, headers, cutoff):
    return client.post('/archives', json={'cutoff': cutoff}, headers=headers)


def cohort(client, headers, **overrides):
    cfg = {'name': '归档前后对照', 'start_week': '2026-09-07', 'end_week': '2026-09-07',
           'as_of': '2026-10-20', 'horizon': 4, **overrides}
    return client.post('/cohorts', json=cfg, headers=headers)


def test_archive_moves_only_earlier_shanghai_days_and_keeps_ledger(client):
    headers = login(client)
    # 2026-09-30 18:00 Shanghai -> strictly before the cutoff day, archives.
    add(client, headers, 'CQ-OLD-1', 'CQ-1', '2026-09-30T10:00:00+00:00')
    # 2026-10-01 00:00 Shanghai, i.e. the cutoff day itself -> stays online.
    add(client, headers, 'CQ-CUT', 'CQ-1', '2026-09-30T16:00:00+00:00')
    add(client, headers, 'CQ-NEW-1', 'CQ-2', '2026-10-05T02:00:00+00:00')

    r = archive(client, headers, '2026-10-01')
    assert r.status_code == 200, r.text
    job = r.json()
    assert job['state'] == 'archived'
    assert job['candidate_count'] == 1 and job['archived_count'] == 1

    by_code = {e['code']: e for e in client.get('/events', headers=headers).json()}
    assert by_code['CQ-OLD-1']['origin'] == 'archive'
    assert by_code['CQ-OLD-1']['archive_job_id'] == job['id']
    assert by_code['CQ-CUT']['origin'] == 'online'
    assert by_code['CQ-NEW-1']['origin'] == 'online'

    with Session() as s:
        assert s.query(Event).filter_by(code='CQ-OLD-1').first() is None
        assert s.query(ArchivedEvent).filter_by(code='CQ-OLD-1').first() is not None
        path = archive_service.batch_path(job['id'])
        payload = json.loads(path.read_text())
        assert payload['count'] == 1 and payload['checksum'] == job['batch_checksum']
        assert payload['events'][0]['code'] == 'CQ-OLD-1'


def test_archived_ledger_is_queryable_and_editable_without_resurrection(client):
    headers = login(client)
    add(client, headers, 'CQ-OLD-2', 'CQ-1', '2026-09-01T02:00:00+00:00')
    job = archive(client, headers, '2026-10-01').json()
    archived = next(e for e in client.get('/events', headers=headers).json() if e['code'] == 'CQ-OLD-2')
    assert archived['record_ref'] == f"a{archived['id']}"

    r = client.put(
        f"/events/{archived['record_ref']}",
        json={'code': 'CQ-OLD-2', 'learner': 'CQ-1', 'occurred_at': '2026-09-01T02:00:00+00:00',
              'course': '服务沟通', 'note': '更正课程'},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()['origin'] == 'archive'

    with Session() as s:
        # The revision stayed in the archive tier; the online table is untouched.
        assert s.query(Event).filter_by(code='CQ-OLD-2').first() is None
        assert s.query(ArchivedEvent).filter_by(code='CQ-OLD-2').one().course == '服务沟通'
        # The batch file keeps the original snapshot, so the move stays auditable.
        payload = json.loads(archive_service.batch_path(job['id']).read_text())
        assert payload['events'][0]['course'] == '数据素养'

    # An edited batch is not a damaged batch: integrity still passes.
    assert client.get('/archives/health', headers=headers).json()['ok'] is True


def test_stale_online_record_id_after_archive_edits_archived_copy(client):
    """The page was loaded before the archive; saving by the old numeric id must
    land on the archived row instead of creating a second online copy."""
    headers = login(client)
    created = add(client, headers, 'CQ-OLD-5', 'CQ-1', '2026-09-01T02:00:00+00:00').json()
    online_id = created['id']
    archive(client, headers, '2026-10-01')

    r = client.put(
        f'/events/{online_id}',
        json={'code': 'CQ-OLD-5', 'learner': 'CQ-1', 'occurred_at': '2026-09-01T02:00:00+00:00',
              'course': '安全规范', 'note': '归档后确认的更正'},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()['origin'] == 'archive'

    with Session() as s:
        assert s.query(Event).count() == 0
        row = s.query(ArchivedEvent).filter_by(code='CQ-OLD-5').one()
        assert row.course == '安全规范'
        assert row.note == '归档后确认的更正'


def test_reentry_of_archived_code_keeps_original_conflict_rules(client):
    headers = login(client)
    add(client, headers, 'CQ-OLD-3', 'CQ-1', '2026-09-01T02:00:00+00:00', note='first')
    archive(client, headers, '2026-10-01')

    same = client.post('/events', json={'code': 'CQ-OLD-3', 'learner': 'CQ-1',
                                        'occurred_at': '2026-09-01T02:00:00+00:00',
                                        'course': '数据素养', 'note': 'first'}, headers=headers)
    assert same.status_code == 200, same.text
    assert same.json()['origin'] == 'archive' and same.json()['created'] is False

    clash = client.post('/events', json={'code': 'CQ-OLD-3', 'learner': 'CQ-1',
                                         'occurred_at': '2026-09-02T02:00:00+00:00',
                                         'course': '数据素养', 'note': 'first'}, headers=headers)
    assert clash.status_code == 409

    with Session() as s:
        assert s.query(Event).filter_by(code='CQ-OLD-3').count() == 0


def test_cohort_freeze_uses_merged_history_once_and_keeps_prior_matrix(client):
    headers = login(client, 'admin')
    for i in range(6):
        add(client, headers, f'CQ-A{i}', f'L-{i}', f'2026-09-{7 + i:02d}T02:00:00+00:00')
        add(client, headers, f'CQ-A{i}-R', f'L-{i}', f'2026-09-{14 + i:02d}T02:00:00+00:00')

    before = cohort(client, headers)
    assert before.status_code == 200, before.text
    before_snapshot = before.json()['result']['snapshot']
    expected = calculate(before_snapshot)
    assert len(before_snapshot['events']) == 12

    assert archive(client, headers, '2026-10-01').json()['state'] == 'archived'
    after = cohort(client, headers)
    assert after.status_code == 200, after.text
    snapshot = after.json()['result']['snapshot']
    actual = calculate(snapshot)

    assert len(snapshot['events']) == 12
    assert len({e['code'] for e in snapshot['events']}) == 12
    assert actual['learners'] == expected['learners'] == 6
    assert actual['unique_learners_before_cutoff'] == expected['unique_learners_before_cutoff'] == 6
    assert [(r['size'], [c['active'] for c in r['cells']]) for r in actual['rows']] == \
           [(r['size'], [c['active'] for c in r['cells']]) for r in expected['rows']]
    assert actual['event_sources'] == {'total': 12, 'archived': 12, 'online': 0}

    # The earlier frozen plan is byte-for-byte unchanged, and its checksum still
    # recomputes from the snapshot it froze.
    earlier = client.get(f"/cohorts/{before.json()['id']}", headers=headers).json()
    assert earlier['result']['snapshot'] == before_snapshot
    assert calculate(earlier['result']['snapshot'])['checksum'] == expected['checksum']


def test_damaged_or_missing_batch_blocks_new_computation_and_names_batch(client):
    headers = login(client, 'admin')
    add(client, headers, 'CQ-OLD-4', 'CQ-1', '2026-09-01T02:00:00+00:00')
    job = archive(client, headers, '2026-10-01').json()
    path = archive_service.batch_path(job['id'])

    path.write_text('{"job_id": ' + str(job['id']) + ', "events": [', encoding='utf-8')
    blocked = cohort(client, headers, name='损坏后阻止', start_week='2026-08-31', end_week='2026-08-31', horizon=2)
    assert blocked.status_code == 409
    assert f'批次 #{job["id"]}' in blocked.json()['detail']
    assert str(path) in blocked.json()['detail']

    health = client.get('/archives/health', headers=headers).json()
    assert health['ok'] is False and health['problems'][0]['job_id'] == job['id']

    path.unlink()
    missing = cohort(client, headers, name='缺失后阻止', start_week='2026-08-31', end_week='2026-08-31', horizon=2)
    assert missing.status_code == 409 and '缺失' in missing.json()['detail']

    with Session() as s:
        assert s.query(Cohort).count() == 0


def test_retry_after_write_interruption_does_not_move_twice(client, monkeypatch):
    headers = login(client, 'admin')
    for i in range(3):
        add(client, headers, f'CQ-R{i}', f'R-{i}', '2026-09-10T02:00:00+00:00')

    original_move = archive_service._move
    calls = {'n': 0}

    def interrupted(session, job, selected, events):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('crash after the batch was written, before commit')
        return original_move(session, job, selected, events)

    monkeypatch.setattr(archive_service, '_move', interrupted)
    failed = archive(client, headers, '2026-10-01')
    assert failed.status_code == 200, failed.text
    assert failed.json()['state'] == 'failed'

    with Session() as s:
        job = s.query(ArchiveJob).one()
        assert job.state == 'failed'
        # Nothing moved yet, but the verified batch file exists.
        assert s.query(Event).count() == 3 and s.query(ArchivedEvent).count() == 0
        path = archive_service.batch_path(job.id)
        written = json.loads(path.read_text())
        assert written['count'] == 3

    resumed = client.post(f'/archives/{job.id}/retry', headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['state'] == 'archived' and resumed.json()['archived_count'] == 3

    with Session() as s:
        assert s.query(Event).count() == 0 and s.query(ArchivedEvent).count() == 3
        # The resumed run reused the verified file instead of rewriting the batch.
        assert json.loads(path.read_text()) == written


def test_cohort_creation_waits_for_in_flight_archive_and_never_sees_half_batch(client, monkeypatch):
    headers = login(client, 'admin')
    for i in range(5):
        add(client, headers, f'CQ-C{i}', f'C-{i}', '2026-09-25T02:00:00+00:00')

    original_move = archive_service._move
    entered = threading.Event()

    def slow_move(session, job, selected, events):
        entered.set()
        time.sleep(0.4)  # hold the ledger guard mid-move
        return original_move(session, job, selected, events)

    monkeypatch.setattr(archive_service, '_move', slow_move)
    result = {}

    def run_archive():
        with Session() as s:
            job = s.query(ArchiveJob).filter_by(state='pending').one()
            archive_service.process(s, job.id)
            result['state'] = s.get(ArchiveJob, job.id).state

    with Session() as s:
        s.add(ArchiveJob(cutoff='2026-10-01', state='pending', created_at=archive_service.now_iso()))
        s.commit()
        job_id = s.query(ArchiveJob).one().id

    thread = threading.Thread(target=run_archive)
    thread.start()
    assert entered.wait(5), 'archive never reached the move step'

    # This call must block on the ledger guard until the archive commits, then
    # freeze the complete archived set rather than a half-moved one.
    created = cohort(client, headers, name='交错建队列', start_week='2026-09-21',
                     end_week='2026-09-21', horizon=1)
    thread.join(5)
    monkeypatch.setattr(archive_service, '_move', original_move)

    assert result['state'] == 'archived'
    assert created.status_code == 200, created.text
    snapshot = created.json()['result']['snapshot']
    assert len(snapshot['events']) == 5
    assert len({e['code'] for e in snapshot['events']}) == 5
    assert all(e['archive_job_id'] == job_id for e in snapshot['events'])
    sources = calculate(snapshot)['event_sources']
    assert sources == {'total': 5, 'archived': 5, 'online': 0}


def test_job_states_are_visible_and_resumable(client, monkeypatch):
    """worker path: enqueue accepted -> job is 'pending' until someone runs it."""
    headers = login(client, 'admin')
    add(client, headers, 'CQ-P1', 'P-1', '2026-09-10T02:00:00+00:00')
    monkeypatch.setattr(archive_service, 'enqueue', lambda job_id: True)
    job = archive(client, headers, '2026-10-01').json()
    assert job['state'] == 'pending'

    listed = client.get('/archives', headers=headers).json()
    assert listed[0]['id'] == job['id'] and listed[0]['state'] == 'pending'

    monkeypatch.setattr(archive_service, 'enqueue', lambda job_id: False)
    resumed = client.post(f"/archives/{job['id']}/retry", headers=headers)
    assert resumed.status_code == 200 and resumed.json()['state'] == 'archived'
    assert resumed.json()['archived_count'] == 1


def test_ledger_flags_events_from_a_damaged_batch(client):
    headers = login(client, 'admin')
    add(client, headers, 'CQ-D1', 'D-1', '2026-09-10T02:00:00+00:00')
    job = archive(client, headers, '2026-10-01').json()
    archive_service.batch_path(job['id']).write_text('not json', encoding='utf-8')

    row = next(e for e in client.get('/events', headers=headers).json() if e['code'] == 'CQ-D1')
    assert row['origin'] == 'archive'
    assert '损坏' in row['archive_problem']


def test_revision_arriving_during_archive_is_preserved_as_confirmed_correction(client, monkeypatch):
    """An edit that races with an in-flight archive must survive: it either lands
    before the snapshot (and the batch carries it) or after the move (and updates
    the archived copy).  It must never be dropped or duplicated online."""
    headers = login(client, 'admin')
    created = add(client, headers, 'CQ-E1', 'E-1', '2026-09-10T02:00:00+00:00', note='original').json()

    with Session() as s:
        s.add(ArchiveJob(cutoff='2026-10-01', state='pending', created_at=archive_service.now_iso()))
        s.commit()
        job_id = s.query(ArchiveJob).one().id

    original_move = archive_service._move
    entered = threading.Event()

    def slow_move(session, job, selected, events):
        entered.set()
        time.sleep(0.4)
        return original_move(session, job, selected, events)

    monkeypatch.setattr(archive_service, '_move', slow_move)
    outcome = {}

    def run_archive():
        with Session() as s:
            archive_service.process(s, job_id)
        outcome['done'] = True

    thread = threading.Thread(target=run_archive)
    thread.start()
    assert entered.wait(5)

    edit = client.put(
        f"/events/{created['id']}",
        json={'code': 'CQ-E1', 'learner': 'E-1', 'occurred_at': '2026-09-10T02:00:00+00:00',
              'course': '数据素养', 'note': '排班核对后的更正'},
        headers=headers,
    )
    thread.join(5)
    monkeypatch.setattr(archive_service, '_move', original_move)

    assert outcome.get('done') is True
    assert edit.status_code == 200, edit.text
    assert edit.json()['origin'] == 'archive'

    with Session() as s:
        # Exactly one copy of the event exists, holding the confirmed correction.
        assert s.query(Event).count() == 0
        rows = s.query(ArchivedEvent).filter_by(code='CQ-E1').all()
        assert len(rows) == 1 and rows[0].note == '排班核对后的更正'
        job = s.get(ArchiveJob, job_id)
        assert job.state == 'archived'
        payload = json.loads(archive_service.batch_path(job_id).read_text())
        assert payload['events'][0]['note'] == 'original'
        # Health compares counts (not revised content), so a documented revision
        # after the move is not misreported as corruption.
        assert archive_service.verify_archives(s) == []


def test_archive_leaves_existing_intervention_list_untouched(client):
    """Freeze -> compute -> draft an intervention -> archive.  The signed-off
    evidence must not change; only future snapshots see the merged history."""
    from src.worker import handle_cohort
    headers = login(client, 'admin')
    for i in range(5):
        add(client, headers, f'CQ-I{i}', f'I-{i}', f'2026-09-{7 + i:02d}T02:00:00+00:00')
        if i < 3:
            add(client, headers, f'CQ-I{i}-R', f'I-{i}', f'2026-09-{14 + i:02d}T02:00:00+00:00')

    plan = cohort(client, headers, name='干预对照', horizon=2).json()
    handle_cohort(plan['id'])
    detail = client.get(f"/cohorts/{plan['id']}", headers=headers).json()
    assert detail['state'] == 'done', detail.get('error')
    row = detail['result']['rows'][0]
    target = next(c for c in row['cells'] if c['week_index'] == 1 and c['mature'] and c['missing'])

    made = client.post('/interventions', json={
        'cohort_id': plan['id'], 'cohort_week': row['cohort_week'],
        'week_index': target['week_index'], 'note': '核对排班后安排站内学习辅导',
    }, headers=headers)
    assert made.status_code == 200, made.text
    signed = client.post(f"/interventions/{made.json()['id']}/approve", json={}, headers=headers)
    assert signed.status_code == 200
    before = signed.json()

    assert archive(client, headers, '2026-10-01').json()['state'] == 'archived'

    after = next(a for a in client.get('/interventions', headers=headers).json() if a['id'] == before['id'])
    assert after == before
    frozen = client.get(f"/cohorts/{plan['id']}", headers=headers).json()
    assert frozen['result']['snapshot'] == plan['result']['snapshot']
    assert frozen['result']['checksum'] == detail['result']['checksum']


def test_seeded_scale_history_is_identical_before_and_after_archive(client):
    """200 learners, duplicate same-week visits, pre-window history and future
    events: the frozen matrix must be numerically identical across an archive."""
    import random
    from datetime import date, timedelta
    from src.worker import handle_cohort

    headers = login(client, 'admin')
    rng = random.Random(3114)
    start = date(2026, 9, 21)
    for i in range(200):
        first = i // 25
        learner = f'CQ-{i + 1:03d}'
        for w in range(first, 9):
            if w != first and rng.random() > .82 - .07 * (w - first):
                continue
            day = start + timedelta(weeks=w, days=1 + i % 3)
            add(client, headers, f'{learner}-W{w}', learner, day.isoformat() + 'T02:00:00+00:00')
            if i % 17 == 0:
                add(client, headers, f'{learner}-W{w}-DUP', learner, day.isoformat() + 'T04:00:00+00:00')
    # A pre-window learner must not be reassigned, and future events stay excluded.
    add(client, headers, 'CQ-OLD', 'CQ-OLD', '2026-08-01T02:00:00+00:00')
    add(client, headers, 'CQ-FUT', 'CQ-001', '2026-12-01T02:00:00+00:00')

    cfg = dict(name='规模化对照', start_week='2026-09-21', end_week='2026-11-09',
               as_of='2026-11-14', horizon=5)
    plan = client.post('/cohorts', json=cfg, headers=headers).json()
    handle_cohort(plan['id'])
    before = client.get(f"/cohorts/{plan['id']}", headers=headers).json()
    assert before['state'] == 'done', before.get('error')

    job = archive(client, headers, '2026-11-01').json()
    assert job['state'] == 'archived' and job['archived_count'] > 0
    after_plan = client.post('/cohorts', json=cfg, headers=headers).json()
    handle_cohort(after_plan['id'])
    after = client.get(f"/cohorts/{after_plan['id']}", headers=headers).json()
    assert after['state'] == 'done', after.get('error')

    def grid(result):
        return [(r['cohort_week'], r['size'], r['members'],
                [(c['week_index'], c['mature'], c['active'], c['rate'], c['visited'], c['missing'])
                 for c in r['cells']]) for r in result['rows']]

    assert grid(after['result']) == grid(before['result'])
    for key in ('learners', 'unique_learners_before_cutoff', 'future_events_excluded', 'week_hits'):
        assert after['result'][key] == before['result'][key], key
    assert after['result']['event_sources']['archived'] == job['archived_count']
    assert before['result']['event_sources']['archived'] == 0


def test_archive_job_requires_a_unique_active_batch(client):
    headers = login(client, 'admin')
    add(client, headers, 'CQ-Q1', 'Q-1', '2026-09-10T02:00:00+00:00')
    with Session() as s:
        s.add(ArchiveJob(cutoff='2026-10-01', state='pending', created_at=archive_service.now_iso()))
        s.commit()
    blocked = archive(client, headers, '2026-10-02')
    assert blocked.status_code == 409 and '待处理' in blocked.json()['detail']


def test_event_edit_conflict_keeps_code_unique_across_tiers(client):
    headers = login(client)
    add(client, headers, 'CQ-U1', 'U-1', '2026-09-01T02:00:00+00:00')
    add(client, headers, 'CQ-U2', 'U-1', '2026-11-01T02:00:00+00:00')
    archive(client, headers, '2026-10-01')
    archived = next(e for e in client.get('/events', headers=headers).json() if e['code'] == 'CQ-U1')

    r = client.put(
        f"/events/{archived['record_ref']}",
        json={'code': 'CQ-U2', 'learner': 'U-1', 'occurred_at': '2026-09-01T02:00:00+00:00',
              'course': '数据素养', 'note': ''},
        headers=headers,
    )
    assert r.status_code == 409

    with Session() as s:
        assert s.query(ArchivedEvent).filter_by(code='CQ-U1').one()
        assert s.query(Event).filter_by(code='CQ-U2').one()


def test_online_edit_cannot_take_an_archived_code(client):
    headers = login(client)
    add(client, headers, 'CQ-V1', 'V-1', '2026-09-01T02:00:00+00:00')
    online = add(client, headers, 'CQ-V2', 'V-2', '2026-11-01T02:00:00+00:00').json()
    archive(client, headers, '2026-10-01')

    r = client.put(
        f"/events/{online['id']}",
        json={'code': 'CQ-V1', 'learner': 'V-2', 'occurred_at': '2026-11-01T02:00:00+00:00',
              'course': '数据素养', 'note': ''},
        headers=headers,
    )
    assert r.status_code == 409
    codes = {e['code'] for e in client.get('/events', headers=headers).json()}
    assert codes == {'CQ-V1', 'CQ-V2'}
