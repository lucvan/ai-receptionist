"""Who the receptionist works for, and how it talks about them.

Prompts are the one place where a deployment's identity has to appear in prose,
so it appears as placeholders and is filled in from configuration rather than
being edited into the text. That keeps `prompts/` shippable as-is: clone the
repo, set `OWNER_NAME`, and the agent introduces itself correctly.

## Placeholders

Written `{{like_this}}` and substituted at load time. The prompt files are
re-read on every call, so changing any of this takes effect on the next call
with no rebuild and no restart.

| placeholder | from | example |
|---|---|---|
| `{{owner_name}}` | `OWNER_NAME` | `Sam` |
| `{{owner_them}}` | `OWNER_PRONOUN_OBJECT` | `them` / `him` / `her` |
| `{{owner_their}}` | `OWNER_PRONOUN_POSSESSIVE` | `their` / `his` / `her` |
| `{{assistant_name}}` | `ASSISTANT_NAME` | `Sam's assistant` |
| `{{locale_note}}` | `LOCALE_NOTE` | accent and number-reading guidance |

**Only the object and possessive pronouns are exposed, deliberately.** Those are
the two that read correctly for every pronoun set without changing the verb
around them: "pass it to them", "pass it to him", "their calendar", "her
calendar" all work unaltered. A subject pronoun would need the verb conjugated
with it ("they are" against "he is"), so the prompts are written to use the
owner's name in those positions instead. If you translate or rewrite a prompt,
keep to that rule.

The default pronouns are they/them, which is correct for an unset value rather
than merely neutral: nothing about a name tells you what someone uses, and this
text is spoken aloud to strangers about a real person.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

# Shipped as the default so the prompt works out of the box for a UK deployment
# and is obviously the thing to change for anywhere else. It is prose, not code:
# rewrite it freely.
DEFAULT_LOCALE_NOTE = (
    "You are British. Speak with a natural, warm southern English accent, at an "
    "unhurried conversational pace. Use British phrasing throughout - "
    '"brilliant", "no problem at all", "lovely", "sorry, could you say that '
    'again?" - and British pronunciation of letters and numbers. Say "oh" for '
    'zero in phone numbers, "zed" for Z, and read numbers in natural pairs the '
    'way people actually do: "oh-seven-seven-double-oh, nine-hundred, '
    'one-two-three".'
)


@dataclass(frozen=True)
class Persona:
    owner_name: str
    owner_them: str
    owner_their: str
    assistant_name: str
    locale_note: str

    @classmethod
    def from_env(cls) -> "Persona":
        owner = os.environ.get("OWNER_NAME", "").strip()
        if not owner:
            # Deliberately not a crash: an unconfigured service should still
            # answer and take a message rather than refuse calls. But it will
            # introduce itself as "the assistant", which is odd enough that
            # nobody leaves it like this for long.
            log.warning(
                "OWNER_NAME is not set - the agent will not use a name. "
                "Set it in .env; see the Setup guide."
            )
            owner = "the owner"

        assistant = os.environ.get("ASSISTANT_NAME", "").strip()
        if not assistant:
            assistant = (
                f"{owner}'s assistant" if owner != "the owner" else "the assistant"
            )

        return cls(
            owner_name=owner,
            owner_them=os.environ.get("OWNER_PRONOUN_OBJECT", "").strip() or "them",
            owner_their=os.environ.get("OWNER_PRONOUN_POSSESSIVE", "").strip()
            or "their",
            assistant_name=assistant,
            locale_note=os.environ.get("LOCALE_NOTE", "").strip()
            or DEFAULT_LOCALE_NOTE,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "owner_name": self.owner_name,
            "owner_them": self.owner_them,
            "owner_their": self.owner_their,
            "assistant_name": self.assistant_name,
            "locale_note": self.locale_note,
        }


def render(text: str, persona: Persona, extra: dict[str, str] | None = None) -> str:
    """Substitute `{{placeholders}}`. Unknown ones are left alone and logged.

    Left alone rather than blanked: a typo that silently deleted a sentence from
    a prompt would be very hard to notice, whereas one that reads `{{ownr_name}}`
    aloud is obvious on the first call.

    `extra` carries placeholders that are computed rather than configured - the
    supervisor's optional enrichment block, for instance - so they resolve in the
    same pass instead of needing a second substitution that would trip the
    unknown-placeholder warning on the way through.
    """
    values = {**persona.as_dict(), **(extra or {})}
    unknown: set[str] = set()

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        unknown.add(key)
        return match.group(0)

    out = PLACEHOLDER.sub(substitute, text)
    if unknown:
        log.warning("unknown prompt placeholders: %s", ", ".join(sorted(unknown)))
    return out
