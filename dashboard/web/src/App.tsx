import { useEffect, useState } from 'react'
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
  countdown,
  money,
  signedMoney,
  signedPct,
  usePoll,
} from './api'
import { Chip, Stat } from './ui'
import {
  DecisionFeed,
  EquityPanel,
  FunnelPanel,
  IvPanel,
  JournalPanel,
  LessonsPanel,
  PositionsPanel,
  RegimePanel,
  ScanPanel,
} from './panels'

/* Poll cadences: money and liveness refresh briskly, historical series slowly.
   The agent itself only cycles every 300s, so nothing here needs to be faster
   than a few seconds to feel live. */
const FAST = 5_000
const MEDIUM = 15_000
const SLOW = 60_000

export default function App() {
  const state = usePoll<State>('/api/state', FAST)
  const positions = usePoll<{ positions: Position[] }>('/api/positions', FAST)
  const decisions = usePoll<{ decisions: Decision[]; funnel: { stage: string; count: number }[] }>(
    '/api/decisions',
    MEDIUM,
  )
  const scan = usePoll<{ thresholds: string; rows: ScanRow[] }>('/api/scan', MEDIUM)
  const signals = usePoll<Signals>('/api/signals', MEDIUM)
  const equity = usePoll<{ points: EquityPoint[]; starting_equity: number | null }>(
    '/api/equity',
    MEDIUM,
  )
  const iv = usePoll<{ series: Record<string, IvPoint[]> }>('/api/iv', SLOW)
  const journal = usePoll<{ history: HistoryEvent[] }>('/api/journal', MEDIUM)
  const lessons = usePoll<{ lessons: Lesson[] }>('/api/lessons', SLOW)

  const s = state.data

  return (
    <div className="min-h-full flex flex-col">
      <TopBar state={s} offline={!!state.error} />

      <main className="flex-1 px-5 pb-6 space-y-4 max-w-[1800px] w-full mx-auto">
        {/* Headline numbers */}
        <section className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          <Stat
            label="Equity"
            value={money(s?.equity)}
            sub={s ? `started ${money(s.starting_equity)}` : undefined}
          />
          <Stat
            label="P&L"
            value={signedMoney(s?.pnl)}
            sub={s ? signedPct(s.pnl_pct) : undefined}
            tone={!s ? 'neutral' : s.pnl >= 0 ? 'up' : 'down'}
          />
          <Stat
            label="Open positions"
            value={s?.open_position_count ?? '—'}
            sub={
              s
                ? `${s.pending_order_count} pending · ${s.exposure.count} long-vol`
                : undefined
            }
          />
          <Stat
            label="Long-vol premium"
            value={money(s?.exposure.debit, 0)}
            sub={s ? `of ${money(s.exposure.cap, 0)} cap (${s.exposure.max_pct * 100}%)` : undefined}
            tone={s?.exposure.breached ? 'down' : 'neutral'}
          />
          <Stat
            label="Hard stop"
            value={<HardStop seconds={s?.hard_stop_seconds_left ?? null} />}
            sub={s ? `${s.hard_stop_et} ET` : undefined}
            tone={
              s?.hard_stop_seconds_left !== null &&
              s?.hard_stop_seconds_left !== undefined &&
              s.hard_stop_seconds_left < 3600
                ? 'warn'
                : 'neutral'
            }
          />
        </section>

        {/* Row 1 — money and the book */}
        <section className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <EquityPanel
            points={equity.data?.points ?? []}
            startingEquity={equity.data?.starting_equity ?? null}
          />
          <PositionsPanel positions={positions.data?.positions ?? []} />
          <RegimePanel signals={signals.data} state={s} />
        </section>

        {/* Row 2 — what it looked at and why it passed */}
        <section className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div className="xl:col-span-2">
            <ScanPanel scan={scan.data} />
          </div>
          <FunnelPanel funnel={decisions.data?.funnel ?? []} />
        </section>

        {/* Row 3 — volatility edge and the decision stream */}
        <section className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div className="xl:col-span-2">
            <IvPanel series={iv.data?.series ?? {}} />
          </div>
          <div className="max-h-[420px] flex">
            <DecisionFeed decisions={decisions.data?.decisions ?? []} />
          </div>
        </section>

        {/* Row 4 — memory */}
        <section className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <div className="max-h-[400px] flex">
            <LessonsPanel lessons={lessons.data?.lessons ?? []} />
          </div>
          <div className="max-h-[400px] flex">
            <JournalPanel history={journal.data?.history ?? []} />
          </div>
        </section>
      </main>

      <footer className="px-5 py-3 text-[10px] text-ink-faint border-t border-line-soft">
        Read-only view. This dashboard never places, cancels or closes an order — the agent
        process is the only thing that touches the account.
      </footer>
    </div>
  )
}

/* -------------------------------------------------------------------------- */

function TopBar({ state, offline }: { state: State | null; offline: boolean }) {
  const alive = state?.agent.alive
  const marketOpen = state?.market.is_open

  return (
    <header className="sticky top-0 z-10 bg-bg/90 backdrop-blur border-b border-line px-5 py-3 mb-4">
      <div className="max-w-[1800px] mx-auto flex items-center gap-3 flex-wrap">
        <div className="flex items-baseline gap-2.5">
          <span className="text-lg font-semibold tracking-tight">Vega</span>
          <span className="text-[11px] text-ink-faint">autonomous options desk</span>
        </div>

        <div className="flex items-center gap-2 ml-auto flex-wrap">
          {offline ? (
            <Chip tone="down">API unreachable</Chip>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-[11px] text-ink-dim">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  alive ? 'bg-up pulse' : 'bg-down'
                }`}
              />
              {alive ? 'agent live' : 'agent stalled'}
              {state?.agent.last_log_age_s !== null && state?.agent.last_log_age_s !== undefined && (
                <span className="num text-ink-faint">
                  · {Math.round(state.agent.last_log_age_s)}s
                </span>
              )}
            </span>
          )}

          <Chip tone={marketOpen ? 'up' : 'muted'}>
            {marketOpen ? 'market open' : 'market closed'}
          </Chip>

          {state?.trading_halted && <Chip tone="down">HALTED</Chip>}
          {state?.hard_stop_done && <Chip tone="warn">hard stop reached</Chip>}

          {state && (
            <>
              <Chip tone="neutral" title="Alpaca account">
                <span className="num">{state.account_id}</span>
              </Chip>
              {state.paper && <Chip tone="accent">paper</Chip>}
            </>
          )}
        </div>
      </div>
    </header>
  )
}

/** Ticks down locally between polls so the countdown never looks frozen. */
function HardStop({ seconds }: { seconds: number | null }) {
  const [local, setLocal] = useState(seconds)

  useEffect(() => setLocal(seconds), [seconds])
  useEffect(() => {
    if (local === null) return
    const id = window.setInterval(() => setLocal((v) => (v === null ? v : v - 1)), 1000)
    return () => window.clearInterval(id)
  }, [local === null])

  return <>{countdown(local)}</>
}
