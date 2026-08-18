"""Investor Services Copilot -- tool surface.

Matches the team's existing tools.py pattern: TOOL_SCHEMAS (OpenAI
function-calling format) + handler functions + dispatch(deps, name,
arguments) -> JSON string. Whoever owns the agent loop just needs:

    result_json = dispatch(deps, tool_call.function.name, tool_call.function.arguments)

Nothing else. No exceptions escape dispatch(); errors come back as a JSON
{"error": "..."} payload, same as the reference file's ApiError handling.

OUT OF SCOPE for this module, by design:
  - The agent loop / orchestration (owned by teammates)
  - The knowledge-base chunking/retrieval implementation (owned by a
    teammate) -- search_knowledge_base calls into ToolDeps.retriever,
    a pluggable callable this module does not implement. This module
    still owns the REFUSE-BELOW-THRESHOLD business rule; the retriever
    just returns scored candidates.

SESSION BINDING -- see the clearly-marked section below. bind_session() /
current_session() are the ONLY two things in this file that know about
per-call identity (rep_id/party_id/interaction_id). Every handler reads
identity via current_session() rather than receiving it as a parameter,
so dispatch()'s signature stays exactly (deps, name, arguments) -- nothing
for the agent-loop team to thread through. If your team ends up binding
sessions a different way, current_session()'s body is the only thing that
needs to change; no handler below touches the contextvar directly.
"""
from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

# ==========================================================================
# SESSION BINDING -- removable seam. Nothing outside this block touches
# the contextvar directly; every handler below calls current_session().
# ==========================================================================

@dataclass(frozen=True)
class SessionContext:
    rep_id: str
    party_id: str          # the ONE subject this session is bound to
    interaction_id: str


class SessionNotBound(Exception):
    """current_session() was called before bind_session() -- fails loudly
    rather than letting entitlement checks silently no-op.
    """


_session_var: ContextVar[Optional[SessionContext]] = ContextVar("_session_var", default=None)


def bind_session(rep_id: str, party_id: str, interaction_id: str) -> None:
    """Call once, upstream, before the tool loop starts for this
    interaction. Uses a contextvar (not a plain global) so concurrent
    sessions in separate async tasks don't bleed into each other.
    """
    _session_var.set(SessionContext(rep_id=rep_id, party_id=party_id, interaction_id=interaction_id))


def current_session() -> SessionContext:
    ctx = _session_var.get()
    if ctx is None:
        raise SessionNotBound("No session bound -- call bind_session() before dispatching tool calls.")
    return ctx


# ==========================================================================
# Data access -- the "client" this module's dispatch() is threaded with,
# same role httpx.Client plays in the reference file.
# ==========================================================================

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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
        self.proposed_actions: list[dict] = []  # in-memory stand-in for the durable ProposedAction store

    def positions_for(self, account_id: str) -> list[dict]:
        return [p for p in self.positions if p["account_id"] == account_id]

    def transactions_for(self, account_id: str) -> list[dict]:
        return [t for t in self.transactions if t["account_id"] == account_id]

    def beneficiaries_for(self, account_id: str) -> list[dict]:
        return [b for b in self.beneficiaries if b["account_id"] == account_id]

    def accounts_for_party(self, party_id: str) -> list[dict]:
        return [a for a in self.accounts if a["party_id"] == party_id]


# Retriever contract: (query, top_k) -> [{"doc_id", "chunk_id", "text", "score"}, ...]
# Implementation is a teammate's; this module only enforces the refusal
# threshold against whatever candidates come back.
RetrieverFn = Callable[[str, int], list[dict[str, Any]]]


@dataclass
class ToolDeps:
    """The single object threaded through dispatch() and every handler --
    same role `client` plays in the reference tools.py.
    """
    store: Store
    retriever: Optional[RetrieverFn] = None


# ==========================================================================
# Errors -- caught inside dispatch(), never raised out to the agent loop.
# ==========================================================================

class EntitlementDenied(Exception):
    """Same message for 'no such account' and 'not entitled' -- avoids
    account-enumeration by error-message difference (SEC-06/07).
    """


class RetrievalRefused(Exception):
    """No candidate cleared the confidence threshold -- a refusal, not an
    empty result. Callers must not fall back to model memory on this.
    """


class ToolNotConfigured(Exception):
    """A required dependency (e.g. ToolDeps.retriever) wasn't wired up."""


# ==========================================================================
# Entitlement resolution.
# READS are session-binding only (account.party_id == ctx.party_id) --
# any rep servicing the bound party can read, matching real contact-center
# behavior. WRITES additionally require account.assigned_rep_id == ctx.rep_id.
# See conversation history for the full rationale on this split.
# ==========================================================================

