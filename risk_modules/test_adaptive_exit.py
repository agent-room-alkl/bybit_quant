# -*- coding: utf-8 -*-
"""
Unit tests for AdaptiveExitManager. Run with:
    python3 -m pytest risk_modules/test_adaptive_exit.py -v
or as a standalone script:
    python3 risk_modules/test_adaptive_exit.py
"""
from __future__ import annotations
import sys
import os
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_modules.adaptive_exit import AdaptiveExitManager, ExitDiagnostics


def _mk():
    return AdaptiveExitManager()


# ── Degenerate inputs ─────────────────────────────────────────────

def test_missing_price_returns_no_exit():
    d = _mk().evaluate(
        last_price=0, entry_price=100, high_since_entry=100, atr_pct=1.5,
        is_bull_regime=True,
    )
    assert d.shadow_exit_now is False
    assert d.reason == "missing_inputs"


def test_zero_atr_returns_no_exit():
    d = _mk().evaluate(
        last_price=110, entry_price=100, high_since_entry=110, atr_pct=0,
        is_bull_regime=True,
    )
    assert d.shadow_exit_now is False
    assert d.reason == "missing_inputs"


# ── Chandelier trailing ───────────────────────────────────────────

def test_chandelier_riding_when_above_stop():
    # entry 100, high 110, ATR 1.5% → atr_abs = 1.5
    # trailing = 110 - 3*1.5 = 105.5. Price 108 → riding.
    d = _mk().evaluate(
        last_price=108, entry_price=100, high_since_entry=110, atr_pct=1.5,
        is_bull_regime=True,
    )
    assert d.shadow_exit_now is False
    assert math.isclose(d.trailing_stop_price, 105.5, rel_tol=1e-6)
    assert d.reason.startswith("riding")


def test_chandelier_exit_when_below_stop():
    # trailing = 110 - 3*1.5 = 105.5. Price 105 → hits.
    # R = (105-100)/2 = 2.5 → reaches breakeven ratchet (1.5R), floor=entry=100,
    # so effective_stop = max(105.5, 100) = 105.5 → hit
    d = _mk().evaluate(
        last_price=105, entry_price=100, high_since_entry=110, atr_pct=1.5,
        is_bull_regime=True,
    )
    assert d.shadow_exit_now is True
    # ratchet moved to breakeven, but trailing is the tighter binding constraint
    assert d.reason in ("trailing_breakeven_hit", "chandelier_hit")


# ── R-multiple ratchet ────────────────────────────────────────────

def test_ratchet_initial_below_1_5r():
    # initial_stop=98 → risk=2. price=101 → R=0.5
    d = _mk().evaluate(
        last_price=101, entry_price=100, high_since_entry=101, atr_pct=1.0,
        initial_stop_price=98, is_bull_regime=True,
    )
    assert d.ratchet_state == "initial"
    assert math.isclose(d.r_multiple, 0.5, rel_tol=1e-6)


def test_ratchet_breakeven_at_1_5r():
    # risk=2, price=103 → R=1.5
    d = _mk().evaluate(
        last_price=103, entry_price=100, high_since_entry=103, atr_pct=1.0,
        initial_stop_price=98, is_bull_regime=True,
    )
    assert d.ratchet_state == "breakeven"
    # effective_stop >= entry (100) — but trailing might be tighter
    # high=103, atr_abs=1, trailing=103-3=100, so effective=max(100,100)=100
    assert math.isclose(d.effective_stop_price, 100.0, rel_tol=1e-6)


def test_ratchet_locked_1r_at_3r():
    # risk=2, price=106 → R=3.0 → locked_1r, floor = entry+risk = 102
    d = _mk().evaluate(
        last_price=106, entry_price=100, high_since_entry=106, atr_pct=1.0,
        initial_stop_price=98, is_bull_regime=True,
    )
    assert d.ratchet_state == "locked_1r"
    # trailing=106-3=103. floor=102. effective=max(103,102)=103.
    assert math.isclose(d.effective_stop_price, 103.0, rel_tol=1e-6)


def test_ratchet_locked_1r_protects_when_trailing_lower():
    # If high was high but ATR huge → trailing very low. Ratchet floor saves us.
    # entry=100, initial_stop=98 (risk=2), high=110, price=106, atr_pct=4
    # → atr_abs=4, trailing=110-12=98. R=(106-100)/2=3.0 → locked_1r, floor=102.
    # effective = max(98, 102) = 102. Price 106 > 102 → riding.
    d = _mk().evaluate(
        last_price=106, entry_price=100, high_since_entry=110, atr_pct=4.0,
        initial_stop_price=98, is_bull_regime=True,
    )
    assert d.ratchet_state == "locked_1r"
    assert math.isclose(d.effective_stop_price, 102.0, rel_tol=1e-6)
    assert d.shadow_exit_now is False


# ── True-risk override ────────────────────────────────────────────

def test_true_risk_l3_forces_exit_even_when_riding():
    d = _mk().evaluate(
        last_price=108, entry_price=100, high_since_entry=110, atr_pct=1.5,
        is_bull_regime=True, risk_level="L3_TRUE_RISK",
    )
    assert d.shadow_exit_now is True
    assert "true_risk_override" in d.reason


def test_true_risk_l4_forces_exit_outside_bull():
    d = _mk().evaluate(
        last_price=108, entry_price=100, high_since_entry=110, atr_pct=1.5,
        is_bull_regime=False, risk_level="L4_BLACK_SWAN",
    )
    assert d.shadow_exit_now is True
    assert "L4_BLACK_SWAN" in d.reason


# ── Non-BULL skip ─────────────────────────────────────────────────

def test_sideways_regime_does_not_recommend_exit():
    d = _mk().evaluate(
        last_price=95, entry_price=100, high_since_entry=110, atr_pct=1.5,
        is_bull_regime=False, risk_level="L1_NOISE",
    )
    assert d.shadow_exit_now is False
    assert d.reason == "non_bull_skip"


# ── R-multiple fallback when no initial_stop ──────────────────────

def test_r_multiple_fallback_no_initial_stop():
    # No initial_stop → fallback risk = 100 * 0.02 = 2. price=104 → R=2.0
    d = _mk().evaluate(
        last_price=104, entry_price=100, high_since_entry=104, atr_pct=1.0,
        initial_stop_price=None, is_bull_regime=True,
    )
    assert math.isclose(d.r_multiple, 2.0, rel_tol=1e-6)
    assert d.ratchet_state == "breakeven"


# ── as_dict serialization ─────────────────────────────────────────

def test_as_dict_roundtrip():
    d = _mk().evaluate(
        last_price=104, entry_price=100, high_since_entry=104, atr_pct=1.0,
        initial_stop_price=98, is_bull_regime=True,
    )
    out = d.as_dict()
    assert isinstance(out["r_multiple"], float)
    assert "shadow_exit_now" in out
    assert "reason" in out


if __name__ == "__main__":
    # Standalone runner so the test file works without pytest installed.
    import inspect
    tests = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name} — {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name} — unexpected {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
