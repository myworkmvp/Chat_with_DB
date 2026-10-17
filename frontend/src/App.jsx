import { useState, useRef, useEffect } from 'react'
import Plotly from 'plotly.js-basic-dist-min'

const STAGES = [
  ['route', 'Understand the question', 'Small talk, or a question about the data?'],
  ['intent', 'Find the intent', 'What does the user want, and what data would that take?'],
  ['search', 'Find relevant data', 'Similarity search over table and column descriptions'],
  ['clarify', 'Ask you', 'Only when nothing matched or the question is unclear'],
  ['schema', 'Fetch the schema', 'Matched columns only, personal data left out'],
  ['plan', 'Plan the work', 'Steps and actions, planned from the matched schema'],
  ['sql', 'Write the SQL', 'Written from the plan and the matched schema'],
  ['validate', 'Check it is read-only', 'Parsed and limited to the matched tables before it runs'],
  ['execute', 'Run the query', 'Read-only connection with a time limit'],
  ['mask', 'Protect personal data', 'Masked before anything reaches the model'],
  ['chart', 'Build the chart', 'The model picks axes; the chart is drawn from the real rows'],
  ['explain', 'Explain the result', 'The model sees only the returned rows'],
]
const fmt = ms => (ms == null ? '' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`)
const label = id => STAGES.find(s => s[0] === id)?.[1] || id

function Chart({ spec }) {
  const ref = useRef()
  useEffect(() => {
    const el = ref.current
    const layout = { ...spec.layout, margin: { t: 44, l: 52, r: 12, b: 52 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      colorway: ['#0f766e', '#b7791f', '#4f46e5', '#be123c'], font: { family: 'Instrument Sans, sans-serif', size: 13 } }
    Plotly.newPlot(el, spec.data, layout, { responsive: true, displaylogo: false })
    return () => { Plotly.purge(el) }
  }, [spec])
  return <div ref={ref} className="chart" />
}

function Table({ cols, rows }) {
  return (
    <div className="tablewrap"><table>
      <thead><tr>{cols.map(c => <th key={c}>{c}</th>)}</tr></thead>
      <tbody>{rows.slice(0, 50).map((r, i) => <tr key={i}>{cols.map(c => <td key={c}>{String(r[c] ?? '')}</td>)}</tr>)}</tbody>
    </table></div>
  )
}

const Fold = ({ title, open, children }) => <details open={open} className="fold"><summary>{title}</summary>{children}</details>
const Code = ({ children }) => <pre>{children}</pre>

function Detail({ id, e }) {
  const d = e.detail || {}
  if (id === 'route') return <p className="gist">{d.intent === 'small_talk' ? 'Small talk: answered with a fixed reply, no model or database used.' : `Data question${d.chart_requested ? ', chart requested' : ''}.`}</p>
  if (id === 'intent') return (
    <>
      <p className="gist"><b>Wants:</b> {d.intent}</p>
      <p className="gist"><b>Data needed:</b> {d.data_needed}</p>
      {d.ambiguous && <p className="gist warn">Unclear: {d.clarifying_question}</p>}
    </>)
  if (id === 'plan') return (
    <>
      <div className="gist">{(d.steps || []).map((s, i) => <div key={i} className="pl">{i + 1}. {s}</div>)}</div>
      <p className="gist"><b>Will run:</b> {(d.actions || []).join(' → ')}{d.chart?.type ? ` (${d.chart.type} chart)` : ''}</p>
      <p className="gist"><b>Question for the SQL writer:</b> {d.question}</p>
    </>)
  if (id === 'clarify') return (
    <>
      <p className="gist">{{ not_found: 'Nothing matched closely enough.', unsure: 'The match was borderline.', unclear: 'The question was unclear.' }[d.reason]} Waiting for your answer.</p>
      {(d.options || []).map((o, i) => <div key={i} className="pl">• {o}</div>)}
    </>)
  if (id === 'search') return (
    <>
      {d.result && <p className="gist warn">{d.result === 'no match above threshold' ? `Nothing scored above the ${d.threshold} threshold.` : d.result}</p>}
      {d.query && <p className="gist">Searched for: “{d.query}”</p>}
      <ul className="hits">{(d.hits || []).map((h, i) => (
        <li key={i} className={h.all_null ? 'nulls' : ''}>
          <span className="hn">{h.match.replace(/^(column|table) /, '')}{h.all_null && <em> all NULL, ignored</em>}</span>
          <span className="bar"><i style={{ width: `${Math.min(h.score, 1) * 100}%` }} /><b style={{ left: `${(d.threshold ?? 0.5) * 100}%` }} /></span>
          <span className="sc">{h.score.toFixed(2)}</span>
        </li>))}
      </ul>
      {d.selected && <p className="gist">Using {Object.entries(d.selected).map(([t, c]) => `${t} (${c.join(', ')})`).join('; ')}</p>}
    </>)
  if (id === 'schema') return <Fold title="Schema sent to the SQL model"><Code>{d.schema}</Code></Fold>
  if (id === 'sql') return <><p className="gist">Written from the plan: “{d.question}”</p><Code>{d.sql}</Code></>
  if (id === 'validate') return d.error ? <p className="gist warn">{d.error}</p> : <Fold title="Query after checks"><Code>{d.sql}</Code></Fold>
  if (id === 'execute') return d.error ? <p className="gist warn">{d.error}{d.retrying && ' Retrying once with this error.'}</p> : <p className="gist">{d.rows} rows in {d.ms} ms</p>
  if (id === 'mask') return <p className="gist">{d.masked_columns?.length ? `Masked: ${d.masked_columns.join(', ')}` : 'No personal data in the result.'}</p>
  if (id === 'chart') return <p className="gist">{d.built ? 'Chart built from the returned rows.' : 'Could not pick usable axes.'}</p>
  return null
}

function Trace({ m }) {
  const route = m.steps.find(s => s.step === 'route')?.detail
  const has = id => m.steps.some(s => s.step === id)
  const list = route?.intent === 'small_talk' ? [STAGES[0]]
    : STAGES.filter(([id]) => (id !== 'chart' && id !== 'clarify') || has(id) || (id === 'chart' && (route?.chart_requested || m.steps.find(s => s.step === 'intent')?.detail?.wants_chart)))
  return (
    <div className="trace">
      {(m.errors || []).map((x, i) => <p key={i} className="retry"><b>Failed at “{label(x.step)}”.</b> {x.error}</p>)}
      <ol>{list.map(([id, name, blurb]) => {
        const e = m.steps.find(s => s.step === id)
        const st = e ? e.status : m.final ? 'skipped' : 'pending'
        const sub = st === 'skipped' ? 'Not reached' : e && (id === 'sql' || id === 'explain') ? e.title : blurb
        return (
          <li key={id} className={st}>
            <span className="dot" />
            <div className="row"><b>{name}</b><time>{fmt(e?.ms)}</time></div>
            <p className="blurb">{sub}</p>
            {e && e.status !== 'running' && <Detail id={id} e={e} />}
          </li>)
      })}</ol>
    </div>)
}

export default function App() {
  const [msgs, setMsgs] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [sel, setSel] = useState(null)
  const [health, setHealth] = useState(null)
  const end = useRef()
  useEffect(() => { fetch('/api/health').then(r => r.json()).then(setHealth).catch(() => setHealth({ db: 'backend unreachable' })) }, [])
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [msgs.length])

  const bots = msgs.map((m, i) => (m.role === 'bot' ? i : -1)).filter(i => i >= 0)
  const shown = sel ?? bots[bots.length - 1]
  const patch = fn => setMsgs(m => { const c = [...m]; c[c.length - 1] = fn(c[c.length - 1]); return c })
  const problems = health && [health.db !== 'ok' && `Database: ${health.db}`, health.ollama && health.ollama !== 'ok' && `Ollama: ${health.ollama}`,
    ...(health.missing_models || []).map(x => `Model not pulled: ${x}`)].filter(Boolean)

  function onEvent(e) {
    if (e.step === 'final') return patch(m => ({ ...m, final: e, total: Math.round(performance.now() - m.t0) }))
    patch(m => {
      const now = performance.now()
      const i = m.steps.findIndex(s => s.step === e.step)
      const prev = i >= 0 ? m.steps[i] : null
      const t0 = prev && prev.status === 'running' ? prev.t0 : now
      const ev = { ...e, t0, ms: e.status === 'running' ? undefined : (e.ms ?? e.detail?.ms ?? Math.round(now - t0)) }
      const steps = [...m.steps]; if (i >= 0) steps[i] = ev; else steps.push(ev)
      const errors = e.detail?.error ? [...(m.errors || []), { step: e.step, error: e.detail.error }] : m.errors
      return { ...m, steps, errors }
    })
  }

  async function send(text) {
    const q = (typeof text === 'string' ? text : input).trim(); if (!q || busy) return
    const lastBot = [...msgs].reverse().find(m => m.role === 'bot')
    const c = lastBot?.final?.clarify
    const clarification = c ? { original: c.original, asked: lastBot.final.answer } : null
    const history = msgs.filter(m => m.role === 'user').map(m => m.text).slice(-4)
    setInput(''); setBusy(true); setSel(null)
    setMsgs(m => [...m, { role: 'user', text: q }, { role: 'bot', q, reply: !!clarification, steps: [], final: null, t0: performance.now() }])
    try {
      const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: q, history, clarification }) })
      const reader = res.body.getReader(), dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read(); if (done) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n'); buf = parts.pop()
        for (const p of parts) if (p.startsWith('data: ')) onEvent(JSON.parse(p.slice(6)))
      }
    } catch (err) {
      onEvent({ step: 'final', answer: `Could not reach the backend (${err.message}). Is it running on port 8000?` })
    } finally { setBusy(false) }
  }

  const sm = msgs[shown]
  return (
    <div className="shell">
      <div className="chat">
        <header>
          <h1>Ask the database</h1>
          {health && <span className={`pill ${problems.length ? 'bad' : 'good'}`} title={problems.join('\n')}>{problems.length ? problems[0] : 'Database and models ready'}</span>}
        </header>
        <main>
          {msgs.length === 0 && <section className="empty"><h2>Ask a question about your data.</h2>
            <p>Every answer comes with its trail: which tables were matched and how closely, the SQL that ran, and what was hidden.</p></section>}
          {msgs.map((m, i) => m.role === 'user' ? null : (
            <article key={i} className={i === shown ? 'on' : ''}>
              <h2 className="q">{m.reply && <span className="tag">Your answer · </span>}{m.q}</h2>
              {m.final ? <p className="answer">{m.final.answer}</p> : <p className="answer wait">Working through the steps…</p>}
              {m.final?.clarify && i === msgs.length - 1 && <div className="opts">{m.final.clarify.options.map(o => <button key={o} onClick={() => send(o)} disabled={busy}>{o}</button>)}</div>}
              {m.final?.chart && <Chart spec={m.final.chart} />}
              {m.final?.rows?.length > 0 && <details className="fold"><summary>Data, {m.final.rows.length} rows</summary><Table cols={m.final.columns} rows={m.final.rows} /></details>}
              <button className="railbtn" onClick={() => setSel(i)} aria-label="Show workflow for this answer">
                <span className="rail">{m.steps.map(s => <i key={s.step} className={s.status} />)}</span>
                <span>{m.final ? `${m.steps.length} steps in ${fmt(m.total)}` : 'Running'}</span>
              </button>
              <details className="inline"><summary>Workflow</summary><Trace m={m} /></details>
            </article>))}
          <div ref={end} className="endmark" />
        </main>
        <div className="ask">
          <input value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === 'Enter' && send()} placeholder={msgs.at(-1)?.final?.clarify ? 'Answer the question above, or ask something new' : 'Ask about your data'} disabled={busy} aria-label="Question" />
          <button onClick={() => send()} disabled={busy || !input.trim()}>{busy ? 'Working' : 'Ask'}</button>
        </div>
      </div>
      <aside>
        <h2>How this answer was made</h2>
        {sm ? <><p className="aq">{sm.q}</p><Trace m={sm} />{sm.final?.run_id && <p className="blurb">Logged as run {sm.final.run_id}</p>}</> : <p className="blurb">The workflow appears here, step by step, as soon as you ask.</p>}
      </aside>
    </div>)
}
