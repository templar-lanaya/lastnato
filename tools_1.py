# JORDAN RUN THIS FIRST BEFORE RUNNING THE TEST_1.PY

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

from agent_framework import tool, Agent
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from threshold_engine import RULES_1, RMD_RULES, RULES_RATES, CORRESPONDENCE_TEMPLATES, get_rmd_age

# FIX (cleanup): merged the duplicate `import json`, and dropped imports that weren't
# referenced anywhere in this file (httpx, FastAPI's HTTPException, typing's Any/Callable,
# os, and the langchain_core.tools alternative that was commented out).

# -----------------------------------------------
# Setting up the MCP Server
# -----------------------------------------------
mcp = FastMCP("shared-tools", stateless_http=True)
app = FastAPI(title="Account & Policy Tools API", version="1.0.0")

# -----------------------------------------------
# Calling the Json files from Data Folder
# -----------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class JsonDataManager:
    account_by_id: dict[str, dict] = {}

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

        self.account_by_id: dict[str, dict] = {a["account_id"]: a for a in self.accounts}
        self.party_by_id: dict[str, dict] = {p["party_id"]: p for p in self.parties}
        self.products_by_ticker = {p["ticker"]: p for p in self.products}
        self.action_log: list[dict] = []  # for auditing

    def positions_for(self, account_id: str) -> list[dict]:
        result = []
        for p in self.positions:
            if p["account_id"] == account_id:
                result.append(p)
        return result

    def transactions_for(self, account_id: str) -> list[dict]:
        result = []
        for t in self.transactions:
            if t["account_id"] == account_id:
                result.append(t)
        return result

    def beneficiaries_for(self, account_id: str) -> list[dict]:
        result = []
        for t in self.transactions:
            if t["account_id"] == account_id:
                result.append(t)
        return result


data_manager = JsonDataManager()


def get_account(account_id: str) -> dict | None:
    return data_manager.account_by_id.get(account_id)


def get_party(party_id: str) -> dict | None:
    return data_manager.party_by_id.get(party_id)


# -----------------------------------------------
# Knowledge agent tools
# -----------------------------------------------
@mcp.tool(
    name="search_knowledge_base",
    description="Query + optional filters → ranked chunks with metadata and reranker scores",
)
def search_knowledge_base():
    pass


# -----------------------------------------------
# Account agent tools
# -----------------------------------------------
@tool(
    name="get_account_summary",
    description="Session-bound subject → account types, balances, delivery preferences, service-fee waiver status",
    approval_mode="never_require",
)
def get_account_summary(account_id: Annotated[str, "Account to summarize"]):
    """Get the balance and status summary of one account"""
    try:
        account = get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}

    result = {}
    for k in ("account_id", "party_id", "account_type", "status", "opened_date",
              "cash_balance", "margin_enabled", "debit_balance"):
        result[k] = account[k]
    return result


@tool(
    name="get_positions",
    description="Session-bound subject → holdings with ticker, share class, cost-basis method elected",
    approval_mode="require_approval",
)
def get_positions(account_id: Annotated[str, "Account to get holdings for"]) -> dict:
    """Get fund holdings for one account."""
    try:
        get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    rows = []
    for pos in data_manager.positions_for(account_id):
        product = data_manager.products_by_ticker.get(pos["ticker"])
        rows.append({
            "ticker": pos["ticker"],
            "fund_name": product["fund_name"] if product else None,
            "share_class": product["share_class"] if product else None,
            "quantity": pos["quantity"],
            "price": pos["price"],
            "market_value": round(pos["quantity"] * pos["price"], 2),
        })
    return {"account_id": account_id, "positions": rows}


@tool(
    name="get_recent_transactions",
    description="Session-bound subject + date range → transactions; date range is model-supplied, subject is not",
    approval_mode="require_approval",
)
def get_recent_transactions(
    account_id: Annotated[str, "Account to get transactions for"],
    limit: Annotated[int, "Max number of transactions to return"] = 20,
) -> dict:
    """Get the most recent transactions for one account, newest first."""
    try:
        get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    txns = sorted(data_manager.transactions_for(account_id), key=lambda t: t["date"], reverse=True)
    return {"account_id": account_id, "transactions": txns[:limit]}


