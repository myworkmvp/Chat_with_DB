import os, re, time
import tools as T

LOG_ROWS = int(os.getenv("LOG_ROWS", "20"))
SMALL = [(r"\b(hi|hello|hey|good (morning|afternoon|evening)|greetings)\b", "Hello! Ask me anything about your data."),
         (r"\b(bye|goodbye|see you|good night)\b", "Goodbye! Have a great day."),
         (r"\b(thanks|thank you)\b", "You're welcome!"),
         (r"\bwelcome\b", "Thanks! How can I help with your data today?")]
CHART_RE = re.compile(r"\b(chart|graph|plot|visuali[sz]e|trend|histogram|pie)\b", re.I)
now, since = time.perf_counter, lambda t: round((time.perf_counter() - t) * 1000)


def ev(step, title, status, detail=None, *, inp=None, out=None, calls=None, ms=None):
    return {"step": step, "title": title, "status": status, "detail": detail, "inp": inp, "out": out, "calls": calls or [], "ms": ms}


def final(answer, **kw):
    return {"step": "final", "answer": answer, **kw}


def small_talk(q):
    if len(q.split()) <= 6:
        for pat, reply in SMALL:
            if re.search(pat, q, re.I):
                return reply


def ask_user(kind, text, options, original):
    """Stop and ask. The user's next message comes back with `clarification` so we continue instead of asking again."""
    yield ev("clarify", "Ask the user", "done", {"reason": kind, "options": options}, inp={"reason": kind, "original_question": original},
             out={"question": text, "options": options}, ms=0)
    yield final(text, clarify={"kind": kind, "original": original, "options": options})


