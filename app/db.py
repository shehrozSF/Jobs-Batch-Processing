from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from app.config import get_settings

# Job lifecycle:  queued -> running -> succeeded
#                                   -> dead     (failed after every retry: the dead-letter list)
#                                   -> paused   (the batch was halted, e.g. billing or quota)
QUEUED, RUNNING, SUCCEEDED, DEAD, PAUSED = "queued", "running", "succeeded", "dead", "paused"
STATUSES = (QUEUED, RUNNING, SUCCEEDED, DEAD, PAUSED)

JSONType = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Set when an error makes every remaining job pointless (402, spent quota).
    halted_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="batch")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default=QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    result: Mapped[dict | None] = mapped_column(JSONType, default=None)

    # Summed over every model call for this job, repair turns and retries included.
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    batch: Mapped[Batch] = relationship(back_populates="jobs")


@lru_cache
def get_engine(url: str | None = None):
    url = url or get_settings().database_url
    if url.startswith("sqlite"):  # tests: one shared in-memory database
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(url, pool_pre_ping=True)


def new_session() -> Session:
    return Session(get_engine(), expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(get_engine())
