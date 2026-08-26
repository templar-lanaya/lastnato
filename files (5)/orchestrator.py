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

# Harness / gate layer
from action_store import action_store
from auth import build_caller_context, caller_context_scope
from gate_models import CallerContext
from gates.approval_gate import ActionNotApproved, approval_gate
from gates.expiration_gate import ProposalExpired, seconds_remaining

# --- tracing: added, no functional change below this line ---
from tracing_provider import configure_tracing, traced_run, set_interaction, get_tracer, INSTRUCTION_SET_VERSION, production_exporters
from opentelemetry.trace import Status, StatusCode


workflow = HandoffBuilder(
    participants=[accountAgent, intakeRouter, knowledgeAgent, fulfillmentAgent, complianceAgent],
    # Set a hard termination condition: stop after 5 assistant messages
    # The agent orchestrator will intelligently decide when to end before this limit but just in case
    termination_condition=lambda messages: sum(1 for msg in messages if msg.role == "assistant") >= 5,
    intermediate_output_from=[accountAgent, intakeRouter, knowledgeAgent, fulfillmentAgent, complianceAgent],



).with_start_agent(intakeRouter).build()


tasks =[]


def review_pending_actions(ctx: CallerContext) -> None:
    """
    The human approval step. No write reaches the servicing system
    without passing through here.
    """

    # --- tracing: one span for the whole review step, one event per
    # decision. Not a tool call, so a plain span rather than tool_span --
    # same pattern used for approval_gate.process()/account_gate.validate()
    # in fulfillment_agent.py/account_agent.py.
    with get_tracer().start_as_current_span("review_pending_actions") as review_span:
        pending = action_store.pending()
        review_span.set_attribute("pending_count", len(pending))

        if not pending:
            print("\nNo actions were proposed. Nothing to approve.")
            return

        print("\n" + "=" * 80)
        print(f"{len(pending)} action(s) awaiting your approval:")

        for action in pending:
            print("-" * 80)
            print(f"action_id   : {action.action_id}")
            print(f"action_type : {action.action_type}")
            print(f"proposed_by : {action.proposed_by}")
            print(f"reason      : {action.reason}")
            print(f"payload     : {action.payload}")
            print(f"expires in  : {seconds_remaining(action):.0f}s")

            choice = input(
                "[a]pprove, [r]eject, or [s]kip? "
            ).strip().lower()

            try:
                if choice.startswith("a"):
                    result = approval_gate.approve_and_execute(
                        action.action_id,
                        approver=ctx.user_id,
                        approver_ctx=ctx,
                    )
                    print(f"Executed: {result}")
                    review_span.add_event("action_reviewed", {
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "decision": "approved",
                    })

                elif choice.startswith("r"):
                    reason = input("Rejection reason: ").strip()
                    print(
                        approval_gate.reject(
                            action.action_id,
                            approver=ctx.user_id,
                            reason=reason,
                            approver_ctx=ctx,
                        )
                    )
                    review_span.add_event("action_reviewed", {
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "decision": "rejected",
                    })

                else:
                    print("Skipped. The proposal stays pending until it expires.")
                    review_span.add_event("action_reviewed", {
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "decision": "skipped",
                    })

            except (ProposalExpired, ActionNotApproved, PermissionError, ValueError) as e:
                print(f"Not executed ({type(e).__name__}): {e}")
                review_span.add_event("action_review_error", {
                    "action_id": action.action_id,
                    "error_type": type(e).__name__,
                })

        print("=" * 80)
        print("Audit trail:")
        for entry in action_store.audit_trail():
            print(f"  {entry['timestamp']}  {entry['event']}  {entry.get('action_id')}")


async def main(user_prompt, party_id, four_digit_pin, party, representative_id="REP-LOCAL") -> None:
    # The task message states the client, the service line, the practice, the quarter, the staffing design, and the requested discount.
    # tasks =[]
    task = tasks[0] if tasks else "No scenarios available."
    scoped = decisions(user_prompt, party_id, four_digit_pin, party)
    task = scoped

    print(f"Task: {task}\n")
    print("=" * 80)

    # Bind the authenticated subject once, before any agent runs. Every
    # protected tool reads its subject from here, so no model output can
    # widen access or point a proposal at another party's account.
    ctx = build_caller_context(
        subject_party_id=party_id,
        user_id=representative_id,
    )

    # --- tracing: root span for this whole interaction, and per-agent spans
    # hooked onto the SAME author-change transition the code already
    # detects (last_author) -- no new detection logic, just span
    # bookkeeping riding alongside the existing one.
    interaction_token = set_interaction(party_id)
    current_agent_span = None

    def _end_current_agent_span() -> None:
        nonlocal current_agent_span
        if current_agent_span is not None:
            current_agent_span.set_status(Status(StatusCode.OK))
            current_agent_span.end()
            current_agent_span = None

    try:
        with traced_run("investor_services_interaction", party_id=party_id, representative_id=representative_id):
            with caller_context_scope(ctx):
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

                # Proposals are only ever executed by the harness, after review.
                review_pending_actions(ctx)
    finally:
        # --- tracing: always detach, even on an exception -- same pattern
        # the reference project's own main() uses for its tenant baggage.
        from opentelemetry import context as otel_context
        otel_context.detach(interaction_token)

    print("\nWorkflow completed.")
if __name__ == '__main__':
    # print(read_out_scenarios())

    party_id = input("Enter your party id: ")
    four_digit_pin = int(input("Enter your four digit pin: "))
    representative_id = input("Enter your representative id [REP-LOCAL]: ") or "REP-LOCAL"
    user_prompt = input("Enter your prompt: ")
    party = Party.model_construct(party_id=party_id, four_digit_pin=four_digit_pin)
    # --- tracing: added, no functional change to the line below ---
    provider = configure_tracing(exporters=production_exporters())
    asyncio.run(main(user_prompt, party_id, four_digit_pin, party, representative_id))
    provider.shutdown()
    #asyncio.run(main())
