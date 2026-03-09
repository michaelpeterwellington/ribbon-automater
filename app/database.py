from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.db_path}",
    echo=settings.debug,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Safe column additions for existing databases (SQLite ignores duplicate ADD COLUMN errors)
        for sql in [
            "ALTER TABLE upgrade_jobs ADD COLUMN backup_path TEXT",
            "ALTER TABLE upgrade_jobs ADD COLUMN upload_bytes_sent INTEGER",
            "ALTER TABLE firmware_files ADD COLUMN platform_tag TEXT",
            "ALTER TABLE devices ADD COLUMN hypervisor_type TEXT",
            "ALTER TABLE devices ADD COLUMN cert_common_name TEXT",
            "ALTER TABLE devices ADD COLUMN cert_expiry TEXT",
        ]:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # Column already exists
