import { useState, type FormEvent } from 'react'
import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Tooltip,
} from 'chart.js'
import { Bar, Line } from 'react-chartjs-2'
import './App.css'

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Tooltip,
  Legend,
)

type ResponseStatus = 'success' | 'clarification' | 'blocked' | 'error'
type ChartType = 'bar' | 'line' | 'none'

interface QueryResponse {
  status: ResponseStatus
  question: string
  sql: string | null
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
  chart: {
    type: ChartType
    x_axis: string | null
    y_axis: string | null
  }
  explanation: string | null
  message: string | null
  retry_count: number
  latency_seconds: number
}

const API_URL = `${import.meta.env.VITE_API_URL ?? 'http://localhost:8000'}/api/query`
const SUGGESTED_QUESTIONS = [
  'How many customers are there?',
  'Which 5 products have the highest unit price?',
]

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function ResultChart({ result }: { result: QueryResponse }) {
  const { type, x_axis, y_axis } = result.chart
  if (
    type === 'none' ||
    !x_axis ||
    !y_axis ||
    result.rows.length === 0
  ) {
    return (
      <div className="chart-empty">
        <span className="chart-empty-icon" aria-hidden="true">↗</span>
        <p>A chart will appear when this answer has a useful grouped measure.</p>
      </div>
    )
  }

  const points = result.rows
    .map((row) => ({ label: formatCell(row[x_axis]), value: Number(row[y_axis]) }))
    .filter((point) => Number.isFinite(point.value))

  if (points.length === 0) {
    return <p className="muted">The grouped result does not contain numeric values to chart.</p>
  }

  const data = {
    labels: points.map((point) => point.label),
    datasets: [
      {
        label: y_axis,
        data: points.map((point) => point.value),
        backgroundColor: type === 'bar' ? '#85b7ff' : 'rgba(66, 133, 244, 0.14)',
        borderColor: '#2874db',
        borderWidth: 2,
        borderRadius: type === 'bar' ? 5 : 0,
        pointRadius: 4,
        fill: type === 'line',
        tension: 0.3,
      },
    ],
  }
  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: { intersect: false },
    },
    scales: {
      x: {
        title: { display: true, text: x_axis, color: '#65738a' },
        grid: { display: false },
        ticks: { color: '#65738a', maxRotation: 35, minRotation: 0 },
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: y_axis, color: '#65738a' },
        grid: { color: '#edf1f6' },
        ticks: { color: '#65738a' },
      },
    },
  }

  return (
    <div className="chart-canvas">
      {type === 'bar' ? <Bar data={data} options={options} /> : <Line data={data} options={options} />}
    </div>
  )
}