@tool(
    name="get_account_registrations",
    description="Session-bound subject → beneficiary designations present, TCP on file, POA status",
    approval_mode="require_approval",
)
def get_account_registrations(
    account_id: Annotated[str, "Account to get registration/beneficiary info for"],
) -> dict:
    """Get account registration (titling) info and beneficiary designations."""
    try:
        account = get_account(account_id)
    except ValueError as e:
        return {"error": str(e)}
    beneficiaries = data_manager.beneficiaries_for(account_id)
    is_ira = account["account_type"] in ("traditional_ira", "roth_ira", "sep_ira", "simple_ira", "401k")
    return {
        "account_id": account_id,
        "account_type": account["account_type"],
        "beneficiaries": beneficiaries,
        # beneficiary_required_but_missing is True only when the account is an IRA type AND
        # it has no beneficiaries on file.
        "beneficiary_required_but_missing": is_ira and not beneficiaries,  # VG-OP-002
    }


def _find_rule(rule_id: str) -> dict | None:
    """Fetch one rule dict from the RULES list by its rule_id."""
    for rule in RULES_1:
        if rule["rule_id"] == rule_id:
            return rule
    return None


# -----------------------------------------------
# Evaluate policy rules
# -----------------------------------------------

class PolicyEvalRequest(BaseModel):
    rule_id: str
    account_id: Optional[str] = Field(default=None, description="Account the rule applies to")
    amount: Optional[float] = Field(default=None, description="Dollar amount for amount-based rules")
    transaction_id: Optional[str] = Field(default=None, description="Transaction id, for holds")


