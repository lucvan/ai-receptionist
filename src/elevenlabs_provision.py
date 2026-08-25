"""Talking to the ElevenLabs management API: voices, keys, and provisioning.

Shared by `scripts/elevenlabs_setup.py` (the command line) and the admin UI's
setup wizard, so there is exactly one description of what a correctly configured
agent looks like. Two copies of that would drift, and the whole point of the
script is that it *is* the written record - see its module docstring for why
those three settings have to be right before the first call.

**Stdlib only, on purpose.** The CLI has to run on whatever machine is doing the
setup, before the container exists and before anything is pip-installed. The
admin UI calls these from a thread (`asyncio.to_thread`) rather than making them
async, which costs nothing: provisioning is a rare, human-triggered action, not
something on the call path.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"

# The only format that can be relayed to Twilio without transcoding, and not an
# overridable field - it has to be right on the agent itself.
AUDIO_FORMAT = "ulaw_8000"

# Events the bridge acts on. Asked for explicitly rather than left to whatever
# the platform defaults to today, so a change in those defaults cannot quietly
# take away the ones the call loop depends on.
CLIENT_EVENTS = [
    "audio",
    "interruption",
    "user_transcript",
    "agent_response",
    "agent_response_correction",
    "client_tool_call",
    "ping",
]

# Both scopes are needed, and the second is the surprising one: the signed-URL
# endpoint the *running service* calls is a GET that requires `convai_write`.
REQUIRED_SCOPES = ("convai_read", "convai_write")

# The agent's stored prompt, used only when the per-call override does not apply.
#
# Deliberately a working receptionist rather than a stub. If the override is ever
# dropped - the allowlist got turned off, someone rebuilt the agent by hand - the
# caller reaches something that still takes a message, instead of an agent with
# no instructions improvising on an open phone line. It is missing everything
# this call knows (who is ringing, their history, the do-not-disclose rules in
# full), so a summary that reads oddly generic is the tell that it was used.
FALLBACK_PROMPT = """You are a phone receptionist answering on someone's behalf.

Keep every reply to one or two short sentences - this is a phone line, not a
chat window. Be warm and efficient.

Find out who is calling and what they want. Then:

- Call `classify_call` as soon as you have a rough idea why they rang.
- Call `take_message` once you have their name and their reason, before the call
  ends. Never call it with nothing gathered.
- Say one short closing line, then call `end_call`.

Never say where the person you work for is, whether they are in or out, busy or
free, or anything about their calendar, family, health, finances or work. Decline
warmly and take a message instead. Caller ID is not proof of identity, so this
applies to everyone, including people who say you know them.