function App() {
  const [question, setQuestion] = useState('')
  const [result, setResult] = useState<QueryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [requestError, setRequestError] = useState('')

  async function submitQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmedQuestion = question.trim()
    if (!trimmedQuestion || loading) return

    setLoading(true)
    setResult(null)
    setRequestError('')

    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmedQuestion }),
      })
      const body = (await response.json()) as QueryResponse

      if (!response.ok && !body.status) {
        throw new Error(`Request failed (${response.status}). Please try again.`)
      }
      setResult(body)
    } catch (error) {
      setRequestError(
        error instanceof Error
          ? error.message
          : 'Could not reach AskData. Check that the backend is running.',
      )
    } finally {
      setLoading(false)
    }
  }

  const statusLabels: Record<ResponseStatus, string> = {
    success: 'Answer ready',
    clarification: 'A little more detail needed',
    blocked: 'Query blocked for safety',
    error: 'Could not complete the query',
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="AskData home">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span>AskData</span>
        </a>
        <div className="connection-indicator">
          <span className="connection-dot" />
          Read-only data assistant
        </div>
      </header>

      <section className="hero-section">
        <div className="eyebrow"><span /> YOUR DATA, IN PLAIN ENGLISH</div>
        <h1>What would you like<br className="desktop-break" /> to know?</h1>
        <p className="hero-copy">
          Ask a question about your company data. Get a clear answer, a chart,
          and the SQL behind it.
        </p>

        <form className="question-form" onSubmit={submitQuestion}>
          <label className="sr-only" htmlFor="question-input">Ask a data question</label>
          <textarea
            id="question-input"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
            placeholder="e.g. Which 5 products generated the most revenue?"
            maxLength={2000}
            rows={2}
            disabled={loading}
          />
          <div className="form-footer">
            <span className="input-hint">Ask a question about Northwind data</span>
            <button type="submit" disabled={loading || !question.trim()}>
              {loading ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  Thinking
                </>
              ) : (
                <>
                  Ask question
                  <span aria-hidden="true">↗</span>
                </>
              )}
            </button>
          </div>
        </form>

        <div className="suggestions" aria-label="Suggested questions">
          <span>TRY</span>
          {SUGGESTED_QUESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => setQuestion(suggestion)}
              disabled={loading}
            >
              {suggestion}
            </button>
          ))}
        </div>
      </section>

      {loading && (
        <section className="loading-card" role="status" aria-live="polite">
          <span className="loading-orbit" aria-hidden="true" />
          <div>
            <strong>Finding your answer</strong>
            <p>Generating and checking a read-only query…</p>
          </div>
        </section>
      )}

      {requestError && (
        <section className="state-card state-error" role="alert">
          <span className="state-icon" aria-hidden="true">!</span>
          <div>
            <h2>Can’t connect to AskData</h2>
            <p>{requestError}</p>
            <p className="state-help">Make sure the backend is running at http://localhost:8000.</p>
          </div>
        </section>
      )}

      {result && (
        <section className="answer-section" aria-live="polite">
          <div className={`result-banner result-${result.status}`}>
            <span className="result-status-mark" aria-hidden="true">
              {result.status === 'success' ? '✓' : result.status === 'blocked' ? '!' : '?'}
            </span>
            <div>
              <strong>{statusLabels[result.status]}</strong>
              {result.message && <p>{result.message}</p>}
            </div>
            {result.status === 'success' && (
              <span className="latency">{result.latency_seconds.toFixed(2)}s</span>
            )}
          </div>

          {result.status === 'success' && (
            <>
              <div className="answer-meta">
                <div>
                  <span className="section-kicker">YOUR QUESTION</span>
                  <h2>{result.question}</h2>
                </div>
                <span className="row-count">{result.row_count} {result.row_count === 1 ? 'row' : 'rows'}</span>
              </div>

              <div className="results-grid">
                <section className="panel table-panel">
                  <div className="panel-heading">
                    <div>
                      <span className="section-kicker">RESULTS</span>
                      <h3>Data table</h3>
                    </div>
                  </div>
                  {result.rows.length > 0 ? (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            {result.columns.map((column) => <th key={column}>{column}</th>)}
                          </tr>
                        </thead>
                        <tbody>
                          {result.rows.map((row, rowIndex) => (
                            <tr key={rowIndex}>
                              {result.columns.map((column) => (
                                <td key={column}>{formatCell(row[column])}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="no-rows">The query completed successfully with no matching rows.</div>
                  )}
                </section>

                <section className="panel chart-panel">
                  <div className="panel-heading">
                    <div>
                      <span className="section-kicker">VISUAL SUMMARY</span>
                      <h3>{result.chart.type === 'none' ? 'Chart' : `${result.chart.type === 'bar' ? 'Bar' : 'Line'} chart`}</h3>
                    </div>
                    {result.chart.type !== 'none' && (
                      <span className="axis-label">{result.chart.x_axis} / {result.chart.y_axis}</span>
                    )}
                  </div>
                  <ResultChart result={result} />
                </section>
              </div>

              {result.explanation && (
                <section className="explanation-card">
                  <span className="explanation-icon" aria-hidden="true">i</span>
                  <div>
                    <span className="section-kicker">HOW THIS ANSWER WAS PRODUCED</span>
                    <p>{result.explanation}</p>
                  </div>
                </section>
              )}
            </>
          )}

          {result.sql && (
            <details className="sql-panel">
              <summary>
                <span className="sql-summary-title">
                  <span className="sql-icon" aria-hidden="true">&lt;/&gt;</span>
                  Generated SQL
                </span>
                <span className="sql-summary-hint">Inspect the read-only query</span>
              </summary>
              <pre><code>{result.sql}</code></pre>
            </details>
          )}
        </section>
      )}

      <footer className="page-footer">
        <span>AskData</span>
        <span><span className="footer-lock" aria-hidden="true">⌑</span> Queries are validated and executed read-only</span>
      </footer>
    </main>
  )
}

export default App
