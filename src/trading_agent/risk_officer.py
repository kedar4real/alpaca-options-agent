"""
risk_officer.py — LLM "second opinion" gate.

After ``risk_manager.check_order()`` has APPROVED a ``ProposedOrder``, the caller
passes the order + the market snapshot + the ``AccountState`` here. This module
asks an LLM to APPROVE or VETO the trade and give a 2-3 sentence thesis,
reasoning about the IV regime, the IV-RV spread, and current risk exposure.

Two providers, tried in order:

  1. **Featherless AI** (primary) — hosted, OpenAI-compatible API, via the
     ``openai`` package. Needs ``FEATHERLESS_API_KEY``.
  2. **Ollama** (fallback) — a local model at ``OLLAMA_HOST``. Used automatically
     whenever the Featherless call fails (connection / timeout / auth / API
     error / malformed or unparseable response) or no Featherless key is set.

Both providers feed the *same* :func:`build_prompt` output through the *same*
:func:`parse_review` parser, so both yield an identical :class:`OfficerReview`.

It is an *additional* judgment layer — never a replacement for ``risk_manager``,
and it only runs once the hard limits already passed. It **fails safe**: only if
**both** providers fail (or return something unparseable) is the review a VETO
(``approved=False``, ``ok=False``). Every call's full output — prompt, raw
response, provider, and any failure — is logged as an evidence trail.

No network in tests: pass a fake ``featherless_client`` and/or a fake ``session``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

from .risk_manager import AccountState, ProposedOrder

log = logging.getLogger("risk_officer")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# .env lookup mirrors alpaca_trader: the cwd, then the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_CANDIDATES = (
    Path.cwd() / ".env",
    _REPO_ROOT / ".env",
    Path(__file__).resolve().parent / ".env",
)
_env_loaded = False


def _ensure_env_loaded() -> None:
    """Load a local ``.env`` once so ``FEATHERLESS_API_KEY`` etc. are visible."""
    global _env_loaded
    if _env_loaded:
        return
    for path in _ENV_CANDIDATES:
        if path.is_file():
            load_dotenv(path, override=False)
    _env_loaded = True


# --- Featherless AI (primary) --------------------------------------------- #
# Qwen2.5-7B-Instruct: a mid-size, non-gated instruct model on Featherless's
# catalogue (32k context, available on the free tier). Override via env.
DEFAULT_FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
DEFAULT_FEATHERLESS_MODEL = "Qwen/Qwen2.5-7B-Instruct"
FEATHERLESS_TIMEOUT = float(os.environ.get("FEATHERLESS_TIMEOUT", "45"))

# --- Ollama (fallback) --------------------------------------------------- #
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
# 120s (not 60): the first call after the model unloads cold-loads ~2GB from
# disk and can exceed a minute; a too-short timeout turns every cold start into
# a fail-safe VETO. Call warm_up() once at startup to avoid paying it in-loop.
DEFAULT_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
WARM_UP_TIMEOUT = float(os.environ.get("OLLAMA_WARM_UP_TIMEOUT", "180"))

CONTRACT_MULTIPLIER = 100
_MAX_THESIS_CHARS = 800

_VERDICT_RE = re.compile(r"\bVERDICT\s*[:\-]\s*(APPROVE|VETO)\b", re.IGNORECASE)
_THESIS_RE = re.compile(r"\bTHESIS\s*[:\-]\s*(.+)\Z", re.IGNORECASE | re.DOTALL)


@dataclass
class OfficerReview:
    approved: bool
    thesis: str
    model: str
    ok: bool                    # False => fail-safe VETO, not a real judgment
    raw_response: str = ""      # full model output (empty on transport failure)
    error: str | None = None    # exception / parse-failure detail
    provider: str = ""          # "featherless" | "ollama" | "none"

    def describe(self) -> str:
        verdict = "APPROVE" if self.approved else "VETO"
        tag = "" if self.ok else "  (FAIL-SAFE)"
        via = f" via {self.provider}" if self.provider else ""
        return f"risk_officer [{self.model}]{via} {verdict}{tag}\n  thesis: {self.thesis}"


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
def build_prompt(order: ProposedOrder, snapshot: dict, account: AccountState) -> str:
    regime = snapshot.get("iv_regime")
    mode = getattr(regime, "mode", "unknown")
    regime_reason = getattr(regime, "reason", "n/a")

    risk = order.risk_dollars
    equity = account.current_equity
    risk_pct = (risk / equity * 100.0) if equity else float("nan")
    n_positions = len(account.open_positions)
    drawdown = account.starting_equity - account.current_equity
    day_pnl = account.current_equity - account.day_start_equity

    legs = "\n".join(
        f"    {leg.action:<4} {leg.right:<4} x{leg.quantity}  {leg.symbol or '(no symbol)'}"
        for leg in order.legs
    )

    return f"""You are the risk officer for an automated SPY options trading agent.
A proposed iron condor has ALREADY passed the agent's hard risk limits. Your job
is the final judgment call: APPROVE only if the trade looks sound given the
volatility backdrop and current exposure; otherwise VETO.

