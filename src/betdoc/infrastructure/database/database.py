from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
import os

DATABASE_URL = os.environ.get('BETDOC_DATABASE_URL', 'postgresql+asyncpg://betdoc:testpass123@postgres:5432/betdoc')

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

import betdoc.infrastructure.database.models
