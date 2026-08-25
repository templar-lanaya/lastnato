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

# --- tracing: the three tools above are instrumented directly inside
# tools_1.py (each wraps its own body in tool_span) -- see that file's
# caveat about plain-string return shapes limiting error detection there.
from tracing_provider import configure_tracing, traced_run, agent_span

load_dotenv()

async def main():
    #load the expense data
    script_dir = Path(__file__).parent
    data_path = script_dir / "Data" / "expenses.txt"

    with data_path.open('r') as f:
        data = f.read()


    user_prompt = input(f"Here's your expenses data: \n {data} \n What would you like me to do with it?")

    #Run our async agent code
    await process_data(user_prompt, data)

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
                          Your job is to draft correspondence from approved templates, open service cases, request advisor callbacks.""",
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


prompt = "Please analyze the following expenses data and provide a summary."
if __name__ == "__main__":
    # --- tracing: standalone run only; no effect when imported by orchestrator.py ---
    provider = configure_tracing()
    with traced_run("fulfillment_agent_standalone_run"):
        asyncio.run(process_data(fulfillmentAgent, prompt))
    provider.shutdown()
