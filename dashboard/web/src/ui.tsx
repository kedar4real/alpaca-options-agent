import type { ReactNode } from 'react'

/* Small shared primitives. Everything larger composes out of these so spacing,
   borders and type scale stay consistent across panels. */

export function Card({
  title,
  subtitle,
  right,
  children,
  className = '',
  bodyClass = '',
}: {
  title?: string
  subtitle?: string
  right?: ReactNode
  children: ReactNode
  className?: string
  bodyClass?: string
}) {
  return (
    <section
      className={`bg-surface border border-line rounded-xl overflow-hidden flex flex-col ${className}`}
    >
      {title && (
        <header className="flex items-baseline gap-3 px-4 py-3 border-b border-line-soft shrink-0">
          <h2 className="text-[13px] font-semibold tracking-wide text-ink uppercase">
            {title}
          </h2>
          {subtitle && (
            <span className="text-[11px] text-ink-faint truncate">{subtitle}</span>
          )}
          <div className="ml-auto shrink-0">{right}</div>
        </header>
      )}
      <div className={`flex-1 min-h-0 ${bodyClass || 'p-4'}`}>{children}</div>
    </section>
  )
}

export function Stat({
  label,
  value,
  sub,
  tone = 'neutral',
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: 'neutral' | 'up' | 'down' | 'warn'
}) {
  const toneClass = {
    neutral: 'text-ink',
    up: 'text-up',
    down: 'text-down',
    warn: 'text-warn',
  }[tone]

  return (
    <div className="bg-surface border border-line rounded-xl px-4 py-3">
      <div className="text-[10px] uppercase tracking-[0.12em] text-ink-faint mb-1.5">
        {label}
      </div>
      <div className={`num text-2xl font-semibold leading-none ${toneClass}`}>{value}</div>
      {sub && <div className="text-[11px] text-ink-dim mt-1.5">{sub}</div>}
    </div>
  )
}

type ChipTone = 'neutral' | 'up' | 'down' | 'warn' | 'accent' | 'muted'

export function Chip({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: ChipTone
  title?: string
}) {
  const tones: Record<ChipTone, string> = {
    neutral: 'bg-surface-2 text-ink-dim border-line',
    muted: 'bg-transparent text-ink-faint border-line-soft',
    up: 'bg-up/10 text-up border-up/25',
    down: 'bg-down/10 text-down border-down/25',
    warn: 'bg-warn/10 text-warn border-warn/25',
    accent: 'bg-accent/10 text-accent border-accent/25',
  }
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[11px] font-medium whitespace-nowrap ${tones[tone]}`}
    >
      {children}
    </span>
  )
}

/** Horizontal usage bar — used for the long-vol cap and stage funnel. */
export function Meter({
  value,
  max,
  tone = 'accent',
  height = 6,
}: {
  value: number
  max: number
  tone?: 'accent' | 'up' | 'down' | 'warn'
  height?: number
}) {
  const fraction = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0
  const bar = {
    accent: 'bg-accent',
    up: 'bg-up',
    down: 'bg-down',
    warn: 'bg-warn',
  }[tone]
  return (
    <div
      className="w-full bg-surface-2 rounded-full overflow-hidden"
      style={{ height }}
      role="meter"
      aria-valuenow={value}
      aria-valuemax={max}
    >
      <div
        className={`h-full ${bar} rounded-full transition-[width] duration-500`}
        style={{ width: `${fraction * 100}%` }}
      />
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="h-full min-h-24 grid place-items-center text-[12px] text-ink-faint">
      {children}
    </div>
  )
}

export const outcomeTone = (outcome: string): ChipTone =>
  ({
    Executed: 'up',
    executed: 'up',
    Blocked: 'down',
    blocked: 'down',
    Vetoed: 'warn',
    vetoed: 'warn',
    Skipped: 'muted',
    skipped: 'muted',
  }[outcome] ?? 'neutral') as ChipTone
