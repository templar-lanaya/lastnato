#Where this leaves you, honestly, since I want to be clear about the boundary of what I can do here: running --http locally proves the server code itself is correct — but it's still just sitting on your laptop at 127.0.0.1. Foundry's servers can't reach 127.0.0.1 on your machine. Per Microsoft's own docs, the next real step is actually deploying this to Azure Container Apps or Azure Functions so it has a genuine, network-reachable URL — and that's infrastructure work I can't do from this sandbox (no Azure credentials, no Azure CLI access here).

#What I can do to help get you there: prepare the deployment artifacts (a Dockerfile for Container Apps, or the Functions-specific wrapper if you go that route) and a requirements.txt, so the code side is ready the moment you're pointed at an actual Azure subscription.

#One decision needed before I build that: does your team have a preference between Azure Container Apps vs Azure Functions for hosting this, or is that still open? They need meaningfully different deployment shapes — Container Apps wants a Dockerfile and runs this almost exactly as-is; Functions wants the app wrapped in their own trigger/binding model, which is a bigger structural change to mcp_server.py than Container Apps would need.

import asyncio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8123/mcp") as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        result = await client.call_tool("evaluate_policy_rule", {
            "rule_name": "wire_callback_required", "amount": 50000,
        })
        print("Result:", result.content[0].text)

asyncio.run(main())
