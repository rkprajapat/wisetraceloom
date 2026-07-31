"""A more realistic wisetraceloom scenario: a support-ticket triage agent that
processes a small batch of tickets, each going through customer lookup ->
intent classification -> refund processing.

Unlike quickstart.py, this shows the parts that matter once you're past a
toy demo:

- PII flowing through both instrumentation paths gets redacted automatically
  -- a structured field named `email`/`phone` (tier 1), *and* an email
  address embedded inside free-text `message` field (tier 2 regex) -- with
  no extra code at the call site.
- one ticket's tool call fails (a real `ValueError`, not a wisetraceloom
  failure) -- the exception still propagates to caller code as normal;
  wisetraceloom's fail-open guarantee only covers its own instrumentation, not
  your business logic. The batch loop below catches it and keeps going, the
  way a real ticket-processing job would.
- multi-tenant: each ticket is bound to its own `tenant_id`, so every span
  and log line in its trace carries that tenant automatically.

Run from the repo root (as a module, so `wisetraceloom/` resolves off the repo
root rather than the script's own directory):

    uv run python -m example.customer_support_agent
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import wisetraceloom
from wisetraceloom.config import set_rotation_config
from wisetraceloom.otel_export import set_export_config
from wisetraceloom.prompts import register_prompt_version


# --- fake external calls, so the example needs no network/API key --------


@dataclass
class Ticket:
    ticket_id: str
    tenant_id: str
    customer_id: str
    message: str


@dataclass
class CustomerRecord:
    name: str
    email: str
    phone: str
    refund_eligible: bool


_FAKE_CUSTOMER_DB = {
    "cust-1": CustomerRecord("Asha Rao", "asha.rao@example.com", "+91-98765-43210", refund_eligible=True),
    "cust-2": CustomerRecord("Ben Ortiz", "ben.ortiz@example.com", "+1-415-555-0132", refund_eligible=False),
}


def lookup_customer(customer_id: str) -> CustomerRecord:
    return _FAKE_CUSTOMER_DB[customer_id]


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _ClassifyResponse:
    usage: _Usage
    intent: str


def classify_intent(message: str) -> _ClassifyResponse:
    # Real code would call an LLM provider here; a keyword match stands in.
    intent = "refund_request" if "refund" in message.lower() else "general_inquiry"
    return _ClassifyResponse(usage=_Usage(input_tokens=64, output_tokens=8), intent=intent)


def process_refund(customer: CustomerRecord, ticket: Ticket) -> str:
    if not customer.refund_eligible:
        raise ValueError(f"customer {ticket.customer_id} is not refund-eligible")
    return f"refund-{ticket.ticket_id}"


@wisetraceloom.trace_tool_call("send_confirmation_email")
def send_confirmation(customer: CustomerRecord, confirmation_id: str) -> None:
    logger.info("confirmation_sent", to=customer.email, confirmation_id=confirmation_id)


# --- setup: logging, rotation, OTel export, one prompt version -------------

# Rotation config is persisted (SQLite), so `log_file_path` set by *any*
# prior run -- this script's or quickstart.py's -- is still the active
# global default the next time `configure()` resolves a destination.
# Create the directory unconditionally so that's never a surprise.
Path("logs").mkdir(exist_ok=True)

wisetraceloom.configure(json_output=False)
logger = wisetraceloom.get_logger("example.customer_support_agent")

set_rotation_config(
    log_file_path="logs/wisetraceloom.log",
    max_size_mb=50.0,
    rotation_interval="midnight",
    backup_count=7,
    compress_backups=True,
)

set_export_config(gen_ai_semconv_enabled=True)

prompt_version = register_prompt_version(
    "support_agent.classify_intent",
    "Classify the customer's message as refund_request or general_inquiry.",
    model_params={"temperature": 0.0},
)


# --- the actual per-ticket pipeline -----------------------------------------


def handle_ticket(ticket: Ticket) -> None:
    with wisetraceloom.bind_context(tenant_id=ticket.tenant_id, request_id=ticket.ticket_id):
        with wisetraceloom.agent_step(agent_id="support-agent", agent_name="support_triage_agent") as agent:
            agent.description = f"Triage ticket {ticket.ticket_id}"

            with wisetraceloom.tool_call("lookup_customer") as tool:
                customer = lookup_customer(ticket.customer_id)
                tool.description = "Look up customer record"
                # `email`/`phone` are struck to [REDACTED] by field name (tier 1);
                # nothing extra needed here.
                logger.info("customer_found", name=customer.name, email=customer.email, phone=customer.phone)

            with wisetraceloom.llm_call(
                "anthropic", "claude-sonnet-5", prompt_version_id=prompt_version.title
            ) as llm:
                classification = classify_intent(ticket.message)
                llm.input_tokens = classification.usage.input_tokens
                llm.output_tokens = classification.usage.output_tokens

            # The ticket message itself may contain PII (e.g. a customer
            # pasting their own email into the body) -- regex scrubbing
            # (tier 2) masks it even though `message` isn't a sensitive
            # field name.
            logger.info("intent_classified", intent=classification.intent, message=ticket.message)

            if classification.intent == "refund_request":
                with wisetraceloom.tool_call("process_refund") as tool:
                    tool.description = "Process refund"
                    confirmation_id = process_refund(customer, ticket)
                send_confirmation(customer, confirmation_id)


def main() -> None:
    tickets = [
        Ticket("t-1", "acme", "cust-1", "Hi, I'd like a refund for my last order please."),
        Ticket(
            "t-2",
            "acme",
            "cust-2",
            "I'd like a refund -- you can reach me at ben.ortiz@example.com if you need details.",
        ),
        Ticket("t-3", "globex", "cust-1", "What are your support hours?"),
    ]

    for ticket in tickets:
        try:
            handle_ticket(ticket)
        except ValueError as exc:
            # wisetraceloom never swallows *your* exceptions -- process_refund's
            # ValueError above propagates normally through the `tool_call`
            # block; the tool_span is still emitted (success=False,
            # error_message set) before it does. Handle it the way any
            # real batch job would: log and move on to the next ticket.
            logger.warning("ticket_failed", ticket_id=ticket.ticket_id, error=str(exc))

    print("Done. Check console output above for structured span/log events.")


if __name__ == "__main__":
    main()
