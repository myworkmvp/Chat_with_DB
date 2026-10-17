"""Deterministic tool functions. Used by the pipeline AND exposed over MCP (mcp_server_postgres.py / mcp_server_mariadb.py).

The database engine is chosen with the DB_ENGINE env var ("postgres" or "mariadb"); all engine-specific code lives in
db_postgres.py / db_mariadb.py, which expose the same small interface (conn, fetch_meta, column_has_data, schema_info, run_query).
"""
import os, re, json, time, threading, importlib, numpy as np, ollama, sqlglot
from contextlib import contextmanager
from sqlglot import exp
from dotenv import load_dotenv
load_dotenv()

DB_ENGINE = os.getenv("DB_ENGINE", "mariadb").lower()
_BACKENDS = {"postgres": "db_postgres", "postgresql": "db_postgres", "mariadb": "db_mariadb", "mysql": "db_mariadb"}
if DB_ENGINE not in _BACKENDS:
    raise RuntimeError(f"Unknown DB_ENGINE={DB_ENGINE!r}; use 'postgres' or 'mariadb'")
DB = importlib.import_module(_BACKENDS[DB_ENGINE])
conn = DB.conn

SQL_MODEL = os.getenv("SQL_MODEL", "hf.co/mradermacher/OmniSQL-7B-GGUF:Q4_K_M")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3:4b-instruct")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
THRESH = float(os.getenv("SIM_THRESHOLD", "0.5"))
CONFIRM_BAND = float(os.getenv("CONFIRM_BAND", "0.05"))
ITEMS, VECS = [], None


_tl = threading.local()


@contextmanager
def record(sink: list):
    """Collect every LLM/embedding call made inside the block into `sink` (used by the pipeline for full logs)."""
    _tl.sink = sink
    try:
        yield
    finally:
        _tl.sink = None


def _rec(r):
    sink = getattr(_tl, "sink", None)
    if sink is not None:
        sink.append(r)


def _chat(**kw):
    t = time.perf_counter()
    base = {"type": "llm_chat", "model": kw.get("model"), "format": kw.get("format"), "options": kw.get("options"), "messages": kw.get("messages")}
    try:
        resp = ollama.chat(**kw)
    except Exception as e:
        _rec({**base, "error": repr(e), "duration_ms": round((time.perf_counter() - t) * 1000)}); raise
    _rec({**base, "response": resp["message"]["content"], "duration_ms": round((time.perf_counter() - t) * 1000)})
    return resp


def embed(texts):
    t = time.perf_counter()
    v = np.array(ollama.embed(model=EMBED_MODEL, input=texts)["embeddings"], dtype="float32")
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    _rec({"type": "embedding", "model": EMBED_MODEL, "input": texts, "dims": int(v.shape[1]), "duration_ms": round((time.perf_counter() - t) * 1000)})
    return v


def build_index():
    """Embed metadata once and flag columns whose every row is NULL."""
    global ITEMS, VECS
    tables, columns = DB.fetch_meta()
    items = []
    for t in tables:
        d = t["description"] or ""
        items.append(dict(kind="table", table=t["table_name"], column=None, pii=None, all_null=False,
                          desc=d, text=f"table {t['table_name']}: {d}"))
    for r in columns:
        d = r["description"] or ""
        try:
            has = DB.column_has_data(r["table_name"], r["column_name"])
        except Exception as exc:  # fail safe: never mark a column all-NULL just because the probe failed
            print(f"Could not inspect {r['table_name']}.{r['column_name']}: {exc}")
            has = True
        items.append(dict(kind="column", table=r["table_name"], column=r["column_name"], pii=r["pii_type"],
                          all_null=not has, desc=d, text=f"column {r['table_name']}.{r['column_name']}: {d}"))
    VECS = embed([i["text"] for i in items])
    ITEMS = items
    return {"engine": DB.ENGINE, "items": len(items),
            "all_null_columns": sum(i["kind"] == "column" and i["all_null"] for i in items)}


