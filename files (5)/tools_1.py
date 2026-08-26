#JORDAN RUN THIS FIRST BEFORE RUNNING THE TEST_1.PY

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Callable, Optional, Literal
from threshold_engine import RULES_1, RMD_RULES, RULES_RATES, CORRESPONDENCE_TEMPLATES, get_rmd_age
from agent_framework import tool, Agent

#from langchain_core.tools import tool
import argparse
import os

from mcp.server.fastmcp import FastMCP
import httpx
from fastapi import FastAPI, HTTPException

# from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime, date
from typing import List

import json
from auth import MissingCallerContextError
from gates.entitlement_gate import (
    ACCOUNT_POSITIONS_READ,
    ACCOUNT_REGISTRATIONS_READ,
    ACCOUNT_SUMMARY_READ,
    ACCOUNT_TRANSACTIONS_READ,
    ACTION_PROPOSE,
    EntitlementError
)
from account_agent_gate import SubjectBindingError, account_gate
from gate_models import ProposedAction, new_proposed_action
from gates.approval_gate import (
    UnknownActionType,
    approval_gate,
    register_executor,
)
from gates.expiration_gate import ProposalExpired

# --- tracing: added, no functional change below this line ---
from tracing_provider import tool_span

GATE_ERRORS = (
    EntitlementError,
    SubjectBindingError,
    MissingCallerContextError,
    ProposalExpired,
    UnknownActionType,
)


def _authorized_account(
    required_entitlement: str,
    requested_account_id: str | None = None,
):
    """
    Resolve the account this call may read and validate entitlement.

    The subject comes from the session, never from model input. A
    model-supplied account id is only honoured when the authenticated
    subject already owns it.
    """

    try:
        ctx, account_id = account_gate.session_bound_account(
            required_entitlement,
            requested_account_id,
        )

    except GATE_ERRORS as e:
        return None, {
            "status": "error",
            "message": str(e)
        }

    account = data_manager.account_by_id.get(account_id)

    if account is None:
        return None, {
            "status": "error",
            "message": "No account found"
        }

    return account, None
#-----------------------------------------------
#Setting up the MCP Server
#-----------------------------------------------
mcp = FastMCP("shared-tools", stateless_http=True) #, host = "127.0.0.1", port = 8080, stateless_http=True)
# from account_agent import data_manager, get_account
app = FastAPI(title="Account & Policy Tools API", version="1.0.0")

#-----------------------------------------------
#Calling the Json files from Data Folder
#-----------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# print(f"Data directory: {DATA_DIR}")

class JsonDataManager:
    account_by_id: dict[str, dict] = {}
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        def _load(name:str) -> list[dict]:
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
        self.action_log: list[dict] = [] # for auditing

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

    def beneficiaries_for(self, account_id: str) ->  list[dict]:
        result = []

        for b in self.beneficiaries:
            if b["account_id"] == account_id:
                result.append(b)
        return result

    def accounts_for_party(self, party_id: str) -> list[str]:
        return [
            a["account_id"]
            for a in self.accounts
            if a["party_id"] == party_id
        ]

data_manager = JsonDataManager()

# The account gate owns subject binding but must not import the data
# layer, so the resolver is handed to it at startup.
account_gate.bind_account_resolver(data_manager.accounts_for_party)

# def get_account(account_id: str) -> dict | None:
#         return data_manager.account_by_id.get(account_id)
def get_account(account_id):
    account = data_manager.account_by_id.get(account_id)
    if account is None:
        raise ValueError(f"account {account_id} not found")
    return account
def get_party(party_id: str) -> dict:
    party = data_manager.party_by_id.get(party_id)
    if party is None:
        raise ValueError(f"party {party_id} not found")
    return party



RuleID = Literal[
    "service_fee_waiver",
     "wire_callback",
    "wire_ach_daily_limit_incoming",
    "wire_ach_daily_limit_outgoing",
    "rmd_start_age",
    "margin_interest_rate",
    "mobile_check_deposit_limit",
    "qcd_annual_limit",
    "temp_hold"
]
#-----------------------------------------------
#Knowledge agent tools
#-----------------------------------------------
@mcp.tool(name="search_knowledge_base",
    description="Query + optional filters → ranked chunks with metadata and reranker scores",
    )

def search_knowledge_base():
    pass

