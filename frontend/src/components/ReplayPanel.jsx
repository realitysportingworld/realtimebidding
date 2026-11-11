import { useEffect, useState } from 'react'
import { Database, CheckCircle, XCircle } from 'lucide-react'
import { fetchReplaySummary, replayAuction } from '../api'

// Real bid requests from the iPinYou logs, priced by the model the engine
// serves. The simulator above runs on simulated traffic; this is the same
// bidding rule on real requests, real winning prices and real clicks. Its
// numbers come from the model's own artifact, never from text in this file.

const STRATEGY_LABELS = { flat: 'Flat bid', ev: 'Expected value (pCTR × value)' }
const FIRST_PRICE_LABELS = { unshaded: 'EV, fixed fraction of value', shaded: 'EV, shaded per request' }

// iPinYou prices are fen per thousand impressions; 100 fen = ¥1.
const yuan = (fen) => `¥${(fen / 100).toFixed(2)}`
const pct = (x, digits = 0) => `${(x * 100).toFixed(digits)}%`
const signed = (x) => `${x < 0 ? '−' : '+'}${Math.abs(x * 100).toFixed(0)}%`

export default function ReplayPanel() {
  const [summary, setSummary] = useState(null)
  const [result, setResult] = useState(null)
  const [fromClicked, setFromClicked] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    // The panel is optional: a backend without the artifacts answers 503,
    // and the panel then stays out of the way instead of showing an error.
    fetchReplaySummary().then(setSummary).catch(() => setSummary(false))
  }, [])

  const run = async (clicked) => {
    setLoading(true)
    setError(null)
    try {
      setResult(await replayAuction(clicked))
      setFromClicked(clicked)
    } catch (e) {
      setError(e.response?.data?.detail || 'Replay failed — is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  if (summary === false) return null

  const half = summary?.results?.find((r) => r.budget === '1/2')
  const others = summary?.results?.filter((r) => r.budget !== '1/2') ?? []
  const shadeHalf = summary?.first_price?.results?.find((r) => r.budget === '1/2')

  const box = {
    background: 'var(--surface)', border: '1px solid var(--border)',
    borderRadius: 'var(--radius)', padding: 20,
  }
  const label = { fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase',
    letterSpacing: '0.06em', marginBottom: 2 }
  const button = (primary) => ({
    background: primary ? 'var(--accent)' : 'var(--surface2)',
    border: primary ? 'none' : '1px solid var(--border)',
    color: primary ? '#fff' : 'var(--text)', padding: '8px 14px',
    borderRadius: 6, fontWeight: 600, fontSize: 13, opacity: loading ? 0.6 : 1,
  })

  return (
    <div style={box}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div style={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Database size={16} color="var(--accent)" /> Real auction replay
        </div>
        {summary && (
          <span style={{ color: 'var(--muted)', fontSize: 12 }}>
            model {summary.model.name} · {summary.model.version}
          </span>
        )}
      </div>
      <p style={{ color: 'var(--muted)', fontSize: 13, margin: '6px 0 12px' }}>
        Real bid requests from the iPinYou RTB logs (June 2013), priced by the
        model this engine serves. A bid wins when it beats the price that
        really won the auction, and the click is the one that really happened.
      </p>

      {half && (
        <div style={{ background: 'var(--surface2)', borderRadius: 8, padding: 12, fontSize: 13, marginBottom: 14 }}>
          On <strong>{summary.test_auctions.toLocaleString()}</strong> real test auctions,
          expected-value bidding won <strong style={{ color: 'var(--green)' }}>{signed(half.click_lift)} clicks</strong> for
          the same budget as a flat bid (95% CI {signed(half.lift_ci[0])} to {signed(half.lift_ci[1])}),
          at {signed(half.ecpc_change)} cost per click.
          {others.length > 0 && (
            <span style={{ color: 'var(--muted)' }}>
              {' '}Tighter budgets: {others.map((r) => `${signed(r.click_lift)} at ${r.budget}`).join(', ')}.
            </span>
          )}
          <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: 4 }}>
            Model AUC {summary.test_auc.toFixed(3)} · calibration error
            {' '}{pct(summary.calibration_error.raw)} → {pct(summary.calibration_error.calibrated)} after isotonic calibration
          </div>
          {shadeHalf && (
            <div style={{ borderTop: '1px solid var(--border)', marginTop: 10, paddingTop: 10 }}>
              <strong>First-price auctions</strong>, where the winner pays its own bid: shading
              each bid by a learned win-price curve won{' '}
              <strong style={{ color: shadeHalf.lift_ci[0] > 0 ? 'var(--green)' : 'var(--text)' }}>
                {signed(shadeHalf.click_lift)} clicks</strong> over the best fixed-fraction bid
              (95% CI {signed(shadeHalf.lift_ci[0])} to {signed(shadeHalf.lift_ci[1])}).
              Overpayment fell from {pct(shadeHalf.unshaded_overpaid_share)} to {pct(shadeHalf.overpaid_share)} of spend.
            </div>
          )}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button style={button(true)} disabled={loading} onClick={() => run(false)}>
          {loading ? 'Replaying…' : 'Replay a real request'}
        </button>
        <button style={button(false)} disabled={loading} onClick={() => run(true)}>
          Replay one that was clicked
        </button>
      </div>

      {error && (
        <div style={{ color: 'var(--red)', fontSize: 13, marginTop: 12 }}>{error}</div>
      )}

      {result && <ReplayResult result={result} fromClicked={fromClicked} label={label} />}
    </div>
  )
}

function ReplayResult({ result, fromClicked, label }) {
  const r = result.request
  const p = result.pctr
  const facts = [
    ['Advertiser', r.advertiser],
    ['Exchange', r.adexchange],
    ['Region / city', `${r.region} / ${r.city}`],
    ['Slot', `${r.slotsize} · visibility ${r.slotvisibility}`],
    ['Device', `${r.device} · ${r.os} · ${r.browser}`],
    ['Time', `${r.day} June, ${r.hour}:00`],
  ]

  return (
    <div style={{ marginTop: 16, display: 'grid', gap: 14 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10 }}>
        {facts.map(([k, v]) => (
          <div key={k}>
            <div style={label}>{k}</div>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{v}</div>
          </div>
        ))}
      </div>

      <div>
        <div style={label}>User segments ({r.user_tags.length})</div>
        <div style={{ fontSize: 12, color: r.user_tags.length ? 'var(--text)' : 'var(--muted)' }}>
          {r.user_tags.length ? r.user_tags.join(' · ') : 'none on this request'}
        </div>
      </div>

      <div style={{ background: 'var(--surface2)', borderRadius: 8, padding: 12 }}>
        <div style={label}>Predicted click probability</div>
        <div style={{ fontSize: 20, fontWeight: 700 }}>{pct(p.calibrated, 3)}</div>
        <div style={{ color: 'var(--muted)', fontSize: 12 }}>
          {(p.calibrated / p.mean_for_advertiser).toFixed(1)}× this advertiser's average ·
          higher than {pct(p.percentile)} of real test traffic ·
          matches the evaluated notebook model {Math.abs(p.calibrated - p.notebook_calibrated) < 1e-12 ? 'exactly' : 'to within rounding'}
        </div>
      </div>

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--muted)', fontSize: 12 }}>
            <th style={{ padding: '6px 0' }}>Strategy</th><th>Bid (CPM)</th><th>Winning price</th><th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {result.strategies.map((s) => (
            <tr key={s.strategy} style={{ borderTop: '1px solid var(--border)' }}>
              <td style={{ padding: '8px 0' }}>{STRATEGY_LABELS[s.strategy] || s.strategy}</td>
              <td>{yuan(s.bid)}</td>
              <td>{yuan(result.market_price)}</td>
              <td style={{ color: s.won ? 'var(--green)' : 'var(--muted)', fontWeight: 600 }}>
                {s.won ? `Won, paid ${yuan(s.paid)}` : 'Lost'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {result.first_price && <FirstPrice fp={result.first_price} market={result.market_price} label={label} />}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
        {result.clicked
          ? <><CheckCircle size={16} color="var(--green)" /> The user really clicked this ad</>
          : <><XCircle size={16} color="var(--muted)" /> No click — like 99.9% of real impressions</>}
      </div>
      {fromClicked && (
        <div style={{ color: 'var(--muted)', fontSize: 12, marginTop: -8 }}>
          Drawn from clicked requests on purpose; only about 1 in 1,200 real requests is clicked.
        </div>
      )}
    </div>
  )
}

function FirstPrice({ fp, market, label }) {
  return (
    <div>
      <div style={label}>If this were a first-price auction</div>
      <div style={{ color: 'var(--muted)', fontSize: 12, margin: '2px 0 6px' }}>
        The winner pays its own bid. The fixed-fraction bid bids the same share of value on
        every request; the shaded one picks the bid that maximises (value − bid) × P(win), with
        P(win) from a curve learned on won/lost outcomes only. Value {yuan(fp.value)} · predicted
        coin-flip price {yuan(fp.predicted_median_price)}.
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--muted)', fontSize: 12 }}>
            <th style={{ padding: '6px 0' }}>Strategy</th><th>Bid (CPM)</th><th>P(win)</th><th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {fp.strategies.map((s) => (
            <tr key={s.strategy} style={{ borderTop: '1px solid var(--border)' }}>
              <td style={{ padding: '8px 0' }}>{FIRST_PRICE_LABELS[s.strategy] || s.strategy}</td>
              <td>{yuan(s.bid)}</td>
              <td>{pct(s.win_probability)}</td>
              <td style={{ color: s.won ? 'var(--green)' : 'var(--muted)', fontWeight: 600 }}>
                {s.won
                  ? `Won, paid ${yuan(s.paid)} (${yuan(s.overpaid)} over ${yuan(market)})`
                  : `Lost to ${yuan(market)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