def search_metadata(question: str, k: int = 8) -> dict:
    """Similarity search over table/column descriptions. status: ok | unknown | no_data.
    Also returns `closest` (nearest tables even below the threshold) and `weak` (best score only just above it)."""
    if VECS is None:
        build_index()
    if not ITEMS or VECS is None or len(VECS) == 0:
        return {"status": "no_data", "hits": [], "closest": [], "best": 0.0, "weak": True}
    qv = embed([question])[0]
    if VECS.shape[1] != len(qv):
        raise ValueError(f"Embedding dimension mismatch: index={VECS.shape[1]}, query={len(qv)}")
    scores = VECS @ qv
    ranked = sorted(zip(scores, ITEMS), key=lambda x: -float(x[0]))
    out = _decide(ranked, k)
    tdesc = {i["table"]: i["desc"] for i in ITEMS if i["kind"] == "table"}
    closest, seen = [], set()
    for sc, i in ranked:
        if i["table"] not in seen:
            seen.add(i["table"]); closest.append({"table": i["table"], "desc": tdesc.get(i["table"], ""), "score": round(float(sc), 3)})
        if len(closest) == 3:
            break
    best = float(ranked[0][0])
    out.update(closest=closest, best=round(best, 3), weak=best < THRESH + CONFIRM_BAND)
    return out


def _decide(ranked, k):
    hits = [{**i, "score": round(float(s), 3)} for s, i in ranked[:k] if s >= THRESH]
    if not hits:
        return {"status": "unknown", "hits": []}
    colhits = [h for h in hits if h["kind"] == "column"]
    if colhits and colhits[0]["all_null"]:      # best match is a column with no data at all
        return {"status": "no_data", "hits": hits}
    cols = {}
    for h in hits:
        if h["kind"] == "column" and not h["all_null"]:
            cols.setdefault(h["table"], []).append(h["column"])
    # table matched without usable columns -> take its best non-null, non-PII columns
    for t in {h["table"] for h in hits} - set(cols):
        cand = [(s, i) for s, i in ranked if i["table"] == t and i["kind"] == "column" and not i["all_null"]]
        if cand and any(h["kind"] == "table" and h["table"] == t for h in hits):
            cols[t] = [i["column"] for _, i in cand[:6]]
    if not cols:
        return {"status": "no_data", "hits": hits}
    return {"status": "ok", "hits": hits, "columns": cols}


def get_schema(columns: dict) -> dict:
    """DDL-style schema for matched columns + key columns. PII columns are included; their values are masked after the query."""
    tables = list(columns)
    if not tables:
        return {"schema": "", "tables": []}
    info, keys = DB.schema_info(tables)
    meta = {(i["table"], i["column"]): i for i in ITEMS if i["kind"] == "column"}
    keycols = {(k["table_name"], k["column_name"]) for k in keys}
    lines = []
    for t in tables:
        parts = []
        for r in info:
            if r["table_name"] != t:
                continue
            key = (t, r["column_name"])
            if r["column_name"] not in columns[t] and key not in keycols:
                continue
            m = meta.get(key, {})
            parts.append(f'  {r["column_name"]} {r["data_type"]}' + (f' -- {m["desc"]}' if m.get("desc") else ""))
        lines.append(f"TABLE {t} (\n" + ",\n".join(parts) + "\n)")
    for k in keys:
        if k["constraint_type"] == "FOREIGN KEY":
            lines.append(f'FK {k["table_name"]}.{k["column_name"]} -> {k["rt"]}.{k["rc"]}')
    return {"schema": "\n".join(lines), "tables": tables}