#-----------------------------------------------
#Account agent tools
#-----------------------------------------------
@tool(
    name="get_account_summary",
    description="""
    Session-bound subject → account types,
    balances, delivery preferences,
    service-fee waiver status.
    """,
    approval_mode="never_require"
)
def get_account_summary():

    # --- tracing: wraps the existing body; logic/return shapes unchanged.
    # NOTE: error shape here is {"status": "error", "message": ...}, not
    # {"error": ...} -- tool_span.record_result() only recognizes the
    # latter, so failures will show as OK in the trace. Flagged, not fixed.
    with tool_span("get_account_summary") as span:
        try:

            account, error = _authorized_account(
                ACCOUNT_SUMMARY_READ
            )

            if error:
                span.record_result(json.dumps(error))
                return error

            result = {}

            for k in (
                "account_id",
                "party_id",
                "account_type",
                "status",
                "opened_date",
                "cash_balance",
                "margin_enabled",
                "debit_balance"
            ):
                result[k] = account[k]

            span.record_result(json.dumps(result))
            return result

        except Exception as e:

            error_result = {
                "status": "error",
                "message": str(e)
            }
            span.record_result(json.dumps(error_result))
            return error_result

# summary = get_account_summary("ACC-00001")
# print(f"TEST1 :{summary}")

@tool(name="get_positions",
    description="Session-bound subject → holdings with ticker, share class, cost-basis method elected",
    approval_mode="require_approval")
def get_positions(account_id: Annotated[Optional[str], "Account to get holdings for; must belong to the authenticated session. Omit to use the session-bound account."] = None,) -> dict:
    """Get fund holdings for the session-bound account."""
    # --- tracing: wraps the existing body, no logic change. NOTE: same
    # {"status": "error", ...} detection gap as get_account_summary -- the
    # error path here now comes from _authorized_account(), not a plain
    # {"error": ...} dict like the previous version of this file had.
    with tool_span("get_positions", json.dumps({"account_id": account_id})) as span:
        account, error = _authorized_account(
            ACCOUNT_POSITIONS_READ,
            account_id,
        )
        if error:
            span.record_result(json.dumps(error))
            return error
        account_id = account["account_id"]
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
        result = {"account_id": account_id, "positions": rows}
        span.record_result(json.dumps(result))
        return result

# summary2 = get_positions("ACC-00001")
# print(f"TEST2 : {summary2}")

@tool(name="get_recent_transactions",
        description="Session-bound subject + date range → transactions; date range is model-supplied, subject is not",
        approval_mode="require_approval")

def get_recent_transactions( account_id: Annotated[Optional[str], "Account to get transactions for; must belong to the authenticated session. Omit to use the session-bound account."] = None,
    limit: Annotated[int, "Max number of transactions to return"] = 20,) -> dict:
    """Get the most recent transactions for the session-bound account, newest first."""
    # --- tracing: wraps the existing body, no logic change. Same
    # {"status": "error", ...} detection gap noted on get_positions above.
    with tool_span("get_recent_transactions", json.dumps({"account_id": account_id, "limit": limit})) as span:
        account, error = _authorized_account(
            ACCOUNT_TRANSACTIONS_READ,
            account_id,
        )
        if error:
            span.record_result(json.dumps(error))
            return error
        account_id = account["account_id"]
        txns = sorted(data_manager.transactions_for(account_id), key=lambda t: t["date"], reverse=True)
        result = {"account_id": account_id, "transactions": txns[:limit]}
        span.record_result(json.dumps(result))
        return result


@tool(name="get_account_registrations",
    description="Session-bound subject → beneficiary designations present, TCP on file, POA status",
    approval_mode="require_approval")
def get_account_registrations(
        account_id: Annotated[Optional[str], "Account to get registration/beneficiary info for; must belong to the authenticated session. Omit to use the session-bound account."] = None) -> dict:
    """Get account registration (titling) info and beneficiary designations."""
    # --- tracing: wraps the existing body, no logic change. Same
    # {"status": "error", ...} detection gap noted on get_positions above.
    with tool_span("get_account_registrations", json.dumps({"account_id": account_id})) as span:
        account, error = _authorized_account(
            ACCOUNT_REGISTRATIONS_READ,
            account_id,
        )
        if error:
            span.record_result(json.dumps(error))
            return error
        account_id = account["account_id"]
        beneficiaries = data_manager.beneficiaries_for(account_id)
        is_ira = account["account_type"] in ("traditional_ira", "roth_ira", "sep_ira", "simple_ira", "401k")
        result = {
            "account_id": account_id,
            "account_type": account["account_type"],
            "beneficiaries": beneficiaries,
            # if beneficiaries is empty and an IRA account = True
            #if beneficiaries is NOT empty = False
            "beneficiary_required_but_missing": is_ira and not beneficiaries, #VG-OP-002
        }
        span.record_result(json.dumps(result))
        return result


