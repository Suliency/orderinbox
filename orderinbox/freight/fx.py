"""Minimal deterministic FX conversion for the freight domain.

The model never does money math (proposal: "deterministic software handles
money"). This is a small, clearly-labelled static rate table used to compare
multi-currency quotes on a common basis. A production deployment replaces the
table with a live FX source; the interface is what matters.
"""
from __future__ import annotations

# Rates expressed as units of currency per 1 USD. This is an illustrative,
# stable table — good enough to compare quotes, not to settle invoices.
_PER_USD: dict[str, float] = {
    "USD": 1.0,
    "CAD": 1.36,
    "EUR": 0.92,
    "GBP": 0.79,
    "JPY": 155.0,
    "CNY": 7.25,
    "SGD": 1.35,
    "AUD": 1.52,
}


def supported() -> list[str]:
    return sorted(_PER_USD)


def rate(currency: str, base: str = "USD") -> float:
    """Units of `currency` per 1 `base`. Raises when either is unknown."""
    c, b = (currency or "").upper(), (base or "USD").upper()
    if c not in _PER_USD or b not in _PER_USD:
        raise ValueError(f"unsupported currency: {c} / {b}")
    # currency-per-USD ratio
    return _PER_USD[c] / _PER_USD[b]


def convert(amount: float, from_currency: str, to_currency: str) -> float:
    f, t = (from_currency or "USD").upper(), (to_currency or "USD").upper()
    if f == t:
        return round(float(amount), 2)
    return round(float(amount) * rate(t, f), 2)
