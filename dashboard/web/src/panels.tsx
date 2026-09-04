import { useMemo, useState } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  type Decision,
  type EquityPoint,
  type HistoryEvent,
  type IvPoint,
  type Lesson,
  type Position,
  type ScanRow,
  type Signals,
  type State,
  money,
  num,
  occLabel,
  pct,
  signedMoney,
  signedPct,
} from './api'
import { Card, Chip, Empty, Meter, outcomeTone } from './ui'

const AXIS = { stroke: '#5d6b7f', fontSize: 10 }
const GRID = '#161f2b'

const tooltipStyle = {
  contentStyle: {
    background: '#0f141d',
    border: '1px solid #1e2836',
    borderRadius: 8,
    fontSize: 12,
  },
  labelStyle: { color: '#93a3b8', fontSize: 11 },
}

/* ========================================================================== */
/* Equity curve                                                                */
/* ========================================================================== */
export function EquityPanel({
  points,
  startingEquity,
}: {
  points: EquityPoint[]
  startingEquity: number | null
}) {
  const data = useMemo(
    () =>
      points.map((p) => ({
        ...p,
        label: new Date(p.t * 1000).toLocaleString('en-US', {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
        }),
      })),
    [points],
  )

  const last = data.at(-1)
  const up = last && startingEquity ? last.equity >= startingEquity : true
  const stroke = up ? '#3ddc97' : '#ff6b81'

  return (
    <Card
      title="Equity"
      subtitle={`${data.length} points · 15-minute bars`}
      right={
        last ? (
          <span className={`num text-sm font-semibold ${up ? 'text-up' : 'text-down'}`}>
            {money(last.equity)}
          </span>
        ) : null
      }
      bodyClass="p-4 pt-2"
    >
      {data.length === 0 ? (
        <Empty>no portfolio history yet</Empty>
      ) : (
        <ResponsiveContainer width="100%" height={196}>
          <AreaChart data={data} margin={{ top: 8, right: 4, left: -8, bottom: 0 }}>
            <defs>
              <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
                <stop offset="100%" stopColor={stroke} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={48} />
            <YAxis
              tick={AXIS}
              tickLine={false}
              axisLine={false}
              width={62}
              domain={['dataMin - 120', 'dataMax + 120']}
              tickFormatter={(v: number) => `$${(v / 1000).toFixed(1)}k`}
            />
            <Tooltip
              {...tooltipStyle}
              formatter={(v) => [money(Number(v)), 'Equity'] as [string, string]}
            />
            {startingEquity && (
              <ReferenceLine
                y={startingEquity}
                stroke="#5d6b7f"
                strokeDasharray="4 4"
                label={{
                  value: 'start',
                  position: 'insideTopLeft',
                  fill: '#5d6b7f',
                  fontSize: 10,
                }}
              />
            )}
            <Area
              type="monotone"
              dataKey="equity"
              stroke={stroke}
              strokeWidth={1.8}
              fill="url(#equityFill)"
              dot={false}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}

/* ========================================================================== */
/* Open positions                                                              */
/* ========================================================================== */
export function PositionsPanel({ positions }: { positions: Position[] }) {
  return (
    <Card
      title="Open positions"
      subtitle={`${positions.length} tracked`}
      bodyClass="p-4 space-y-3 overflow-y-auto"
    >
      {positions.length === 0 ? (
        <Empty>flat — no open structures</Empty>
      ) : (
        positions.map((p) => {
          const up = p.unrealized_pl >= 0
          const drift = p.legs_matched !== p.legs_expected
          return (
            <article
              key={p.id}
              className="bg-surface-2 border border-line rounded-lg p-3 fade-up"
            >
              <div className="flex items-center gap-2 mb-2">
                <span className="num text-base font-semibold">{p.symbol}</span>
                <Chip tone="accent">{p.structure.replace(/_/g, ' ')}</Chip>
                <Chip tone="muted">{p.quantity}×</Chip>
                <Chip tone="muted">exp {p.expiry}</Chip>
                {drift && (
                  <Chip tone="warn" title="Tracked legs not all present at the broker">
                    {p.legs_matched}/{p.legs_expected} legs
                  </Chip>
                )}
                <span
                  className={`num ml-auto text-base font-semibold ${up ? 'text-up' : 'text-down'}`}
                >
                  {signedMoney(p.unrealized_pl)}
                </span>
              </div>

              <div className="grid grid-cols-3 gap-2 text-[11px] mb-2">
                <Field label="Premium paid" value={money(p.entry_dollars)} />
                <Field
                  label="Return"
                  value={signedPct(p.unrealized_pct)}
                  tone={up ? 'up' : 'down'}
                />
                <Field label="Peak gain" value={pct(p.peak_gain_fraction)} />
              </div>

              <div className="space-y-1">
                {p.legs.map((leg) => (
                  <div
                    key={leg.symbol}
                    className="flex items-center gap-2 text-[11px] num text-ink-dim"
                  >
                    <span className={leg.right === 'call' ? 'text-accent' : 'text-warn'}>
                      {leg.action === 'buy' ? '+' : '−'}
                      {leg.quantity}
                    </span>
                    <span className="text-ink">{occLabel(leg.symbol)}</span>
                    <span className="text-ink-faint">
                      {leg.current_price !== null ? `@ ${money(leg.current_price)}` : 'no quote'}
                    </span>
                    {leg.unrealized_pl !== null && (
                      <span
                        className={`ml-auto ${leg.unrealized_pl >= 0 ? 'text-up' : 'text-down'}`}
                      >
                        {signedMoney(leg.unrealized_pl)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </article>
          )
        })
      )}
    </Card>
  )
}

function Field({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'up' | 'down'
}) {
  const toneClass = tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-ink'
  return (
    <div>
      <div className="text-ink-faint text-[10px] uppercase tracking-wider">{label}</div>
      <div className={`num ${toneClass}`}>{value}</div>
    </div>
  )
}

/* ========================================================================== */
/* Regime & market signals                                                     */
/* ========================================================================== */
export function RegimePanel({ signals, state }: { signals: Signals | null; state: State | null }) {
  if (!signals) return <Card title="Regime"><Empty>waiting for a scan</Empty></Card>

  const danger = signals.regime_signals.includes('MACRO_DANGER')
  const exposure = state?.exposure

  return (
    <Card title="Regime & risk" subtitle={signals.vix_state || undefined}>
      {danger && (
        <div className="flex items-start gap-2 bg-warn/10 border border-warn/25 rounded-lg px-3 py-2 mb-3">
          <span className="text-warn text-sm leading-none mt-0.5">▲</span>
          <div className="text-[11px] leading-relaxed">
            <span className="text-warn font-semibold">MACRO_DANGER</span>
            <span className="text-ink-dim">
              {' '}— {signals.macro_event} on {signals.macro_date}. Short-vol structures are
              vetoed; every trade is forced to a long strangle.
            </span>
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 mb-3">
        <MiniStat label="VIX" value={num(signals.vix, 2)} />
        <MiniStat label="VIX3M" value={num(signals.vix3m, 2)} />
        <MiniStat
          label="Term ratio"
          value={num(signals.vix_ratio, 2)}
          hint={signals.vix_state}
        />
      </div>

      {exposure && (
        <div className="mb-3">
          <div className="flex items-baseline justify-between text-[11px] mb-1.5">
            <span className="text-ink-dim">
              Long-vol premium
              <span className="text-ink-faint">
                {' '}· {exposure.count} position{exposure.count === 1 ? '' : 's'}
              </span>
            </span>
            <span className="num text-ink">
              {money(exposure.debit, 0)}
              <span className="text-ink-faint"> / {money(exposure.cap, 0)}</span>
            </span>
          </div>
          <Meter
            value={exposure.debit}
            max={exposure.cap}
            tone={exposure.breached ? 'down' : exposure.pct > exposure.max_pct * 0.85 ? 'warn' : 'accent'}
          />
          <div className="flex justify-between text-[10px] text-ink-faint mt-1">
            <span>{pct(exposure.pct)} of equity</span>
            <span>
              {exposure.breached
                ? 'cap breached — new long-vol blocked'
                : `${money(exposure.headroom, 0)} headroom`}
            </span>
          </div>
        </div>
      )}

      {signals.correlated.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-faint mb-1.5">
            Correlated clusters (&gt;0.8, 10d) — one slot each
          </div>
          <div className="flex flex-wrap gap-1.5">
            {signals.correlated.map((cluster) => (
              <Chip key={cluster.join()} tone="neutral">
                {cluster.join(' · ')}
              </Chip>
            ))}
          </div>
        </div>
      )}
    </Card>
  )
}

function MiniStat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="bg-surface-2 border border-line-soft rounded-lg px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wider text-ink-faint">{label}</div>
      <div className="num text-lg leading-tight">{value}</div>
      {hint && <div className="text-[10px] text-ink-faint capitalize">{hint}</div>}
    </div>
  )
}

/* ========================================================================== */
/* Decision funnel                                                             */
/* ========================================================================== */
const STAGE_LABEL: Record<string, string> = {
  precheck: 'Pre-check',
  strategy: 'Strategy',
  risk_manager: 'Risk manager',
  risk_officer: 'Risk officer',
  executor: 'Executed',
}

export function FunnelPanel({ funnel }: { funnel: { stage: string; count: number }[] }) {
  const data = funnel.map((f) => ({ ...f, label: STAGE_LABEL[f.stage] ?? f.stage }))
  const total = data.reduce((sum, d) => sum + d.count, 0)

  return (
    <Card
      title="Decision funnel"
      subtitle={`${total} ticker decisions · where each one stopped`}
      bodyClass="p-4 pt-2"
    >
      {total === 0 ? (
        <Empty>no decisions logged yet</Empty>
      ) : (
        <ResponsiveContainer width="100%" height={168}>
          <BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} interval={0} />
            <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} />
            <Tooltip {...tooltipStyle} cursor={{ fill: '#141b26' }} />
            <Bar dataKey="count" radius={[4, 4, 0, 0]} isAnimationActive={false}>
              {data.map((d) => (
                <Cell
                  key={d.stage}
                  fill={d.stage === 'executor' ? '#3ddc97' : d.stage === 'risk_manager' ? '#ff6b81' : '#2f6ea8'}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}

/* ========================================================================== */
/* Basket scan table                                                           */
/* ========================================================================== */
export function ScanPanel({ scan }: { scan: { thresholds: string; rows: ScanRow[] } | null }) {
  return (
    <Card
      title="Basket scan"
      subtitle={scan?.thresholds || undefined}
      bodyClass="overflow-auto"
    >
      {!scan || scan.rows.length === 0 ? (
        <Empty>no scan table yet — the agent writes one per open-market cycle</Empty>
      ) : (
        <table className="w-full text-[11px] num">
          <thead className="sticky top-0 bg-surface">
            <tr className="text-ink-faint text-left">
              {['', 'Price', 'IV', 'RV', 'IV−RV', 'Gates', 'Outcome'].map((h) => (
                <th key={h} className="font-medium px-3 py-2 border-b border-line uppercase tracking-wider text-[10px]">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scan.rows.map((r) => (
              <tr key={r.symbol} className="border-b border-line-soft last:border-0 hover:bg-surface-2">
                <td className="px-3 py-2 font-semibold text-ink">{r.symbol}</td>
                <td className="px-3 py-2 text-ink-dim">{money(r.price)}</td>
                <td className="px-3 py-2 text-ink-dim">{num(r.iv)}</td>
                <td className="px-3 py-2 text-ink-dim">{num(r.rv)}</td>
                <td className={`px-3 py-2 ${r.iv_rv_ok ? 'text-up' : 'text-down'}`}>
                  {r.iv_rv !== null && r.iv_rv >= 0 ? '+' : ''}
                  {num(r.iv_rv)}
                </td>
                <td className="px-3 py-2">
                  <span className="flex gap-1">
                    <Gate ok={r.iv_rv_ok} label="IV" />
                    <Gate ok={r.er_ok} label="ER" />
                    <Gate ok={r.floor_ok} label="FL" />
                  </span>
                </td>
                <td className="px-3 py-2 max-w-[22rem]">
                  <span className="flex items-center gap-1.5">
                    <Chip tone={outcomeTone(r.outcome)}>{r.outcome || '—'}</Chip>
                    <span className="text-ink-faint truncate" title={r.reason}>
                      {r.reason}
                    </span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

function Gate({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      title={`${label}: ${ok ? 'pass' : 'fail'}`}
      className={`inline-grid place-items-center w-6 h-4 rounded text-[9px] font-semibold border ${
        ok ? 'bg-up/10 text-up border-up/25' : 'bg-down/10 text-down border-down/25'
      }`}
    >
      {label}
    </span>
  )
}

/* ========================================================================== */
/* IV vs RV history                                                            */
/* ========================================================================== */
export function IvPanel({ series }: { series: Record<string, IvPoint[]> }) {
  const symbols = useMemo(() => Object.keys(series).sort(), [series])
  const [selected, setSelected] = useState<string | null>(null)
  const active = selected && series[selected] ? selected : symbols[0]

  const data = useMemo(
    () =>
      (active ? series[active] : []).map((p) => ({
        ...p,
        label: new Date(p.t).toLocaleString('en-US', {
          month: 'short',
          day: 'numeric',
          hour: 'numeric',
        }),
      })),
    [series, active],
  )

  return (
    <Card
      title="Implied vs realised vol"
      subtitle={active ? `${data.length} readings` : undefined}
      right={
        <div className="flex flex-wrap gap-1 justify-end max-w-[26rem]">
          {symbols.map((s) => (
            <button
              key={s}
              onClick={() => setSelected(s)}
              className={`num px-1.5 py-0.5 rounded text-[10px] border transition-colors ${
                s === active
                  ? 'bg-accent/15 text-accent border-accent/30'
                  : 'text-ink-faint border-line-soft hover:text-ink-dim hover:border-line'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      }
      bodyClass="p-4 pt-2"
    >
      {data.length === 0 ? (
        <Empty>no volatility history yet</Empty>
      ) : (
        <ResponsiveContainer width="100%" height={188}>
          <LineChart data={data} margin={{ top: 8, right: 4, left: -14, bottom: 0 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={44} />
            <YAxis
              tick={AXIS}
              tickLine={false}
              axisLine={false}
              width={48}
              tickFormatter={(v: number) => v.toFixed(2)}
            />
            <Tooltip {...tooltipStyle} formatter={(v) => Number(v).toFixed(4)} />
            <Line
              type="monotone"
              dataKey="iv"
              name="Implied"
              stroke="#4ea8ff"
              strokeWidth={1.8}
              dot={false}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="rv"
              name="Realised"
              stroke="#ffc046"
              strokeWidth={1.5}
              strokeDasharray="4 3"
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      )}
      <div className="flex gap-4 text-[10px] text-ink-faint mt-1 px-1">
        <Legend color="#4ea8ff" label="Implied" />
        <Legend color="#ffc046" label="Realised" dashed />
        <span className="ml-auto">
          The agent buys vol when implied sits below realised, sells when above.
        </span>
      </div>
    </Card>
  )
}

function Legend({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block w-4 h-0"
        style={{ borderTop: `2px ${dashed ? 'dashed' : 'solid'} ${color}` }}
      />
      {label}
    </span>
  )
}

/* ========================================================================== */
/* Decision feed                                                               */
/* ========================================================================== */
export function DecisionFeed({ decisions }: { decisions: Decision[] }) {
  return (
    <Card
      title="Decision feed"
      subtitle="newest first"
      bodyClass="overflow-y-auto divide-y divide-line-soft"
    >
      {decisions.length === 0 ? (
        <Empty>nothing logged yet</Empty>
      ) : (
        decisions.map((d, i) => (
          <div key={`${d.ts}-${d.symbol}-${i}`} className="px-4 py-2 hover:bg-surface-2">
            <div className="flex items-center gap-2 mb-0.5">
              <span className="num text-[11px] font-semibold text-ink">{d.symbol}</span>
              <Chip tone={outcomeTone(d.outcome)}>{d.outcome}</Chip>
              <span className="text-[10px] text-ink-faint">{STAGE_LABEL[d.stage] ?? d.stage}</span>
              <span className="num ml-auto text-[10px] text-ink-faint">{d.ts.slice(11)}</span>
            </div>
            <p className="text-[11px] text-ink-dim leading-snug">{d.reason}</p>
          </div>
        ))
      )}
    </Card>
  )
}

/* ========================================================================== */
/* Lessons learned                                                             */
/* ========================================================================== */
export function LessonsPanel({ lessons }: { lessons: Lesson[] }) {
  return (
    <Card
      title="Lessons learned"
      subtitle="written by the risk officer after each close"
      bodyClass="overflow-y-auto divide-y divide-line-soft"
    >
      {lessons.length === 0 ? (
        <Empty>no closed trades to learn from yet</Empty>
      ) : (
        lessons.map((l, i) => (
          <div key={`${l.id}-${i}`} className="px-4 py-3 hover:bg-surface-2">
            <div className="flex items-center gap-2 mb-1">
              <span className="num text-[11px] font-semibold">{l.symbol}</span>
              <Chip tone="muted">{l.structure.replace(/_/g, ' ')}</Chip>
              <Chip tone="muted">{l.reason}</Chip>
              <span
                className={`num ml-auto text-[11px] font-semibold ${
                  l.pnl >= 0 ? 'text-up' : 'text-down'
                }`}
              >
                {signedMoney(l.pnl)}
              </span>
            </div>
            <p className="text-[11px] text-ink-dim leading-relaxed">{l.lesson}</p>
          </div>
        ))
      )}
    </Card>
  )
}

/* ========================================================================== */
/* Trade journal                                                               */
/* ========================================================================== */
const KIND_TONE: Record<string, 'up' | 'down' | 'warn' | 'accent' | 'muted' | 'neutral'> = {
  opened: 'up',
  submitted: 'accent',
  closed: 'neutral',
  reconciled: 'warn',
  order_stale_cancelled: 'down',
  order_abandoned: 'down',
  position_dropped: 'down',
}

export function JournalPanel({ history }: { history: HistoryEvent[] }) {
  return (
    <Card
      title="Journal"
      subtitle={`${history.length} events`}
      bodyClass="overflow-y-auto divide-y divide-line-soft"
    >
      {history.length === 0 ? (
        <Empty>no events recorded</Empty>
      ) : (
        history.map((h, i) => (
          <div key={`${h.at}-${i}`} className="px-4 py-2 hover:bg-surface-2">
            <div className="flex items-center gap-2 mb-0.5">
              <Chip tone={KIND_TONE[h.kind] ?? 'neutral'}>{h.kind.replace(/_/g, ' ')}</Chip>
              {h.symbol && <span className="num text-[11px] font-semibold">{h.symbol}</span>}
              <span className="num ml-auto text-[10px] text-ink-faint">
                {h.at.slice(0, 16).replace('T', ' ')}
              </span>
            </div>
            {h.detail && (
              <p className="text-[11px] text-ink-dim leading-snug break-words">{h.detail}</p>
            )}
            {h.regime && <p className="text-[10px] text-ink-faint mt-0.5">{h.regime}</p>}
          </div>
        ))
      )}
    </Card>
  )
}