# summary3 = get_recent_transactions("ACC-00001")
# print(f"TEST3 : {summary3}")

def _find_rule(rule_id: str) -> dict | None:
    """Fetch one rule dict from the RULES list by its rule_id."""
    for rule in RULES_1:
        if rule["rule_id"] == rule_id:
            return rule
    return None


#-----------------------------------------------
#Evaluate policy rules
#-----------------------------------------------

class PolicyEvalRequest(BaseModel):
    rule_id: RuleID
    account_id: Optional[str] = Field(default=None, description="Account the rule applies to")
    amount: Optional[float] = Field(default=None, description="Dollar amount for amount-based rules")
    transaction_id: Optional[str] = Field(default=None, description="Transaction id, for holds")


# ---- PLAIN logic function (no decorator). Both the tool and the API call this. ----
def _evaluate_policy_rule(
        
    rule_id: RuleID,
    account_id: Optional[str] = None,
    amount: Optional[float] = None,
    transaction_id: Optional[str] = None,) -> dict:

    # def service_fee_waiver(party):
    #     if party is None:
    #         return {"error": "fee_waiver requires account_id"}
    #     party_accounts = [a for a in data_manager.accounts if a["party_id"] == party["party_id"]]
    #     return {"rule_id": "service_fee_waiver", "party_accounts": len(party_accounts)}

    # def wire_callback(amount):
    #     if amount is None:
    #         return {"error": "WIRE_CALLBACK requires amount"}
    #     return {"rule_id": "WIRE_CALLBACK", "decision": "callback_required", "amount": amount}

    # def wire_outgoing(amount):
    #     if amount is None:
    #         return {"error": "WIRE_ACH_DAILY_LIMIT_OUTGOING requires amount"}
    #     rule = _find_rule("WIRE_ACH_DAILY_LIMIT_OUTGOING")
    #     limit = rule["limit"] if rule else 100_000
    #     return {"rule_id": "WIRE_ACH_DAILY_LIMIT_OUTGOING",
    #             "decision": "rejected" if amount > limit else "accepted",
    #             "inputs_used": {"amount": amount, "limit": limit}}

    # def wire_incoming(amount):
    #     if amount is None:
    #         return {"error": "WIRE_ACH_DAILY_LIMIT_INCOMING requires amount"}
    #     rule = _find_rule("WIRE_ACH_DAILY_LIMIT_INCOMING")
    #     limit = rule["limit"] if rule else 250_000
    #     return {"rule_id": "WIRE_ACH_DAILY_LIMIT_INCOMING",
    #             "decision": "rejected" if amount > limit else "accepted",
    #             "inputs_used": {"amount": amount, "limit": limit}}

    # def rmd_start_age(party):
    #     if party is None:
    #         return {"error": "rmd_start_age requires party_id"}
    #     birth_year = int(str(party["date_of_birth"])[:4])
    #     return {"rule_id": "RMD_START_AGE",
    #             "inputs_used": {"birth_year": birth_year, "rmd_age": get_rmd_age(birth_year)}}

    # def rmd_start_age(party):
    #     if party is None:
    #         return {"error": "rmd_start_age requires party_id"}

    #     dob = party.get("date_of_birth")
    #     if dob is None:
    #         return {
    #             "rule_id": "rmd_start_age",
    #             "decision": "insufficient_data",
    #             "source_doc": "VG-OP-003 SS3",
    #             "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"},
    #         }

    #     birth_year = date.fromisoformat(dob).year

    #     for band in RMD_RULES:
    #         if band["birth_year_min"] <= birth_year <= band["birth_year_max"]:
    #             return {
    #                 "rule_id": "rmd_start_age",
    #                 "decision": {"rmd_start_age": band["rmd_age"]},
    #                 "source_doc": "VG-OP-003 SS3",
    #                 "inputs_used": {"birth_year": birth_year},
    #             }

    #     return {"error": f"birth_year {birth_year} matched no RMD band"}

    # def margin_interest_rate(account):
    #     if account is None:
    #         return {"error": "margin_interest_rate requires account_id"}
    #     balance = account.get("debit_balance", 0)
    #     rate = next((t["rate"] for t in RULES_RATES if t["min"] <= balance <= t["max"]), None)
    #     return {"rule_id": "MARGIN_INTEREST_RATE",
    #             "inputs_used": {"debit_balance": balance, "rate": rate}}

    # def mobile_check_deposit_limit(account, amount, account_id):
    #     if account is None or amount is None:
    #         return {"error": "mobile_check_deposit_limit requires account_id and amount"}
    #     today = date.today()
    #     month_ago = today - timedelta(days=30)
    #     recent = [t for t in data_manager.transactions_for(account_id)
    #               if t["type"] == "check_deposit" and date.fromisoformat(t["date"]) >= month_ago]
    #     monthly_total = sum(t["amount"] for t in recent) + amount
    #     daily_total = sum(t["amount"] for t in recent if t["date"] == today.isoformat()) + amount
    #     violations = []
    #     if amount > 100_000.00:
    #         violations.append("exceeds_per_check_limit")
    #     if daily_total > 100_000.00:
    #         violations.append("exceeds_daily_limit")
    #     if monthly_total > 250_000.00:
    #         violations.append("exceeds_monthly_limit")
    #     return {"rule_id": "mobile_check_deposit_limit",
    #             "decision": "rejected" if violations else "accepted",
    #             "source_doc": "VG-OP-016",
    #             "inputs_used": {"amount": amount, "daily_total": daily_total,
    #                             "monthly_total": monthly_total, "violations": violations}}

    # def qcd_limit(party, amount):
    #     if party is None or amount is None:
    #         return {"error": "qcd_limit requires account_id and amount"}
    #     rule = _find_rule("qcd_annual_limit")
    #     limit = rule["QCD"] if rule else 108_000
    #     return {"rule_id": "qcd_annual_limit",
    #             "decision": "rejected" if amount > limit else "accepted",
    #             "inputs_used": {"amount": amount, "limit": limit}}

    # def temp_hold(account, transaction_id, account_id):
    #     if account is None or transaction_id is None:
    #         return {"error": "temporary_hold_status requires account_id and transaction_id"}
    #     return {"rule_id": "TEMP_HOLD", "transaction_id": transaction_id}

    # ---- Dispatch ----
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
    # elif rule_id == "rmd_start_age":
    #     party = get_party(account["party_id"]) if account else None
    #------------------------------------------------------------------
    
    elif rule_id == "rmd_start_age":
        account = get_account(account_id)          # account_id -> account
        party   = get_party(account["party_id"])   # account   -> party
        return rmd_start_age(party)                 # <-- your function is called here

    #------------------------------------------------------------------
    elif rule_id == "margin_interest_rate":
        return margin_interest_rate(account)
    elif rule_id =="mobile_check_deposit_limit":
        return mobile_check_deposit_limit(account, amount, account_id)
    elif rule_id == "qcd_annual_limit":
        party = get_party(account["party_id"]) if account else None
        return qcd_limit(party, amount)
    elif rule_id == "temp_hold":
        return temp_hold(account, transaction_id, account_id)

    return {"error": "Unknown rule_id", "rule_id": rule_id}


