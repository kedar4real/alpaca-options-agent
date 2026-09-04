/**
 * The Volatility Arbiter — hackathon landing page.
 *
 * Deployed copy. The canonical portable single-file version lives at
 * ../../../landing/LandingPage.jsx — keep the two in sync by hand (they are
 * identical apart from this file's TypeScript prop typing and the `React`
 * import being dropped for this project's `noUnusedLocals` tsconfig).
 *
 * Styling is Tailwind utility classes plus one injected <style> block for the
 * keyframe / gradient-text bits Tailwind can't express inline. No third-party
 * runtime deps: entrance + scroll-reveal animation is pure CSS driven by an
 * IntersectionObserver. Inter + JetBrains Mono load from Google Fonts via the
 * injected stylesheet.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

/* ------------------------------------------------------------------ config -- */

const DASHBOARD_URL = "https://alpaca-options-agent-ulckr2ugynvqnerbdwohb2.streamlit.app/";
const GITHUB_URL = "https://github.com/kedar4real/alpaca-options-agent";
const WRITEUP_URL = "https://github.com/kedar4real/alpaca-options-agent#readme";
const VIDEO_URL = "#";

const SIGNAL = "#F5A623"; // the one accent — "signal" amber, used everywhere

/* ------------------------------------------------------------------- data --- */
// Every figure below is parsed from the agent's own logs + the live Alpaca
// account state for the competition session. Nothing here is illustrative.

type Tone = "loss" | "gain" | "signal" | undefined;

interface Stat {
  label: string;
  value: string;
  sub?: string;
  note?: string;
  tone?: Tone;
  span: string;
}

const STATS: Stat[] = [
  {
    label: "Starting equity",
    value: "$100,000.00",
    note: "Dedicated paper account, verified against Alpaca's own portfolio-history baseline.",
    span: "md:col-span-1",
  },
  {
    label: "Net P&L",
    value: "-$3,131.03",
    sub: "-3.13%",
    tone: "loss",
    note: "Fully realised — every leg closed flat at the session's final market open.",
    span: "md:col-span-2",
  },
  {
    label: "Proposals evaluated → executed",
    value: "165 → 40",
    note: "76% of what the scanner proposed never became an order.",
    span: "md:col-span-2",
  },
  {
    label: "Rejected by the risk gate",
    value: "98",
    tone: "signal",
    note: "A deterministic check in risk_manager.py. No model in this loop.",
    span: "md:col-span-1",
  },
  {
    label: "Vetoed in Bull / Bear debate",
    value: "27",
    note: "The LLM Judge talked the desk out of the trade.",
    span: "md:col-span-1",
  },
  {
    label: "Option-chain scans",
    value: "447",
    note: "One narrowed snapshot per symbol, every 5 minutes of market hours.",
    span: "md:col-span-1",
  },
  {
    label: "Safety floor — never breached",
    value: "$95,000",
    tone: "signal",
    note: "A 5% drawdown latches a sticky halt for the rest of the run.",
    span: "md:col-span-1",
  },
];

interface DebateSide {
  side: string;
  role: string;
  accent: "gain" | "loss";
  points: string[];
}

const DEBATE: DebateSide[] = [
  {
    side: "Bull",
    role: "Argues to enter",
    accent: "gain",
    points: [
      "Names the regime edge — implied vol rich to realised, or a sentiment-aligned trend.",
      "Quantifies the credit taken in and the room the short strike has to be wrong.",
    ],
  },
  {
    side: "Bear",
    role: "Argues to stand down",
    accent: "loss",
    points: [
      "Surfaces the macro event, the thin venue, the assignment risk on the short leg.",
      "Assumes the tail: asks what a gap straight through the long strike actually costs.",
    ],
  },
];

interface LinkCard {
  title: string;
  desc: string;
  href: string;
  cta: string;
}

