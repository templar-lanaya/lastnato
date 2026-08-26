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
# from tools_1 import search_knowledge_base
    
load_dotenv()
import httpx


#RET imports
from retrieval.retriever import search

#Gate imports
from knowledge_gate import knowledge_gate

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, agent_span, tool_span, production_exporters



###I think this is for knowledge retrieval, but the description is empty. I will fill it in with a brief description of what the tool does.
@tool
def evaluate_rule(
    rule_id: str,
    account_id: str,
    amount: float,
) -> dict:
    # --- tracing: wraps the existing body, no logic change. This tool's
    # error paths already use {"error": ...}, so record_result() correctly
    # flags failures here -- no detection gap, unlike several tools_1.py
    # functions.
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


@tool(

    name = "search_knowledge_base",
    description= (
        "Searches the Knowledge Base for policy, procedure, and product information"
    ),
    approval_mode= "never_require"

)
def search_knowledge_base(query: str, top_k: int = 5):

    # --- tracing: wraps the existing body, no logic change ---
    with tool_span("search_knowledge_base", json.dumps({"query": query, "top_k": top_k})) as span:
        results = search(
            query = query,
            top_k=top_k
        )

        if not results:
            result = {
                "found": False,
                "message": (
                    "No relevant information found in "
                    "the knowledge base."
                )

            }
            span.record_result(json.dumps(result))
            return result

        # Retrieved text is data, not instruction (HAR-05).
        result = {
            "found": True,
            **knowledge_gate.wrap_retrieved(results)
        }
        span.record_result(json.dumps(result, default=str))
        return result



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
            #response_json = json.loads(str(response))

            print(response.text)
            # Read-only gate: no actions, no account data, no
            # uncited answers.
            #return await knowledge_gate.validate(response_json)
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
        credential=AzureCliCredential(),
        
    )
knowledgeAgent= Agent(
            client=client,
            name = "KnowledgeAgent",
            instructions = """You are a knowledge agent.
                          Your job is to answer policy, product and procedure questions grounded in the corpus, with citations and an explicit refusal path.
                          
                          Your responsilibities:

                          -Answer policy questions
                          -Answer procedure question
                          -Answer product questions

                          You MUST use the search _knowledge_base tool before answering corpus questions.

                          If information is found:
                          -Answer using retrieved content.
                          -Cite the source document.

                          If information is NOT found:
                          -Refuse to answer.
                          -Explain that no support evidence exists in the knowledge base.

                          DO NOT INVENT FACTS
                          """,

            tools=[search_knowledge_base, evaluate_rule],
            require_per_service_call_history_persistence=True
        )


prompt = "Please answer the following question using the knowledge base: What Is a Roth Conversion?"
if __name__ == "__main__":
    # --- tracing: standalone run only; no effect when imported by orchestrator.py ---
    provider = configure_tracing(exporters=production_exporters())
    with traced_run("knowledge_agent_standalone_run"):
        asyncio.run(process_data(knowledgeAgent, prompt))
    provider.shutdown()
