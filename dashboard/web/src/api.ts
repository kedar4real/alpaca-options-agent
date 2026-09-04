import { useEffect, useRef, useState } from 'react'

/* --------------------------------------------------------------------------
   Shapes returned by the read-only dashboard API. These mirror the endpoints
   in server/app.py; anything the server cannot supply arrives as null/empty
   rather than absent, so panels render in a degraded state instead of
   throwing.
-------------------------------------------------------------------------- */

export interface Exposure {
  debit: number
  cap: number
  pct: number
  max_pct: number
  count: number
  headroom: number
  breached: boolean
}

export interface State {
  account_id: string
  paper: boolean
  equity: number
  starting_equity: number
  pnl: number
  pnl_pct: number
  cash: number
  buying_power: number
  trading_halted: boolean
  hard_stop_et: string
  hard_stop_done: boolean
  hard_stop_seconds_left: number | null
  open_position_count: number
  pending_order_count: number
  exposure: Exposure
  market: { is_open?: boolean; next_open?: string; next_close?: string }
  agent: { alive: boolean; last_log_age_s: number | null; last_log_at: string | null }
  daily_activity: Record<string, DailyActivity>
}

export interface DailyActivity {
  date: string
  basket_size: number
  ticker_scans: number
  proposed: number
  approved: number
  rm_vetoes: number
  ro_vetoes: number
  regimes: Record<string, number>
}

export interface Leg {
  action: string
  right: string
  quantity: number
  symbol: string
  current_price: number | null
  market_value: number | null
  unrealized_pl: number | null
  at_broker: boolean
}

export interface Position {
  id: string
  symbol: string
  structure: string
  expiry: string
  quantity: number
  entry_credit: number
  entry_dollars: number
  opened_at: string
  peak_gain_fraction: number
  unrealized_pl: number
  unrealized_pct: number
  legs_matched: number
  legs_expected: number
  legs: Leg[]
}

export interface Decision {
  ts: string
  symbol: string
  outcome: string
  stage: string
  reason: string
}

export interface OrderEvent {
  ts: string
  event: string
  symbol: string
  order_id: string
  structure: string
  detail: string
}

export interface ScanRow {
  symbol: string
  price: number | null
  iv: number | null
  rv: number | null
  iv_rv: number | null
  iv_rv_ok: boolean
  er: number | null
  er_ok: boolean
  floor_ok: boolean
  credit_to_width: number | null
  outcome: string
  stage: string
  reason: string
}

export interface SymbolSignal {
  rsi: number | null
  rsi_label: string
  adx: number | null
  adx_label: string
  news: string[]
}

export interface Signals {
  macro_event: string
  macro_date: string
  vix: number | null
  vix3m: number | null
  vix_ratio: number | null
  vix_state: string
  regime_signals: string[]
  correlated: string[][]
  symbols: Record<string, SymbolSignal>
}

export interface IvPoint { t: string; iv: number; rv: number | null; spread: number | null }
export interface EquityPoint { t: number; equity: number; pl: number }

export interface HistoryEvent {
  kind: string
  at: string
  id?: string
  symbol?: string
  structure?: string
  regime?: string | null
  detail?: string
  cycles?: number
}

export interface Lesson {
  at: string
  id: string
  symbol: string
  structure: string
  reason: string
  pnl: number
  lesson: string
}

/* -------------------------------------------------------------------------- */

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`${path} -> ${res.status}`)
  return res.json() as Promise<T>
}

/**
 * Poll an endpoint on an interval.
 *
 * Keeps the last good value on failure so a transient API blip dims the
 * screen rather than blanking it, and skips the timer while the tab is
 * hidden to avoid a backlog of requests on return.
 */
export function usePoll<T>(path: string, intervalMs: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    let timer: number

    const tick = async () => {
      if (!document.hidden) {
        try {
          const next = await get<T>(path)
          if (!alive.current) return
          setData(next)
          setError(null)
          setUpdatedAt(Date.now())
        } catch (e) {
          if (alive.current) setError(e instanceof Error ? e.message : 'request failed')
        }
      }
      timer = window.setTimeout(tick, intervalMs)
    }

    tick()
    return () => {
      alive.current = false
      window.clearTimeout(timer)
    }
  }, [path, intervalMs])

  return { data, error, updatedAt }
}

/* ------------------------------- formatting ------------------------------- */

export const money = (n: number | null | undefined, decimals = 2) =>
  n === null || n === undefined
    ? '—'
    : n.toLocaleString('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })

export const signedMoney = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : (n >= 0 ? '+' : '') + money(n)

export const pct = (n: number | null | undefined, decimals = 2) =>
  n === null || n === undefined ? '—' : `${(n * 100).toFixed(decimals)}%`

export const signedPct = (n: number | null | undefined, decimals = 2) =>
  n === null || n === undefined ? '—' : (n >= 0 ? '+' : '') + pct(n, decimals)

export const num = (n: number | null | undefined, decimals = 3) =>
  n === null || n === undefined ? '—' : n.toFixed(decimals)

/** "10h 34m" / "18m 02s" — a countdown that stays readable at any range. */
export function countdown(totalSeconds: number | null): string {
  if (totalSeconds === null) return '—'
  if (totalSeconds <= 0) return 'reached'
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = Math.floor(totalSeconds % 60)
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  return `${m}m ${String(s).padStart(2, '0')}s`
}

/** OCC option symbol -> readable strike, e.g. IWM260904P00290000 -> "290 P". */
export function occLabel(symbol: string): string {
  const m = /^([A-Z]{1,6})(\d{6})([CP])(\d{8})$/.exec(symbol)
  if (!m) return symbol
  const strike = parseInt(m[4], 10) / 1000
  return `${strike % 1 === 0 ? strike.toFixed(0) : strike.toFixed(2)} ${m[3]}`
}
