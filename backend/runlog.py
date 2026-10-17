import json, logging, logging.handlers, os, time, uuid
from datetime import datetime, timezone
import tools as T

LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_FULL = os.getenv("LOG_FULL", "1") != "0"
LOG_ROWS = int(os.getenv("LOG_ROWS", "20"))
MAX_STR, MAX_ITEMS = int(os.getenv("LOG_MAX_STR", "20000")), int(os.getenv("LOG_MAX_ITEMS", "500"))
os.makedirs(LOG_DIR, exist_ok=True)


def _logger(name, fname=None):
    lg = logging.getLogger(name); lg.setLevel(logging.INFO); lg.propagate = False
    if not lg.handlers:
        h = (logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, fname), maxBytes=20_000_000, backupCount=5, encoding="utf-8")
             if fname else logging.StreamHandler())
        h.setFormatter(logging.Formatter("%(message)s")); lg.addHandler(h)
    return lg


EVENTS, RUNS, CONSOLE = _logger("dbchat.events", "events.jsonl"), _logger("dbchat.runs", "runs.jsonl"), _logger("dbchat.console")
now_iso = lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _trim(v):
    """Cap huge values; a marker records exactly what was cut."""
    if isinstance(v, str): return v if len(v) <= MAX_STR else v[:MAX_STR] + f"...[truncated {len(v) - MAX_STR} chars]"
    if isinstance(v, dict): return {k: _trim(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        out = [_trim(x) for x in v[:MAX_ITEMS]]
        return out + [f"...[truncated {len(v) - MAX_ITEMS} items]"] if len(v) > MAX_ITEMS else out
    return v


class RunLog:
    def __init__(self, question, history=None, clarification=None):
        self.run_id = uuid.uuid4().hex[:12]
        self.request = {"question": question, "history": history or [], "clarification": clarification} if LOG_FULL else None
        self.clarified = bool(clarification); self.started = now_iso()
        self.t0 = self.last = time.perf_counter()
        self.open, self.steps, self.final, self.done = {}, [], None, False

    def event(self, e):
        now = time.perf_counter()
        if e["step"] == "final":
            self.final = e; return
        if e["status"] == "running":
            self.open[e["step"]] = now; return
        start = self.open.pop(e["step"], self.last)
        dur = e["ms"] if e.get("ms") is not None else round((now - start) * 1000)   # measured around the work itself
        det = e.get("detail") or {}
        rec = {"seq": len(self.steps) + 1, "run_id": self.run_id, "ts": now_iso(), "step": e["step"], "title": e["title"], "status": e["status"],
               "offset_ms": round((start - self.t0) * 1000), "duration_ms": dur,
               "input": _trim(e.get("inp")) if LOG_FULL else None, "output": _trim(e.get("out")) if LOG_FULL else None,
               "llm_calls": _trim(e.get("calls") or []) if LOG_FULL else [],
               "error": (det.get("error") or det.get("result")) if e["status"] == "error" else None}
        self.steps.append(rec); self.last = now
        EVENTS.info(json.dumps(rec, default=str))
        CONSOLE.info(json.dumps({"run_id": self.run_id, "step": rec["step"], "status": rec["status"], "duration_ms": dur}))

    def finish(self, error=None):
        if self.done: return
        self.done = True
        f = self.final or {}
        route = next((s["output"] for s in self.steps if s["step"] == "route"), None) or {}
        blocked = next((s for s in self.steps if s["step"] == "search" and s["status"] == "error"), None)
        if error: outcome, reason = "error", error
        elif not self.final: outcome, reason = "aborted", "client disconnected"
        elif f.get("clarify"): outcome, reason = "clarification_asked", f["clarify"]["kind"]
        elif route.get("intent") == "small_talk": outcome, reason = "small_talk", None
        elif blocked: outcome, reason = "not_answered", blocked["error"] or "no match"
        elif f.get("columns") is not None: outcome, reason = "answered", None if f.get("rows") else "query returned no rows"
        else: outcome, reason = "failed", f.get("answer")
        rows = f.get("rows")
        calls = [c for s in self.steps for c in s["llm_calls"] if isinstance(c, dict)]
        resp = {"answer": f.get("answer"), "sql": f.get("sql"), "columns": f.get("columns"), "row_count": len(rows) if rows is not None else None,
                "rows_sample": _trim(rows[:LOG_ROWS]) if rows else [], "chart_built": bool(f.get("chart")), "clarify": f.get("clarify")} if LOG_FULL else \
               {"row_count": len(rows) if rows is not None else None, "chart_built": bool(f.get("chart")), "clarify_kind": (f.get("clarify") or {}).get("kind")}
        rec = {"schema_version": 2, "run_id": self.run_id, "started_at": self.started, "ended_at": now_iso(),
               "duration_ms": round((time.perf_counter() - self.t0) * 1000), "outcome": outcome, "reason": reason,
               "after_clarification": self.clarified, "request": self.request,
               "config": {"models": {"sql": T.SQL_MODEL, "chat": T.CHAT_MODEL, "embed": T.EMBED_MODEL},
                          "similarity_threshold": T.THRESH, "confirm_band": T.CONFIRM_BAND},
               "response": resp,
               "totals": {"steps": len(self.steps), "errors": sum(s["status"] == "error" for s in self.steps),
                          "llm_calls": len(calls), "llm_ms": sum(c.get("duration_ms", 0) for c in calls),
                          "db_ms": sum(s["duration_ms"] for s in self.steps if s["step"] in ("schema", "execute"))},
               "steps": self.steps}
        RUNS.info(json.dumps(rec, default=str))


def read_runs(limit=50, run_id=None):
    path = os.path.join(LOG_DIR, "runs.jsonl")
    if not os.path.exists(path): return []
    with open(path, encoding="utf-8") as fh:
        recs = [json.loads(l) for l in fh if l.strip()]
    if run_id: return [r for r in recs if r["run_id"] == run_id]
    return recs[-limit:][::-1]
