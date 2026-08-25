import os
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import json

#SDK specific imports
from agent_framework import tool, Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from typing import Annotated
from pydantic import Field
import contextvars
# from auth import current_caller_context, require_entitlement
# from services import servicing
import json
from tools_1 import get_account_summary, get_positions, get_recent_transactions, get_account_registrations, mcp
# from tools import search_knowledge_base
from typing import Annotated, Any, Callable, Optional
from azure.ai.projects.models import PromptAgentDefinition, MCPTool
from har import validate_account_response, summarize_account, LoopDetectionMiddleware
from har import MaxIterationMiddleware, Backoffandretry

# --- tracing: the four tools below are instrumented directly inside
# tools_1.py (get_account_summary, get_positions, get_recent_transactions,
# get_account_registrations each wrap their own body in tool_span). tool_1
# (the MCPTool) still executes remotely and can't be traced from this file.
from tracing_provider import configure_tracing, traced_run, agent_span

load_dotenv()





DATA_DIR = Path(__file__).resolve().parent.parent / "data"
print(f"Data directory: {DATA_DIR}")

# from mcp.server import FastMCP

# mcp = FastMCP("Demo")

#Read JSONs



# Rules-engine constants

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


# tool_1 = MCPTool(
#         server_label="api-specs",
#         server_url="https://learn.microsoft.com/api/mcp",
#         require_approval="require_approval",
#     )

tool_1 = MCPTool(
server_label="shared-tools",
server_url="http://127.0.0.1:8000/mcp",
require_approval= "never"
)
# --- tracing: named for reuse by agent_span() below, same value either way ---
MODEL_NAME = os.getenv("MODEL_MINI")

client = FoundryChatClient(
        project_endpoint= os.getenv("ENDPOINT"),
        model=MODEL_NAME,
        credential=AzureCliCredential()
    )
accountAgent= Agent(
            client=client,
            name = "AccountAgent",
            instructions = """You are an account agent.
                          Your job is to retrieve the caller's servicing facts — positions, recent transactions, delivery preferences, beneficiary and TCP status — and hand them to the rules engine.""",
            tools=[get_account_summary, get_positions, get_recent_transactions, get_account_registrations, tool_1 ],
            middleware= [LoopDetectionMiddleware(),MaxIterationMiddleware(10), Backoffandretry()],
            require_per_service_call_history_persistence=True
           
        ) 




async def process_data(agent, prompt: str):

    async with agent:
        try:
            # --- tracing: wraps the existing call, does not change it ---
            with agent_span(agent.name, deployment=MODEL_NAME):
                response = await agent.run([prompt])

            print("Response type:", type(response))
            print("Response repr:", repr(response))
            print("Response string:", str(response))
            print("Response attributes:", dir(response))
            response_json = json.loads(str(response))
            validated = validate_account_response(
                response_json
            )

            summary = summarize_account(validated)

            return {
                "validated_state": validated,
                "summary": summary
            }

        except Exception as e:
            print(e)
            raise


if __name__ == "__main__":
    prompt = """
            My account id is ACC-00001.

            Return ONLY JSON.

            Schema:

            {
                "account_id": "",
                "customer_name": "",
                "account_type": "",
                "status": "",
                "cash_balance": 0,
                "debit_balance": 0,
                "margin_enabled": false
            }
            """

    prompt_2 = """Can you use the MCP tools to figure out whether I can deposit today $50,000 (using rule_name: WIRE_ACH_DAILY_LIMIT_INCOMING) with account_id ACC-00001.
            Return ONLY JSON.

            Schema:

            {
                "account_id": "",
                "customer_name": "",
                "account_type": "",
                "status": "",
                "cash_balance": 0,
                "debit_balance": 0,
                "margin_enabled": false,
                "can_deposit_50k_today": false
            }
            """
    # --- tracing: standalone run only; no effect when imported by orchestrator.py ---
    provider = configure_tracing()
    with traced_run("account_agent_standalone_run"):
        asyncio.run(process_data(accountAgent, prompt=prompt))
    provider.shutdown()
