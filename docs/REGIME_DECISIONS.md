# Regime decisions — full specification

Source of truth: `src/trading_agent/strategy.py`, `select_regime()`. Checked
once per ticker per cycle against that ticker's market snapshot. The
overrides run in a fixed order — each one either returns a decision
immediately or falls through to the next check:

```
1. HARVEST override (opt-in, checked first)
2. base quantitative regime (A / B / C / none)
3. macro / panic override (vetoes short-vol regimes)
4. ADX trend-strength override (vetoes the iron condor specifically)
```

## 1 — HARVEST override (opt-in, final-session only)

Gated by `AGENT_HARVEST_MODE` (off by default — this was turned on for the
final competition session, and accounted for 61 of the session's 297 logged
regime decisions). Checked *before* anything quantitative:

```python
HARVEST_SENTIMENT_MIN = 0.2
HARVEST_SHORT_DELTA = 0.35        # wider band than the base 0.25-0.30 — max premium
HARVEST_SPREAD_WIDTH = 5.0
HARVEST_MIN_CTW = 0.10            # credit-to-width floor (vs 0.20-0.25 elsewhere)
HARVEST_DTE_MIN = 0               # allows the nearest listed expiry, incl. 0-DTE
```

A news-sentiment score above `+0.2` forces a **bull put** credit spread;
below `-0.2` forces a **bear call**. Sentiment inside the band falls through
to the normal quant regime below — this never manufactures a trade the news
doesn't back, it only redirects which structure the regime logic would have
picked otherwise. It is a deliberate directional theta grab, not a
volatility-edge trade, and it trades a wider, cheaper-premium band than the
rest of the system (0.35Δ vs 0.25–0.30Δ, 10% min credit/width vs 20–25%
elsewhere, 0-DTE allowed vs a 1–3 DTE floor).

## 2 — Base quantitative regime

```python
MIN_IV_RV_SPREAD = 0.015   # "IV rich" threshold
LOW_IV_RV_SPREAD = -0.02   # "IV cheap" threshold (from strategy.py's constant, unchanged)
RANGE_BOUND_ER = 0.45      # Kaufman efficiency ratio: < this is range-bound
```

| Regime | Condition | Structure |
|---|---|---|
| **A** | `iv_rv_spread >= 0.015` **and** the IV-regime gate says elevated (`iv_regime.trade_eligible`) | Iron condor |
| **B** | `iv_rv_spread <= -0.02` **and** `efficiency_ratio < 0.45` (range-bound) | Long strangle — buy ~0.25Δ put + call, net debit |
| **C** | `iv_rv_spread <= -0.02` **and** `efficiency_ratio >= 0.45` (trending) | Bull put (uptrend) / bear call (downtrend) |
| — | ATM IV or realized vol unavailable, or none of the above | No trade |

`efficiency_ratio(closes, window=10)` = `|net change| / Σ|daily abs changes|`
over the last 10 sessions — a pure trend-vs-chop measure (Kaufman's Efficiency
Ratio), independent of the IV/RV spread.

## 3 — Macro / panic override

If the `IntelligenceHub` context flags `MACRO_DANGER` (a high-impact
calendar event inside 48h) or `PANIC_REGIME` (VIX term structure inverted —
front-month VIX above the 3-month), **any** short-volatility base regime
(iron condor, bull put, bear call) is vetoed and swapped for a **long
strangle** instead:

> `Regime OVERRIDE: {flags} -> Long Strangle (vetoed short-vol {base.regime})`

A base regime of "no trade" is left alone — the override never manufactures
a position, it only redirects one the quant regime already wanted to open.
This override alone accounted for 48 of the session's 297 regime decisions.

## 4 — ADX trend-strength override

```python
ADX_TREND_HIGH = 25   # >= this: confirmed strong trend
```

Runs only when the base regime (after the macro check) is still an **iron
condor** — selling a range right as it breaks into a confirmed trend is the
classic condor killer. If Wilder's 14-period ADX is `>= 25`:

- Trend direction **up** → forced into a **bull put** instead (condor
  disabled).
- Trend direction **down** → forced into a **bear call** instead.
- No clear direction → stand aside (`No trade`), the condor is still
  disabled but nothing replaces it.

ADX in the 20–25 band is deliberately left to the efficiency-ratio call in
regime B/C above — only a *confirmed* strong trend (≥25) overrides the
condor.

## What actually ran, final competition session

From the last nightly post-mortem's regime breakdown (`agent_activity.log`):

| Label | Count |
|---|---|
| No trade | 98 |
| Regime OVERRIDE: MACRO_DANGER → Long Strangle (vetoed short-vol) | 48 |
| Regime A: High Volatility → Iron Condor | 45 |
| HARVEST: bullish sentiment → Bull Put (score 1) | 40 |
| Regime B: Low IV / Range-Bound → Long Strangle | 38 |
| HARVEST: bullish sentiment → Bull Put (score 2) | 21 |
| ADX OVERRIDE: ADX ≥25, up-trend → Bull Put (condor disabled) | 6 |
| ADX OVERRIDE: ADX ≥25, up-trend → Bull Put (condor disabled) | 1 |

297 total logged regime decisions in the final post-mortem's tally (a subset
of the session's 1,009 total pipeline decisions across all stages — most
cycles resolve at `precheck` before a regime is ever computed).
