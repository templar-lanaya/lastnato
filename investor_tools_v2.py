"""Investor Services Copilot -- tool surface, v2.

Rebuilt to match the team's process_refund/@tool template: one
@tool-decorated function per capability, Annotated[...] parameters,
docstring-as-description, no dispatch() layer.

CHANGES FROM v1 (investor_tools.py), per direction:
  - CallerContext (SessionContext/bind_session/current_session/
    EntitlementDenied) is REMOVED. Every tool takes account_id as a
    plain, model-supplied argument; the agent is expected to have found
    that account_id itself by reading the JSON data (e.g. via
    get_account_summary), the same way the template's process_refund
    trusts whatever order_number it's handed.
  - Nothing in this file checks whether the caller is entitled to the
    account_id it's given. This is a deliberate, direction-confirmed
    scope decision, not an oversight -- flagged here so it reads that
    way to anyone auditing this file later, the same way the fast-path
    guardrail bypass was flagged as an accepted risk earlier in this
    project rather than left silent.
  - propose_service_case / propose_advisor_callback / propose_correspondence
    are renamed to file_service_case / schedule_advisor_callback /
    send_correspondence and now EXECUTE IMMEDIATELY, returning a plain
    confirmation string -- matching process_refund's
    "Refund processed successfully for order {order_number}" style.
    There is no pending-approval state and no human-in-the-loop gate.
  - Store still logs every action taken (see _log_action) purely for an
    audit trail -- this is NOT an approval queue, nothing reads
    "pending"; it's a record of what already happened, written after
    the fact.

search_knowledge_base's retriever is still a pluggable dependency (a
teammate's chunking/search implementation) -- that part of the design
didn't change.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

from langchain_core.tools import tool

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ==========================================================================
# Data access
# ==========================================================================

class Store:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        def _load(name: str) -> list[dict]:
            path = data_dir / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

        self.parties = _load("parties.json")
        self.products = _load("products.json")
        self.accounts = _load("accounts.json")
        self.positions = _load("positions.json")
        self.transactions = _load("transactions.json")
        self.beneficiaries = _load("beneficiaries.json")

        self.accounts_by_id = {a["account_id"]: a for a in self.accounts}
        self.parties_by_id = {p["party_id"]: p for p in self.parties}
        self.products_by_ticker = {p["ticker"]: p for p in self.products}
        self.action_log: list[dict] = []  # audit record ONLY -- not an approval queue

    def positions_for(self, account_id: str) -> list[dict]:
        return [p for p in self.positions if p["account_id"] == account_id]

    def transactions_for(self, account_id: str) -> list[dict]:
        return [t for t in self.transactions if t["account_id"] == account_id]

    def beneficiaries_for(self, account_id: str) -> list[dict]:
        return [b for b in self.beneficiaries if b["account_id"] == account_id]


RetrieverFn = Callable[[str, int], list[dict[str, Any]]]

_store = Store()
_retriever: Optional[RetrieverFn] = None  # set by whoever wires in the real KB search


def configure(retriever: Optional[RetrieverFn] = None, data_dir: Optional[Path] = None) -> None:
    """Optional wiring point -- call once at startup if you need a real
    retriever or a non-default data directory. Everything works without
    calling this at all (retriever just stays unset; see
    search_knowledge_base's own not-configured message).
    """
    global _store, _retriever
    if data_dir is not None:
        _store = Store(data_dir)
    if retriever is not None:
        _retriever = retriever


def _get_account(account_id: str) -> dict:
    account = _store.accounts_by_id.get(account_id)
    if account is None:
        raise ValueError(f"No such account: {account_id}")
    return account


def _log_action(action_type: str, account_id: str, payload: dict) -> dict:
    entry = {
        "account_id": account_id,
        "action_type": action_type,
        "payload": payload,
        "executed_at": datetime.utcnow().isoformat(),
    }
    _store.action_log.append(entry)
    return entry


# ==========================================================================
# Rules-engine constants (VG-OP-* sourced)
# ==========================================================================

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

CORRESPONDENCE_TEMPLATES: dict[str, dict] = {
    "beneficiary_update_confirmation": {
        "required_fields": ["party_name", "account_id", "beneficiary_summary"],
        "body": (
            "Dear {party_name},\n\nThis confirms your beneficiary designation on "
            "account {account_id} has been updated as follows: {beneficiary_summary}\n\n"
            "If you did not request this change, contact us immediately at 800-555-0199."
        ),
        "required_disclosure": (
            "Beneficiary designations govern the disposition of retirement and "
            "TOD-eligible accounts independent of your will. See VG-OP-002 for details."
        ),
    },
    "wire_callback_confirmation_letter": {
        "required_fields": ["party_name", "account_id", "wire_amount", "recipient_name"],
        "body": (
            "Dear {party_name},\n\nThis confirms the outgoing wire of {wire_amount} from "
            "account {account_id} to {recipient_name} following verbal callback confirmation."
        ),
        "required_disclosure": (
            "Wires are generally final once sent. Contact us immediately at "
            "800-555-0166 if you did not authorize this transfer."
        ),
    },
    "rmd_distribution_notice": {
        "required_fields": ["party_name", "account_id", "rmd_amount", "tax_year"],
        "body": (
            "Dear {party_name},\n\nYour Required Minimum Distribution of {rmd_amount} for "
            "tax year {tax_year} has been processed from account {account_id}."
        ),
        "required_disclosure": (
            "This distribution is taxable as ordinary income. Vanterra does not "
            "provide tax advice; consult a tax professional. See VG-OP-003."
        ),
    },
}

_RETRIEVAL_THRESHOLD = 0.34  # placeholder pending real calibration


def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ==========================================================================
# Tools
# ==========================================================================

@tool
def search_knowledge_base(
    query: Annotated[str, "Search query for Vanterra product/policy documents"],
    top_k: Annotated[int, "Max number of passages to return"] = 3,
) -> dict:
    """Search Vanterra product and policy documents. Returns cited passages,
    or a refusal if nothing meets the confidence threshold -- never falls
    back to unsourced knowledge.
    """
    if _retriever is None:
        return {"error": "search_knowledge_base is not configured -- call configure(retriever=...) first."}
    candidates = _retriever(query, top_k)
    results = [c for c in candidates if c.get("score", 0) >= _RETRIEVAL_THRESHOLD][:top_k]
    if not results:
        return {"error": f"No corpus passage met the confidence threshold ({_RETRIEVAL_THRESHOLD})."}
    return {"results": results}


@tool
def get_account_summary(
    account_id: Annotated[str, "Account to summarize, e.g. ACC-00001"],
) -> dict:
    """Get balance and status summary for one account."""
    try:
        account = _get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    return {k: account[k] for k in (
        "account_id", "party_id", "account_type", "status", "opened_date",
        "cash_balance", "margin_enabled", "debit_balance", "assigned_rep_id",
    )}


@tool
def get_positions(
    account_id: Annotated[str, "Account to get holdings for"],
) -> dict:
    """Get fund holdings for one account."""
    try:
        _get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    rows = []
    for pos in _store.positions_for(account_id):
        product = _store.products_by_ticker.get(pos["ticker"])
        rows.append({
            "ticker": pos["ticker"],
            "fund_name": product["fund_name"] if product else None,
            "share_class": product["share_class"] if product else None,
            "quantity": pos["quantity"],
            "price": pos["price"],
            "market_value": round(pos["quantity"] * pos["price"], 2),
        })
    return {"account_id": account_id, "positions": rows}


@tool
def get_recent_transactions(
    account_id: Annotated[str, "Account to get transactions for"],
    limit: Annotated[int, "Max number of transactions to return"] = 20,
) -> dict:
    """Get the most recent transactions for one account, newest first."""
    try:
        _get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    txns = sorted(_store.transactions_for(account_id), key=lambda t: t["date"], reverse=True)
    return {"account_id": account_id, "transactions": txns[:limit]}


@tool
def get_account_registrations(
    account_id: Annotated[str, "Account to get registration/beneficiary info for"],
) -> dict:
    """Get account registration (titling) info and beneficiary designations."""
    try:
        account = _get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    beneficiaries = _store.beneficiaries_for(account_id)
    is_ira = account["account_type"] in ("traditional_ira", "roth_ira")
    return {
        "account_id": account_id,
        "account_type": account["account_type"],
        "beneficiaries": beneficiaries,
        "beneficiary_required_but_missing": is_ira and not beneficiaries,
    }


@tool
def evaluate_policy_rule(
    rule_name: Annotated[str, "One of: fee_waiver, wire_callback_required, rmd_start_age, "
                               "margin_interest_rate, mobile_check_deposit_limit, qcd_limit, "
                               "temporary_hold_status"],
    account_id: Annotated[Optional[str], "Account the rule applies to, if applicable"] = None,
    amount: Annotated[Optional[float], "Dollar amount, for amount-based rules"] = None,
    transaction_id: Annotated[Optional[str], "Transaction id, for temporary_hold_status"] = None,
) -> dict:
    """ACTION tool -- runs a deterministic Vanterra policy rule (never an
    LLM judgment). Returns {rule_id, decision, source_doc, inputs_used}.
    Party-level rules (fee_waiver, rmd_start_age, qcd_limit) resolve the
    party from account_id automatically.
    """
    try:
        account = _get_account(account_id) if account_id else None
    except ValueError as e:
        return {"error": str(e)}
    party = _store.parties_by_id.get(account["party_id"]) if account else None

    if rule_name == "fee_waiver":
        if party is None:
            return {"error": "fee_waiver requires account_id"}
        party_accounts = [a for a in _store.accounts if a["party_id"] == party["party_id"]]
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
        recent = [t for t in _store.transactions_for(account_id)
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
        txn = next((t for t in _store.transactions_for(account_id) if t["transaction_id"] == transaction_id), None)
        if txn is None or not txn.get("hold_start_date"):
            return {"rule_id": "temporary_hold_status", "decision": "not_on_hold", "source_doc": "VG-OP-013 SS2.5", "inputs_used": {}}
        elapsed = _business_days_between(date.fromisoformat(txn["hold_start_date"]), date.today())
        decision = "within_initial_hold" if elapsed <= 15 else "within_extension_window" if elapsed <= 40 else "must_release_or_escalate"
        return {"rule_id": "temporary_hold_status", "decision": decision, "source_doc": "VG-OP-013 SS2.5",
                "inputs_used": {"hold_start_date": txn["hold_start_date"], "business_days_elapsed": elapsed}}

    return {"error": f"Unknown rule_name: {rule_name}"}


@tool
def file_service_case(
    account_id: Annotated[str, "Account the case is for"],
    case_type: Annotated[str, "One of: beneficiary_update, address_change, poa_review, "
                               "fraud_review, inactive_account_reactivation, general_servicing"],
    description: Annotated[str, "Free-text description of the service request"],
) -> str:
    """Simulated function to file a service case for a given account. Executes
    immediately -- there is no approval step.
    """
    try:
        _get_account(account_id)
    except ValueError as e:
        return str(e)
    _log_action("service_case", account_id, {"case_type": case_type, "description": description})
    return f"Service case ({case_type}) filed successfully for account {account_id}."


@tool
def schedule_advisor_callback(
    account_id: Annotated[str, "Account the callback is for"],
    reason: Annotated[str, "One of: wire_callback_required, fraud_review, "
                            "financial_planning_referral, general_inquiry"],
    preferred_time: Annotated[Optional[str], "Investor's preferred callback time, if given"] = None,
) -> str:
    """Simulated function to schedule an advisor callback for a given account.
    Executes immediately -- there is no approval step.
    """
    try:
        _get_account(account_id)
    except ValueError as e:
        return str(e)
    _log_action("advisor_callback", account_id, {"reason": reason, "preferred_time": preferred_time})
    return f"Callback scheduled successfully for account {account_id}. Reason: {reason}."


@tool
def send_correspondence(
    account_id: Annotated[str, "Account the correspondence is for"],
    template_id: Annotated[str, f"One of: {', '.join(CORRESPONDENCE_TEMPLATES)}"],
    template_fields: Annotated[dict, "Values for the template's required fields"],
) -> str:
    """Simulated function to send correspondence to the investor on file for a
    given account, using a pre-approved template. Executes immediately --
    there is no approval step. Cannot draft free-form text; unknown
    templates or missing fields are rejected.
    """
    try:
        _get_account(account_id)
    except ValueError as e:
        return str(e)
    template = CORRESPONDENCE_TEMPLATES.get(template_id)
    if template is None:
        return f"Unknown template_id: {template_id}"
    missing = [f for f in template["required_fields"] if f not in template_fields]
    if missing:
        return f"Missing required template fields: {missing}"
    body = template["body"].format(**{k: template_fields[k] for k in template["required_fields"]})
    rendered = f"{body}\n\n---\n{template['required_disclosure']}"
    _log_action("correspondence", account_id, {"template_id": template_id, "rendered_text": rendered})
    return f"Correspondence ({template_id}) sent successfully for account {account_id}."


INVESTOR_TOOLS = [
    search_knowledge_base, get_account_summary, get_positions, get_recent_transactions,
    get_account_registrations, evaluate_policy_rule, file_service_case,
    schedule_advisor_callback, send_correspondence,
]
