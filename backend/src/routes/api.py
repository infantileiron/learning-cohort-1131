import os,json,logging
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from src.config.database import db
from src.models.entities import User,Cohort,Intervention,Audit,ArchiveJob
from src.schemas.business import Login,EventInput,CohortInput,InterventionInput,NoteInput,ArchiveInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.interventions import draft
from src.services import archive as archive_service
from src.services import ledger as ledger_service
from src.services.locking import ledger_guard
from src.controllers.business import ensure_cohort,ensure_intervention
log=logging.getLogger(__name__)
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/events')
def events(u=Depends(current),s=Depends(db)):
 problems=archive_service.verify_archives(s)
 return ledger_service.ledger(s,problems)
@router.post('/events')
def event(data:EventInput,u=Depends(editor),s=Depends(db)):
 with ledger_guard(s):
  row,created=ledger_service.create_event(s,data)
  if created:audit(s,u,'录入学习事件 '+row['code'])
 return {**row,'created':created}
@router.put('/events/{i}')
def edit_event(i:str,data:EventInput,u=Depends(editor),s=Depends(db)):
 with ledger_guard(s):
  row=ledger_service.edit_event(s,i,data)
  audit(s,u,'修订学习事件 '+row['code'])
 return row
@router.post('/archives')
def create_archive(data:ArchiveInput,u=Depends(editor),s=Depends(db)):
 with ledger_guard(s):
  pending=s.query(ArchiveJob).filter(ArchiveJob.state.in_(['pending','processing'])).first()
  if pending:raise HTTPException(409,f'已有归档任务 #{pending.id} 待处理，请完成后再建新批次')
  job=ArchiveJob(cutoff=data.cutoff.isoformat(),state='pending',created_at=archive_service.now_iso())
  s.add(job);s.commit()
 jid=job.id;cutoff=job.cutoff
 queued=archive_service.enqueue(jid)
 if not queued:
  try:archive_service.process(s,jid)
  except Exception:
   # The job row carries the failure; the page offers a resumable retry.
   log.exception('archive job %s failed',jid)
 audit(s,u,f'归档业务日以前事件，截至 {cutoff}，批次 #{jid}')
 return dump(s.get(ArchiveJob,jid))
@router.get('/archives')
def archives(u=Depends(current),s=Depends(db)):
 problems={p['job_id']:p for p in archive_service.verify_archives(s)}
 return [{**dump(r),'problem':problems.get(r.id)} for r in s.query(ArchiveJob).order_by(ArchiveJob.id.desc()).all()]
@router.get('/archives/health')
def archive_health(u=Depends(current),s=Depends(db)):
 problems=archive_service.verify_archives(s)
 return {'ok':not problems,'problems':problems}
@router.get('/archives/{i}')
def archive_detail(i:int,u=Depends(current),s=Depends(db)):
 r=s.get(ArchiveJob,i)
 if not r:raise HTTPException(404,'归档任务不存在')
 problems={p['job_id']:p for p in archive_service.verify_archives(s)}
 return {**dump(r),'problem':problems.get(r.id)}
@router.post('/archives/{i}/retry')
def retry_archive(i:int,u=Depends(editor),s=Depends(db)):
 r=s.get(ArchiveJob,i)
 if not r:raise HTTPException(404,'归档任务不存在')
 if r.state=='archived':raise HTTPException(409,'该批次已归档，无需重试')
 try:archive_service.process(s,r.id)
 except Exception as exc:
  log.exception('archive retry %s failed',i)
  if isinstance(exc,archive_service.ArchiveError):s.rollback()
 audit(s,u,f'续跑归档批次 #{i}');return dump(s.get(ArchiveJob,i))
@router.get('/cohorts')
def cohorts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Cohort).order_by(Cohort.id.desc()).all()]
@router.post('/cohorts')
def cohort(data:CohortInput,u=Depends(editor),s=Depends(db)):
 cfg=data.model_dump(mode='json')
 with ledger_guard(s):
  archive_service.assert_archives_healthy(s)
  events=ledger_service.all_events(s)
  snapshot={'config':cfg,'events':events,'event_sources':{'total':len(events)}}
  r=Cohort(**cfg,result={'snapshot':snapshot});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('cohort:jobs',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，请稍后重试';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交留存队列 '+str(r.id));return dump(r)
@router.get('/cohorts/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_cohort(s,i))
@router.get('/cohorts/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_cohort(s,i)
 if r.state!='done':raise HTTPException(409,'计算成功后才能导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="learning-cohort-{i}.json"'})
@router.get('/interventions')
def interventions(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Intervention).order_by(Intervention.id.desc()).all()]
@router.post('/interventions')
def intervention(data:InterventionInput,u=Depends(editor),s=Depends(db)):
 r=draft(s,ensure_cohort(s,data.cohort_id),data);audit(s,u,'生成干预清单 '+str(r.id));return dump(r)
@router.put('/interventions/{i}')
def edit_intervention(i:int,data:NoteInput,u=Depends(editor),s=Depends(db)):
 r=ensure_intervention(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核清单不可修改')
 r.note=data.note;s.commit();audit(s,u,'修订干预说明 '+str(i));return dump(r)
@router.post('/interventions/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure_intervention(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核，不可重复签核')
 r.status='approved';s.commit();audit(s,u,'签核干预清单 '+str(i));return dump(r)
@router.get('/interventions/{i}/export')
def action_export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_intervention(s,i);return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