def run(q: str, history=None, clarification=None):
    history = history or []
    clarified = bool(clarification)
    user_q = clarification["original"] if clarified else q
    effective = f"{user_q} (user's clarification: {q})" if clarified else q

    t = now(); yield ev("route", "Intent router", "running")
    reply = None if clarified else small_talk(q)
    want_chart = bool(CHART_RE.search(effective))
    rin = {"text": q, "clarification": clarification}
    if reply:
        d = {"intent": "small_talk", "llm_used": False}
        yield ev("route", "Intent router", "done", d, inp=rin, out={**d, "reply": reply}, ms=since(t))
        yield final(reply); return
    d = {"intent": "data_question", "chart_requested": want_chart, "after_clarification": clarified}
    yield ev("route", "Intent router", "done", d, inp=rin, out=d, ms=since(t))

    t = now(); cs = []; yield ev("intent", "Find the intent", "running")
    with T.record(cs):
        intent = T.find_intent(effective, history)
    want_chart = want_chart or intent["wants_chart"]
    yield ev("intent", "Find the intent", "done", intent, inp={"text": effective, "history": history}, out=intent, calls=cs, ms=since(t))

    sq = f'{intent["data_needed"]}. {intent["intent"]}'
    t = now(); cs = []; yield ev("search", "Metadata similarity search", "running")
    with T.record(cs):
        s = T.search_metadata(sq)
    sin = {"query": sq, "threshold": T.THRESH, "confirm_band": T.CONFIRM_BAND}
    brief = [{"match": h["text"].split(":")[0], "score": h["score"], "all_null": h["all_null"]} for h in s["hits"]]
    base = {"hits": brief, "query": sq, "threshold": T.THRESH, "best": s["best"]}
    opts = [f'{c["table"]}: {c["desc"]}'.strip(": ") for c in s["closest"]]

    def blocked(result):
        return ev("search", "Metadata similarity search", "error", {**base, "result": result}, inp=sin, out={**s, "result": result}, calls=cs, ms=since(t))
    if s["status"] == "unknown":
        yield blocked("no match above threshold")
        if clarified:
            yield final("I don't know — even with that detail, nothing in the available data matches."); return
        yield from ask_user("not_found", f"I couldn't find anything about “{intent['data_needed']}”. These are the closest things I have. "
                            "Which did you mean, or could you rephrase?", opts, user_q); return
    if s["status"] == "no_data":
        yield blocked("matched columns contain only NULL")
        yield final("Data not found — that information exists in the schema but has no values recorded."); return
    if "columns" not in s:  # defensive: any status without usable columns must not crash the run
        yield blocked(f"no usable columns (status={s['status']})")
        yield final("I couldn't find usable data for that question."); return
    yield ev("search", "Metadata similarity search", "done", {**base, "selected": s["columns"]}, inp=sin, out=s, calls=cs, ms=since(t))

    if not clarified and (intent["ambiguous"] or s["weak"]):  # confusion: ask before doing any work
        if intent["ambiguous"]:
            yield from ask_user("unclear", intent["clarifying_question"], opts, user_q); return
        top = s["closest"][0]["table"]
        yield from ask_user("unsure", f"I'm not fully sure what you mean. My best match is the {top} data. Is that right, or did you mean something else?",
                            [f"Yes, use {top}"] + opts[1:], user_q); return

    t = now(); yield ev("schema", "Schema fetch", "running")
    sch = T.get_schema(s["columns"])
    yield ev("schema", "Schema fetch", "done", {"schema": sch["schema"]}, inp={"columns": s["columns"]}, out=sch, ms=since(t))

    t = now(); cs = []; yield ev("plan", "Plan the work", "running")
    with T.record(cs):
        plan = T.plan_question(user_q, intent, sch["schema"], want_chart)
    yield ev("plan", "Plan the work", "done", plan, inp={"question": user_q, "intent": intent, "schema": sch["schema"], "want_chart": want_chart},
             out=plan, calls=cs, ms=since(t))

    pii = {i["column"] for i in T.ITEMS if i["kind"] == "column" and i["pii"]}
    raw = err = None
    stage, ts, cs = "sql", now(), []
    titles = {"sql": f"SQL generation ({T.SQL_MODEL})", "validate": "SELECT-only validation", "execute": "Run on read-only connection"}
    for attempt in (1, 2):                      # action: query
        cs = []
        try:
            stage, ts = "sql", now()
            yield ev("sql", titles["sql"], "running", {"attempt": attempt})
            sin = {"attempt": attempt, "question": plan["question"], "intent": intent["intent"], "schema": sch["schema"], "previous_sql": raw, "previous_error": err}
            with T.record(cs):
                raw = T.generate_sql(plan["question"], sch["schema"], raw, err, intent["intent"])
            yield ev("sql", titles["sql"], "done", {"sql": raw, "attempt": attempt, "question": plan["question"]}, inp=sin, out={"sql": raw}, calls=cs, ms=since(ts))
            stage, ts = "validate", now()
            yield ev("validate", titles["validate"], "running")
            safe = T.validate_sql(raw, sch["tables"])
            yield ev("validate", titles["validate"], "done", {"sql": safe}, inp={"sql": raw, "allowed_tables": sch["tables"]}, out={"sql": safe}, ms=since(ts))
            stage, ts = "execute", now()
            yield ev("execute", titles["execute"], "running")
            res = T.run_readonly_query(safe)
            sample = T.mask_pii(res["columns"], res["rows"][:LOG_ROWS], pii)
            yield ev("execute", titles["execute"], "done", {"rows": len(res["rows"]), "ms": since(ts)}, inp={"sql": safe},
                     out={"columns": res["columns"], "row_count": len(res["rows"]), "rows_sample": sample}, ms=since(ts))
            break
        except Exception as e:
            err = str(e)
            yield ev(stage, titles[stage], "error", {"error": err, "retrying": attempt == 1}, inp={"attempt": attempt, "sql": raw},
                     out=None, calls=cs if stage == "sql" else None, ms=since(ts))
    else:
        yield final("I couldn't build a safe, working query for that question.", sql=raw); return

    t = now()
    rows = T.mask_pii(res["columns"], res["rows"], pii)
    masked = [c for c in res["columns"] if c in pii]
    yield ev("mask", "PII masking", "done", {"masked_columns": masked}, inp={"pii_columns": sorted(pii), "columns": res["columns"]},
             out={"masked_columns": masked, "row_count": len(rows)}, ms=since(t))
    if not rows:
        yield final("The query ran but returned no rows.", sql=safe, columns=res["columns"], rows=[]); return

    chart = None
    if "chart" in plan["actions"]:              # action: chart
        t = now(); cs = []; yield ev("chart", "Chart builder", "running")
        hint = (plan["chart"] or {}).get("type")
        with T.record(cs):
            chart = T.make_chart(user_q, res["columns"], rows, hint)
        yield ev("chart", "Chart builder", "done" if chart else "error", {"built": bool(chart)},
                 inp={"question": user_q, "columns": res["columns"], "type_hint": hint, "first_row": rows[0]}, out={"built": bool(chart), "spec": chart}, calls=cs, ms=since(t))

    t = now(); cs = []; yield ev("explain", f"Explanation ({T.CHAT_MODEL})", "running")   # action: explain
    with T.record(cs):
        ans = T.explain(user_q, safe, rows, intent["intent"])
    yield ev("explain", f"Explanation ({T.CHAT_MODEL})", "done", inp={"question": user_q, "intent": intent["intent"], "sql": safe,
             "rows_sent_to_model": min(len(rows), 50)}, out={"answer": ans}, calls=cs, ms=since(t))
    yield final(ans, sql=safe, columns=res["columns"], rows=rows[:200], chart=chart)