from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import service
from app.db import Batch, Job, init_db, new_session
from app.queue import enqueue_jobs

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Batch extraction", lifespan=lifespan)


def get_session():
    with new_session() as session:
        yield session


def get_enqueue() -> Callable[[list[int]], None]:
    return enqueue_jobs  # replaced in tests


SessionDep = Annotated[Session, Depends(get_session)]
EnqueueDep = Annotated[Callable[[list[int]], None], Depends(get_enqueue)]


class PostingIn(BaseModel):
    source_key: str = Field(min_length=1)
    body: str = Field(min_length=1)


class BatchIn(BaseModel):
    name: str = "batch"
    postings: list[PostingIn] = Field(min_length=1)


def _start(session: Session, enqueue, name: str, postings: list[tuple[str, str]]) -> Batch:
    try:
        batch = service.create_batch(session, name, postings)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    enqueue(service.job_ids(session, batch.id))
    return batch


def _progress(session: Session, batch_id: int) -> dict:
    try:
        return service.progress(session, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---- JSON API ---------------------------------------------------------------

@app.post("/api/batches", status_code=201)
def create_batch(payload: BatchIn, session: SessionDep, enqueue: EnqueueDep) -> dict:
    postings = [(p.source_key, p.body) for p in payload.postings]
    batch = _start(session, enqueue, payload.name, postings)
    return {"id": batch.id, "jobs": len(postings)}


@app.get("/api/batches/{batch_id}")
def batch_progress(batch_id: int, session: SessionDep) -> dict:
    return _progress(session, batch_id)


@app.get("/api/batches/{batch_id}/jobs")
def batch_jobs(batch_id: int, session: SessionDep, status: str | None = None) -> list[dict]:
    _progress(session, batch_id)  # 404 for an unknown batch
    query = select(Job).where(Job.batch_id == batch_id).order_by(Job.id)
    if status:
        query = query.where(Job.status == status)
    return [
        {
            "id": j.id, "source_key": j.source_key, "status": j.status,
            "attempts": j.attempts, "error": j.error, "result": j.result,
            "cost_usd": round(j.cost_usd, 6),
        }
        for j in session.scalars(query)
    ]


@app.post("/api/batches/{batch_id}/retry")
def retry_batch(batch_id: int, session: SessionDep, enqueue: EnqueueDep) -> dict:
    try:
        ids = service.requeue(session, batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    enqueue(ids)
    return {"requeued": len(ids)}


# ---- HTML UI ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: SessionDep):
    batches = session.scalars(select(Batch).order_by(Batch.id.desc()).limit(50)).all()
    rows = [service.progress(session, b.id) for b in batches]
    return templates.TemplateResponse(request, "index.html", {"batches": rows})


@app.post("/batches")
async def upload_batch(
    session: SessionDep,
    enqueue: EnqueueDep,
    files: list[UploadFile],
    name: Annotated[str, Form()] = "upload",
):
    """One posting per uploaded .txt file; the file name is its source key."""
    postings = [(f.filename or f"file-{i}", (await f.read()).decode("utf-8", "replace"))
                for i, f in enumerate(files)]
    batch = _start(session, enqueue, name, postings)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(batch_id: int, request: Request, session: SessionDep):
    context = {"p": _progress(session, batch_id)}
    return templates.TemplateResponse(request, "batch.html", context)


@app.get("/batches/{batch_id}/progress", response_class=HTMLResponse)
def batch_progress_fragment(batch_id: int, request: Request, session: SessionDep):
    context = {"p": _progress(session, batch_id)}
    return templates.TemplateResponse(request, "_progress.html", context)


@app.post("/batches/{batch_id}/retry")
def retry_batch_form(batch_id: int, session: SessionDep, enqueue: EnqueueDep):
    retry_batch(batch_id, session, enqueue)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)
