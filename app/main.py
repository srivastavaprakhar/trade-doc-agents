"""FastAPI backend: run the pipeline, read results, answer NL queries.

Start: .venv/bin/uvicorn app.main:app --port 8000
UI:    http://localhost:8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db
from .graph import run_pipeline
from .nlquery import answer_question

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DOCS = ROOT / "sample_docs"
UPLOADS = ROOT / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="Trade Document Agents")
db.init_db()


class RunRequest(BaseModel):
    doc_name: str
    customer_id: str = "customer_x"


class QueryRequest(BaseModel):
    question: str


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/documents")
def documents():
    docs = sorted(p.name for p in SAMPLE_DOCS.glob("*") if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"))
    return {"documents": docs}


@app.post("/api/run")
async def run(req: RunRequest):
    path = SAMPLE_DOCS / req.doc_name
    if not path.exists():
        raise HTTPException(404, f"unknown sample document {req.doc_name!r}")
    state = await run_pipeline(str(path), req.customer_id)
    return db.get_run(state.run_id)


@app.post("/api/run/upload")
async def run_upload(file: UploadFile):
    if Path(file.filename).suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg"):
        raise HTTPException(400, "only PDF/PNG/JPG documents are supported")
    dest = UPLOADS / Path(file.filename).name
    dest.write_bytes(await file.read())
    state = await run_pipeline(str(dest))
    return db.get_run(state.run_id)


@app.post("/api/resume/{run_id}")
async def resume(run_id: str):
    try:
        state = await run_pipeline("", run_id=run_id, resume=True)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return db.get_run(state.run_id)


@app.get("/api/runs")
def runs():
    return {"runs": db.list_runs()}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    rec = db.get_run(run_id)
    if rec is None:
        raise HTTPException(404, "unknown run_id")
    return rec


@app.post("/api/query")
async def query(req: QueryRequest):
    return await answer_question(req.question)