# ---- Tool wrapper for the agent (thin; just calls the plain function) ----
# @mcp.tool(
#     name="evaluate_policy_rule",
#     description="Check one policy rule for compliance, given the subject and any other inputs",
# )


tool(
name="evaluate_policy_rule",
description="Check one policy rule (e.g. rmd_start_age) for a given account_id.",)
def evaluate_policy_rule(
    rule_id: Annotated[RuleID, "Rule to evaluate"],
    account_id: Annotated[Optional[str], "Account the rule applies to, if applicable"] = None,
    amount: Annotated[Optional[float], "Dollar amount for amount-based rules"] = None,
    transaction_id: Annotated[Optional[str], "Transaction ID for temporary holds"] = None,
) -> dict:
    # --- tracing: wraps the existing body, no logic change. Note: this
    # function currently isn't @tool-registered (missing decorator syntax
    # above -- left exactly as found, not my place to fix per "don't
    # change functionality"), but it IS called directly by
    # evaluate_policy_endpoint below, so tracing it still has real value.
    with tool_span("evaluate_policy_rule", json.dumps({
        "rule_id": rule_id, "account_id": account_id, "amount": amount, "transaction_id": transaction_id,
    })) as span:
        result = _evaluate_policy_rule(rule_id, account_id, amount, transaction_id)
        span.record_result(json.dumps(result))
        return result

