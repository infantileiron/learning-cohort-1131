from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Event(Base):
 __tablename__='events'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);learner=Column(String(60),nullable=False,index=True);occurred_at=Column(String(40),nullable=False);course=Column(String(40));note=Column(Text,default='')
class Cohort(Base):
 __tablename__='cohorts'
 id=Column(Integer,primary_key=True);name=Column(String(100));start_week=Column(String(10));end_week=Column(String(10));as_of=Column(String(10));horizon=Column(Integer);state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class Intervention(Base):
 __tablename__='interventions'
 id=Column(Integer,primary_key=True);cohort_id=Column(Integer,ForeignKey('cohorts.id'));cohort_week=Column(String(10));week_index=Column(Integer);note=Column(Text);members=Column(JSON);status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
class ArchiveJob(Base):
 __tablename__='archive_jobs'
 id=Column(Integer,primary_key=True);cutoff=Column(String(10),nullable=False,index=True);state=Column(String(20),default='pending',index=True)
 candidate_count=Column(Integer,default=0);archived_count=Column(Integer,default=0);batch_checksum=Column(String(64),default='')
 error=Column(Text,default='');created_at=Column(String(40));started_at=Column(String(40));finished_at=Column(String(40))
class ArchivedEvent(Base):
 __tablename__='archived_events'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);learner=Column(String(60),nullable=False,index=True);occurred_at=Column(String(40),nullable=False);course=Column(String(40));note=Column(Text,default='')
 origin_event_id=Column(Integer,nullable=True,index=True)
 archive_job_id=Column(Integer,ForeignKey('archive_jobs.id'),nullable=False,index=True)
class EventLock(Base):
 __tablename__='event_locks'
 id=Column(Integer,primary_key=True)
