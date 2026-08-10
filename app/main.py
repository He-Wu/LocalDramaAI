from fastapi import FastAPI
from app.api.projects import router as projects_router
from app.api.jobs import router as jobs_router
from app.db.session import create_schema
from app.core.config import settings

app = FastAPI(title="LocalDramaAI", version="0.1.0")
app.include_router(projects_router); app.include_router(jobs_router)

@app.on_event("startup")
def startup(): create_schema(settings.database_url)

@app.get("/health")
def health(): return {"status": "ok", "service": "api"}