def _resolve_account_for_read(store: Store, ctx: SessionContext, account_id: str) -> dict:
    account = store.accounts_by_id.get(account_id)
    if account is None or account["party_id"] != ctx.party_id:
        raise EntitlementDenied(f"No such account: {account_id}")
    return account


def _resolve_account_for_write(store: Store, ctx: SessionContext, account_id: str) -> dict:
    account = _resolve_account_for_read(store, ctx, account_id)
    if account["assigned_rep_id"] != ctx.rep_id:
        raise EntitlementDenied(f"No such account: {account_id}")
    return account


# ==========================================================================
# Rules-engine constants (VG-OP-* sourced, see prior seed.py citations)
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

_RETRIEVAL_THRESHOLD = 0.34  # placeholder pending Wave-2 calibration -- not owned by the retriever


def _business_days_between(start: date, end: date) -> int:
    d, n = start, 0
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ==========================================================================
# TOOL_SCHEMAS -- OpenAI function-calling format. This is the contract the
# agent-loop team binds to.
# ==========================================================================

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search Vanterra product and policy documents. Returns cited "
                            "passages, or refuses if nothing meets the confidence threshold -- "
                            "never falls back to unsourced knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_summary",
            "description": "Get account summary/balances for the current caller's accounts. "
                            "Omit account_id for a summary across ALL of the caller's accounts.",
            "parameters": {
                "type": "object",
                "properties": {"account_id": {"type": "string", "description": "e.g. ACC-00001"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": "Get fund holdings for one account.",
            "parameters": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_transactions",
            "description": "Get the most recent transactions for one account, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_registrations",
            "description": "Get account registration/titling info and beneficiary designations "
                            "for one account.",
            "parameters": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_policy_rule",
            "description": "ACTION tool -- runs a deterministic Vanterra policy rule (never an "
                            "LLM judgment). Returns {rule_id, decision, source_doc, inputs_used}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_name": {
                        "type": "string",
                        "enum": ["fee_waiver", "wire_callback_required", "rmd_start_age",
                                 "margin_interest_rate", "mobile_check_deposit_limit",
                                 "qcd_limit", "temporary_hold_status"],
                    },
                    "account_id": {"type": "string"},
                    "amount": {"type": "number"},
                    "debit_balance": {"type": "number"},
                    "transaction_id": {"type": "string"},
                },
                "required": ["rule_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_service_case",
            "description": "ACTION tool (PROPOSE ONLY -- no side effect). Drafts a service case "
                            "for human approval. Does not open a case.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "case_type": {
                        "type": "string",
                        "enum": ["beneficiary_update", "address_change", "poa_review",
                                 "fraud_review", "inactive_account_reactivation", "general_servicing"],
                    },
                    "description": {"type": "string", "maxLength": 2000},
                },
                "required": ["account_id", "case_type", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_advisor_callback",
            "description": "ACTION tool (PROPOSE ONLY -- no side effect). Drafts a callback "
                            "request for human approval. Does not schedule a callback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "enum": ["wire_callback_required", "fraud_review",
                                 "financial_planning_referral", "general_inquiry"],
                    },
                    "preferred_time": {"type": "string"},
                },
                "required": ["account_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_correspondence",
            "description": "ACTION tool (PROPOSE ONLY -- no side effect). Fills a pre-approved "
                            "correspondence template's typed fields. Cannot draft free-form text; "
                            "unknown templates or missing fields are rejected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "template_id": {
                        "type": "string",
                        "enum": list(CORRESPONDENCE_TEMPLATES.keys()),
                    },
                    "template_fields": {"type": "object"},
                },
                "required": ["account_id", "template_id", "template_fields"],
            },
        },
    },
]


# ==========================================================================
# Handlers -- deps injected explicitly (same pattern as `client` in the
# reference), identity read via current_session(), never as a parameter.
# ==========================================================================

def _run_search_knowledge_base(deps: ToolDeps, query: str, top_k: int = 3) -> dict:
    if deps.retriever is None:
        raise ToolNotConfigured("ToolDeps.retriever is not wired up -- search_knowledge_base cannot run.")
    candidates = deps.retriever(query, top_k)
    results = [c for c in candidates if c.get("score", 0) >= _RETRIEVAL_THRESHOLD][:top_k]
    if not results:
        raise RetrievalRefused(f"No corpus passage met the confidence threshold ({_RETRIEVAL_THRESHOLD}).")
    return {"results": results}


def _run_get_account_summary(deps: ToolDeps, account_id: Optional[str] = None) -> dict:
    ctx = current_session()
    store = deps.store
    accounts = [_resolve_account_for_read(store, ctx, account_id)] if account_id else store.accounts_for_party(ctx.party_id)
    household_total = sum(a["cash_balance"] for a in store.accounts_for_party(ctx.party_id))

    def _summarize(a: dict) -> dict:
        return {k: a[k] for k in (
            "account_id", "account_type", "status", "opened_date",
            "cash_balance", "margin_enabled", "debit_balance", "assigned_rep_id",
        )}

    return {"accounts": [_summarize(a) for a in accounts], "household_total_balance": household_total}


