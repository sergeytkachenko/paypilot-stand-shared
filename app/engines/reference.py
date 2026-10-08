""
from app.engines import policy

TABLES = ("FX_SPREAD_PCT", "FX_FREE_MONTHLY_ALLOWANCE_EUR",
          "RATES_TO_EUR", "TRANSFER_FEES", "DAILY_LIMIT_EUR",
          "MONTHLY_LIMIT_EUR", "DISPUTE_WINDOWS_DAYS")


def transfer_fees() -> dict:
    return {rail: {"flat_fee_eur": flat, "percent_fee": pct}
            for rail, (flat, pct) in policy.TRANSFER_FEES.items()}


def tables() -> dict:
    out = {name: getattr(policy, name) for name in TABLES}
    out["TRANSFER_FEES"] = transfer_fees()
    out["source"] = "app/engines/policy.py"
    return out
