import json, os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import tools, pipeline, runlog


@asynccontextmanager
async def lifespan(app):
    try:
        print("index:", tools.build_index())
    except Exception as e:  # DB/Ollama not up yet: index is built lazily on first question
        print("index build deferred:", e)
    yield


app = FastAPI(lifespan=lifespan)


class Q(BaseModel):
    question: str
    history: list[str] = []
    clarification: dict | None = None  # {original, asked} when this message answers a clarifying question


@app.get("/api/health")
def health():
    out = {"index_items": len(tools.ITEMS)}
    try:
        with tools.conn() as c:
            cursor = c.cursor()
            try:
                cursor.execute("SELECT 1")
                out["db"] = "ok"
            finally:
                cursor.close()
        #with tools.conn() as c: c.execute("SELECT 1"); out["db"] = "ok"
    except Exception as e: out["db"] = str(e)
    try:
        import ollama; names = [m.model for m in ollama.list().models]
        out["ollama"] = "ok"; out["missing_models"] = [m for m in (tools.SQL_MODEL, tools.CHAT_MODEL, tools.EMBED_MODEL)
                                                     if not any(n.startswith(m.split(":")[0]) for n in names)]
    except Exception as e: out["ollama"] = str(e)
    return out


@app.post("/api/reindex")
def reindex():
    return tools.build_index()


@app.get("/api/logs")
def logs(limit: int = 50):
    """Most recent executions (newest first), each with all steps and durations."""
    return runlog.read_runs(limit)


@app.get("/api/logs/{run_id}")
def log_one(run_id: str):
    return (runlog.read_runs(run_id=run_id) or [{"error": "run not found"}])[0]


@app.post("/api/chat")
def chat(q: Q):
    def gen():
        log = runlog.RunLog(q.question, q.history, q.clarification)
        try:
            for e in pipeline.run(q.question, q.history, q.clarification):
                log.event(e)
                if e["step"] == "final": e = {**e, "run_id": log.run_id}
                slim = {k: v for k, v in e.items() if k not in ("inp", "out", "calls")}   # full input/output only go to the logs
                yield f"data: {json.dumps(slim, default=str)}\n\n"
        except Exception as ex:
            log.finish(error=repr(ex))
            yield f"data: {json.dumps({'step': 'final', 'answer': f'Something went wrong: {ex}', 'run_id': log.run_id})}\n\n"
        finally:
            log.finish()   # no-op if already finished; records "aborted" if the client disconnected mid-run
    return StreamingResponse(gen(), media_type="text/event-stream")


DIST = os.getenv("FRONTEND_DIST", os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"))
if os.path.isdir(DIST):  # serve the built React app from the same port
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
