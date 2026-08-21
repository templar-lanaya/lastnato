"""Manual MCP test client for evaluate_policy_rule.
 
Run this directly in VS Code (F5, or `python3 manual_test_client.py` in
the integrated terminal). It spawns mcp_server.py as a subprocess and
gives you an interactive prompt to call evaluate_policy_rule with
whatever inputs you want -- no code editing needed to try a new case.
 
Requirements:
    pip install fastmcp
 
Expects this file to sit next to mcp_server.py, and mcp_server.py's own
DATA_DIR (via policy_engine.py) to still resolve correctly to your
seed.py data folder -- same project layout as before.
"""
from __future__ import annotations
 
import asyncio
import json
import sys
from pathlib import Path
 
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
 
SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"
# Use the SAME interpreter that's running this script, rather than a
# hardcoded "python3" -- Windows Python installs typically only expose
# "python.exe", not "python3", which is what caused Connection-closed
# errors there. sys.executable always exists and always has fastmcp
# installed, since it's this exact process's own interpreter.
SERVER_PARAMS = StdioServerParameters(command=sys.executable, args=[str(SERVER_PATH)])
 
RULE_NAMES = [
    "fee_waiver", "wire_callback_required", "ach_outgoing_limit",
    "ach_incoming_limit", "rmd_start_age", "margin_interest_rate",
    "mobile_check_deposit_limit", "qcd_limit", "temporary_hold_status",
]
 
 
def _prompt(label: str) -> str | None:
    value = input(f"  {label} (Enter to skip): ").strip()
    return value or None
 
 
def _print_result(raw_text: str) -> None:
    try:
        print(json.dumps(json.loads(raw_text), indent=2))
    except json.JSONDecodeError:
        print(raw_text)  # fall back to raw text if it wasn't JSON for some reason
 
 
async def interactive_loop(session: ClientSession) -> None:
    while True:
        print("\nAvailable rules:")
        for i, name in enumerate(RULE_NAMES, start=1):
            print(f"  {i}. {name}")
        print("  q. quit")
 
        choice = input("Pick a rule (number) or 'q': ").strip().lower()
        if choice == "q":
            break
        try:
            rule_name = RULE_NAMES[int(choice) - 1]
        except (ValueError, IndexError):
            print("Not a valid choice -- try again.")
            continue
 
        print(f"\nCalling '{rule_name}'. Leave a field blank if this rule doesn't need it:")
        account_id = _prompt("account_id  (e.g. ACC-00001)")
        amount_raw = _prompt("amount      (e.g. 50000)")
        transaction_id = _prompt("transaction_id")
 
        args: dict = {"rule_name": rule_name}
        if account_id:
            args["account_id"] = account_id
        if amount_raw:
            try:
                args["amount"] = float(amount_raw)
            except ValueError:
                print(f"  '{amount_raw}' isn't a number -- skipping amount.")
        if transaction_id:
            args["transaction_id"] = transaction_id
 
        print(f"\nSending: {args}")
        result = await session.call_tool("evaluate_policy_rule", args)
        print("Result:")
        if result.content:
            _print_result(result.content[0].text)
        else:
            print("(empty response)")
 
 
async def main() -> None:
    print(f"Starting MCP server: {SERVER_PATH}")
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Connected. Tools available:", [t.name for t in tools.tools])
            await interactive_loop(session)
    print("\nSession closed.")
 
 
if __name__ == "__main__":
    asyncio.run(main())