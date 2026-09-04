# Landing page — The Volatility Arbiter

Marketing / submission landing page for the hackathon entry.

## Files

| File | Role |
|---|---|
| `LandingPage.jsx` | Canonical **portable single-file** component. Plain JSX, one injected `<style>` block, no runtime deps beyond React. Drop it into any React + Tailwind app. |

## Deployed copy

The version that actually ships is **`dashboard/web/src/LandingPage.tsx`** — the
same component, TypeScript-typed for that project's `tsconfig` (`noUnusedLocals`
means the `React` import is dropped there). It is wired as the **home route** of
`dashboard/web` (`src/main.tsx`); the old live React dashboard stays reachable at
`#/legacy` and is lazy-loaded so it never weighs down the landing bundle.

`dashboard/web` deploys as a static Vite build (`npm run build` → `dist/`). The
read-only **audit dashboard is a separate Streamlit deployment** (`audit/dashboard.py`);
the two are linked only by the `DASHBOARD_URL` constant in the landing page.

**Keep `LandingPage.jsx` and `dashboard/web/src/LandingPage.tsx` in sync by hand.**
They should differ only in the import line and the added TS type annotations.

## Config constants (top of the component)

- `DASHBOARD_URL` — public URL of the deployed Streamlit audit dashboard (placeholder until it's live)
- `GITHUB_URL` — repo
- `WRITEUP_URL` — one-page write-up (currently the GitHub README)
- `VIDEO_URL` — walkthrough video (placeholder until recorded)

## Numbers

Every figure in the stat grid is the real competition-session result, verified
against the live Alpaca API for account `PA3FCNG4S7EO`. Baseline for P&L is the
account's `$100,000.00` funding (`portfolio/history` `base_value`), not the
agent's persisted run-start.