const LINKS: LinkCard[] = [
  {
    title: "Live Dashboard",
    desc: "Read-only audit view — the equity curve, every pipeline decision, and the cage-compliance strip showing each trade sized under its cap.",
    href: DASHBOARD_URL,
    cta: "Open dashboard",
  },
  {
    title: "GitHub Repo",
    desc: "Full source. The defined-risk invariant lives in risk_manager.is_defined_risk() and is re-checked in the executor before every submit.",
    href: GITHUB_URL,
    cta: "View code",
  },
  {
    title: "One-Page Write-up",
    desc: "How a cycle works, the volatility-regime table, and exactly what the deterministic risk gate checks before a model ever sees the trade.",
    href: WRITEUP_URL,
    cta: "Read write-up",
  },
  {
    title: "Video Walkthrough",
    desc: "Five minutes end to end: a scan, a Bull/Bear/Judge debate, a gate rejection, and a fill landing on the book.",
    href: VIDEO_URL,
    cta: "Watch",
  },
];

/* --------------------------------------------------------------- primitives - */

function Reveal({
  children,
  delay = 0,
  className = "",
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setShown(true);
      return;
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          io.disconnect();
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -40px 0px" }
    );
    io.observe(el);
    // safety net: never leave content permanently hidden if it's already
    // past the viewport on load or the observer never fires.
    const t = setTimeout(() => setShown(true), 1600);
    return () => {
      io.disconnect();
      clearTimeout(t);
    };
  }, []);

  return (
    <div
      ref={ref}
      className={`reveal ${shown ? "is-visible" : ""} ${className}`}
      style={{ transitionDelay: `${delay}ms` }}
    >
      {children}
    </div>
  );
}

function toneClass(tone: Tone): string {
  if (tone === "loss") return "text-[#F87171]";
  if (tone === "gain") return "text-[#4ADE80]";
  if (tone === "signal") return "text-[#F5A623]";
  return "text-white";
}

function StatCard({ label, value, sub, note, tone }: Stat) {
  const color = toneClass(tone);
  return (
    <div className="group relative h-full overflow-hidden rounded-2xl border border-[#201733] bg-[#0F0B18] p-6 transition-all duration-300 hover:scale-[1.02] hover:border-[#F5A623]/40 hover:shadow-[0_0_36px_-10px_rgba(245,166,35,0.28)]">
      <div
        aria-hidden
        className="pointer-events-none absolute -right-12 -top-12 h-36 w-36 rounded-full bg-[#F5A623]/10 blur-2xl opacity-0 transition-opacity duration-300 group-hover:opacity-100"
      />
      <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-slate-500">
        {label}
      </p>
      <p className={`va-mono mt-3 text-3xl font-semibold sm:text-4xl ${color}`}>
        {value}
      </p>
      {sub && <p className={`va-mono mt-1 text-sm ${color}`}>{sub}</p>}
      {note && (
        <p className="mt-3 text-xs leading-relaxed text-slate-500">{note}</p>
      )}
    </div>
  );
}