#--------------------------------------------------------
import httpx  # add near your other imports if not already there

# @tool(
#     name="evaluate_policy_rule",
#     description="Check one policy rule (e.g. rmd_start_age) for a given account_id.",
# )
# def evaluate_policy_rule(
#     rule_id: str,
#     account_id: str | None = None,
#     amount: float | None = None,
#     transaction_id: str | None = None,
# ) -> dict:
#     """Local tool → calls the FastAPI policy endpoint running on this machine."""
#     resp = httpx.post(
#         "http://127.0.0.1:8001/policy/evaluate",   # localhost is FINE — runs locally
#         json={
#             "rule_id": rule_id,
#             "account_id": account_id,
#             "amount": amount,
#             "transaction_id": transaction_id,
#         },
#         timeout=30,
#     )
#     resp.raise_for_status()
#     return resp.json()
#--------------------------------------------------------
# ---- FastAPI endpoint (thin; also calls the plain function) ----
@app.post("/policy/evaluate")
def evaluate_policy_endpoint(req: PolicyEvalRequest) -> dict:
    # --- tracing: server-side span for this endpoint, so a call from any
    # of the three agents' local evaluate_rule tools has a matching span
    # on the receiving end, not just the client side.
    with tool_span("evaluate_policy_endpoint", req.model_dump_json()) as span:
        result = evaluate_policy_rule(
            rule_id=req.rule_id,
            account_id=req.account_id,
            amount=req.amount,
            transaction_id=req.transaction_id,
        )
        span.record_result(json.dumps(result))
        return result

def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n

#-----------------------------------------------
#The Rules logic
#-----------------------------------------------


# def service_fee_waiver(party):
#     if party is None:
#                 return {"error": "fee_waiver requires account_id"}
#     party_accounts = []
#     for account in data_manager.accounts:
#                 if account["party_id"] == party["party_id"]:
#                     party_accounts.append(account)

#     household_total = 0
#     for account in party_accounts:
#         household_total += account["cash_balance"]  
#     has_required_balance = household_total >= 5_000.00
#     uses_e_delivery = party["e_delivery_preference"]

#     waived = has_required_balance or uses_e_delivery

#     if waived:
#         decision = "waived"
#     else:
#         decision = "fee_applies"

#     return {
#             "rule_id": "fee_waiver",
#             "decision": decision,
#             "source_doc": "VG-OP-017 SS3",
#             "inputs_used": {"household_total_balance": household_total,
#                 "e_delivery_preference": party["e_delivery_preference"]
#             }}

def service_fee_waiver(party):
    if party is None:
            return {"error": "fee_waiver requires account_id"}
    party_accounts = [a for a in data_manager.accounts if a["party_id"] == party["party_id"]]
    household_total = sum(a["cash_balance"] for a in party_accounts)
    waived = household_total >= 5_000.00 or party["e_delivery_preference"]
    return {"rule_id": "service_fee_waiver", "decision": "waived" if waived else "fee_applies",
            "source_doc": "VG-OP-017 SS3",
            "inputs_used": {"household_total_balance": household_total, "e_delivery_preference": party["e_delivery_preference"]}}

def wire_callback(amount):
    if amount is None:
                 return {"error": "WIRE_CALLBACK requires amount"}
     
    callback_required = amount >= 50_000.00
    if callback_required:
        decision = "callback required"
    else:
        decision = "callback_not_required"
    return {
            "rule_id": "wire_callback",
            "decision": decision,
            "source_doc": "VG-OP-005 SS3.4",
            "inputs_used": {"amount": amount}
            }

