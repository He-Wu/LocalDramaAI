import asyncio
from app.core.config import settings
from app.db.session import initialize_database
from app.workers.worker import LocalDramaWorker

def startup():
    initialize_database(settings.database_url)

if __name__ == "__main__":
    startup()
    asyncio.run(LocalDramaWorker().run())
