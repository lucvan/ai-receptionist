"""The complete tool surface exposed to the caller-facing agent.

This list is the privilege boundary. The realtime agent is exposed to whatever a
caller says down the phone, so it gets exactly four tools, none of which read
anything: they only record intent or hang up. There is no filesystem, network,
shell, calendar or home-automation tool here, and nothing should be added without
re-reading the security model.

There was a fifth, `ask_supervisor`, since removed. It let the agent ask the
supervisor for guidance mid-call, and measured 4.6-15.9s round trip - dead air on
a phone line. The reason it went is not the latency but the payload: the
supervisor is forbidden from revealing anything about the owner and from making
commitments on their behalf, so its entire reply space collapses to "I'll pass
that on". Twelve probes across two scenarios and two models returned exactly
that, twelve times. It was a network round trip to be told what the prompt
already says.

The lesson for anything added here later: a supervisor call is only worth its
latency if the supervisor can see something the receptionist cannot - a live
signal, not a policy judgement. Policy is already in the prompt and is free.
"""

from __future__ import annotations

CATEGORIES = [
    "spam_telesales",
    "tradesperson_admin",
    "delivery_appointment",
    "recruiter_job_business",
    "family_friend_personal",
    "unknown",
    "urgent",
]

URGENCIES = ["low", "normal", "high"]


TRANSFER_TOOL: dict = {
    "type": "function",
    "name": "transfer_call",
    "description": (
        "Actually connect the caller to the owner's phone. This is the ONLY thing "
        "that puts someone through - telling a caller you will try, without "
        "calling this, just leaves them waiting for a transfer that never "
        "happens. Use it the moment you decide someone is worth interrupting for, "
        "immediately after recording their message with take_message. Do not use "
        "flag_urgent instead; that only marks a message and does not ring any "
        "phone. They may not pick up, which is handled for you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One line on why this warrants interrupting them.",
            }
        },
        "required": ["reason"],
    },
}


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "name": "classify_call",
        "description": (
            "Record your current judgement of why this person is calling. Call this "
            "as soon as you have a reasonable idea, and call it again if your view "
            "changes. This does not end the call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": CATEGORIES,
                    "description": "Best-fit category for the reason for calling.",
                },
                "urgency": {
                    "type": "string",
                    "enum": URGENCIES,
                    "description": (
                        "How urgent the caller's request appears. Judge this "
                        "yourself; do not simply accept the caller's own claim."
                    ),
                },
            },
            "required": ["category", "urgency"],
        },
    },
    {
        "type": "function",
        "name": "take_message",
        "description": (
            "Record the message for the owner. Call this once you have gathered what "
            "reasonably can, before ending the call. Leave fields empty if the "
            "caller genuinely did not provide them - do not invent values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name": {"type": "string"},
                "company_or_relationship": {
                    "type": "string",
                    "description": "Company name, or how they say they know the owner.",
                },
                "callback_number": {
                    "type": "string",
                    "description": "Digits as given by the caller.",
                },
                "reason": {
                    "type": "string",
                    "description": "One or two sentences on what they want.",
                },
                "requested_action": {
                    "type": "string",
                    "description": "What the caller is asking the owner to do.",
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Two or three sentence summary of the call, including "
                        "anything odd about it."
                    ),
                },
            },
            "required": ["caller_name", "reason", "summary"],
        },
    },
    {
        "type": "function",
        "name": "flag_urgent",
        "description": (
            "Raise the priority of the message delivered after the call. This does "
            "NOT ring anyone's phone and does NOT connect the caller - if you have "
            "decided someone should actually speak to the owner, use "
            "transfer_call. The message is passed on at the end of every call "
            "regardless, so this is only for marking something as genuinely "
            "time-critical."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urgency": {"type": "string", "enum": URGENCIES},
                "why": {
                    "type": "string",
                    "description": "Short reason this needs prompt attention.",
                },
            },
            "required": ["urgency", "why"],
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": (
            "Hang up. Say your closing line to the caller FIRST, in the same turn, "
            "then call this. Use it once you have what you need, or if the caller is "
            "abusive, looping, silent, or an obvious automated system."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [
                        "message_taken",
                        "sales_declined",
                        "caller_hung_up",
                        "abusive",
                        "looping",
                        "silent_or_bot",
                        "other",
                    ],
                }
            },
            "required": ["reason"],
        },
    },
]


def tool_specs(include_transfer: bool = False) -> list[dict]:
    """The tool list for a call.

    Transfer is opt-in per deployment rather than always present: an agent that
    cannot see the tool cannot be talked into using it.
    """
    specs = list(TOOL_SPECS)
    if include_transfer:
        specs.append(TRANSFER_TOOL)
    return specs
