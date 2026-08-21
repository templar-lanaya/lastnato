"""Shared policy-rule engine.

Single source of truth for evaluate_policy_rule's logic. Both
investor_tools_v2.py (the LangChain @tool version, in-process) and
mcp_server.py (the MCP-served version, cross-process/cross-framework) call
INTO this module rather than each keeping their own copy -- so the two
never quietly drift apart as rules get added or fixed.

Store is defined here too, for the same reason: both callers need the
same data-loading logic. Each process still gets its OWN Store instance
(this module doesn't hold a singleton) -- an MCP server and a LangChain
agent running in separate processes can't share one in-memory object
anyway, so there's no reason to pretend they do.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data"


class Store:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        def _load(name: str) -> list[dict]:
            path = data_dir / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

        self.parties = _load("parties.json")
        self.accounts = _load("accounts.json")
        self.transactions = _load("transactions.json")

        self.accounts_by_id = {a["account_id"]: a for a in self.accounts}
        self.parties_by_id = {p["party_id"]: p for p in self.parties}

    def transactions_for(self, account_id: str) -> list[dict]:
        return [t for t in self.transactions if t["account_id"] == account_id]


RMD_AGE_BANDS = [
    {"born_through": 1950, "rmd_age": None},
    {"born_min": 1951, "born_max": 1959, "rmd_age": 73},
    {"born_min": 1960, "born_max": None, "rmd_age": 75},
]

MARGIN_INTEREST_TIERS = [
    (0.00, 24_999.99, 9.50),
    (25_000.00, 49_999.99, 9.00),
    (50_000.00, 99_999.99, 8.25),
    (100_000.00, 249_999.99, 7.50),
    (250_000.00, 499_999.99, 6.50),
    (500_000.00, 999_999.99, 6.00),
    (1_000_000.00, None, 5.50),
]


def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def evaluate_policy_rule(
    store: Store,
    rule_name: str,
    account_id: Optional[str] = None,
    amount: Optional[float] = None,
    transaction_id: Optional[str] = None,
) -> dict:
    """Deterministic dispatch -- plain Python, never an LLM call. Every
    branch returns {rule_id, decision, source_doc, inputs_used} (or a
    plain {"error": "..."} for a bad call), the shape both the LangChain
    tool and the MCP tool hand back unchanged.
    """
    account = store.accounts_by_id.get(account_id) if account_id else None
    if account_id and account is None:
        return {"error": f"No such account: {account_id}"}
    party = store.parties_by_id.get(account["party_id"]) if account else None

    if rule_name == "fee_waiver":
        if party is None:
            return {"error": "fee_waiver requires account_id"}
        party_accounts = [a for a in store.accounts if a["party_id"] == party["party_id"]]
        household_total = sum(a["cash_balance"] for a in party_accounts)
        waived = household_total >= 5_000.00 or party["e_delivery_election"]
        return {"rule_id": "fee_waiver", "decision": "waived" if waived else "fee_applies",
                "source_doc": "VG-OP-017 SS3",
                "inputs_used": {"household_total_balance": household_total, "e_delivery_election": party["e_delivery_election"]}}

    if rule_name == "wire_callback_required":
        if amount is None:
            return {"error": "wire_callback_required requires amount"}
        return {"rule_id": "wire_callback_required",
                "decision": "callback_required" if amount >= 50_000.00 else "callback_not_required",
                "source_doc": "VG-OP-005 SS3.4", "inputs_used": {"amount": amount}}

    if rule_name == "ach_outgoing_limit":
        if amount is None:
            return {"error": "ach_outgoing_limit requires amount"}
        # Single-amount check only -- see mobile_check_deposit_limit's
        # comment below for why this doesn't aggregate stored history.
        return {"rule_id": "ach_outgoing_limit",
                "decision": "exceeds_limit" if amount > 100_000.00 else "within_limit",
                "source_doc": "VG-OP-005 SS2.3", "inputs_used": {"amount": amount}}

    if rule_name == "ach_incoming_limit":
        if amount is None:
            return {"error": "ach_incoming_limit requires amount"}
        return {"rule_id": "ach_incoming_limit",
                "decision": "exceeds_limit" if amount > 250_000.00 else "within_limit",
                "source_doc": "VG-OP-005 SS2.3", "inputs_used": {"amount": amount}}

    if rule_name == "rmd_start_age":
        if party is None:
            return {"error": "rmd_start_age requires account_id"}
        dob = party.get("date_of_birth")
        if dob is None:
            return {"rule_id": "rmd_start_age", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS3",
                    "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"}}
        birth_year = date.fromisoformat(dob).year
        for band in RMD_AGE_BANDS:
            if "born_through" in band and birth_year <= band["born_through"]:
                return {"rule_id": "rmd_start_age", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS3",
                        "inputs_used": {"birth_year": birth_year, "reason": "prior-law band has no single seedable value"}}
            if "born_min" in band and birth_year >= band["born_min"] and (band["born_max"] is None or birth_year <= band["born_max"]):
                return {"rule_id": "rmd_start_age", "decision": {"rmd_start_age": band["rmd_age"]},
                        "source_doc": "VG-OP-003 SS3", "inputs_used": {"birth_year": birth_year}}
        return {"error": f"birth_year {birth_year} matched no RMD band"}

    if rule_name == "margin_interest_rate":
        debit_balance = account["debit_balance"] if account else None
        if debit_balance is None:
            return {"error": "margin_interest_rate requires account_id"}
        for floor, ceiling, rate in MARGIN_INTEREST_TIERS:
            if debit_balance >= floor and (ceiling is None or debit_balance <= ceiling):
                return {"rule_id": "margin_interest_rate", "decision": {"annual_rate_pct": rate},
                        "source_doc": "VG-OP-009 SS4", "inputs_used": {"debit_balance": debit_balance}}
        return {"error": f"debit_balance {debit_balance} matched no margin tier"}

    if rule_name == "mobile_check_deposit_limit":
        if account is None or amount is None:
            return {"error": "mobile_check_deposit_limit requires account_id and amount"}
        today = date.today()
        month_ago = today - timedelta(days=30)
        # Safe to aggregate here (unlike ACH above): check_deposit is
        # unambiguously an incoming transaction, no direction field needed.
        recent = [t for t in store.transactions_for(account_id)
                  if t["type"] == "check_deposit" and date.fromisoformat(t["date"]) >= month_ago]
        monthly_total = sum(t["amount"] for t in recent) + amount
        daily_total = sum(t["amount"] for t in recent if t["date"] == today.isoformat()) + amount
        violations = []
        if amount > 100_000.00:
            violations.append("exceeds_per_check_limit")
        if daily_total > 100_000.00:
            violations.append("exceeds_daily_limit")
        if monthly_total > 250_000.00:
            violations.append("exceeds_monthly_limit")
        return {"rule_id": "mobile_check_deposit_limit", "decision": "rejected" if violations else "accepted",
                "source_doc": "VG-OP-016", "inputs_used": {"amount": amount, "daily_total": daily_total,
                                                            "monthly_total": monthly_total, "violations": violations}}

    if rule_name == "qcd_limit":
        if party is None or amount is None:
            return {"error": "qcd_limit requires account_id and amount"}
        dob = party.get("date_of_birth")
        if dob is None:
            return {"rule_id": "qcd_limit", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS7",
                    "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"}}
        age_years = (date.today() - date.fromisoformat(dob)).days / 365.25
        eligible = age_years >= 70.5 and amount <= 108_000.00
        return {"rule_id": "qcd_limit", "decision": "eligible" if eligible else "not_eligible",
                "source_doc": "VG-OP-003 SS7", "inputs_used": {"age_years": round(age_years, 1), "amount": amount}}

    if rule_name == "temporary_hold_status":
        if account is None or transaction_id is None:
            return {"error": "temporary_hold_status requires account_id and transaction_id"}
        txn = next((t for t in store.transactions_for(account_id) if t["transaction_id"] == transaction_id), None)
        if txn is None or not txn.get("hold_start_date"):
            return {"rule_id": "temporary_hold_status", "decision": "not_on_hold", "source_doc": "VG-OP-013 SS2.5", "inputs_used": {}}
        elapsed = _business_days_between(date.fromisoformat(txn["hold_start_date"]), date.today())
        decision = "within_initial_hold" if elapsed <= 15 else "within_extension_window" if elapsed <= 40 else "must_release_or_escalate"
        return {"rule_id": "temporary_hold_status", "decision": decision, "source_doc": "VG-OP-013 SS2.5",
                "inputs_used": {"hold_start_date": txn["hold_start_date"], "business_days_elapsed": elapsed}}

    return {"error": f"Unknown rule_name: {rule_name}"}