def _run_get_positions(deps: ToolDeps, account_id: str) -> dict:
    ctx = current_session()
    _resolve_account_for_read(deps.store, ctx, account_id)
    products = deps.store.products_by_ticker
    rows = []
    for pos in deps.store.positions_for(account_id):
        product = products.get(pos["ticker"])
        rows.append({
            "ticker": pos["ticker"],
            "fund_name": product["fund_name"] if product else None,
            "share_class": product["share_class"] if product else None,
            "quantity": pos["quantity"],
            "price": pos["price"],
            "market_value": round(pos["quantity"] * pos["price"], 2),
        })
    return {"account_id": account_id, "positions": rows}


def _run_get_recent_transactions(deps: ToolDeps, account_id: str, limit: int = 20) -> dict:
    ctx = current_session()
    _resolve_account_for_read(deps.store, ctx, account_id)
    txns = sorted(deps.store.transactions_for(account_id), key=lambda t: t["date"], reverse=True)
    return {"account_id": account_id, "transactions": txns[:limit]}


def _run_get_account_registrations(deps: ToolDeps, account_id: str) -> dict:
    ctx = current_session()
    account = _resolve_account_for_read(deps.store, ctx, account_id)
    beneficiaries = deps.store.beneficiaries_for(account_id)
    is_ira = account["account_type"] in ("traditional_ira", "roth_ira")
    return {
        "account_id": account_id,
        "account_type": account["account_type"],
        "beneficiaries": beneficiaries,
        "beneficiary_required_but_missing": is_ira and not beneficiaries,
    }


def _run_evaluate_policy_rule(deps: ToolDeps, rule_name: str, account_id: Optional[str] = None, **inputs: Any) -> dict:
    ctx = current_session()
    store = deps.store

    if rule_name == "fee_waiver":
        party = store.parties_by_id[ctx.party_id]
        household_total = sum(a["cash_balance"] for a in store.accounts_for_party(ctx.party_id))
        waived = household_total >= 5_000.00 or party["e_delivery_election"]
        return {"rule_id": "fee_waiver", "decision": "waived" if waived else "fee_applies",
                "source_doc": "VG-OP-017 SS3",
                "inputs_used": {"household_total_balance": household_total, "e_delivery_election": party["e_delivery_election"]}}

    if rule_name == "wire_callback_required":
        amount = inputs["amount"]
        return {"rule_id": "wire_callback_required",
                "decision": "callback_required" if amount >= 50_000.00 else "callback_not_required",
                "source_doc": "VG-OP-005 SS3.4", "inputs_used": {"amount": amount}}

    if rule_name == "rmd_start_age":
        party = store.parties_by_id[ctx.party_id]
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
        raise ValueError(f"birth_year {birth_year} matched no RMD band")

    if rule_name == "margin_interest_rate":
        debit_balance = _resolve_account_for_read(store, ctx, account_id)["debit_balance"] if account_id else inputs["debit_balance"]
        for floor, ceiling, rate in MARGIN_INTEREST_TIERS:
            if debit_balance >= floor and (ceiling is None or debit_balance <= ceiling):
                return {"rule_id": "margin_interest_rate", "decision": {"annual_rate_pct": rate},
                        "source_doc": "VG-OP-009 SS4", "inputs_used": {"debit_balance": debit_balance}}
        raise ValueError(f"debit_balance {debit_balance} matched no margin tier")

    if rule_name == "mobile_check_deposit_limit":
        _resolve_account_for_read(store, ctx, account_id)
        amount = inputs["amount"]
        today = date.today()
        month_ago = today - timedelta(days=30)
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
        party = store.parties_by_id[ctx.party_id]
        dob = party.get("date_of_birth")
        if dob is None:
            return {"rule_id": "qcd_limit", "decision": "insufficient_data", "source_doc": "VG-OP-003 SS7",
                    "inputs_used": {"date_of_birth": None, "missing_field": "date_of_birth"}}
        amount = inputs["amount"]
        age_years = (date.today() - date.fromisoformat(dob)).days / 365.25
        eligible = age_years >= 70.5 and amount <= 108_000.00
        return {"rule_id": "qcd_limit", "decision": "eligible" if eligible else "not_eligible",
                "source_doc": "VG-OP-003 SS7", "inputs_used": {"age_years": round(age_years, 1), "amount": amount}}

    if rule_name == "temporary_hold_status":
        _resolve_account_for_read(store, ctx, account_id)
        txn = next((t for t in store.transactions_for(account_id) if t["transaction_id"] == inputs["transaction_id"]), None)
        if txn is None or not txn.get("hold_start_date"):
            return {"rule_id": "temporary_hold_status", "decision": "not_on_hold", "source_doc": "VG-OP-013 SS2.5", "inputs_used": {}}
        elapsed = _business_days_between(date.fromisoformat(txn["hold_start_date"]), date.today())
        decision = "within_initial_hold" if elapsed <= 15 else "within_extension_window" if elapsed <= 40 else "must_release_or_escalate"
        return {"rule_id": "temporary_hold_status", "decision": decision, "source_doc": "VG-OP-013 SS2.5",
                "inputs_used": {"hold_start_date": txn["hold_start_date"], "business_days_elapsed": elapsed}}

    raise ValueError(f"Unknown rule_name: {rule_name}")