def _clean(s):
    """Pull just the SQL out of a model reply (fenced block, or first SELECT/WITH), dropping any prose around it."""
    m = re.search(r"```(?:sql)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1)
    m = re.search(r"\bwith\s+[\w\"`]+\s+as\s*\(|\bselect\b", s, re.I)
    if m:
        s = s[m.start():]
    head, _, rest = s.partition(";")
    if re.match(r"\s*(with|select|insert|update|delete|drop|alter|create|truncate)\b", rest, re.I):
        return s.strip()  # a second statement: leave it in so the validator rejects it
    return head.strip()


CHART_WORDS = re.compile(r"\b(?:(?:as|in|with|using)\s+)?(?:an?\s+)?(?:(?:pie|bar|line|scatter|column)\s+)?(?:chart|graph|plot|histogram|visuali[sz]ation)s?(?:\s+(?:of|for|showing))?\b", re.I)


def find_intent(question: str, history: list = None) -> dict:
    """Step 1: what does the user want, and what data would that take? Flags questions too vague to act on."""
    if VECS is None:
        build_index()
    tables = "\n".join(f"- {i['table']}: {i['desc']}" for i in ITEMS if i["kind"] == "table")
    sysmsg = ("You identify what a user wants from a database assistant. Reply with JSON only, with keys: "
              '"intent" (one sentence), '
              '"data_needed" (the data required, in plain words like a column description; resolve follow-ups using the earlier questions), '
              '"wants_chart" (true only if they asked for a chart, graph or plot), '
              '"ambiguous" (true only if you cannot tell which data or measure they mean), '
              '"clarifying_question" (when ambiguous: one short question for the user, otherwise null).')
    user = f"Available tables:\n{tables}\n\nEarlier questions:\n{chr(10).join((history or [])[-4:]) or 'none'}\n\nQuestion: {question}"
    try:
        p = json.loads(_chat(model=CHAT_MODEL, format="json", options={"temperature": 0},
                             messages=[{"role": "system", "content": sysmsg}, {"role": "user", "content": user}])["message"]["content"])
    except Exception:
        p = {}
    cq = p.get("clarifying_question")
    return {"intent": str(p.get("intent") or question), "data_needed": str(p.get("data_needed") or question),
            "wants_chart": bool(p.get("wants_chart")), "ambiguous": bool(p.get("ambiguous")) and bool(cq),
            "clarifying_question": str(cq) if cq else None}


def plan_question(question: str, intent: dict, schema: str, want_chart: bool = False) -> dict:
    """Step 2 (after the schema is known): precise question for the SQL writer + ordered steps/actions to perform."""
    sysmsg = ("You plan how to answer a data question using ONLY the schema given. Reply with JSON only, with keys: "
              '"question" (a precise standalone question for a SQL writer: name the metric, grouping and filters; never mention charts), '
              '"steps" (3 to 5 short steps, in order), '
              '"actions" (ordered list using only "query", "chart", "explain"; include "chart" only if the user wants one), '
              '"chart" (null, or {"type": "bar|line|pie|scatter"}).')
    user = (f"Schema:\n{schema}\n\nUser question: {question}\nIntent: {intent['intent']}\nData needed: {intent['data_needed']}")
    try:
        p = json.loads(_chat(model=CHAT_MODEL, format="json", options={"temperature": 0},
                             messages=[{"role": "system", "content": sysmsg}, {"role": "user", "content": user}])["message"]["content"])
    except Exception:
        p = {}
    acts = p.get("actions") if isinstance(p.get("actions"), list) else []
    chart = p.get("chart") if isinstance(p.get("chart"), dict) else None
    chart_on = want_chart or "chart" in acts or bool(chart)
    steps = p.get("steps") if isinstance(p.get("steps"), list) else []
    return {"question": str(p.get("question") or intent["data_needed"] or question), "steps": [str(x) for x in steps][:6],
            "actions": ["query"] + (["chart"] if chart_on else []) + ["explain"], "chart": chart}


def generate_sql(question: str, schema: str, prev_sql: str = None, error: str = None, intent: str = None) -> str:
    """`question` is the planner's precise question (not the raw user text); charts are never the SQL writer's job."""
    question = re.sub(r"\s{2,}", " ", CHART_WORDS.sub("", question)).strip()
    msgs = [{"role": "system", "content":
             f"You write ONE read-only {DB.DIALECT_NAME} SQL SELECT query. Use only the tables and columns in the schema. "
             "Return plain tabular rows: one row per group/category with named columns (e.g. status, total). "
             "Never return JSON or chart specifications and never use SQLite or JSON-aggregate functions; charts are drawn separately from the rows. "
             "Never write INSERT/UPDATE/DELETE/DDL. Output ONLY the SQL in a single ```sql block, with no explanation."},
            {"role": "user", "content": f"Schema:\n{schema}\n\n" + (f"Goal: {intent}\n" if intent else "") + f"Question: {question}"}]
    if error:
        msgs += [{"role": "assistant", "content": f"```sql\n{prev_sql or ''}\n```"},
                 {"role": "user", "content": f"That query failed: {error}\nReturn a corrected SQL SELECT in a single ```sql block, nothing else."}]
    return _clean(_chat(model=SQL_MODEL, messages=msgs, options={"temperature": 0})["message"]["content"])


def validate_sql(query: str, allowed_tables: list) -> str:
    tree = sqlglot.parse_one(query, read=DB.SQL_DIALECT)  # raises on invalid / multiple statements
    if not isinstance(tree, exp.Select):
        raise ValueError("Only SELECT statements are allowed")
    if tree.find(exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command):
        raise ValueError("Write operation blocked")
    ctes = {c.alias for c in tree.find_all(exp.CTE)}
    used = {t.name for t in tree.find_all(exp.Table)} - ctes
    if not used <= set(allowed_tables):
        raise ValueError(f"Tables not allowed: {sorted(used - set(allowed_tables))}")
    if not tree.args.get("limit"):
        tree = tree.limit(1000)
    return tree.sql(dialect=DB.SQL_DIALECT)


def run_readonly_query(query: str) -> dict:
    cols, rows = DB.run_query(query, 1000)
    return {"columns": cols, "rows": json.loads(json.dumps(rows, default=str))}


PII_VISIBLE = int(os.getenv("PII_VISIBLE", "2"))   # how many leading characters stay visible


def _mask(v):
    if v is None:
        return None
    t = str(v)
    if len(t) <= PII_VISIBLE + 1:               # too short to show anything safely
        return "*" * len(t)
    return t[:PII_VISIBLE] + "*" * (len(t) - PII_VISIBLE)


def mask_pii(columns, rows, pii_cols):
    """Partially mask PII columns (first PII_VISIBLE chars shown, rest '*')."""
    hit = {c for c in columns if c in pii_cols}
    return [{k: (_mask(v) if k in hit else v) for k, v in r.items()} for r in rows]


def explain(question: str, query: str, rows: list, intent: str = None) -> str:
    data = json.dumps(rows[:50], default=str)
    msg = [{"role": "system", "content": "You are a friendly analyst. Answer in a warm, natural, conversational tone: lead with the direct answer, "
            "then one or two notable details. No SQL or column-name jargon, and no bullet lists unless the answer is a list. "
            "Use ONLY the data given and never invent numbers. If the data is truncated, say so."},
           {"role": "user", "content": f"User asked: {question}\n" + (f"What they want: {intent}\n" if intent else "") +
            f"SQL used: {query}\nRows ({len(rows)} total, first 50 shown): {data}"}]
    return _chat(model=CHAT_MODEL, messages=msg)["message"]["content"].strip()


def make_chart(question: str, columns: list, rows: list, hint: str = None) -> dict | None:
    """LLM picks type/x/y from column names only; the figure is built from real rows in code."""
    sample = rows[0] if rows else {}
    p = (f"Columns with sample values: {json.dumps(sample, default=str)}\nQuestion: {question}\n"
         + (f"Preferred chart type: {hint}\n" if hint else "") + 'Return JSON {"type":"bar|line|pie|scatter","x":"<column>","y":["<numeric column>"]}')
    try:
        s = json.loads(_chat(model=CHAT_MODEL, messages=[{"role": "user", "content": p}], format="json")["message"]["content"])
        x, ys, kind = s["x"], [y for y in s["y"] if y in columns], s["type"]
        if x not in columns or not ys:
            return None
    except Exception:
        return None
    xs = [r[x] for r in rows]
    num = lambda v: float(v) if v not in (None, "") else None
    if kind == "pie":
        return {"data": [{"type": "pie", "labels": xs, "values": [num(r[ys[0]]) for r in rows]}], "layout": {"title": question}}
    mode = {"line": "lines+markers", "scatter": "markers"}.get(kind)
    data = [{"type": "bar" if kind == "bar" else "scatter", **({"mode": mode} if mode else {}),
             "name": y, "x": xs, "y": [num(r[y]) for r in rows]} for y in ys]
    return {"data": data, "layout": {"title": question, "xaxis": {"title": x}}}


def rewrite_question(question: str, history: list) -> str:
    """Turn a follow-up ("and for 2025?") into a standalone question using earlier user questions."""
    msg = [{"role": "system", "content": "Rewrite the final question as a standalone question using the earlier questions "
            "for context. If it is already standalone, return it unchanged. Output only the question."},
           {"role": "user", "content": "Earlier questions:\n" + "\n".join(history[-4:]) + "\nFinal question: " + question}]
    return _chat(model=CHAT_MODEL, messages=msg, options={"temperature": 0})["message"]["content"].strip() or question