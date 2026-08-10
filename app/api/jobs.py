from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import session_scope
from app.core.config import settings
from app.models import GenerationJob, JobEvent
from app.schemas.api import JobCreate, JobRead
from app.core.enums import JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
def db_session():
    with session_scope(settings.database_url) as session: yield session

@router.post("", response_model=JobRead, status_code=201)
def create_job(data: JobCreate, session: Session = Depends(db_session)):
    job = GenerationJob(project_id=data.project_id, type=data.type, status=JobStatus.QUEUED, input_json=data.input_json)
    session.add(job); session.flush(); session.add(JobEvent(job_id=job.id, sequence=1, event_type="queued", progress=0, message="Job queued")); session.flush(); return job

@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, session: Session = Depends(db_session)):
    job = session.get(GenerationJob, job_id)
    if not job: raise HTTPException(404, "job not found")
    return job

@router.get("/{job_id}/events")
def get_events(job_id: str, session: Session = Depends(db_session)):
    if not session.get(GenerationJob, job_id): raise HTTPException(404, "job not found")
    return session.query(JobEvent).filter_by(job_id=job_id).order_by(JobEvent.sequence).all()
