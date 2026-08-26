import os
from dotenv import load_dotenv
from pathlib import Path
import asyncio

#SDK specific imports
from agent_framework import tool, Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from typing import Annotated
from pydantic import Field
from har import validate_account_response, summarize_account, LoopDetectionMiddleware
from har import MaxIterationMiddleware, Backoffandretry
import json
from tools_1 import propose_service_case, propose_advisor_callback, propose_correspondence
from action_store import action_store
from gates.approval_gate import approval_gate

# --- tracing: added, no functional change below this line. The three
# propose_* tools are instrumented directly inside tools_1.py.
from tracing_provider import configure_tracing, traced_run, agent_span, set_interaction, get_tracer, production_exporters

load_dotenv()



# async def process_data(prompt, data):
#     client = FoundryChatClient(
#         project_endpoint= os.getenv("PROJECT_ENDPOINT"),
#         model=os.getenv("MODEL_DEPLOYMENT"),
#         credential=AzureCliCredential()
#     )

#     #initialize an agent and give it tools and instructions
#     async with(
#         Agent(
#             client=client,
#             name = "Fulfillment Agent",
#             instructions = """You are a fulfillment agent.
#                           Your job is to draft correspondence from approved templates, open service cases, request advisor callbacks.""",
#             tools=[propose_service_case, propose_advisor_callback, propose_correspondence]
#         ) as agent
#     ):
#         #use the agent to process the data
#         try:
#             prompt_messages = [f"{prompt}: \n {data}"]
#             response = await agent.run(prompt_messages)
#             print(f"Agent: \n {response}")
#         except Exception as e:
#             print(e)






# --- tracing: named for reuse by agent_span() below, same value either way ---
MODEL_NAME = os.getenv("MODEL_41")

client = FoundryChatClient(
        project_endpoint= os.getenv("ENDPOINT"),
        model=MODEL_NAME,
        credential=AzureCliCredential(),
        
    )
fulfillmentAgent =  Agent(
            client=client,
            name = "FulfillmentAgent",
            instructions = """You are a fulfillment agent.
                          Your job is to draft correspondence from approved templates, open service cases, and request advisor callbacks.

                          You propose actions. You never execute them.

                          Every propose_* tool returns a pending_approval
                          envelope with an action_id. A representative must
                          approve that action before anything is written.

                          You MUST:
                          - Report the action as prepared and awaiting approval.
                          - Include the action_id in your response.
                          - Use only approved correspondence templates.

                          You MUST NOT:
                          - Say or imply that a case was opened, a callback was
                            scheduled, or correspondence was sent.
                          - Draft free-form correspondence.
                          - Retry a proposal that already returned an action_id.""",
            tools=[propose_service_case, propose_advisor_callback, propose_correspondence],
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

            # Defence in depth: if the agent hands back a ProposedAction
            # object instead of the tool envelope, the gate still parks
            # it for approval rather than letting it through.
            # --- tracing: approval_gate isn't a tool call, so this uses a
            # plain span rather than tool_span/agent_span -- same pattern
            # as team.py's own ad-hoc "route_ticket" span in the reference.
            with get_tracer().start_as_current_span("approval_gate.process"):
                gated = await approval_gate.process(response)

            pending = [
                action.model_dump(mode="json")
                for action in action_store.pending()
            ]

            return {
                "status": (
                    "pending_approval" if pending else "no_action_proposed"
                ),
                "draft": str(gated),
                "pending_actions": pending,
                "message": (
                    "Nothing has been written. Approve an action_id "
                    "through the approval gate to execute it."
                ),
            }

        except Exception as e:
            print(e)
            raise


prompt = (
    "Open a service case for a missing dividend payment on the caller's account."
)
if __name__ == "__main__":
    # Proposals are bound to the authenticated session, and the
    # representative approves them after the run.
    from auth import build_caller_context, caller_context_scope

    ctx = build_caller_context(
        subject_party_id="PTY-00001",
        user_id="REP-LOCAL",
    )

    # --- tracing: added, no functional change below this line ---
    provider = configure_tracing(exporters=production_exporters())
    interaction_token = set_interaction(ctx.subject_party_id)

    with caller_context_scope(ctx):
        with traced_run("fulfillment_agent_standalone_run"):
            print(asyncio.run(process_data(fulfillmentAgent, prompt)))

            # --- tracing: one span for the whole interactive approval
            # review, with one event per decision -- mirrors
            # orchestrator.py's review_pending_actions.
            with get_tracer().start_as_current_span("approval_review") as review_span:
                pending_actions = action_store.pending()
                review_span.set_attribute("pending_count", len(pending_actions))
                for action in pending_actions:
                    print(f"\n{action.action_type}: {action.reason}")
                    print(f"payload: {action.payload}")

                    if input("Approve and execute? [y/N] ").strip().lower() == "y":
                        result = approval_gate.approve_and_execute(
                            action.action_id,
                            approver=ctx.user_id,
                            approver_ctx=ctx,
                        )
                        print(result)
                        review_span.add_event("action_reviewed", {
                            "action_id": action.action_id,
                            "action_type": action.action_type,
                            "decision": "approved",
                        })
                    else:
                        print("Left pending. Nothing was written.")
                        review_span.add_event("action_reviewed", {
                            "action_id": action.action_id,
                            "action_type": action.action_type,
                            "decision": "skipped",
                        })
    provider.shutdown()
