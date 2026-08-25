"""Client for the supervisor: any OpenAI-compatible chat-completions endpoint.

The supervisor is optional and does two things, both **after the caller has hung
up**, so it is never on the critical path of a live call:

- writes the summary that gets delivered, and
- turns the owner's reply into a spoken script when a caller is rung back.

There is nothing framework-specific here. It POSTs to
``{url}/v1/chat/completions`` with a bearer token and reads
``choices[0].message.content``, which is what OpenAI, Ollama, vLLM, LM Studio,
LiteLLM, OpenRouter and most agent frameworks all speak.

Trust direction matters. The receptionist never forwards caller speech as an
instruction; the supervisor receives a structured summary the receptionist wrote
*about* the call, and is told so explicitly. Every failure mode degrades to a
locally composed summary or a verbatim relay, never to silence and never to an
error message reaching a caller.

Prompts live in ``prompts/supervisor/`` rather than in this file, so they can be
reworded without a rebuild - the same treatment the caller-facing prompts get.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx

from .persona import Persona, render

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "supervisor"


def _load_prompt(name: str, persona: Persona, extra: dict | None = None) -> str:
    """Read and render one supervisor prompt.

    Read per call rather than cached, matching the caller-facing prompts: editing
    the wording takes effect on the next call with no rebuild and no restart.
    """
    try:
        text = (PROMPT_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        log.error("could not read supervisor prompt %s: %s", name, exc)
        return ""
    return render(text, persona, extra)


def _enrichment(persona: Persona) -> str:
    """The optional 'go and look things up' section.

    Absent unless the deployer has written `prompts/supervisor/enrichment.md`.
    Deliberately opt-in: pointed at a plain model, an instruction to search notes
    tells something with no filesystem to read files, and it will invent what it
    thinks it found. See `enrichment.md.example` for the full warning.
    """
    path = PROMPT_DIR / "enrichment.md"
    if not path.exists():
        return ""
    try:
        return render(path.read_text(encoding="utf-8"), persona)
    except OSError as exc:
        log.error("could not read enrichment prompt: %s", exc)
        return ""


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of a model reply that may be wrapped in prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        brace = re.search(r"\{.*\}", text, re.S)
        if not brace:
            return None
        text = brace.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class SupervisorClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        enabled: bool = True,
        persona: Persona | None = None,
    ):
        self._url = url
        self._api_key = api_key
        self._model = model
        self._enabled = enabled and bool(url)
        self._persona = persona or Persona.from_env()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _chat(self, system: str, user: str, timeout: float) -> str | None:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._url}/v1/chat/completions", headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("supervisor returned an unexpected response shape")
            return None

    async def build_callback_script(
        self, context: dict, reply_text: str, timeout: float
    ) -> tuple[str, bool, str]:
        """Turn a terse reply into something speakable, plus a note for the file.

        Returns ``(script, expect_reply, note)``. Falls back to relaying the words
        verbatim - clumsy read aloud, but it preserves the meaning exactly, which
        matters more than polish.

        This is the one call that needs structured output back. A small local
        model will fail it regularly; that is survivable precisely because the
        fallback keeps the meaning.
        """
        fallback = (
            f"{self._persona.owner_name} asked me to pass this on: {reply_text}",
            True,
            f"{self._persona.owner_name} replied: {reply_text}",
        )
        if not self._enabled:
            return fallback

        system = _load_prompt("callback_script.md", self._persona)
        if not system:
            return fallback

        payload = {
            "owner_reply": reply_text,
            "call_was_about": context.get("summary", ""),
            "caller_name": context.get("caller_name", ""),
            "category": context.get("category", ""),
        }
        try:
            content = await self._chat(
                system, json.dumps(payload, ensure_ascii=False), timeout
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("callback script failed: %s", type(exc).__name__)
            return fallback

        parsed = _extract_json(content or "")
        if not parsed:
            log.info("supervisor did not return usable JSON, relaying verbatim")
            return fallback

        script = re.sub(r"\s+", " ", str(parsed.get("script", "") or "")).strip()
        if not script:
            return fallback

        note = re.sub(r"\s+", " ", str(parsed.get("note", "") or "")).strip()
        return script[:600], bool(parsed.get("expect_reply", True)), note[:300]

    async def deliver(self, payload: dict, timeout: float) -> tuple[bool, str]:
        """Hand the finished call record over and read back the summary.

        Returns ``(accepted, composed_summary)``. The text is returned rather than
        sent, because the supervisor is not in the delivery path - this service
        sends it. See `notify.py` for why that split exists.
        """
        if not self._enabled:
            return False, ""

        system = _load_prompt(
            "summary.md", self._persona, {"enrichment": _enrichment(self._persona)}
        )
        if not system:
            return False, ""

        try:
            content = await self._chat(
                system, json.dumps(payload, ensure_ascii=False), timeout
            )
            return True, (content or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("supervisor delivery failed: %s", type(exc).__name__)
            return False, ""
