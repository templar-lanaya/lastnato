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

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, agent_span, tool_span

load_dotenv()


#RET imports 
from retrieval.retriever import search



###I think this is for knowledge retrieval, but the description is empty. I will fill it in with a brief description of what the tool does.



@tool(

    name = "search_knowledge_base",
    description= (
        "Searches the Knowledge Base for policy, procedure, and product information"
    ),
    approval_mode= "never_require"

)
def search_knowledge_base(query: str, top_k: int = 5):

    # --- tracing: wraps the existing body; return values/logic unchanged,
    # including the "resuls" key below -- left exactly as originally written.
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
        else:
            result = {
                "found": True,
                "resuls": results
            }

        span.record_result(json.dumps(result))
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

            tools=[search_knowledge_base],
            require_per_service_call_history_persistence=True
        )


prompt = "Please answer the following question using the knowledge base: What Is a Roth Conversion?"
if __name__ == "__main__":
    # --- tracing: standalone run only; no effect when imported by orchestrator.py ---
    provider = configure_tracing()
    with traced_run("knowledge_agent_standalone_run"):
        asyncio.run(process_data(knowledgeAgent, prompt))
    provider.shutdown()
