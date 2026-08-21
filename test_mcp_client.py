"""Proves mcp_server.py actually works over the real MCP protocol -- spawns
it as a subprocess and talks to it via stdio, the same way a real agent
(regardless of framework) would connect to it. This is NOT just importing
policy_engine directly; it's testing the transport layer too.
"""
import asyncio
import json
 
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
 
SERVER_PARAMS = StdioServerParameters(command="python3", args=["mcp_server.py"])
 
 
async def main() -> None:
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
 
            print("=== Tools the server advertises (auto-generated schema) ===")
            tools = await session.list_tools()
            for t in tools.tools:
                print(f"- {t.name}: {t.description[:80]}...")
                print(f"  inputSchema: {json.dumps(t.inputSchema)}")
 
            print()
            print("=== Call 1: fee_waiver, real boundary fixture (PTY-00001, cash_balance=4980, e_delivery=True) ===")
            result = await session.call_tool("evaluate_policy_rule", {
                "rule_name": "fee_waiver", "account_id": "ACC-00001",
            })
            print(result.content[0].text)
 
            print()
            print("=== Call 2: wire_callback_required, at the $50,000 boundary ===")
            result = await session.call_tool("evaluate_policy_rule", {
                "rule_name": "wire_callback_required", "amount": 50000,
            })
            print(result.content[0].text)
 
            print()
            print("=== Call 3: malformed call (missing amount) -- should come back as a clean error, not crash the session ===")
            result = await session.call_tool("evaluate_policy_rule", {
                "rule_name": "wire_callback_required",
            })
            print(result.content[0].text)
 
            print()
            print("=== Session still alive after the error -- one more real call ===")
            result = await session.call_tool("evaluate_policy_rule", {
                "rule_name": "margin_interest_rate", "account_id": "ACC-00001", "amount": 0,
            })
            print(result.content[0].text)
 
 
if __name__ == "__main__":
    asyncio.run(main())