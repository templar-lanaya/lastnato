import json
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
# from tools import search_knowledge_base

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, agent_span, tool_span

load_dotenv()





###I think this is for knowledge retrieval, but the description is empty. I will fill it in with a brief description of what the tool does.
@tool(
    name="search_knowledge_base",
    description="",
    approval_mode="never_require",
)
def search_knowledge_base(
    to: Annotated[str, Field(description="The email address of the claim approver")],
    subject: Annotated[str, Field(description="The subject of the email")],
    body: Annotated[str, Field(description="The body of the email")]
):
    # NOTE (tracing pass only -- not a functional change): this tool's
    # params (to/subject/body) and behavior (printing an email draft) don't
    # match its name "search_knowledge_base" or the compliance-review role
    # described in this agent's instructions. Looks like it may have been
    # copy-pasted from an unrelated email-drafting tool example. Flagging
    # for the team to confirm; left exactly as originally written.
    with tool_span("search_knowledge_base", json.dumps({"to": to, "subject": subject, "body": body})) as span:
        print(f"To: {to}\n")
        print(f"Subject: {subject}\n")
        print(f"Body: {body}\n")
        span.record_result(json.dumps({"status": "printed"}))



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
MODEL_NAME = os.getenv("MODEL_MINI")

client = FoundryChatClient(
        project_endpoint= os.getenv("ENDPOINT"),
        model=MODEL_NAME,
        credential=AzureCliCredential()
    )
complianceAgent= Agent(
            client=client,
            name = "ComplianceReviewerAgent",
            instructions = """You are a compliance reviewer agent.
                          Your job is to review the drafted response against the published response policy before it reaches the representative: is it grounded, does it cite, does it recommend, does it disclose, does it leak PII?.""",
            tools=[search_knowledge_base],
            require_per_service_call_history_persistence=True
        )

prompt = ""
if __name__ == "__main__":
    # --- tracing: standalone run only; no effect when imported by orchestrator.py ---
    provider = configure_tracing()
    with traced_run("compliance_reviewer_standalone_run"):
        asyncio.run(process_data(complianceAgent, prompt))
    provider.shutdown()