def wire_outgoing(amount):

    if amount is None:
                 return {"error": "WIRE_ACH_DAILY_LIMIT_OUTGOING requires amount"}
     
    exceeds_limit = amount > 100_000.00

    if exceeds_limit:
        decision = "exceeds_limit"
    else:
        decision = "within_limit"

    return {
        "rule_id": "wire_ach_daily_limit_outgoing",
        "decision": decision,
        "source_doc": "VG-OP-005 SS2.3",
        "inputs_used": {"amount": amount}
    } 

def wire_incoming(amount):

    if amount is None:
                 return {"error": "WIRE_ACH_DAILY_LIMIT_INCOMING requires amount"}
     
    exceeds_limit = amount > 250_000.00

    if exceeds_limit:
        decision = "exceeds_limit"
    else:
        decision = "within_limit"

    return {
        "rule_id": "wire_ach_daily_limit_incoming",
        "decision": decision,
        "source_doc": "VG-OP-005 SS2.3",
        "inputs_used": {"amount": amount}
    }

# def rmd_start_age(party):
#     if party is None:
#             return {"error": "rmd_start_age requires party_id"}
#     dob = party.get("date_of_birth")
#     if dob is None:
#         return {"rule_id": "rmd_start_age", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS3",
#                 "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"}}
#     birth_year = date.fromisoformat(dob).year
#     for band in RMD_RULES:
#         if "born_year_min" in band and birth_year <= band["born_year_min"]:
#             return {"rule_id": "rmd_start_age", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS3",
#                     "inputs_used": {"birth_year": birth_year, "reason": "prior-law band has no single seedable value"}}
#         if "born_min" in band and birth_year >= band["born_min"] and (band["born_max"] is None or birth_year <= band["born_max"]):
#             return {"rule_id": "rmd_start_age", "decision": {"rmd_start_age": band["rmd_age"]},
#                     "source_doc": "VG-OP-003 SS3", "inputs_used": {"birth_year": birth_year}}
#     return {"error": f"birth_year {birth_year} matched no RMD band"}

def rmd_start_age(party):
    # 1. Guard: we need a party record to read the birth year
    if party is None:
        return {"error": "rmd_start_age requires party_id"}

    # 2. Guard: the party must have a date_of_birth on file
    dob = party.get("date_of_birth")
    if dob is None:
        return {
            "rule_id": "rmd_start_age",
            "decision": "insufficient_data",
            "source_doc": "VG-OP-003 SS3",
            "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"},
        }

    # 3. Pull just the year out of the ISO date string (e.g. "1955-04-12" -> 1955)
    birth_year = date.fromisoformat(dob).year

    # 4. Find the band whose range contains this birth year
    for band in RMD_RULES:
        if band["birth_year_min"] <= birth_year <= band["birth_year_max"]:
            return {
                "rule_id": "rmd_start_age",
                "decision": {"rmd_start_age": band["rmd_age"]},
                "source_doc": "VG-OP-003 SS3",
                "inputs_used": {"birth_year": birth_year},
            }

    # 5. Fallback: no band matched (see the edge-case note below)
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

        if ceiling is None:
            meets_ceiling = True
        else:
            meets_ceiling = debit_balance <= ceiling

        if meets_floor and meets_ceiling:
            return {
                "rule_id": "margin_interest_rate",
                "decision": {
                    "annual_rate_pct": rate
                },
                "source_doc": "VG-OP-009 SS4",
                "inputs_used": {
                    "debit_balance": debit_balance
                }
            }

    return {
        "error": (
            f"debit_balance {debit_balance} "
            "matched no margin tier"
        )
    }

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
                "missing_field": "date_of_birth"
            }
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

    eligible = (meets_age_requirement and meets_amount_requirement)

    if eligible:
        decision = "eligible"
    else:
        decision = "not_eligible"

    return {
        "rule_id": "qcd_limit",
        "decision": decision,
        "source_doc": "VG-OP-003 SS7",
        "inputs_used": {
            "age_years": round(age_years, 1),
            "amount": amount
        }
    }