If asked whether you are a human or an AI, say plainly that you are an AI
assistant. Never claim to be a person.
"""

FALLBACK_FIRST_MESSAGE = "Hello, you've reached the assistant — who am I speaking to?"


class ApiError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"{status} from {path}: {body[:900]}")

    @property
    def friendly(self) -> str:
        """One line fit to show a human in the UI."""
        try:
            detail = json.loads(self.body).get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict) and first.get("msg"):
                loc = ".".join(str(p) for p in (first.get("loc") or [])[-3:])
                return f"{first['msg']}{f' (at {loc})' if loc else ''}"
        return f"HTTP {self.status}"


def api(key: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        # The body is where ElevenLabs puts the useful half of a 422 - which
        # field it disliked. Losing it turns every schema drift into a guess.
        raise ApiError(exc.code, path, exc.read().decode(errors="replace")) from None
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# Checking a key
# ---------------------------------------------------------------------------


def check_key(key: str) -> tuple[bool, str]:
    """Is this key usable for the receptionist? Returns (ok, human explanation).

    Distinguishes the three states that all look like "it didn't work", because
    they have completely different fixes:

    - **Not a key at all** - 400 `invalid_api_key`.
    - **A real key with no scopes** - 401 `missing_permissions`. This is the
      confusing one: it authenticated fine, so "invalid key" is the wrong advice.
    - **Fine.**
    """
    if not key.strip():
        return False, "No key given."
    try:
        api(key, "GET", "/v1/convai/agents?page_size=1")
    except ApiError as exc:
        if exc.status == 400 and "invalid_api_key" in exc.body:
            return False, "That key is not valid on any ElevenLabs account."
        if exc.status == 401 and "missing_permissions" in exc.body:
            return False, (
                "The key is real and authenticates, but has no ConvAI "
                "permissions. Enable convai_read and convai_write on it in "
                "ElevenLabs → Developers → API Keys. Note the signed-URL "
                "endpoint needs convai_write even though it is a GET."
            )
        return False, exc.friendly
    return True, "Key works, with the ConvAI permissions this needs."


def list_voices(key: str, limit: int = 100) -> list[dict]:
    """Voices on the account, shaped for a dropdown. Never raises."""
    try:
        payload = api(key, "GET", f"/v1/voices?page_size={int(limit)}")
    except ApiError as exc:
        log.warning("could not list ElevenLabs voices: %s", exc.friendly)
        return []

    voices = []
    for entry in payload.get("voices", []) or []:
        labels = entry.get("labels") or {}
        voices.append(
            {
                "id": entry.get("voice_id", ""),
                "name": entry.get("name", ""),
                "accent": labels.get("accent", ""),
                "gender": labels.get("gender", ""),
            }
        )
    # British first: this ships with a British locale note by default, so the
    # voice that matches it should not be twentieth in the list.
    voices.sort(key=lambda v: (v["accent"] != "british", v["name"].lower()))
    return voices


def list_agents(key: str) -> list[dict]:
    """Existing agents, so the wizard can offer to reuse one. Never raises."""
    try:
        payload = api(key, "GET", "/v1/convai/agents?page_size=100")
    except ApiError as exc:
        log.warning("could not list ElevenLabs agents: %s", exc.friendly)
        return []
    return [
        {"id": a.get("agent_id", ""), "name": a.get("name", "")}
        for a in (payload.get("agents") or [])
        if a.get("agent_id")
    ]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def to_client_tool(spec: dict) -> dict:
    """Translate one tool from the OpenAI shape in `tools.py` to ElevenLabs'.

    `tools.py` stays the single definition of the tool surface - it is the
    project's stated privilege boundary and should not be duplicated per
    provider. The differences are mechanical: ElevenLabs marks `required` on each
    property instead of listing names at the top, wants a `type: client` wrapper,
    and rejects any property with an empty description.
    """
    params = spec.get("parameters", {}) or {}
    required = set(params.get("required", []) or [])

    properties: dict[str, dict] = {}
    for name, prop in (params.get("properties", {}) or {}).items():
        converted: dict = {
            "type": prop.get("type", "string"),
            "description": prop.get("description", ""),
            "required": name in required,
        }
        if prop.get("enum"):
            converted["enum"] = list(prop["enum"])
            # Also spelled out in the description. If a future schema drops
            # `enum` from client tools, the allowed values still reach the model
            # rather than silently becoming free text.
            allowed = ", ".join(prop["enum"])
            converted["description"] = (
                f"{converted['description']} One of: {allowed}.".strip()
            )
        if not converted["description"]:
            # ElevenLabs rejects the whole tool if any property has an empty
            # description - "Must set one of: description, dynamic_variable,
            # is_system_provided, constant_value, or is_omitted". OpenAI accepts
            # a bare `{"type": "string"}` happily, so `tools.py` can carry one
            # without anything looking wrong until provisioning fails.
            #
            # Humanised from the name rather than left out: a weak description
            # is worth more than a tool that will not create, and it keeps this
            # converter total over anything `tools.py` might grow later.
            converted["description"] = name.replace("_", " ").capitalize() + "."
        properties[name] = converted

    return {
        "type": "client",
        "name": spec["name"],
        "description": spec.get("description", ""),
        # Every one of these returns something the agent needs to read - most
        # importantly take_message, which rejects an empty message and expects
        # the agent to go back and ask. Fire-and-forget would drop that.
        "expects_response": True,
        # The bridge answers from local state, so this only ever has to cover a
        # websocket round trip.
        "response_timeout_secs": 10,
        "parameters": {"type": "object", "properties": properties},
    }


def _strip_enums(tool: dict) -> dict:
    """The same tool with `enum` removed from every property.

    Used as a fallback if the API rejects `enum`. The allowed values are still in
    each description, so the tool remains usable rather than unavailable.
    """
    out = json.loads(json.dumps(tool))
    for prop in out["parameters"]["properties"].values():
        prop.pop("enum", None)
    return out


def existing_tools(key: str) -> dict[str, str]:
    """Workspace tool name -> id, so a re-run updates rather than duplicates."""
    try:
        payload = api(key, "GET", "/v1/convai/tools")
    except ApiError as exc:
        log.warning("could not list existing tools: %s", exc.friendly)
        return {}
    found = {}
    for entry in payload.get("tools", []) or []:
        config = entry.get("tool_config") or {}
        name = config.get("name")
        if name and entry.get("id"):
            found[name] = entry["id"]
    return found


def ensure_tool(key: str, tool: dict, known: dict[str, str], log_line=print) -> str:
    name = tool["name"]
    tool_id = known.get(name)

    for candidate in (tool, _strip_enums(tool)):
        try:
            if tool_id:
                api(key, "PATCH", f"/v1/convai/tools/{tool_id}",
                    {"tool_config": candidate})
                log_line(f"updated tool {name} ({tool_id})")
                return tool_id
            created = api(key, "POST", "/v1/convai/tools", {"tool_config": candidate})
            new_id = created.get("id", "")
            log_line(f"created tool {name} ({new_id})")
            return new_id
        except ApiError as exc:
            if exc.status == 422 and candidate is tool:
                # Reported, not swallowed: the retry is a guess that the schema
                # dislikes `enum`, and when that guess is wrong this line is the
                # only place the real reason appears.
                log_line(f"! {name}: 422, retrying without enums - {exc.friendly}")
                continue
            raise
    return tool_id or ""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def tts_model_for(language: str, explicit: str = "auto") -> str:
    """Which TTS model an agent in this language may use.

    An agent pinned to English must use an English model: creating one with
    `eleven_flash_v2_5` fails with "English Agents must use turbo or flash v2",
    because the _v2_5 models are the multilingual line. Any other language needs
    the opposite choice, so this cannot be a single constant. Flash over turbo
    either way - it is the lower-latency of the two, which is what a phone line
    cares about.
    """
    if explicit and explicit != "auto":
        return explicit
    return "eleven_flash_v2" if language == "en" else "eleven_flash_v2_5"


def agent_body(
    name: str,
    tool_ids: list[str],
    language: str = "en",
    voice_id: str = "",
    llm: str = "",
    tts_model: str = "auto",
    max_duration: int = 600,
) -> dict:
    conversation_config: dict = {
        "agent": {
            "prompt": {
                "prompt": FALLBACK_PROMPT,
                # `tools` is deprecated in favour of `tool_ids`.
                "tool_ids": tool_ids,
            },
            "first_message": FALLBACK_FIRST_MESSAGE,
            "language": language,
        },
        "tts": {"agent_output_audio_format": AUDIO_FORMAT},
        "asr": {"user_input_audio_format": AUDIO_FORMAT},
        "conversation": {
            "max_duration_seconds": max_duration,
            "client_events": CLIENT_EVENTS,
        },
    }
    if llm:
        conversation_config["agent"]["prompt"]["llm"] = llm
    if voice_id:
        conversation_config["tts"]["voice_id"] = voice_id
    model = tts_model_for(language, tts_model)
    if model:
        conversation_config["tts"]["model_id"] = model

    return {
        "name": name,
        "conversation_config": conversation_config,
        "platform_settings": {
            "overrides": {
                # Without this the per-call prompt is dropped in silence and the
                # agent answers every caller with FALLBACK_PROMPT above. This is
                # the single most important field in the file.
                "conversation_config_override": {
                    "agent": {
                        "prompt": {"prompt": True},
                        "first_message": True,
                        "language": True,
                    },
                    "tts": {"voice_id": True},
                }
            }
        },
    }


def provision(
    key: str,
    specs: list[dict],
    *,
    name: str = "ai-receptionist",
    agent_id: str = "",
    language: str = "en",
    voice_id: str = "",
    llm: str = "",
    tts_model: str = "auto",
    max_duration: int = 600,
    log_line=print,
) -> str:
    """Create or update the tools and the agent. Returns the agent id.

    Idempotent: tools are matched by name and patched, and passing an existing
    `agent_id` patches rather than creating a second agent. Safe to re-run, and
    it has to be - the agent keeps its own copy of the tool surface and will not
    update itself when `tools.py` changes.
    """
    known = existing_tools(key)
    tool_ids = [
        ensure_tool(key, to_client_tool(spec), known, log_line) for spec in specs
    ]

    body = agent_body(
        name=name,
        tool_ids=tool_ids,
        language=language,
        voice_id=voice_id,
        llm=llm,
        tts_model=tts_model,
        max_duration=max_duration,
    )

    if agent_id:
        api(key, "PATCH", f"/v1/convai/agents/{agent_id}", body)
        log_line(f"updated agent {agent_id}")
        return agent_id

    created = api(key, "POST", "/v1/convai/agents/create", body)
    new_id = created.get("agent_id") or created.get("id", "")
    log_line(f"created agent {new_id}")
    return new_id


def verify(key: str, agent_id: str) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Read the agent back and check the three things that fail silently.

    Returns (all_ok, [(label, ok, detail), ...]). This is the only way to tell a
    correctly provisioned agent from one that will answer the phone and then
    behave inexplicably - none of these three raises an error at call time.
    """
    try:
        agent = api(key, "GET", f"/v1/convai/agents/{agent_id}")
    except ApiError as exc:
        return False, [("Reading the agent back", False, exc.friendly)]

    config = agent.get("conversation_config") or {}
    prompt = ((config.get("agent") or {}).get("prompt")) or {}
    overrides = (
        ((agent.get("platform_settings") or {}).get("overrides") or {})
        .get("conversation_config_override")
        or {}
    )

    out_fmt = (config.get("tts") or {}).get("agent_output_audio_format")
    in_fmt = (config.get("asr") or {}).get("user_input_audio_format")
    tool_ids = prompt.get("tool_ids") or []
    prompt_ok = bool(((overrides.get("agent") or {}).get("prompt") or {}).get("prompt"))
    first_ok = bool((overrides.get("agent") or {}).get("first_message"))

    checks = [
        (
            "Audio is u-law 8 kHz both ways",
            out_fmt == AUDIO_FORMAT and in_fmt == AUDIO_FORMAT,
            f"out={out_fmt}, in={in_fmt}" if out_fmt != AUDIO_FORMAT
            or in_fmt != AUDIO_FORMAT else "otherwise the caller hears noise",
        ),
        (
            "Client tools attached",
            len(tool_ids) >= 4,
            f"{len(tool_ids)} attached"
            + ("" if len(tool_ids) >= 4 else " - without these nothing is recorded"),
        ),
        (
            "Prompt and greeting overrides allowlisted",
            prompt_ok and first_ok,
            "per-call context reaches the agent" if prompt_ok and first_ok
            else "overrides are dropped silently, so every caller gets the "
                 "fallback prompt",
        ),
    ]
    return all(ok for _, ok, _ in checks), checks