def _persist_proposed_action(store: Store, ctx: SessionContext, action_type: str, account_id: str, payload: dict) -> dict:
    action = {
        "action_id": f"ACT-{uuid.uuid4().hex[:12]}",
        "action_type": action_type,
        "account_id": account_id,
        "proposed_by_rep_id": ctx.rep_id,
        "interaction_id": ctx.interaction_id,
        "payload": payload,
        "status": "pending_approval",
        "idempotency_key": uuid.uuid4().hex,  # harness-generated, never model-supplied
        "created_at": datetime.utcnow().isoformat(),
    }
    store.proposed_actions.append(action)
    return action


def _run_propose_service_case(deps: ToolDeps, account_id: str, case_type: str, description: str) -> dict:
    if not (1 <= len(description) <= 2_000):
        raise ValueError("description must be 1-2000 characters")
    ctx = current_session()
    _resolve_account_for_write(deps.store, ctx, account_id)
    return _persist_proposed_action(deps.store, ctx, "service_case", account_id,
                                     {"case_type": case_type, "description": description})


def _run_propose_advisor_callback(deps: ToolDeps, account_id: str, reason: str, preferred_time: Optional[str] = None) -> dict:
    ctx = current_session()
    _resolve_account_for_write(deps.store, ctx, account_id)
    return _persist_proposed_action(deps.store, ctx, "advisor_callback", account_id,
                                     {"reason": reason, "preferred_time": preferred_time})


def _run_propose_correspondence(deps: ToolDeps, account_id: str, template_id: str, template_fields: dict) -> dict:
    ctx = current_session()
    _resolve_account_for_write(deps.store, ctx, account_id)
    template = CORRESPONDENCE_TEMPLATES.get(template_id)
    if template is None:
        raise ValueError(f"Unknown template_id: {template_id}")
    missing = [f for f in template["required_fields"] if f not in template_fields]
    if missing:
        raise ValueError(f"Missing required template fields: {missing}")
    body = template["body"].format(**{k: template_fields[k] for k in template["required_fields"]})
    rendered = f"{body}\n\n---\n{template['required_disclosure']}"
    return _persist_proposed_action(deps.store, ctx, "correspondence", account_id,
                                     {"template_id": template_id, "rendered_text": rendered})


_HANDLERS: dict[str, Callable[..., dict]] = {
    "search_knowledge_base": _run_search_knowledge_base,
    "get_account_summary": _run_get_account_summary,
    "get_positions": _run_get_positions,
    "get_recent_transactions": _run_get_recent_transactions,
    "get_account_registrations": _run_get_account_registrations,
    "evaluate_policy_rule": _run_evaluate_policy_rule,
    "propose_service_case": _run_propose_service_case,
    "propose_advisor_callback": _run_propose_advisor_callback,
    "propose_correspondence": _run_propose_correspondence,
}


# ==========================================================================
# dispatch() -- the one function the agent-loop team calls.
# ==========================================================================

def dispatch(deps: ToolDeps, name: str, arguments: str) -> str:
    """Run one tool call and return a JSON string result. No exception
    ever escapes this function -- errors come back as {"error": "..."}.
    """
    if name not in _HANDLERS:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        args = json.loads(arguments)
        result = _HANDLERS[name](deps, **args)
    except SessionNotBound as e:
        result = {"error": f"session_not_bound: {e}"}
    except EntitlementDenied as e:
        result = {"error": f"entitlement_denied: {e}"}
    except RetrievalRefused as e:
        result = {"error": f"retrieval_refused: {e}"}
    except ToolNotConfigured as e:
        result = {"error": f"tool_not_configured: {e}"}
    except (TypeError, KeyError, json.JSONDecodeError):
        result = {"error": "These arguments don't work"}
    except ValueError as e:
        result = {"error": f"invalid_request: {e}"}
    return json.dumps(result)
