/** @typedef {{id:number,record_ref:string,code:string,learner:string,occurred_at:string,course:string,note:string,origin:'online'|'archive',archive_job_id:number|null,archive_problem:string|null}} Event */
/** @typedef {{id:number,name:string,start_week:string,end_week:string,as_of:string,horizon:number,state:string,result:object}} Cohort */
/** @typedef {{id:number,cohort_id:number,cohort_week:string,week_index:number,members:string[],note:string,status:string}} Intervention */
/** @typedef {{id:number,cutoff:string,state:'pending'|'processing'|'archived'|'failed',candidate_count:number,archived_count:number,batch_checksum:string,error:string,created_at:string,started_at:string,finished_at:string,problem:object|null}} ArchiveJob */