# ---- PLAIN logic function (no decorator). Both tools and the API call this. ----
def _evaluate_policy_rule(
    rule_id: str,
    account_id: Optional[str] = None,
    amount: Optional[float] = None,
    transaction_id: Optional[str] = None,
) -> dict:

    def wire_callback(amount):
        if amount is None:
            return {"error": "WIRE_CALLBACK requires amount"}
        return {"rule_id": "WIRE_CALLBACK", "decision": "callback_required", "amount": amount}

    def wire_outgoing(amount):
        if amount is None:
            return {"error": "WIRE_ACH_DAILY_LIMIT_OUTGOING requires amount"}
        rule = _find_rule("WIRE_ACH_DAILY_LIMIT_OUTGOING")
        limit = rule["limit"] if rule else 100_000
        return {"rule_id": "WIRE_ACH_DAILY_LIMIT_OUTGOING",
                "decision": "rejected" if amount > limit else "accepted",
                "inputs_used": {"amount": amount, "limit": limit}}

    def wire_incoming(amount):
        if amount is None:
            return {"error": "WIRE_ACH_DAILY_LIMIT_INCOMING requires amount"}
        rule = _find_rule("WIRE_ACH_DAILY_LIMIT_INCOMING")
        limit = rule["limit"] if rule else 250_000
        return {"rule_id": "WIRE_ACH_DAILY_LIMIT_INCOMING",
                "decision": "rejected" if amount > limit else "accepted",
                "inputs_used": {"amount": amount, "limit": limit}}

    def rmd_start_age(party):
        if party is None:
            return {"error": "rmd_start_age requires account_id"}
        birth_year = int(str(party["date_of_birth"])[:4])
        return {"rule_id": "RMD_START_AGE",
                "inputs_used": {"birth_year": birth_year, "rmd_age": get_rmd_age(birth_year)}}

    def margin_interest_rate(account):
        if account is None:
            return {"error": "margin_interest_rate requires account_id"}
        balance = account.get("debit_balance", 0)
        rate = next((t["rate"] for t in RULES_RATES if t["min"] <= balance <= t["max"]), None)
        return {"rule_id": "MARGIN_INTEREST_RATE",
                "inputs_used": {"debit_balance": balance, "rate": rate}}

    def mobile_check_deposit_limit(account, amount, account_id):
        if account is None or amount is None:
            return {"error": "mobile_check_deposit_limit requires account_id and amount"}
        today = date.today()
        month_ago = today - timedelta(days=30)
        recent = [t for t in data_manager.transactions_for(account_id)
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
        return {"rule_id": "mobile_check_deposit_limit",
                "decision": "rejected" if violations else "accepted",
                "source_doc": "VG-OP-016",
                "inputs_used": {"amount": amount, "daily_total": daily_total,
                                "monthly_total": monthly_total, "violations": violations}}

    def qcd_limit(party, amount):
        if party is None or amount is None:
            return {"error": "qcd_limit requires account_id and amount"}
        rule = _find_rule("QCD_ANNUAL_LIMIT")
        limit = rule["QCD"] if rule else 108_000
        return {"rule_id": "QCD_ANNUAL_LIMIT",
                "decision": "rejected" if amount > limit else "accepted",
                "inputs_used": {"amount": amount, "limit": limit}}

    def temp_hold(account, transaction_id, account_id):
        if account is None or transaction_id is None:
            return {"error": "temporary_hold_status requires account_id and transaction_id"}
        return {"rule_id": "TEMP_HOLD", "transaction_id": transaction_id}

    # ---- Dispatch ----
    # FIX (Unknown rule_id bug, from an earlier pass): normalize casing so callers can send
    # any casing and still match the lowercase snake_case branches below.
    rule_id = (rule_id or "").strip().lower()
    account = get_account(account_id) if account_id else None

    if rule_id == "service_fee_waiver":
        party = get_party(account["party_id"]) if account else None
        return service_fee_waiver(party)
    elif rule_id == "wire_ach_daily_limit_incoming":
        return wire_incoming(amount)
    elif rule_id == "wire_ach_daily_limit_outgoing":
        return wire_outgoing(amount)
    elif rule_id == "wire_callback":
        return wire_callback(amount)
    elif rule_id == "rmd_start_age":
        party = get_party(account["party_id"]) if account else None
        return rmd_start_age(party)
    elif rule_id == "margin_interest_rate":
        return margin_interest_rate(account)
    elif rule_id == "mobile_check_deposit_limit":
        return mobile_check_deposit_limit(account, amount, account_id)
    elif rule_id == "qcd_annual_limit":
        party = get_party(account["party_id"]) if account else None
        return qcd_limit(party, amount)
    elif rule_id == "temp_hold":
        return temp_hold(account, transaction_id, account_id)

    return {"error": "Unknown rule_id", "rule_id": rule_id}
    # FIX (cleanup): the branch literals above were previously written as e.g.
    # "WIRE_ACH_DAILY_LIMIT_INCOMING".lower() -- functionally identical to the plain
    # lowercase string, just harder to read. Replaced with the literal lowercase strings.


# ---- Tool wrapper for MCP clients (thin; calls the same plain function) ----
# FIX (reverted the agent_framework-native duplicate from an earlier pass): evaluate_policy_rule
# must stay MCP-only, not be re-wrapped with agent_framework's @tool, because it's meant to be
# one shared implementation served over MCP to every agent that needs it (Account Agent,
# Fulfillment Agent, Compliance Reviewer, ...), not copy-pasted as a native tool per consumer.
# To fix the original "model doesn't know valid rule_id values" problem without duplicating the
# tool, the description/Annotated hint below now spells out every valid rule_id directly.
@mcp.tool(
    name="evaluate_policy_rule",
    description=(
        "Check one policy rule for compliance, given the subject and any other inputs. "
        "rule_id must be exactly one of: service_fee_waiver, wire_callback, "
        "wire_ach_daily_limit_outgoing, wire_ach_daily_limit_incoming, rmd_start_age, "
        "margin_interest_rate, mobile_check_deposit_limit, qcd_annual_limit, temp_hold."
    ),
)
def evaluate_policy_rule(
    rule_id: Annotated[str, "Policy rule identifier; see tool description for valid values"],
    account_id: Annotated[Optional[str], "Account the rule applies to, if applicable"] = None,
    amount: Annotated[Optional[float], "Dollar amount for amount-based rules; omit if not applicable"] = None,
    transaction_id: Annotated[Optional[str], "Transaction ID, only for temp_hold"] = None,
) -> dict:
    return _evaluate_policy_rule(rule_id, account_id, amount, transaction_id)


# ---- FastAPI endpoint (thin; also calls the plain function) ----
@app.post("/policy/evaluate")
def evaluate_policy_endpoint(req: PolicyEvalRequest) -> dict:
    return _evaluate_policy_rule(
        rule_id=req.rule_id,
        account_id=req.account_id,
        amount=req.amount,
        transaction_id=req.transaction_id,
    )


def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# -----------------------------------------------
# The Rules logic
# -----------------------------------------------

def service_fee_waiver(party):
    if party is None:
        return {"error": "fee_waiver requires account_id"}
    party_accounts = [a for a in data_manager.accounts if a["party_id"] == party["party_id"]]
    household_total = sum(a["cash_balance"] for a in party_accounts)
    waived = household_total >= 5_000.00 or party["e_delivery_election"]
    return {"rule_id": "service_fee_waiver", "decision": "waived" if waived else "fee_applies",
            "source_doc": "VG-OP-017 SS3",
            "inputs_used": {"household_total_balance": household_total, "e_delivery_election": party["e_delivery_election"]}}


def wire_callback(amount):
    if amount is None:
        return {"error": "WIRE_CALLBACK requires amount"}

    callback_required = amount >= 50_000.00
    decision = "callback required" if callback_required else "callback_not_required"
    return {
        "rule_id": "WIRE_CALLBACK",
        "decision": decision,
        "source_doc": "VG-OP-005 SS3.4",
        "inputs_used": {"amount": amount},
    }


def wire_outgoing(amount):
    if amount is None:
        return {"error": "WIRE_ACH_DAILY_LIMIT_OUTGOING requires amount"}

    exceeds_limit = amount > 100_000.00
    decision = "exceeds_limit" if exceeds_limit else "within_limit"

    return {
        "rule_id": "WIRE_ACH_DAILY_LIMIT_OUTGOING",
        "decision": decision,
        "source_doc": "VG-OP-005 SS2.3",
        "inputs_used": {"amount": amount},
    }


def wire_incoming(amount):
    if amount is None:
        return {"error": "WIRE_ACH_DAILY_LIMIT_INCOMING requires amount"}

    exceeds_limit = amount > 250_000.00
    decision = "exceeds_limit" if exceeds_limit else "within_limit"

    return {
        "rule_id": "WIRE_ACH_DAILY_LIMIT_INCOMING",
        "decision": decision,
        "source_doc": "VG-OP-005 SS2.3",
        "inputs_used": {"amount": amount},
    }


def rmd_start_age(party):
    if party is None:
        return {"error": "rmd_start_age requires account_id"}

    # Get the customer's date of birth.
    date_of_birth = party.get("date_of_birth")

    if date_of_birth is None:
        return {
            "rule_id": "rmd_start_age",
            "decision": "insufficient_data",
            "source_doc": "VG-OP-003 SS3",
            "inputs_used": {
                "date_of_birth": None,
                "missing_field": "date_of_birth",
            },
        }

    # Convert the date of birth from text into a date, and pull out the year.
    birth_date = date.fromisoformat(date_of_birth)
    birth_year = birth_date.year

    # Check each RMD age band.
    for band in RMD_RULES:

        # Some bands describe people born through a particular year.
        if "born_through" in band:
            if birth_year <= band["born_through"]:
                return {
                    "rule_id": "rmd_start_age",
                    "decision": "insufficient_data",
                    "source_doc": "VG-OP-003 SS3",
                    "inputs_used": {
                        "birth_year": birth_year,
                        "reason": "prior-law band has no single seedable value",
                    },
                }

        # Check whether the person's birth year falls inside this band's range.
        if "born_min" in band:
            born_min = band["born_min"]
            born_max = band["born_max"]

            meets_minimum = birth_year >= born_min
            meets_maximum = True if born_max is None else birth_year <= born_max

            if meets_minimum and meets_maximum:
                return {
                    "rule_id": "rmd_start_age",
                    "decision": {"rmd_start_age": band["rmd_age"]},
                    "source_doc": "VG-OP-003 SS3",
                    "inputs_used": {"birth_year": birth_year},
                }

    return {"error": f"birth_year {birth_year} matched no RMD band"}


def margin_interest_rate(account):
    # We need an account because the debit balance belongs to the account.
    if account is None:
        return {"error": "margin_interest_rate requires account_id"}

    debit_balance = account.get("debit_balance")

    if debit_balance is None:
        return {"error": "margin_interest_rate requires account_id"}

    # Check each margin interest tier.
    for tier in RULES_RATES:
        floor = tier[0]
        ceiling = tier[1]
        rate = tier[2]

        meets_floor = debit_balance >= floor
        meets_ceiling = True if ceiling is None else debit_balance <= ceiling

        if meets_floor and meets_ceiling:
            return {
                "rule_id": "margin_interest_rate",
                "decision": {"annual_rate_pct": rate},
                "source_doc": "VG-OP-009 SS4",
                "inputs_used": {"debit_balance": debit_balance},
            }

    return {"error": f"debit_balance {debit_balance} matched no margin tier"}


def mobile_check_deposit_limit(account, amount, account_id):
    if account is None or amount is None:
        return {"error": "mobile_check_deposit_limit requires account_id and amount"}
    today = date.today()
    month_ago = today - timedelta(days=30)
    recent = [t for t in data_manager.transactions_for(account_id)
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


def qcd_limit(party, amount):
    if party is None or amount is None:
        return {"error": "qcd_limit requires account_id and amount"}

    date_of_birth = party.get("date_of_birth")

    if date_of_birth is None:
        return {
            "rule_id": "qcd_limit",
            "decision": "insufficient_data",
            "source_doc": "VG-OP-003 SS7",
            "inputs_used": {
                "date_of_birth": None,
                "missing_field": "date_of_birth",
            },
        }

    # Calculate the person's age in years.
    birth_date = date.fromisoformat(date_of_birth)
    days_alive = (date.today() - birth_date).days
    age_years = days_alive / 365.25

    # A QCD is eligible when BOTH conditions are true:
    # 1. Person is at least 70.5 years old.
    # 2. Amount is no more than $108,000.
    meets_age_requirement = age_years >= 70.5
    meets_amount_requirement = amount <= 108_000.00
    eligible = meets_age_requirement and meets_amount_requirement

    return {
        "rule_id": "qcd_limit",
        "decision": "eligible" if eligible else "not_eligible",
        "source_doc": "VG-OP-003 SS7",
        "inputs_used": {
            "age_years": round(age_years, 1),
            "amount": amount,
        },
    }


def temp_hold(account, transaction_id, account_id):
    if account is None or transaction_id is None:
        return {"error": "temporary_hold_status requires account_id and transaction_id"}

    # Find the transaction we are checking.
    transaction = None
    transactions = data_manager.transactions_for(account_id)
    for current_transaction in transactions:
        if current_transaction["transaction_id"] == transaction_id:
            transaction = current_transaction
            break

    # If we cannot find the transaction, or there is no hold date, the transaction is not
    # currently considered to be on hold.
    if transaction is None:
        return {
            "rule_id": "temporary_hold_status",
            "decision": "not_on_hold",
            "source_doc": "VG-OP-013 SS2.5",
            "inputs_used": {},
        }

    hold_start_date = transaction.get("hold_start_date")

    if not hold_start_date:
        return {
            "rule_id": "temporary_hold_status",
            "decision": "not_on_hold",
            "source_doc": "VG-OP-013 SS2.5",
            "inputs_used": {},
        }

    # Calculate how many business days have passed since the hold started.
    start_date = date.fromisoformat(hold_start_date)
    business_days_elapsed = _business_days_between(start_date, date.today())

    # Determine the current status.
    if business_days_elapsed <= 15:
        decision = "within_initial_hold"
    elif business_days_elapsed <= 40:
        decision = "within_extension_window"
    else:
        decision = "must_release_or_escalate"

    return {
        "rule_id": "temporary_hold_status",
        "decision": decision,
        "source_doc": "VG-OP-013 SS2.5",
        "inputs_used": {
            "hold_start_date": hold_start_date,
            "business_days_elapsed": business_days_elapsed,
        },
    }


def _log_action(action_type: str, account_id: str, payload: dict) -> dict:
    entry = {
        "account_id": account_id,
        "action_type": action_type,
        "payload": payload,
        "executed_at": datetime.utcnow().isoformat(),
    }
    data_manager.action_log.append(entry)
    return entry


# -----------------------------------------------
# Tools for the Fulfillment Agent
# -----------------------------------------------
@tool(
    name="propose_service_case",
    description=(
        "Draft a proposed service case for the representative to review. Takes the case type, "
        "a short summary of the issue, and a priority level, and returns a typed ProposedAction "
        "with no side effect. The case is only opened after the representative approves the "
        "proposal — this tool never writes to the servicing system itself."
    ),
)
def propose_service_case(
    account_id: Annotated[str, "Account the case is for"],
    case_type: Annotated[str, "Type of the service case"],
    description: Annotated[str, "Free-text description of the service request"],
) -> str:
    """Simulated function to file a service case for a given account. Executes immediately -- there is no approval step."""
    try:
        get_account(account_id)
    except ValueError as e:
        return str(e)
    _log_action("service_case", account_id, {"case_type": case_type, "description": description})
    return f"Service case ({case_type}) filed successfully for account {account_id}."


@tool(
    name="propose_advisor_callback",
    description=(
        "Evaluate whether the client's inquiry requires advisor involvement and recommend an "
        "advisor callback when appropriate, including the reason, urgency, and relevant context "
        "for the advisor."
    ),
)
def propose_advisor_callback(
    account_id: Annotated[str, "Account the callback is for"],
    reason: Annotated[str, "One of: wire_callback_required, fraud_review, "
                            "financial_planning_referral, general_inquiry"],
    preferred_time: Annotated[Optional[str], "Investor's preferred callback time, if given"] = None,
) -> str:
    """Simulated function to schedule an advisor callback for a given account. Executes immediately -- there is no approval step."""
    try:
        get_account(account_id)
    except ValueError as e:
        return str(e)
    _log_action("advisor_callback", account_id, {"reason": reason, "preferred_time": preferred_time})
    return f"Callback scheduled successfully for account {account_id}. Reason: {reason}."


@tool(
    name="propose_correspondence",
    description=(
        "Propose outbound correspondence to the investor by selecting an approved template and "
        "supplying its merge fields. Returns a typed ProposedAction for human approval."
    ),
)
def propose_correspondence(
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
        get_account(account_id)
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


# FIX (cleanup): removed the unused `RuleID = Literal[...]` alias -- it wasn't referenced by
# any function signature or by any other file, just dead weight.


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true",
                         help="Serve over HTTP (default behaviour for this API).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


if __name__ == "__main__":
    import uvicorn

    args = build_parser().parse_args()
    print(f"Serving HTTP on http://{args.host}:{args.port}  (docs at /docs)")
    uvicorn.run(app, host=args.host, port=args.port)
