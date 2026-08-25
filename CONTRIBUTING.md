# Onboarding

Orientation for anyone changing this code — including future you.

Read **[SETUP.md](SETUP.md)** first if you just want it running. This is about how
the thing is built and which rules are load-bearing.

---

## The one idea

Everything turns on a single boundary: **the moment the caller hangs up.**

Before it, a stranger is on a live phone line and every millisecond is audible. The
service makes **no outbound network calls at all** there beyond the audio socket.
After it, nobody is waiting, and everything optional lives there — the supervisor,
the notification channels, the callback machinery.

When you add something, the first question is which side of that line it goes on. If
the answer is "before", the bar is very high.

## The second idea

**The caller is untrusted.** Anyone can dial the number and say anything, including a
scripted attempt to talk the agent into disclosing something or into taking an
action. The caller-facing agent is therefore the least privileged component in the
system, and it stays that way.

That is why it has four tools, **none of which read anything**. They record intent
(`classify_call`, `take_message`, `flag_urgent`) or end the call (`end_call`). There
is no filesystem, network, shell, calendar or home-automation tool, and adding one is
a security decision rather than a feature decision.

`transfer_call` is only added to the tool list when `TRANSFER_ENABLED` is set. An
agent that cannot see a tool cannot be argued into using it — prefer that pattern
over prompt instructions when the stakes are real.

---

## Code map

```
src/
  server.py         HTTP + WebSocket surface, and the wiring of everything else
  realtime.py       one call, answer to hangup: audio pumps, tool dispatch, barge-in
  tools.py          the complete tool surface — this file IS the privilege boundary
  callrecord.py     the only thing kept about a call
  persona.py        whose calls this answers; prompt placeholder rendering
  config.py         env baseline
  settings.py       the UI-writable overlay on top of it, and the live-config proxy
  supervisor.py     optional: any OpenAI-compatible endpoint that writes summaries
  notify.py         delivery: Telegram, email, webhook, WhatsApp, plus routing
  whatsapp.py       bridge adapters and QR pairing
  telegram_listener.py  the only inbound control path
  contacts.py       caller recognition, number normalising, near-miss correction
  history.py        previous calls and notes, indexed by number
  outbound.py       placing and redirecting calls via Twilio
  pending.py        which summary belongs to which call; in-flight callbacks
  twilio_auth.py    signature validation and per-call stream tokens
  admin.py          the admin UI
prompts/            bind-mounted, re-read per call, placeholder-templated
tests/              pytest, no network
```

Start with `server.py`. It is the assembly point and reading it top to bottom tells
you what exists.

---

## Rules that are load-bearing

Each of these is here because something broke.

**The two hops are authenticated separately.** Twilio signs the webhook POST but
**not** the WebSocket upgrade. `/incoming-call` verifies the signature, then mints an
HMAC token bound to the `CallSid` into the TwiML `<Stream>`, which `/media-stream`
verifies. Remove the second hop and anyone who learns your hostname can open a media
socket and spend your OpenAI credit.

**Signatures are computed against `PUBLIC_BASE_URL`, not the observed request URL.**
Behind a proxy those differ and rebuilding from the request fails every call.

**Every tool result must be followed by a `response.create`.** The model does not
resume speaking after a function result — the turn is over until you ask for a new
response. Miss this and the caller sits in silence.

**...but that response must force `tool_choice: "none"`.** Prompting for a new
response after a tool result reads as "carry on", and the model carries on by calling
the *next* tool. The first fix for the silence produced a stampede:
`classify_call` → `take_message` (empty) → `end_call` inside one second. Forcing
speech means it talks to the caller between bookkeeping actions.

**`tool_choice: {"type":"function","name":"..."}` is accepted and then ignored.** The
model replies in prose. `"required"` genuinely forces a call but lets the model pick
which, so the end-of-call extraction loops until the right one lands. The nested
chat-completions form (`{"function":{"name":...}}`) is a hard error.

