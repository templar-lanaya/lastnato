import truststore
truststore.inject_into_ssl()
import asyncio
from agent_framework.orchestrations import HandoffBuilder
from account_agent import accountAgent
from intake_router_agent import intakeRouter
from knowledge_agent import knowledgeAgent
from fulfillment_agent import fulfillmentAgent
from compliance_reviewer_agent import complianceAgent
from agent_framework import AgentResponseUpdate
from scopes import decisions 
from config import client
from model import Party

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, set_interaction, get_tracer, INSTRUCTION_SET_VERSION
from opentelemetry.trace import Status, StatusCode


workflow = HandoffBuilder(
    participants=[accountAgent, intakeRouter, knowledgeAgent, fulfillmentAgent, complianceAgent],
    # Set a hard termination condition: stop after 5 assistant messages
    # The agent orchestrator will intelligently decide when to end before this limit but just in case
    termination_condition=lambda messages: sum(1 for msg in messages if msg.role == "assistant") >= 5,
    intermediate_output_from=[accountAgent, intakeRouter, knowledgeAgent, fulfillmentAgent, complianceAgent],

   

).with_start_agent(intakeRouter).build()


tasks =[]
async def main(user_prompt, party_id, four_digit_pin, party) -> None:
    # The task message states the client, the service line, the practice, the quarter, the staffing design, and the requested discount.
    # tasks =[]
    task = tasks[0] if tasks else "No scenarios available."
    scoped = decisions(user_prompt, party_id, four_digit_pin, party)
    task = scoped

    print(f"Task: {task}\n")
    print("=" * 80)

    # --- tracing: root span for this whole interaction, and per-agent spans
    # hooked onto the SAME author-change transition the code already detects
    # (last_author) -- no new detection logic, just span bookkeeping riding
    # alongside the existing one. This is a manually-managed span (start/end
    # across loop iterations), not the lexically-scoped agent_span() helper
    # used in the individual agent files, since a single `with` block can't
    # span multiple loop iterations. One documented tradeoff versus
    # agent_span(): this does not set baggage (set_agent), so any span
    # created deeper inside HandoffBuilder's own internals (if it does its
    # own OTel instrumentation) won't automatically inherit an agent name
    # from context the way spans nested under agent_span() would.
    interaction_token = set_interaction(party_id)
    current_agent_span = None

    def _end_current_agent_span() -> None:
        nonlocal current_agent_span
        if current_agent_span is not None:
            current_agent_span.set_status(Status(StatusCode.OK))
            current_agent_span.end()
            current_agent_span = None

    try:
        with traced_run("investor_services_interaction", party_id=party_id):
            last_author: str | None = None
            # Run the workflow with streaming enabled
            stream = workflow.run(task, stream=True)
            async for event in stream:
                if event.type in ("intermediate", "output") and isinstance(event.data, AgentResponseUpdate):
                    # Print streaming agent updates
                    author = event.data.author_name
                    if author != last_author:
                        if last_author is not None:
                            print()
                        print(f"[{author}]:", end=" ", flush=True)
                        last_author = author
                        # --- tracing: close the previous agent's span, open this one's ---
                        _end_current_agent_span()
                        current_agent_span = get_tracer().start_span(f"invoke_agent {author}")
                        current_agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
                        current_agent_span.set_attribute("gen_ai.agent.name", author)
                        current_agent_span.set_attribute("instruction_set.version", INSTRUCTION_SET_VERSION)
                    print(event.data.text, end="", flush=True)
            # --- tracing: close whichever agent's span was still open when streaming ended ---
            _end_current_agent_span()

            result = await stream.get_final_response()
            if outputs := result.get_outputs():
                print("\n\n" + "=" * 80)
                print("Final Response:")
                print(outputs[-1])

            print("\nWorkflow completed.")
    finally:
        # --- tracing: always detach, even on an exception -- same pattern
        # the reference project's own main() uses for its tenant baggage.
        from opentelemetry import context as otel_context
        otel_context.detach(interaction_token)
if __name__ == '__main__':
    # print(read_out_scenarios())
    
    party_id = input("Enter your party id: ")
    four_digit_pin = int(input("Enter your four digit pin: "))
    user_prompt = input("Enter your prompt: ")
    party = Party.model_construct(party_id=party_id, four_digit_pin=four_digit_pin)
    # --- tracing: added, no functional change to the two lines below ---
    provider = configure_tracing()
    asyncio.run(main(user_prompt, party_id, four_digit_pin, party))
    provider.shutdown()
    #asyncio.run(main())