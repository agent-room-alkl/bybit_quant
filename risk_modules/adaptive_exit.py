# -*- coding: utf-8 -*-
"""
AdaptiveExitManager — shadow-mode diagnostics for BULL-regime exits.

Computes a Chandelier-style ATR trailing stop and R-multiple stop ratcheting,
returning what the strategy *would* do if it followed adaptive trailing rules
in BULL — without changing any live trade behavior.

Used by SmartStrategy.classify_shadow(): the returned diagnostics are written
into signals.json so we can later compare shadow_exit_now vs the actual fills
to validate whether adaptive trailing would have held big runs.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any


# ── Tuning constants ─────────────────────────────────────────────
# Kept module-level so they're discoverable in one place and easy to sweep
# during walk-forward backtests.

CHANDELIER_ATR_MULT = 3.0       # trailing = high_since_entry - N * ATR_abs
RATCHET_TO_BREAKEVEN_R = 1.5    # at +1.5R, move stop to entry
RATCHET_TO_1R_R = 3.0           # at +3R, move stop to entry + 1R
INITIAL_RISK_R_FALLBACK = 0.02  # if entry/initial_stop missing, assume 2% risk


@dataclass
class ExitDiagnostics:
    """Shadow-only output. Never used to actually place orders."""
    trailing_stop_price: float       # Chandelier trailing line
    effective_stop_price: float      # max(trailing, ratcheted floor)
    r_multiple: float                # (price - entry) / initial_risk_per_unit
    ratchet_state: str               # initial | breakeven | locked_1r
    shadow_exit_now: bool            # True if price <= effective_stop in BULL
    reason: str                      # human-readable diagnostic

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["trailing_stop_price"] = round(self.trailing_stop_price, 4)
        d["effective_stop_price"] = round(self.effective_stop_price, 4)
        d["r_multiple"] = round(self.r_multiple, 3)
        return d


class AdaptiveExitManager:
    """Stateless evaluator. Caller passes in tracked entry/high context."""

    def __init__(
        self,
        chandelier_mult: float = CHANDELIER_ATR_MULT,
        ratchet_breakeven_r: float = RATCHET_TO_BREAKEVEN_R,
        ratchet_1r_r: float = RATCHET_TO_1R_R,
    ):
        self.chandelier_mult = chandelier_mult
        self.ratchet_breakeven_r = ratchet_breakeven_r
        self.ratchet_1r_r = ratchet_1r_r

    def evaluate(
        self,
        *,
        last_price: float,
        entry_price: float,
        high_since_entry: float,
        atr_pct: float,
        initial_stop_price: Optional[float] = None,
        is_bull_regime: bool = False,
        risk_level: str = "L1_NOISE",
    ) -> ExitDiagnostics:
        """
        Returns the trailing stop, ratchet state, and whether we *would* exit.

        Args:
            last_price: current mark price
            entry_price: position entry (avg cost) price
            high_since_entry: peak price since entry (caller tracks this)
            atr_pct: ATR as percentage of price (e.g. 1.5 means 1.5%)
            initial_stop_price: original stop set at entry; if None, we infer
                from INITIAL_RISK_R_FALLBACK
            is_bull_regime: only BULL gets adaptive trailing diagnostics;
                other regimes return shadow_exit_now=False with reason
            risk_level: L1-L4 from RiskClassifier. L3/L4 forces shadow_exit_now=True
                regardless of trailing — the "true-risk override".

        All inputs are read-only. No side effects.
        """
        # ── Defensive defaults ──
        if last_price <= 0 or entry_price <= 0 or atr_pct <= 0:
            return ExitDiagnostics(
                trailing_stop_price=0.0,
                effective_stop_price=0.0,
                r_multiple=0.0,
                ratchet_state="initial",
                shadow_exit_now=False,
                reason="missing_inputs",
            )

        high = high_since_entry if high_since_entry > 0 else last_price
        atr_abs = entry_price * atr_pct / 100.0

        # ── 1. Chandelier trailing stop ──
        trailing_stop = high - self.chandelier_mult * atr_abs

        # ── 2. R-multiple computation ──
        if initial_stop_price is not None and initial_stop_price > 0:
            initial_risk_per_unit = entry_price - initial_stop_price
        else:
            initial_risk_per_unit = entry_price * INITIAL_RISK_R_FALLBACK

        if initial_risk_per_unit <= 0:
            # Degenerate (e.g., short or zero risk) — fall back to ATR
            initial_risk_per_unit = atr_abs

        r_multiple = (last_price - entry_price) / initial_risk_per_unit

        # ── 3. Stop ratchet ──
        ratchet_floor = 0.0
        ratchet_state = "initial"
        if r_multiple >= self.ratchet_1r_r:
            ratchet_floor = entry_price + initial_risk_per_unit
            ratchet_state = "locked_1r"
        elif r_multiple >= self.ratchet_breakeven_r:
            ratchet_floor = entry_price
            ratchet_state = "breakeven"

        effective_stop = max(trailing_stop, ratchet_floor)

        # ── 4. Shadow exit decision ──
        # In BULL: would exit iff price breached the effective stop OR true risk override.
        # Outside BULL: we don't recommend adaptive trailing — let original logic run.
        true_risk = risk_level in ("L3_TRUE_RISK", "L4_BLACK_SWAN")

        if true_risk:
            shadow_exit_now = True
            reason = f"true_risk_override({risk_level})"
        elif not is_bull_regime:
            shadow_exit_now = False
            reason = "non_bull_skip"
        elif last_price <= effective_stop:
            if ratchet_state == "locked_1r":
                reason = "trailing_locked_1r_hit"
            elif ratchet_state == "breakeven":
                reason = "trailing_breakeven_hit"
            else:
                reason = "chandelier_hit"
            shadow_exit_now = True
        else:
            shadow_exit_now = False
            reason = f"riding(R={r_multiple:.2f},state={ratchet_state})"

        return ExitDiagnostics(
            trailing_stop_price=trailing_stop,
            effective_stop_price=effective_stop,
            r_multiple=r_multiple,
            ratchet_state=ratchet_state,
            shadow_exit_now=shadow_exit_now,
            reason=reason,
        )
