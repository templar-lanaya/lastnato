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

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, agent_span, production_exporters

load_dotenv()

# async def main():

#     # script_dir = Path(__file__).parent
#     # data_path = script_dir / "Data" / "expenses.txt"

#     # with data_path.open('r') as f:
#     #     data = f.read()


#     user_prompt = "" #need to import this from the output of guardrail-checked representative request

#     await process_data(user_prompt)

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
            return response_json
            # validated = validate_account_response(
            #     response_json
            # )

            # summary = summarize_account(validated)

            # return {
            #     "validated_state": validated,
            #     "summary": summary
            # }

        except Exception as e:
            print(e)
            raise


# --- tracing: named for reuse by agent_span() below, same value either way ---
MODEL_NAME = os.getenv("MODEL_41")

client = FoundryChatClient(
        project_endpoint= os.getenv("ENDPOINT"),
        model=MODEL_NAME,
        credential=AzureCliCredential()
    )

intakeRouter =  Agent(
            client=client,
            name = "IntakeRouter",
            instructions = """You are an intake router agent. Your job is to classify the request recieved from the human representative, apply the permitted-scope gate, emit a structured routing decision to another agent. """,
            require_per_service_call_history_persistence=True
        )

''''NO TOOLS, DOES NOT ACT'''

prompt = "Please classify the following request and route it to the appropriate agent: I am having trouble accessing my account and need assistance with resetting my password."

if __name__ == "__main__":
    # --- tracing: only runs on a standalone `python intake_router_agent.py`.
    # Has no effect when this module is imported by orchestrator.py, since
    # that file never executes this block.
    provider = configure_tracing(exporters=production_exporters())
    with traced_run("intake_router_standalone_run"):
        asyncio.run(process_data(intakeRouter, prompt))
    provider.shutdown()
