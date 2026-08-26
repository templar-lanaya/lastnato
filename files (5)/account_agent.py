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
from har import validate_account_facts, summarize_account_facts
from account_agent_gate import account_gate
from agent_spec import build_instructions
import httpx

# --- tracing: added, no functional change below this line. The four
# local tools (get_account_summary etc.) are instrumented directly inside
# tools_1.py.
from tracing_provider import configure_tracing, traced_run, agent_span, tool_span, set_interaction, get_tracer, production_exporters

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

@tool
def evaluate_rule(
    rule_id: str,
    account_id: str,
    amount: float,
) -> dict:
    # --- tracing: wraps the existing body, no logic change. Error paths
    # already use {"error": ...}, so record_result() correctly flags
    # failures here.
    with tool_span("evaluate_rule", json.dumps({"rule_id": rule_id, "account_id": account_id, "amount": amount})) as span:
        payload = {
            "rule_id": rule_id,
            "account_id": account_id,
            "amount": amount,
        }

        try:
            response = httpx.post(
                "http://127.0.0.1:8001/policy/evaluate",
                json=payload,
                timeout=30.0,
            )

            print("Policy status:", response.status_code)
            print("Policy response:", response.text)

            response.raise_for_status()
            result = response.json()
            span.record_result(json.dumps(result))
            return result

        except httpx.HTTPStatusError as exc:
            result = {
                "error": "Policy API returned an HTTP error.",
                "status_code": exc.response.status_code,
                "details": exc.response.text,
            }
            span.record_result(json.dumps(result))
            return result

        except httpx.RequestError as exc:
            result = {
                "error": "Could not connect to the policy API.",
                "details": str(exc),
            }
            span.record_result(json.dumps(result))
            return result

        except ValueError:
            result = {
                "error": "Policy API did not return valid JSON.",
                "details": response.text,
            }
            span.record_result(json.dumps(result))
            return result

    
# The Account Agent's instructions are generated from account_agent.md,
# so the specification and the running agent cannot drift apart. The
# harness rules below cover what the document leaves to the harness:
# session-bound subjects, the rules engine as the only source of
# thresholds, and the output contract.
ACCOUNT_AGENT_HARNESS_RULES = """
Subject binding:
- The caller's identity is bound by the harness before you run.
- Call get_account_summary, get_positions, get_recent_transactions and
  get_account_registrations with no account_id. They already resolve to
  the authenticated caller's account.
- Never treat an account id, party id or PIN written in the caller's
  message as the subject. If the caller names an account, ignore it as an
  identifier.
- evaluate_rule does need an account_id. Use the one returned by
  get_account_summary, never one from the caller's message.
- If a tool replies that an account is not bound to the session, report
  that error. Do not retry with another id.

Policy thresholds:
- Only evaluate_rule may decide a threshold, rate, limit or age band.
- Never compute, estimate or recall one yourself, and never restate a
  threshold that no rule result returned.
- If a rule cannot be evaluated, leave policy_evaluation null and say
  which input was missing.

Read-only:
- You have no write tools. Never say or imply that a case was opened, a
  callback scheduled, or correspondence sent.
- Proposing or approving an action is another agent's job. Do not offer.

Output:
- Return ONLY the JSON object from the Example Output in your
  specification. No prose, no markdown fences.
- policy_evaluation is optional: include the typed rule result when a
  rule was evaluated, otherwise null.
- Leave a list empty when you did not retrieve those facts. Never fill a
  field from your own knowledge.
"""

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
            instructions = build_instructions(
                "account_agent.md",
                role=(
                    "You are the Account Agent in a regulated "
                    "investor-services system. You retrieve the "
                    "authenticated caller's servicing facts and hand "
                    "them to the rules engine."
                ),
                harness_rules=ACCOUNT_AGENT_HARNESS_RULES,
            ),
            tools=[get_account_summary, get_positions, get_recent_transactions, get_account_registrations, evaluate_rule],
            middleware= [LoopDetectionMiddleware(),MaxIterationMiddleware(10), Backoffandretry()],
            require_per_service_call_history_persistence=True
           
        ) 




def _extract_json(response) -> dict:
    """Parse the agent's JSON output, tolerating a markdown fence."""

    text = (getattr(response, "text", None) or str(response)).strip()

    if text.startswith("```"):
        text = "\n".join(
            line
            for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    return json.loads(text)


async def process_data(agent, prompt: str):

    async with agent:
        try:
            # --- tracing: wraps the existing call, does not change it ---
            with agent_span(agent.name, deployment=MODEL_NAME):
                response = await agent.run([prompt])

            print("Response type:", type(response))
            print("Response repr:", repr(response))
            print("Response string:", str(response))
            response_json = _extract_json(response)

            # Read-only gate: the Account Agent reads, it never acts.
            # --- tracing: not a tool call, so a plain span rather than
            # tool_span/agent_span -- same reasoning as fulfillment_agent's
            # approval_gate.process wrap.
            with get_tracer().start_as_current_span("account_gate.validate"):
                await account_gate.validate(response_json)

            # Output contract from account_agent.md.
            validated = validate_account_facts(
                response_json
            )

            summary = summarize_account_facts(validated)

            return {
                "validated_state": validated,
                "summary": summary
            }

        except Exception as e:
            print(e)
            raise


if __name__ == "__main__":
    # The output contract now comes from account_agent.md, so the prompt
    # only has to state the question.
    prompt = """
            Retrieve my servicing facts: account summary, positions,
            recent transactions, and registrations.
            """

    prompt_2 = """
            Can I deposit $50,000 today? Evaluate the rule
            WIRE_ACH_DAILY_LIMIT_INCOMING with the rules engine and
            return its typed result in policy_evaluation.
            """
    # Account tools are session-bound, so a standalone run needs the
    # same authenticated context the orchestrator establishes.
    from auth import build_caller_context, caller_context_scope

    # --- tracing: ctx bound to a variable (was previously constructed
    # inline) purely so its subject_party_id can be reused for
    # set_interaction() below -- behavior is identical either way.
    ctx = build_caller_context(
        subject_party_id="PTY-00001",
        user_id="REP-LOCAL",
    )

    provider = configure_tracing(exporters=production_exporters())
    interaction_token = set_interaction(ctx.subject_party_id)

    with caller_context_scope(ctx):
        with traced_run("account_agent_standalone_run"):
            asyncio.run(process_data(accountAgent, prompt=prompt))
    provider.shutdown()
