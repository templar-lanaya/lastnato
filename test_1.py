# JORDAN THIS IS THE LEGIT TEST CASE FOR MCP INTEGRATION


import os
from dotenv import load_dotenv
from pathlib import Path
import asyncio
import json
from agent_framework import tool, Agent
from tools import get_account_summary, get_positions, get_recent_transactions, get_account_registrations, evaluate_policy_rule  # FIXED: was "from test_1 import ..." (imported from itself)

from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from azure.ai.projects.models import PromptAgentDefinition, MCPTool
import httpx
#from har import validate_account_response, summarize_account, LoopDetectionMiddleware

load_dotenv()

tool_1 = MCPTool(
server_label="shared-tools",
server_url="http://127.0.0.1:8000/mcp",
require_approval= "never"
)

@tool
def evaluate_rule(
    rule_id: str,
    account_id: str,
    amount: float,
) -> dict:
    payload = {
        "rule_id": rule_id,
        "account_id": account_id,
        "amount": amount,
    }

    try:
        response = httpx.post(
            "http://127.0.0.1:8000/policy/evaluate",
            json=payload,
            timeout=30.0,
        )

        print("Policy status:", response.status_code)
        print("Policy response:", response.text)

        response.raise_for_status()
        return response.json()

    except httpx.HTTPStatusError as exc:
        return {
            "error": "Policy API returned an HTTP error.",
            "status_code": exc.response.status_code,
            "details": exc.response.text,
        }

    except httpx.RequestError as exc:
        return {
            "error": "Could not connect to the policy API.",
            "details": str(exc),
        }

    except ValueError:
        return {
            "error": "Policy API did not return valid JSON.",
            "details": response.text,
        }

client = FoundryChatClient(
        project_endpoint= os.getenv("ENDPOINT"),
        model=os.getenv("MODEL_MINI"),
        credential=AzureCliCredential()
    )

accountAgent= Agent(
            client=client,
            name = "AccountAgent",
            instructions = """You are an account agent.
                          Your job is to retrieve the caller's servicing facts — positions, recent transactions, delivery preferences, beneficiary and TCP status — and hand them to the rules engine.""",
            tools=[evaluate_rule, get_account_summary, get_positions, get_recent_transactions, get_account_registrations ],
)
async def process_data(agent, prompt: str):
    async with agent:
        response = await agent.run([prompt])
        print("Response text:", response.text)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "Agent returned text, not JSON.",
                    "raw_response": response.text}
            validated = validate_account_response(response_json)
            
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

    prompt_2 = """Please determine whether account ACC-00001 qualifies for Venterra's service fee waiver, using rule_id = service_fee_waiver. Check the client's household relationship, all associated accounts, and total assets under management.  """

    asyncio.run(process_data(accountAgent, prompt=prompt_2))



"""Mehul test prompt -> Can you use the tool to figure out whether I can deposit today $50,000 (using rule_id: WIRE_ACH_DAILY_LIMIT_INCOMING) with account_id ACC-00001.
            Return a 2 sentence explanation"""

           
#Testing prompts templates
# 1. get_account_summary - get_account_summary(account_id="ACC-00001") (WORKING)
# 2. get_positions - Show all positions held in account ACC-00001. (WORKING)
# 3. get_recent_transactions - Show the 10 most recent transactions for account ACC-00001. (WORKING)
# 4. get_account_registrations - For account ACC-00001, tell me: (WORKING)

# x
# 5. evaluate_policy_rule 
# 5a. "SERVICE_FEE_WAIVER" - Account ACC-00001 wants to know whether it qualifies for a service fee waiver.
# 5b. "WIRE_CALLBACK" -A client from account ACC-00001 is requesting an outgoing wire transfer of $60,000. Please evaluate policy rule
# 5c. "WIRE_ACH_DAILY_LIMIT_INCOMING", An incoming ACH deposit of $275,000 is being submitted to account ACC-00001. Please evaluate policy rule
# 5d. "WIRE_ACH_DAILY_LIMIT_OUTGOING", An outgoing ACH transfer of $290,000 has been requested from account ACC-00001.
# 5e. "RMD_START_AGE", Determine whether the owner of account ACC-00001 has reached the age at which Required Minimum Distributions must begin.
# 5f. "MARGIN_INTEREST_RATE", What margin interest rate applies to account ACC-00001 based on its current margin debit balance?
# 5g. "MOBILE_CHECK_DEPOSIT_LIMIT", A client is attempting to deposit a check for $120,000 into account ACC-00001 using mobile check deposit.
# 5h. "QCD_ANNUAL_LIMIT", An IRA owner from account ACC-00001 wants to make a Qualified Charitable Distribution of $125,000.
# 5i. "TEMP_HOLD", I need to determine whether transaction TXN-00015 in account ACC-00001 is still under a temporary hold.
