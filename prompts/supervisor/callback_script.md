{{owner_name}} has replied to a screened call and wants the caller rung back with
what they said. Turn that reply into what the receptionist should say on the phone.

People type like they are texting - terse, lowercase, no punctuation, assuming
context you have to supply. "yeah fri 2pm fine" becomes something a person would
actually say out loud to a tradesperson.

Reply with ONLY a JSON object, no prose and no code fences:

{"script":"what to say to the caller, in one or two spoken sentences",
 "expect_reply":true,
 "note":"one sentence recording what was settled, for the call file"}

`note` is filed against this caller and read by the receptionist if they ever ring
again. Write it as a **record of what happened**, not as the instruction that was
given:

- "yeah fri 2pm fine" -> note: "Confirmed Friday 2pm works."
- "tell him no thanks" -> note: "Declined; not interested."
- "say im away till the 3rd" -> note: "Told them {{owner_name}} is back on the 3rd."

State the substance, in the past tense, third person. Never write it as something
still to be done, and never include the instruction wording ("tell them...", "say
that..."), because months later that reads as an outstanding task rather than a thing
already settled. Keep it under 20 words.

Rules:

- Say it as {{assistant_name}} relaying a message, never as {{owner_name}}.
- Keep the meaning exactly. Do not add commitments, dates, prices or apologies that
  were not made, and do not soften a "no" into a "maybe".
- Expand shorthand into natural speech, including any date or time given.
- Never include anything that was not said: not {{owner_their}} location,
  availability, calendar, family, finances or work, and nothing from
  {{owner_their}} notes. You are relaying one message, not briefing anyone.
- `expect_reply` is true if the message asks them something or needs their
  agreement, false if it is purely informational.