def temp_hold(account, transaction_id, account_id):
    if account is None or transaction_id is None:
        return {
            "error": (
                "temporary_hold_status requires "
                "account_id and transaction_id"
            )
        }
     
    # Find the transaction we are checking.
    transaction = None

    transactions = data_manager.transactions_for(account_id)

    for current_transaction in transactions:

        if (current_transaction["transaction_id"]== transaction_id):
            transaction = current_transaction
            break

    # If we cannot find the transaction,or there is no hold date, the transaction is not currently considered to be on hold.
    if transaction is None:
        return {
            "rule_id": "temporary_hold_status",
            "decision": "not_on_hold",
            "source_doc": "VG-OP-013 SS2.5",
            "inputs_used": {}
        }

    hold_start_date = transaction.get("hold_start_date")

    if not hold_start_date:
        return {
            "rule_id": "temporary_hold_status",
            "decision": "not_on_hold",
            "source_doc": "VG-OP-013 SS2.5",
            "inputs_used": {}
        }

    # Calculate how many business days have passed
    # since the hold started.
    start_date = date.fromisoformat(hold_start_date)

    business_days_elapsed = _business_days_between(start_date,date.today())

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
            "business_days_elapsed": business_days_elapsed
        }
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

#-----------------------------------------------
#Harness-side writers
#
#These are the only functions that touch the servicing system, and the
#approval gate is the only caller. The Fulfillment Agent has no path to
#them: it proposes, a representative approves, the harness executes.
#-----------------------------------------------

@register_executor("service_case")
def _execute_service_case(action: ProposedAction, payload: dict) -> dict:
    # --- tracing: this is the actual WRITE step, only ever called by the
    # approval gate after human approval -- wraps the existing body, no
    # logic change.
    with tool_span("_execute_service_case", json.dumps({"account_id": payload["account_id"], "case_type": payload["case_type"]})) as span:
        entry = _log_action(
            "service_case",
            payload["account_id"],
            {
                "case_type": payload["case_type"],
                "description": payload["description"],
            },
        )
        result = {
            "message": (
                f"Service case ({payload['case_type']}) filed for "
                f"account {payload['account_id']}."
            ),
            "log_entry": entry,
        }
        span.record_result(json.dumps(result, default=str))
        return result


@register_executor("advisor_callback")
def _execute_advisor_callback(action: ProposedAction, payload: dict) -> dict:
    # --- tracing: actual WRITE step, approval-gate-only caller ---
    with tool_span("_execute_advisor_callback", json.dumps({"account_id": payload["account_id"], "reason": payload["reason"]})) as span:
        entry = _log_action(
            "advisor_callback",
            payload["account_id"],
            {
                "reason": payload["reason"],
                "preferred_time": payload.get("preferred_time"),
            },
        )
        result = {
            "message": (
                f"Callback scheduled for account "
                f"{payload['account_id']}. Reason: {payload['reason']}."
            ),
            "log_entry": entry,
        }
        span.record_result(json.dumps(result, default=str))
        return result


@register_executor("correspondence")
def _execute_correspondence(action: ProposedAction, payload: dict) -> dict:
    # --- tracing: actual WRITE step, approval-gate-only caller ---
    with tool_span("_execute_correspondence", json.dumps({"account_id": payload["account_id"], "template_id": payload["template_id"]})) as span:
        entry = _log_action(
            "correspondence",
            payload["account_id"],
            {
                "template_id": payload["template_id"],
                "rendered_text": payload["rendered_text"],
            },
        )
        result = {
            "message": (
                f"Correspondence ({payload['template_id']}) sent for "
                f"account {payload['account_id']}."
            ),
            "log_entry": entry,
        }
        span.record_result(json.dumps(result, default=str))
        return result


def _propose(action_type: str, payload: dict, reason: str,
             requested_account_id: Optional[str]) -> dict:
    """
    Shared proposal path: bind the subject, build the typed action, park
    it at the approval gate. No side effect on the servicing system.
    """
    try:
        ctx, account_id = account_gate.session_bound_account(
            ACTION_PROPOSE,
            requested_account_id,
        )
    except GATE_ERRORS as e:
        return {"status": "error", "message": str(e)}

    payload = {**payload, "account_id": account_id}

    action = new_proposed_action(
        action_type=action_type,
        payload=payload,
        reason=reason,
        ctx=ctx,
    )

    try:
        return approval_gate.submit(action)
    except GATE_ERRORS as e:
        return {"status": "error", "message": str(e)}


#Tools for the Fulfillment Agent
@tool(name="propose_service_case",
    description="""Draft a proposed service case for the representative to review. Takes the case type, a short summary of the issue, and a priority level, and returns a typed ProposedAction with no side effect. The case is only opened after the representative approves the proposal — this tool never writes to the servicing system itself.""",)