PROPOSED TRADE (defined-risk iron condor)
{legs}
    wing width      ${order.wing_width:.2f}
    net credit      ${order.net_credit:.2f}
    contracts       {order.quantity}
    max loss        ${risk:,.0f}  ({risk_pct:.2f}% of equity)

VOLATILITY BACKDROP
    SPY price       {snapshot.get("current_price")}
    ATM IV          {snapshot.get("atm_iv")}
    realized vol    {snapshot.get("realized_vol")}   (10-day, annualized)
    IV - RV spread  {snapshot.get("iv_rv_spread")}   (positive = options price more move than realized)
    IV regime       {mode} -> {regime_reason}

CURRENT EXPOSURE
    equity          ${equity:,.0f}
    open positions  {n_positions}
    day P&L         ${day_pnl:,.0f}
    total drawdown  ${drawdown:,.0f} from ${account.starting_equity:,.0f} start

Reason about (a) whether the IV regime and IV-RV spread justify selling premium
here, and (b) whether adding this exposure is prudent right now. Then answer
EXACTLY in this format, nothing else:

VERDICT: APPROVE
THESIS: <2-3 sentences>

or

VERDICT: VETO
THESIS: <2-3 sentences>
"""


# --------------------------------------------------------------------------- #
# Parsing — shared by both providers
# --------------------------------------------------------------------------- #
def parse_review(text: str, model: str) -> OfficerReview:
    """Parse an LLM reply into an :class:`OfficerReview`. Unparseable -> VETO."""
    match = _VERDICT_RE.search(text or "")
    if not match:
        return OfficerReview(
            approved=False,
            thesis="model response had no 'VERDICT: APPROVE|VETO' line; fail-safe VETO",
            model=model,
            ok=False,
            raw_response=text or "",
            error="unparseable: no VERDICT line",
        )

    approved = match.group(1).upper() == "APPROVE"
    thesis_match = _THESIS_RE.search(text)
    thesis = (thesis_match.group(1) if thesis_match else text[match.end():]).strip()
    thesis = re.sub(r"\s+", " ", thesis)[:_MAX_THESIS_CHARS] or "(model gave no thesis)"
    return OfficerReview(approved=approved, thesis=thesis, model=model, ok=True, raw_response=text)


# --------------------------------------------------------------------------- #
# Provider calls
# --------------------------------------------------------------------------- #
def _build_featherless_client(api_key: str, base_url: str):
    """Build an OpenAI-compatible client pointed at Featherless. ``max_retries=0``
    so a transient error fails fast to the Ollama fallback instead of the SDK
    retrying for tens of seconds."""
    from openai import OpenAI

    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=FEATHERLESS_TIMEOUT,
        max_retries=0,
    )


def _call_featherless(client, model: str, prompt: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise ValueError("featherless response had no choices")
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return str(content or "").strip()


def _post_ollama(session, host: str, model: str, prompt: str, timeout: float) -> str:
    resp = session.post(
        f"{host.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("response", "")).strip()


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #
def review_trade(
    order: ProposedOrder,
    snapshot: dict,
    account: AccountState,
    *,
    # Featherless (primary)
    featherless_api_key: str | None = None,
    featherless_model: str | None = None,
    featherless_base_url: str | None = None,
    featherless_client=None,
    # Ollama (fallback)
    host: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    session=None,
) -> OfficerReview:
    """Ask an LLM for a second opinion on an already-risk-approved order.

    Featherless AI is tried first; on ANY failure (or an unparseable reply) the
    local Ollama model is tried next. Only if **both** fail is the result a
    fail-safe VETO (``ok=False``). Call this only AFTER
    ``risk_manager.check_order(order, account).approved`` is True.

    Test seams: ``featherless_client`` (an object exposing
    ``.chat.completions.create``) and ``session`` (a ``requests``-like session).
    """
    _ensure_env_loaded()

    fl_key = (
        featherless_api_key
        if featherless_api_key is not None
        else os.environ.get("FEATHERLESS_API_KEY", "")
    )
    fl_model = featherless_model or os.environ.get(
        "FEATHERLESS_MODEL", DEFAULT_FEATHERLESS_MODEL
    )
    fl_base = featherless_base_url or os.environ.get(
        "FEATHERLESS_BASE_URL", DEFAULT_FEATHERLESS_BASE_URL
    )
    ol_host = host or DEFAULT_HOST
    ol_model = model or DEFAULT_MODEL
    ol_timeout = DEFAULT_TIMEOUT if timeout is None else timeout

    prompt = build_prompt(order, snapshot, account)
    log.info(
        "risk_officer review -> featherless(%s) primary / ollama(%s) fallback\n"
        "--- prompt ---\n%s",
        fl_model, ol_model, prompt,
    )

    last_raw = ""

    # ---- primary: Featherless ------------------------------------------- #
    if featherless_client is not None or fl_key:
        try:
            client = featherless_client or _build_featherless_client(fl_key, fl_base)
            text = _call_featherless(client, fl_model, prompt)
            last_raw = text or last_raw
            review = parse_review(text, fl_model)
            review.provider = "featherless"
            if review.ok:
                log.info(
                    "risk_officer [featherless:%s] verdict=%s\n"
                    "--- raw response ---\n%s\n--- parsed thesis ---\n%s",
                    fl_model, "APPROVE" if review.approved else "VETO",
                    text, review.thesis,
                )
                return review
            fl_error = review.error or "unparseable"
            log.warning(
                "risk_officer featherless UNPARSEABLE -> falling back to ollama\n"
                "--- raw response ---\n%s",
                text,
            )
        except Exception as exc:  # noqa: BLE001 - ANY failure -> fall back
            fl_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "risk_officer featherless FAILED (%s) -> falling back to ollama",
                fl_error,
            )
    else:
        fl_error = "no FEATHERLESS_API_KEY configured"
        log.info("risk_officer: %s -> using ollama", fl_error)

    # ---- fallback: Ollama --------------------------------------------- #
    try:
        sess = session if session is not None else requests.Session()
        text = _post_ollama(sess, ol_host, ol_model, prompt, ol_timeout)
        last_raw = text or last_raw
        review = parse_review(text, ol_model)
        review.provider = "ollama"
        if review.ok:
            log.info(
                "risk_officer [ollama:%s] verdict=%s  (featherless fallback: %s)\n"
                "--- raw response ---\n%s\n--- parsed thesis ---\n%s",
                ol_model, "APPROVE" if review.approved else "VETO", fl_error,
                text, review.thesis,
            )
            return review
        ol_error = review.error or "unparseable"
        log.warning(
            "risk_officer ollama UNPARSEABLE\n--- raw response ---\n%s", text
        )
    except Exception as exc:  # noqa: BLE001 - ANY failure -> fail-safe VETO
        ol_error = f"{type(exc).__name__}: {exc}"
        log.warning("risk_officer ollama FAILED (%s)", ol_error)

    # ---- both providers failed -> fail-safe VETO --------------------- #
    review = OfficerReview(
        approved=False,
        thesis=(
            f"no reasoning provider succeeded (featherless: {fl_error}; "
            f"ollama: {ol_error}); fail-safe VETO"
        ),
        model=f"{fl_model} / {ol_model}",
        ok=False,
        raw_response=last_raw,
        error=f"featherless: {fl_error} | ollama: {ol_error}",
        provider="none",
    )
    log.error("risk_officer NO PROVIDER SUCCEEDED -> VETO  %s", review.error)
    return review


def warm_up(
    *,
    host: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    session=None,
) -> bool:
    """Fire a throwaway one-token generation so **Ollama** loads the model into
    memory before the first real :func:`review_trade` fallback.

    Only the Ollama fallback needs this — Featherless is a hosted API with no
    cold-load. Call this once at agent startup, before the trading loop. It never
    raises — if Ollama is down at startup that is not fatal here; the in-loop
    review still fails safe to VETO. Returns True if the model responded.
    """
    host = host or DEFAULT_HOST
    model = model or DEFAULT_MODEL
    timeout = WARM_UP_TIMEOUT if timeout is None else timeout
    sess = session if session is not None else requests.Session()

    log.info("risk_officer warm-up -> %s  model=%s  (timeout=%.0fs)", host, model, timeout)
    try:
        resp = sess.post(
            f"{host.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": "ready",
                "stream": False,
                "options": {"num_predict": 1},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        resp.json()
    except Exception as exc:  # noqa: BLE001 - startup warm-up never fatal
        log.warning(
            "risk_officer warm-up FAILED: %s: %s  (first live review may be slow "
            "or fail-safe VETO)",
            type(exc).__name__,
            exc,
        )
        return False

    log.info("risk_officer warm-up OK: model=%s resident", model)
    return True


# --------------------------------------------------------------------------- #
# Demo — real call: Featherless first, Ollama fallback, VETO only if both fail.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from types import SimpleNamespace

    from .risk_manager import OrderLeg

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    demo_order = ProposedOrder(
        wing_width=4.0,
        net_credit=1.2,
        quantity=3,
        legs=(
            OrderLeg("sell", "put", 3, "SPY260901P00762000"),
            OrderLeg("buy", "put", 3, "SPY260901P00758000"),
            OrderLeg("sell", "call", 3, "SPY260901C00770000"),
            OrderLeg("buy", "call", 3, "SPY260901C00772000"),
        ),
    )
    demo_snapshot = {
        "current_price": 766.0,
        "atm_iv": 0.22,
        "realized_vol": 0.14,
        "iv_rv_spread": 0.08,
        "iv_regime": SimpleNamespace(
            mode="hackathon_static", trade_eligible=True,
            reason="ATM IV 0.220 > 0.15", atm_iv=0.22, iv_percentile=None,
        ),
    }
    demo_account = AccountState(100_000.0, 99_000.0, 99_400.0, open_positions=())

    warm_up()  # main.py does this once before the loop (Ollama fallback only)
    print(review_trade(demo_order, demo_snapshot, demo_account).describe())