**A hang-up mid-response leaves a response in flight.** `response.create` is rejected
outright while one is generating, and the end-of-call extraction has to
`response.cancel` and drain first. Treat "active response" as retryable, not fatal.

**`httpx` INFO logging leaks the Telegram bot token.** The Bot API carries the token
in the URL *path* and httpx logs full URLs at INFO. `server.py` pins `httpx` and
`httpcore` to WARNING. Do not remove that, and remember it for anything else that
talks to an API with credentials in the path.

**A number the agent heard down a phone line is the least reliable thing on the
record.** One was once transcribed with an extra trailing digit and the callback
dialled it — it connected only because the carrier discarded the overdial. Twilio's
caller ID and the saved contact book both outrank the agent's transcription;
`resolved_callback()` and `ContactBook.correct()` implement that. A number two or
more digits away is treated as genuinely different and honoured, because quietly
redirecting a call to the wrong person is worse than failing to fix a typo.

**Only one process may long-poll a Telegram bot token.** Two pollers get updates
handed out alternately and both behave erratically. If your supervisor also speaks
Telegram, turn its Telegram platform off.

---

## Working on it

```bash
pip install -r requirements-dev.txt
pytest
```

The tests need no network, no Twilio, no OpenAI, and no SMTP server. They run in
about a second. There is no excuse for not running them.

What is covered: the settings/env resolution, channel selection, routing, the
WhatsApp request shapes and template escaping, and the admin settings page including
the assertion that **no secret ever reaches `settings.json`**.

What is **not** covered yet, and is the most valuable thing you could add: signature
validation, stream token minting and verification, `resolved_callback()`, and
`ContactBook.near_miss()`. Those are pure functions with no infrastructure, and they
are the ones that have actually broken.

### Changing a prompt

Just edit it. `prompts/` is bind-mounted and re-read on every call — no rebuild, no
restart. Use the `{{placeholders}}` documented in `persona.py` rather than writing a
name into the text.

### Changing behaviour

Most knobs are in `config.py` with an env var. If it should also be editable in the
UI, add it to `BEHAVIOUR_KEYS` in `settings.py` — but read the two-tier rule there
first. **Secrets and anything whose misuse costs money or safety stay env-only.** The
admin UI is one password away from being the whole security model; a field that can
retarget a transfer number would turn a stolen session into phone fraud.

### Adding a notification channel

Implement `Notifier` in `notify.py`: a `name`, an `enabled` property, and
`async send(text, payload) -> str | None`. Return `""` for "delivered but not
repliable". Add it to `CHANNEL_NAMES`, give it a card in the admin settings page, and
add its non-secret fields to `_merged()`.

Keep it **send-only**. The reply path is deliberately singular and Telegram-only —
replying is what makes the service ring a real person, and that needs both a reliable
message-to-call correlation and a sender identity that is not trivially forged.

---

## Things that look like bugs and are not

- **The agent does not read the summary back to the caller.** Deliberate.
- **It refuses to say whether the owner is available**, even to a saved contact.
  "He's away" leaks occupancy exactly as much as an address does.
- **Recognition changes tone but never privilege.** Caller ID is spoofable, so any
  rule keyed on it can be triggered by an attacker dialling from a faked number.
- **The supervisor is not in the delivery path.** It returns text; the service sends
  it. That split is why a compromise of this container buys an attacker the ability
  to send *you* messages and nothing more.
- **There is no Docker `HEALTHCHECK`.** A CLI probe that boots a Python interpreter
  on a short interval burns measurable idle CPU for very little signal. Probe
  `/health` externally.

---

## Reporting a security issue

Please do not open a public issue for anything exploitable. Open a private security
advisory on the repository instead.

Things worth reporting: anything that lets a caller extract information the prompt
forbids, anything that makes the service place a call it should not, anything that
reaches the admin surface without the password, and any path that writes a
credential to a log or to `settings.json`.
