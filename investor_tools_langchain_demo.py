"""DEMO ONLY -- mirrors triage_tools.py's style: one @tool-decorated
function per tool, no TOOL_SCHEMAS, no dispatch(). NOT the recommended
version -- see the trade-offs called out inline and summarized at the
bottom. This file exists so the team can compare the two shapes directly
before picking one.

Reuses the actual business logic from investor_tools.py (_run_* handlers,
Store, entitlement checks, rules engine) rather than reimplementing it --
the comparison here is about the WRAPPING layer, not the logic underneath.
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

import investor_tools as it

# --------------------------------------------------------------------------
# PROBLEM #3, made concrete: triage_tools.py imports its client at module
# level (`from support_agents.LCEL import api_client`) rather than
# receiving it as a parameter. Mirroring that faithfully means `deps` has
# to go back to being a module-level singleton -- exactly what ToolDeps
# was introduced to get away from. There's no way to keep per-call
# dependency injection AND match this style; the decorator's signature is
# fixed by what the model is allowed to pass, so `deps` can't be a
# parameter without also exposing it to the model as an argument.
# --------------------------------------------------------------------------
_deps = it.ToolDeps(store=it.Store(), retriever=None)


def _as_error(exc: Exception) -> dict:
    """PROBLEM #2, made concrete: dispatch() did this ONCE, centrally, for
    all nine tools. Here it has to be repeated inside every single
    decorated function below, or errors propagate uncaught into whatever
    loop calls tool.ainvoke() -- see triage_agent.py's bare
    `await tool.ainvoke(tool_call["args"])`, no try/except at all.
    """
    if isinstance(exc, it.SessionNotBound):
        return {"error": f"session_not_bound: {exc}"}
    if isinstance(exc, it.EntitlementDenied):
        return {"error": f"entitlement_denied: {exc}"}
    if isinstance(exc, it.RetrievalRefused):
        return {"error": f"retrieval_refused: {exc}"}
    if isinstance(exc, it.ToolNotConfigured):
        return {"error": f"tool_not_configured: {exc}"}
    if isinstance(exc, ValueError):
        return {"error": f"invalid_request: {exc}"}
    return {"error": "These arguments don't work"}


@tool
async def search_knowledge_base(query: str, top_k: int = 3) -> dict:
    """Search Vanterra product and policy documents. Returns cited passages,
    or refuses if nothing meets the confidence threshold -- never falls
    back to unsourced knowledge.
    """
    try:
        return it._run_search_knowledge_base(_deps, query, top_k)
    except Exception as e:
        return _as_error(e)


@tool
async def get_account_summary(account_id: Optional[str] = None) -> dict:
    """Get account summary/balances for the current caller's accounts.
    Omit account_id for a summary across ALL of the caller's accounts.
    """
    try:
        return it._run_get_account_summary(_deps, account_id)
    except Exception as e:
        return _as_error(e)


@tool
async def get_positions(account_id: str) -> dict:
    """Get fund holdings for one account."""
    try:
        return it._run_get_positions(_deps, account_id)
    except Exception as e:
        return _as_error(e)


@tool
async def get_recent_transactions(account_id: str, limit: int = 20) -> dict:
    """Get the most recent transactions for one account, newest first."""
    try:
        return it._run_get_recent_transactions(_deps, account_id, limit)
    except Exception as e:
        return _as_error(e)


@tool
async def get_account_registrations(account_id: str) -> dict:
    """Get account registration/titling info and beneficiary designations
    for one account.
    """
    try:
        return it._run_get_account_registrations(_deps, account_id)
    except Exception as e:
        return _as_error(e)


@tool
async def evaluate_policy_rule(rule_name: str, account_id: Optional[str] = None, **inputs) -> dict:
    """ACTION tool -- runs a deterministic Vanterra policy rule (never an
    LLM judgment). Returns {rule_id, decision, source_doc, inputs_used}.
    """
    try:
        return it._run_evaluate_policy_rule(_deps, rule_name, account_id, **inputs)
    except Exception as e:
        return _as_error(e)


@tool
async def propose_service_case(account_id: str, case_type: str, description: str) -> dict:
    """ACTION tool (PROPOSE ONLY -- no side effect). Drafts a service case
    for human approval. Does not open a case.
    """
    try:
        return it._run_propose_service_case(_deps, account_id, case_type, description)
    except Exception as e:
        return _as_error(e)


@tool
async def propose_advisor_callback(account_id: str, reason: str, preferred_time: Optional[str] = None) -> dict:
    """ACTION tool (PROPOSE ONLY -- no side effect). Drafts a callback
    request for human approval. Does not schedule a callback.
    """
    try:
        return it._run_propose_advisor_callback(_deps, account_id, reason, preferred_time)
    except Exception as e:
        return _as_error(e)


@tool
async def propose_correspondence(account_id: str, template_id: str, template_fields: dict) -> dict:
    """ACTION tool (PROPOSE ONLY -- no side effect). Fills a pre-approved
    correspondence template's typed fields. Cannot draft free-form text;
    unknown templates or missing fields are rejected.
    """
    try:
        return it._run_propose_correspondence(_deps, account_id, template_id, template_fields)
    except Exception as e:
        return _as_error(e)


# Mirrors triage_tools.py's `TRIAGE_TOOLS = [...]` list, for bind_tools().
INVESTOR_TOOLS = [
    search_knowledge_base, get_account_summary, get_positions, get_recent_transactions,
    get_account_registrations, evaluate_policy_rule, propose_service_case,
    propose_advisor_callback, propose_correspondence,
]


# --------------------------------------------------------------------------
# TRADE-OFFS, summarized (see full discussion in conversation history):
#
# 1. LangChain-specific. These are StructuredTool objects, not portable
#    data -- they only bind via bind_tools()/ainvoke(). TOOL_SCHEMAS +
#    dispatch() in investor_tools.py is framework-agnostic and matches the
#    assignment brief's Azure Agent Framework / no-LangGraph constraint.
#
# 2. Error handling is now per-function (_as_error() called nine times)
#    instead of once, centrally, in dispatch(). Forget it on a tenth tool
#    later and that one raises straight into the agent loop.
#
# 3. `_deps` is a module-level singleton again -- the exact pattern
#    ToolDeps/dispatch() were introduced to move away from. Swapping the
#    Store or wiring a real retriever means reaching into this module and
#    mutating _deps, not passing a parameter.
#
# What's IDENTICAL either way: TOOL_SCHEMAS's descriptions were written
# from the same docstrings, current_session()/bind_session() didn't
# change at all, and the entitlement/rules logic underneath is the exact
# same investor_tools.py code in both files -- this file adds a thinner
# wrapping layer, it doesn't reimplement anything.
# --------------------------------------------------------------------------
