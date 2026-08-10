import asyncio
from app.core.config import settings
from app.db.session import create_schema
from app.workers.worker import LocalDramaWorker

if __name__ == "__main__":
    create_schema(settings.database_url)
    asyncio.run(LocalDramaWorker().run())