function DebateCard({ side, role, accent, points }: DebateSide) {
  const bar =
    accent === "gain" ? "border-l-[#4ADE80]/70" : "border-l-[#F87171]/70";
  const tag = accent === "gain" ? "text-[#4ADE80]" : "text-[#F87171]";
  return (
    <div
      className={`h-full rounded-2xl border border-[#201733] border-l-2 ${bar} bg-[#0F0B18] p-6 transition-all duration-300 hover:border-[#F5A623]/30 hover:shadow-[0_0_30px_-12px_rgba(245,166,35,0.25)]`}
    >
      <div className="flex items-baseline justify-between">
        <h3 className="text-lg font-semibold text-white">{side}</h3>
        <span className={`va-mono text-[11px] uppercase tracking-[0.14em] ${tag}`}>
          {role}
        </span>
      </div>
      <ul className="mt-4 space-y-3">
        {points.map((p) => (
          <li
            key={p}
            className="flex gap-2.5 text-sm leading-relaxed text-slate-400"
          >
            <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-600" />
            {p}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SectionHeading({
  kicker,
  title,
  children,
}: {
  kicker?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="mb-10 max-w-2xl">
      {kicker && (
        <p className="va-mono mb-3 text-[11px] uppercase tracking-[0.2em] text-[#F5A623]">
          {kicker}
        </p>
      )}
      <h2 className="text-2xl font-bold tracking-tight text-white sm:text-3xl">
        {title}
      </h2>
      {children && (
        <p className="mt-3 text-sm leading-relaxed text-slate-400">{children}</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- styles - */

const CSS = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

.va-root { font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif; }
.va-mono {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-feature-settings: "tnum" 1;
  letter-spacing: -0.01em;
}

html { scroll-behavior: smooth; }

@keyframes va-fade-up {
  from { opacity: 0; transform: translateY(22px); }
  to   { opacity: 1; transform: none; }
}
@keyframes va-drift-a {
  0%   { transform: translate3d(-8%, -6%, 0) scale(1);    opacity: .30; }
  50%  { transform: translate3d(6%, 4%, 0)  scale(1.15);  opacity: .50; }
  100% { transform: translate3d(-8%, -6%, 0) scale(1);    opacity: .30; }
}
@keyframes va-drift-b {
  0%   { transform: translate3d(5%, 8%, 0)   scale(1.1);  opacity: .20; }
  50%  { transform: translate3d(-6%, -4%, 0) scale(1);    opacity: .40; }
  100% { transform: translate3d(5%, 8%, 0)   scale(1.1);  opacity: .20; }
}
@keyframes va-draw { to { stroke-dashoffset: 0; } }

.va-hero-in   { animation: va-fade-up .75s cubic-bezier(.22,1,.36,1) both; }
.va-hero-in-2 { animation: va-fade-up .75s cubic-bezier(.22,1,.36,1) .12s both; }
.va-hero-in-3 { animation: va-fade-up .75s cubic-bezier(.22,1,.36,1) .24s both; }

.va-blob-a { animation: va-drift-a 19s ease-in-out infinite; }
.va-blob-b { animation: va-drift-b 23s ease-in-out infinite; }

.va-draw-path {
  stroke-dasharray: 2600;
  stroke-dashoffset: 2600;
  animation: va-draw 3.6s ease-out .3s forwards;
}

.reveal {
  opacity: 0;
  transform: translateY(18px);
  transition: opacity .6s cubic-bezier(.22,1,.36,1), transform .6s cubic-bezier(.22,1,.36,1);
}
.reveal.is-visible { opacity: 1; transform: none; }

.va-gradient-text {
  background: linear-gradient(115deg, #FFFFFF 0%, #FFE7BD 40%, #F5A623 72%, #C77D12 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

@media (prefers-reduced-motion: reduce) {
  .va-hero-in, .va-hero-in-2, .va-hero-in-3,
  .va-blob-a, .va-blob-b, .va-draw-path { animation: none !important; }
  .reveal { opacity: 1 !important; transform: none !important; transition: none !important; }
  .va-draw-path { stroke-dashoffset: 0 !important; }
  html { scroll-behavior: auto; }
}
`;

/* ------------------------------------------------------------------- page --- */

export default function LandingPage() {
  return (
    <div className="va-root relative isolate min-h-screen overflow-x-hidden bg-[#06030B] text-slate-200 antialiased">
      <style dangerouslySetInnerHTML={{ __html: CSS }} />

      {/* ambient drifting glows — behind everything */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 z-0 overflow-hidden"
      >
        <div className="va-blob-a absolute left-[6%] top-[4%] h-[42rem] w-[42rem] rounded-full bg-[#F5A623]/20 blur-3xl" />
        <div className="va-blob-b absolute right-[2%] top-[42%] h-[38rem] w-[38rem] rounded-full bg-[#6D28D9]/20 blur-3xl" />
      </div>

      {/* ---------------------------------------------------------- navbar -- */}
      <header className="fixed inset-x-0 top-0 z-50 border-b border-white/5 bg-[#06030B]/70 backdrop-blur-md">
        <nav className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5">
          <a href="#top" className="flex items-center gap-2">
            <span className="text-lg" aria-hidden>
              ⚖
            </span>
            <span className="text-sm font-semibold tracking-tight text-white">
              The Volatility Arbiter
            </span>
          </a>

          <div className="hidden items-center gap-8 text-sm text-slate-400 md:flex">
            <a href={DASHBOARD_URL} className="transition-colors hover:text-white">
              Dashboard
            </a>
            <a href={GITHUB_URL} className="transition-colors hover:text-white">
              GitHub
            </a>
            <a href={WRITEUP_URL} className="transition-colors hover:text-white">
              Write-up
            </a>
          </div>

          <a
            href={DASHBOARD_URL}
            className="rounded-lg border border-[#F5A623]/40 px-3.5 py-2 text-xs font-medium text-[#F5A623] transition-all duration-300 hover:border-[#F5A623]/70 hover:bg-[#F5A623]/10"
          >
            View Live Dashboard
          </a>
        </nav>
      </header>

      {/* ------------------------------------------------------------ hero -- */}
      <section
        id="top"
        className="relative z-10 mx-auto flex min-h-[82vh] max-w-4xl flex-col items-center justify-center px-5 pb-16 pt-32 text-center"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute left-1/2 top-1/2 -z-10 h-[34rem] w-[34rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#F5A623]/15 blur-3xl"
        />

        {/* faint volatility trace, draws in on load */}
        <svg
          aria-hidden
          viewBox="0 0 1200 300"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-x-0 bottom-8 -z-10 h-56 w-full opacity-[0.16]"
        >
          <path
            className="va-draw-path"
            d="M0,214 L60,204 L120,232 L180,188 L240,206 L300,150 L360,176 L420,120 L480,160 L540,132 L600,198 L660,168 L720,236 L780,206 L840,150 L900,184 L960,138 L1020,168 L1080,118 L1140,150 L1200,108"
            fill="none"
            stroke={SIGNAL}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>

        <span className="va-hero-in mb-6 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1 text-xs text-slate-400">
          <span className="h-1.5 w-1.5 rounded-full bg-[#F5A623]" aria-hidden />
          Alpaca &times; LabLab.ai Hackathon
        </span>

        <h1 className="va-hero-in text-4xl font-extrabold leading-[1.08] tracking-tight text-white sm:text-6xl">
          An options desk that trades inside a{" "}
          <span className="va-gradient-text">cage it can&rsquo;t open</span>.
        </h1>

        <p className="va-hero-in-2 mt-6 max-w-2xl text-base leading-relaxed text-slate-400 sm:text-lg">
          An autonomous agent picks a structure for the volatility regime, sizes
          every trade against a hard risk budget, and makes an LLM argue both
          sides before a single order goes out. Defined-risk only. A 5% drawdown
          latches it shut for the rest of the run.
        </p>

        <div className="va-hero-in-3 mt-9 flex flex-col items-center gap-4 sm:flex-row">
          <a
            href={DASHBOARD_URL}
            className="rounded-xl bg-[#F5A623] px-6 py-3 text-sm font-semibold text-[#1a1206] shadow-[0_0_40px_-8px_rgba(245,166,35,0.6)] transition-all duration-300 hover:scale-[1.02] hover:bg-[#ffb739]"
          >
            View the Live Dashboard
          </a>
          <a
            href={GITHUB_URL}
            className="group inline-flex items-center gap-1.5 text-sm text-slate-300 transition-colors hover:text-white"
          >
            Read the source on GitHub
            <span
              aria-hidden
              className="transition-transform group-hover:translate-x-0.5"
            >
              &rarr;
            </span>
          </a>
        </div>
      </section>

      {/* ----------------------------------------------------- stat bento -- */}
      <section className="relative z-10 mx-auto max-w-6xl px-5 py-20">
        <Reveal>
          <SectionHeading kicker="The session" title="One run, on a paper account">
            Every number is parsed straight from the agent&rsquo;s logs and its
            live Alpaca account state. Nothing on this page is illustrative.
          </SectionHeading>
        </Reveal>

        <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
          {STATS.map((s, i) => (
            <Reveal key={s.label} delay={i * 80} className={s.span}>
              <StatCard {...s} />
            </Reveal>
          ))}
        </div>
      </section>

      {/* --------------------------------------------------- how it thinks - */}
      <section className="relative z-10 mx-auto max-w-6xl px-5 py-20">
        <Reveal>
          <SectionHeading
            kicker="How it thinks"
            title="Every trade is argued before it's placed"
          >
            The top-ranked candidate each cycle goes to a three-role debate. A
            Bull builds the case to enter. A Bear builds the case to stand down.
            A Judge rules &mdash; and it can only ever add a reason to pass, never
            to override the risk gate that ran first.
          </SectionHeading>
        </Reveal>

        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          {DEBATE.map((d, i) => (
            <Reveal key={d.side} delay={i * 90}>
              <DebateCard {...d} />
            </Reveal>
          ))}
        </div>

        <Reveal delay={140}>
          <div className="mt-5 rounded-2xl border border-[#201733] bg-[#0F0B18] p-6">
            <p className="va-mono text-[11px] uppercase tracking-[0.14em] text-[#F5A623]">
              The Judge
            </p>
            <p className="mt-2 text-sm leading-relaxed text-slate-300">
              Weighs both sides, then returns{" "}
              <span className="va-mono text-[#4ADE80]">APPROVE</span> or{" "}
              <span className="va-mono text-[#F87171]">VETO</span>. It runs after
              the deterministic risk check &mdash; a trade the gate has already
              rejected never reaches the table.
            </p>
          </div>
        </Reveal>

        <Reveal delay={180}>
          <p className="mt-10 border-l-2 border-[#F5A623]/60 pl-4 text-lg font-medium leading-relaxed text-slate-200 sm:text-xl">
            The result isn&rsquo;t more trades. Of 165 proposals, 98 died at the
            gate and 27 more lost the debate. What&rsquo;s left is slower,
            smaller, and sized so a wrong one can&rsquo;t reach the floor.
          </p>
        </Reveal>
      </section>

      {/* ------------------------------------------------------- links out - */}
      <section className="relative z-10 mx-auto max-w-6xl px-5 py-20">
        <Reveal>
          <SectionHeading kicker="Dig in" title="Look at everything" />
        </Reveal>

        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
          {LINKS.map((c, i) => (
            <Reveal key={c.title} delay={i * 70}>
              <a
                href={c.href}
                target="_blank"
                rel="noreferrer"
                className="group flex h-full flex-col rounded-2xl border border-[#201733] bg-[#0F0B18] p-6 transition-all duration-300 hover:scale-[1.02] hover:border-[#F5A623]/45 hover:shadow-[0_0_36px_-10px_rgba(245,166,35,0.3)]"
              >
                <h3 className="text-base font-semibold text-white">{c.title}</h3>
                <p className="mt-2 flex-1 text-sm leading-relaxed text-slate-400">
                  {c.desc}
                </p>
                <span className="va-mono mt-5 inline-flex items-center gap-1.5 text-[11px] uppercase tracking-[0.14em] text-[#F5A623]">
                  {c.cta}
                  <span
                    aria-hidden
                    className="transition-transform group-hover:translate-x-1"
                  >
                    &rarr;
                  </span>
                </span>
              </a>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ---------------------------------------------------------- footer - */}
      <footer className="relative z-10 border-t border-white/5">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-5 py-10 text-xs text-slate-500 sm:flex-row">
          <p>
            The Volatility Arbiter &mdash; built for the Alpaca &times; LabLab.ai
            Hackathon, 2026.
          </p>
          <p className="flex items-center gap-4">
            <a href={GITHUB_URL} className="transition-colors hover:text-slate-300">
              GitHub
            </a>
            <a
              href={DASHBOARD_URL}
              className="transition-colors hover:text-slate-300"
            >
              Dashboard
            </a>
            <span className="va-mono">PA3FCNG4S7EO &middot; paper</span>
          </p>
        </div>
      </footer>
    </div>
  );
}
