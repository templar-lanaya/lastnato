"""MCP server -- the shared tool surface.

Runs evaluate_policy_rule as a standalone service, reachable by any
agent (Knowledge, Account, or anything else) over the MCP protocol,
regardless of what framework that agent is built with. This is what
solves the "shared across agents built by different people/frameworks"
problem -- an import can only be shared within one process; an MCP
server can be shared across processes and frameworks.

Calls policy_engine.evaluate_policy_rule() for the actual logic -- this
file owns transport/protocol only, not rule content. If a rule is wrong,
fix it in policy_engine.py, not here.

NOTE ON AUTH: per the brief, "MCP validates tokens" is a real requirement
-- but that's a DIFFERENT, coarser check than the per-account entitlement
logic that was intentionally removed from the tool layer earlier ("is
this caller a legitimate agent talking to this server at all," not "is
this caller entitled to this specific account"). Not wired up in this
first pass (matches PROVE-02's hello-world scope) -- flagged so it isn't
mistaken for a decision to skip it permanently.

TRANSPORT: supports both stdio and HTTP, selected at launch time --
neither is "the real one," they serve different callers:

  stdio (default, `python3 mcp_server.py`)
    For MCPStdioTool (agent_framework's in-process Agent/ChatAgent) and
    for manual_test_client.py / test_mcp_client.py -- anything that runs
    ON THE SAME MACHINE and can spawn this file as a subprocess.

  HTTP (`python3 mcp_server.py --http`)
    Required for Foundry Persistent Agents (azure.ai.agents.models.MCPTool
    / azure.ai.projects.models.MCPTool). Foundry's runtime executes agents
    server-side, inside Azure -- it cannot spawn a subprocess on your
    machine, so MCPTool only accepts a remote server_url. Per Microsoft's
    own docs, this means the server needs an actual network endpoint --
    self-hosted on Azure Container Apps or Azure Functions -- not just
    "run with --http on a laptop." This flag is what makes that possible;
    it does NOT by itself make this Foundry-reachable until it's deployed
    somewhere with a real, reachable URL.

Run directly to serve over stdio:
    python3 mcp_server.py
Run over HTTP (for local testing of the HTTP path, or once containerized):
    python3 mcp_server.py --http [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import os

from fastmcp import FastMCP

import policy_engine

app = FastMCP(name="investor-services-shared-tools")

_store = policy_engine.Store()  # this process's own instance -- see policy_engine.py docstring


@app.tool
def evaluate_policy_rule(
    rule_name: str,
    account_id: str | None = None,
    amount: float | None = None,
    transaction_id: str | None = None,
) -> dict:
    """Run a deterministic Vanterra policy rule (never an LLM judgment).

    rule_name: one of fee_waiver, wire_callback_required, ach_outgoing_limit,
    ach_incoming_limit, rmd_start_age, margin_interest_rate,
    mobile_check_deposit_limit, qcd_limit, temporary_hold_status.

    Returns {rule_id, decision, source_doc, inputs_used}, or {"error": "..."}
    for a malformed call.
    """
    return policy_engine.evaluate_policy_rule(
        _store, rule_name, account_id=account_id, amount=amount, transaction_id=transaction_id,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true",
                         help="Serve over HTTP instead of stdio -- required for Foundry "
                              "Persistent Agents (MCPTool only accepts a remote server_url).")
    parser.add_argument("--host", default="0.0.0.0")
    # Azure Container Apps injects the listen port via the PORT env var --
    # honor it if present, so this doesn't need editing per-environment.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()

    if args.http:
        app.run(transport="http", host=args.host, port=args.port)
    else:
        app.run()  # stdio