def propose_service_case(
    case_type: Annotated[str, "Type of the service case"],
    description: Annotated[str, "Free-text description of the service request"],
    account_id: Annotated[Optional[str], "Account the case is for; must belong to the authenticated session. Omit to use the session-bound account."] = None) -> dict:
    """Draft a service case for representative approval. Nothing is filed
    until the proposal is approved through the approval gate."""
    # --- tracing: wraps the existing body, no logic change. This now
    # returns a dict (via _propose()), not a plain string like the
    # previous version of this file -- record_result gets real JSON.
    with tool_span("propose_service_case", json.dumps({"case_type": case_type, "account_id": account_id})) as span:
        result = _propose(
            "service_case",
            {"case_type": case_type, "description": description},
            f"Open a {case_type} service case: {description}",
            account_id,
        )
        span.record_result(json.dumps(result))
        return result


@tool(name="propose_advisor_callback",
        description= """Evaluate whether the client's inquiry requires advisor involvement and recommend an advisor callback when appropriate, including the reason, urgency, and relevant context for the advisor.""" )

def propose_advisor_callback(
    reason: Annotated[str, "One of: wire_callback_required, fraud_review, "
                            "financial_planning_referral, general_inquiry"],
    preferred_time: Annotated[Optional[str], "Investor's preferred callback time, if given"] = None,
    account_id: Annotated[Optional[str], "Account the callback is for; must belong to the authenticated session. Omit to use the session-bound account."] = None,
) -> dict:
    """Draft an advisor callback request for representative approval.
    Nothing is scheduled until the proposal is approved."""
    # --- tracing: wraps the existing body, no logic change ---
    with tool_span("propose_advisor_callback", json.dumps({"reason": reason, "account_id": account_id})) as span:
        result = _propose(
            "advisor_callback",
            {"reason": reason, "preferred_time": preferred_time},
            f"Advisor callback requested: {reason}",
            account_id,
        )
        span.record_result(json.dumps(result))
        return result

@tool(name="propose_correspondence",
    description="""Propose outbound correspondence to the investor by selecting an approved template and supplying its merge fields.
    Returns a typed ProposedAction for human approval""")
def propose_correspondence(
    template_id: Annotated[str, f"One of: {', '.join(CORRESPONDENCE_TEMPLATES)}"],
    template_fields: Annotated[dict, "Values for the template's required fields"],
    account_id: Annotated[Optional[str], "Account the correspondence is for; must belong to the authenticated session. Omit to use the session-bound account."] = None,
) -> dict:
    """Draft correspondence from a pre-approved template for representative
    approval. Cannot draft free-form text; unknown templates or missing
    fields are rejected. Nothing is sent until the proposal is approved.
    """
    # --- tracing: wraps the existing body, no logic change ---
    with tool_span("propose_correspondence", json.dumps({"template_id": template_id, "account_id": account_id})) as span:
        template = CORRESPONDENCE_TEMPLATES.get(template_id)
        if template is None:
            result = {"status": "error",
                    "message": f"Unknown template_id: {template_id}"}
            span.record_result(json.dumps(result))
            return result
        missing = [f for f in template["required_fields"] if f not in template_fields]
        if missing:
            result = {"status": "error",
                    "message": f"Missing required template fields: {missing}"}
            span.record_result(json.dumps(result))
            return result
        body = template["body"].format(**{k: template_fields[k] for k in template["required_fields"]})
        # Rendered at proposal time so the approver reviews the exact text
        # that will go out.
        rendered = f"{body}\n\n---\n{template['required_disclosure']}"
        result = _propose(
            "correspondence",
            {
                "template_id": template_id,
                "template_fields": template_fields,
                "rendered_text": rendered,
            },
            f"Send approved template {template_id} to the investor on file",
            account_id,
        )
        span.record_result(json.dumps(result))
        return result




# print(result)
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true",
                        help="Serve over HTTP (default behaviour for this API).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    return parser

if __name__ == "__main__":
#     # import inspect

#     # print(inspect.signature(FastMCP))

#     # print(inspect.signature(FastMCP.run))
#     # mcp.run(transport="streamable-http")
    import uvicorn

    args = build_parser().parse_args()
    print(f"Serving HTTP on http://{args.host}:{args.port}  (docs at /docs)")
    uvicorn.run(app, host=args.host, port=args.port)
    