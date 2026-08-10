from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import session_scope
from app.core.config import settings
from app.models import Project
from app.schemas.api import ProjectCreate, ProjectRead

router = APIRouter(prefix="/api/projects", tags=["projects"])

def db_session():
    with session_scope(settings.database_url) as session: yield session

@router.post("", response_model=ProjectRead, status_code=201)
def create_project(data: ProjectCreate, session: Session = Depends(db_session)):
    project = Project(**data.model_dump()); session.add(project); session.flush(); return project

@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, session: Session = Depends(db_session)):
    from fastapi import HTTPException
    project = session.get(Project, project_id)
    if not project: raise HTTPException(404, "project not found")
    return project
