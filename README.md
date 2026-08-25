# ai-receptionist

A phone receptionist for a Twilio number. It answers the call, finds out who is
ringing and why, takes a message, classifies it, and sends you a summary — over
Telegram, email, a webhook, WhatsApp, or any combination of those.

Optionally it hands the call record to a **supervisor** first: any endpoint
speaking the OpenAI chat-completions API, which writes the summary and can enrich
it from whatever it has access to. Without one, summaries are composed locally.

It is built around one assumption: **the caller is untrusted**. Anyone can dial the
number and say anything, including a scripted attempt to talk the agent into
disclosing something. So the caller-facing agent is deliberately the least
privileged component in the system.

**[Setup guide →](SETUP.md)**  ·  **[Onboarding for contributors →](CONTRIBUTING.md)**

```
caller → Twilio number → POST /incoming-call  (signature-checked)
                       → WSS /media-stream    (per-call token)
                       → OpenAI Realtime  ─┐  (g711 μ-law, both directions)
                         or ElevenLabs    ─┘
                       → structured summary → supervisor (optional)
                                             → Telegram / email / webhook / WhatsApp
```

Either voice provider answers the phone; everything after it is the same code.
See [Voice provider](#voice-provider).

The supervisor is any endpoint that speaks the OpenAI chat-completions API —
OpenAI itself, a local Ollama or vLLM, a gateway like LiteLLM or OpenRouter, or
an agent framework with an OpenAI-compatible front door. It only ever writes
text; it is not in the delivery path and the service works with it switched off
(`SUPERVISOR_ENABLED=false`), composing summaries locally instead.

## Privilege boundary

This is the part to re-read before changing anything.

The container has **no** access to: any notes or document store, agent profile
data or memory, home automation, the Docker socket, the LAN, any host path
outside its own directory, or any shell/admin API. It holds one voice-provider
key (OpenAI or ElevenLabs, depending on `VOICE_PROVIDER`), a Twilio auth token, a
bearer key for the supervisor endpoint, and one credential per notification
channel you enable — nothing else.

**Composition and delivery are split on purpose.** The supervisor is the side
with the context to write a useful summary, so it writes one; but it is also the
side with read access to your notes, and giving *that* a messaging tool is a
worse trade than giving this container a narrow outbound credential. So the
supervisor writes the words and this container sends them.

That trade is what bounds the blast radius, and it is worth keeping in mind when
choosing channels. A Telegram bot that can only post to one chat id, or an SMTP
account that can only send mail, means a compromise of this container buys an
attacker the ability to send *you* messages. A WhatsApp bridge signed into your
personal account would buy them rather more — which is why the docs keep saying
to pair a dedicated number.

The agent has exactly four tools, none of which read anything:

| tool | what it does |
|---|---|
| `classify_call` | records a category and urgency |
| `take_message` | records the caller's details and the message |
| `flag_urgent` | raises the priority of the end-of-call notification |
| `end_call` | hangs up |

`transfer_call` is added only when `TRANSFER_ENABLED=1`, so an agent that must not
transfer cannot be talked into it — it never sees the tool.

The live call makes **no outbound calls of any kind**. The supervisor is reached
only after the caller has hung up, to compose the notification. A fifth tool,
`ask_supervisor`, was removed: see `src/tools.py` for the measurements that
killed it.

Nothing the caller says is ever forwarded to the supervisor as an instruction. The
supervisor sees a summary the receptionist wrote *about* the call.

## Data minimisation

One JSON line per call in `logs/calls-YYYY-MM.jsonl`, holding only: caller name,
company/relationship, callback number, reason, requested action, urgency,
category, a short summary, and call metadata.

**Full transcripts are not retained.** `RETAIN_TRANSCRIPTS` defaults to false. On
OpenAI the service does not even ask for transcription when it is off, so caller
audio is never sent to a second model. On ElevenLabs the transcript arrives
unasked as part of the conversation stream — it is discarded rather than written.
Turning retention on is a policy decision, not a config tweak, and the fact that
one provider gives it away free does not change that.

Phone numbers are partially redacted in container logs (`+44...000`). Secrets are
never logged — note that `httpx` request logging is forced to WARNING, because the
Telegram Bot API carries the bot token **in the URL path** and httpx logs full URLs
at INFO. Leaving that at default writes a live credential into the logs on every
call.

## Caller history

Previous calls from the same number are read back out of the call log and put in
front of the agent before it speaks, so a repeat caller is not interrogated from
scratch. Nothing extra is collected — this is the log that was always being
written, indexed by `from_number`.

It is the counter-example to the supervisor consult that was removed: a signal the
agent cannot otherwise have, costing a file read rather than a network round trip.
The agent is told to use it to avoid re-asking, to recognise a repeat matter, and
to treat a third chase about the same thing as more urgent than the caller's tone.

**Numbers are verified against what we already hold.** A number the agent heard
down a phone line is the least reliable thing on a call record — one was once
written with an extra trailing digit and the callback dialled it, connecting only
because the carrier discarded the overdial. Twilio's caller ID and the
saved contact book are both authoritative over the agent's transcription: a spoken
number that is a *near miss* of either (one digit extra, missing, wrong, or two
transposed) is replaced by the saved one. Two or more digits apart counts as a
genuinely different number and is honoured, since that is what the field is for.
The history index applies the same rule, so a call logged against a mistyped
number still appears under the right person instead of splitting them in two.

**Your own replies are part of it.** Replying to a call summary in Telegram files
that reply against the caller's number, so the next call — and any callback —
starts knowing what you said. A reply beginning `note:` files it *without* ringing
anyone back, for context only. Notes can also be added from the admin UI.

`HISTORY_ENABLED=false` turns the whole thing off; `HISTORY_MAX_CALLS` (3) bounds
how many previous calls are shown.

## Admin UI

A small password-protected UI for the contact book and the call log: one profile
per caller with their full history, contact details pre-filled with a suggested
name taken from what they have given on previous calls, and a box for your notes.

Contacts carry a full name, **what you actually call them**, their relationship to
you, and free-text context the agent reads when they ring. The original flat
`{"number": "Name"}` entries still load unchanged, and an entry with nothing but a
name is written back in that same short form rather than being inflated into an
object.

One list, not two: saved contacts and people who have merely rung are the same
kind of thing, so they share a page, with a `contact` flag, a Contacts filter, and
an Add contact button. A saved contact who has never called still gets a profile
page — otherwise the people most worth annotating would have no way in.

**Merging** folds one number's calls into another profile, which is also how a
person gets two numbers: the profile then lists both, each call row shows which
one they rang on, and an incoming call on either is recognised as that contact. It
is stored as an alias map (`logs/number-aliases.json`), not a log rewrite, so the
call log keeps saying what actually happened and a merge can be undone.

**Deleting** works on a single call, on every call for a caller, or on a note.
Deletes rewrite the log through a temp file and an atomic move, so an interrupted
delete cannot truncate it. Deleting call history also removes it from the agent's
context, which is the point.

**Settings** live here too: which notification channels are on, where each call
category goes, and how the agent handles a call — greeting, voice, turn-taking,
wrap-up thresholds, how much caller history it sees. Saved to
`config/settings.json` and re-read when it changes, so a change applies to the
next call with no restart.

What is *not* here, deliberately: every secret, plus `TRANSFER_ENABLED`,
`TRANSFER_TO_NUMBER`, `CALLBACKS_ENABLED`, `VALIDATE_TWILIO_SIGNATURE` and
`RETAIN_TRANSCRIPTS`. The dividing line is what a stolen admin session would buy.
A field on this page is one where the worst outcome of someone else editing it is
annoyance; a field that stays in `.env` is one where the worst outcome is a bill,
a disabled signature check, or a stranger's phone ringing.

**It runs on its own port and must never be proxied publicly.** The main app has
to stay reachable by Twilio; anything mounted there is on the open internet. The
admin UI listens on `ADMIN_PORT` (5051), published to `127.0.0.1` only, and is
reached over a VPN or Tailscale rather than by opening the port:

```bash
# example: publish it to your tailnet only, never to the internet
tailscale serve --bg --https=8443 http://localhost:5051
tailscale serve --https=8443 off           # to withdraw it
```

The password (`ADMIN_PASSWORD`, in `.env`) is the second lock, not the only one —
an empty value disables the UI rather than serving it open. Sessions are
HMAC-signed cookies; eight failed attempts locks that IP out for five minutes.

## Endpoints

| route | purpose |
|---|---|
| `GET /health` | liveness and configuration readiness. Reports missing setting *names*, never values. |
| `POST /incoming-call` | Twilio voice webhook. Returns TwiML connecting the call to the media stream. |
| `WSS /media-stream` | bidirectional call audio. |

FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled.

### How the two hops are authenticated

Twilio signs the webhook POST but **not** the WebSocket upgrade, so each hop needs
its own check:

1. `/incoming-call` verifies `X-Twilio-Signature` (HMAC-SHA1 over the URL plus
   alphabetically-sorted POST params, keyed on the account auth token).
2. `/incoming-call` then mints a token bound to that `CallSid`, valid for 120
   seconds, and embeds it in the TwiML `<Stream>` element. `/media-stream` verifies
   it before opening anything upstream.

Without step 2, anyone who learned the hostname could open a media socket and burn
your voice-provider credit. Tokens for a different call, forged tokens, and
expired tokens are all rejected with close code 1008.

The signature is computed against `PUBLIC_BASE_URL`, not against the URL the app
sees. Behind the reverse proxy those differ, and rebuilding from the request would
fail every signature.

## Configuration

Copy `.env.example` to `.env`. Every value is environment-specific; nothing real is
committed.

The two that are easy to get wrong:

- **`TWILIO_AUTH_TOKEN` is the account Auth Token, not the API key secret.** Only
  the auth token signs webhooks, and it cannot be read back through the API — it
  has to come from Console → Account → API keys & tokens.
- **`PUBLIC_BASE_URL` must match the Twilio webhook URL character for character**,
  or signature validation fails on every call.

While anything required is missing, `/health` reports `degraded` and lists the
missing names, and inbound calls get a polite unavailable message instead of
connecting.

## Setup wizard

`http://127.0.0.1:5051/setup` — five steps from a clean install to a phone that
answers, without editing a dotfile.

Two principles run through it:

**Every list is read from the account, not hardcoded.** ElevenLabs voices and
agents, OpenAI Realtime models, Twilio phone numbers — all fetched live, so a
model the vendor shipped yesterday is in the dropdown today. This is not
theoretical: the first draft offered two OpenAI models from a literal, against an
account that had eight. The one exception is the OpenAI *voice* list, which has
no endpoint anywhere and is maintained by hand; the UI says so rather than
implying otherwise.

**Anything with a knowable set is a dropdown.** A mistyped voice name or phone
number does not fail on the settings page — it fails on a live call, days later,
as a caller hearing silence. Fetching the list doubles as a credential check, so
"this key has no ConvAI permissions" or "this account has no Realtime access" is
said at setup time.

| Step | What it does |
|---|---|
| Identity | Who the receptionist answers for — fills the prompt placeholders |
| Voice | Provider, key, voice, and the **ElevenLabs provisioning button** |
| Phone line | Twilio credentials, checked; number picked from your account |
| Notifications | Credentials for the delivery channels |
| Finish | Live readiness check, and a generator for the media-signing key |

### Credentials in the UI, and the line that did not move

The wizard writes credentials to `config/secrets.json` (gitignored, mode 0600).
They are **write-only**: a value that goes in never comes back out, on any page.
So a stolen admin session can *replace* a key — loud, and recoverable by setting
it again — but cannot *read* one, which would be silent and permanent.

What stayed in `.env`, deliberately, is everything whose misuse changes the
**security posture** rather than a destination:

| Env-only | Why |
|---|---|
| `ADMIN_PASSWORD` | a session must not be able to change its own lock |
| `VALIDATE_TWILIO_SIGNATURE` | turning it off opens the webhook to anyone |
| `TRANSFER_ENABLED` / `TRANSFER_TO_NUMBER` | retargeting where a caller is patched through is phone fraud |
| `CALLBACKS_ENABLED` | spends money and rings real people |
| `RETAIN_TRANSCRIPTS` | a retention policy, not a setting |

`STREAM_TOKEN_SECRET` is a half-exception: the wizard can *generate* one, but
there is no box to type one into — nobody should be inventing that value by hand.

A deployment configured entirely through `.env` is unaffected by any of this: the
overlay only applies where a value has actually been set.

## Voice provider

`VOICE_PROVIDER` picks what answers the phone: `openai` (Realtime) or
`elevenlabs` (Agents). Both speak G.711 μ-law at 8 kHz, so audio is relayed to
Twilio without transcoding either way.

Everything downstream is the same code: the same five tools, the same
`CallRecord`, the same supervisor, notification channels, routing, caller
history, callbacks and transfers. `src/bridge.py` holds all of it; the two
providers differ only in wire format.

|  | OpenAI Realtime | ElevenLabs Agents |
|---|---|---|
| Tools | sent per session, from `tools.py` | **provisioned on the agent first** |
| Prompt | per session | per call, but must be **allowlisted** on the agent |
| Turn-taking / barge-in | ours to enforce | server-side |
| Opening line | a forced first turn | `first_message` config |
| After a tool result | must ask for the next turn | continues by itself |
| Where you tune it | `.env` + admin UI | the ElevenLabs dashboard |

### ElevenLabs needs provisioning before the first call

A Realtime session is handed its tool list at connect time. An ElevenLabs agent
cannot be: tools are workspace objects the agent references by id. So three
things have to be right on the agent *before* it can take a call, and all three
fail quietly when they are not:

| Wrong | What you hear |
|---|---|
| Tools missing | A perfectly normal call that records nothing. The notification says a call happened and nothing else. |
| Overrides not allowlisted | The agent answers with its stored prompt and none of the call's context — it reads like a bad model, not a bad config. |
| Audio not `ulaw_8000` both ways | White noise. |

`scripts/elevenlabs_setup.py` sets all three and prints the agent id:

```bash
python scripts/elevenlabs_setup.py --dry-run   # see exactly what it will send
python scripts/elevenlabs_setup.py             # create it
python scripts/elevenlabs_setup.py --agent-id agent_xxx   # re-run after editing tools.py
```

Stdlib only, so it runs before the container exists. Re-run it whenever
`src/tools.py` changes — the agent's copy of the tool surface does not update
itself.

Two things it deliberately does *not* do. It leaves `transfer_call` out unless
you pass `--include-transfer`, matching the runtime rule that an agent which
cannot see the tool cannot be talked into using it. And it gives the agent a
working fallback prompt rather than a stub, so a dropped override degrades to a
generic-but-safe receptionist instead of an agent improvising with no
instructions on an open line.

### The two caveats worth knowing

**Post-hangup message recovery works differently.** When the agent hangs up
without having called `take_message`, both bridges go back and ask for it rather
than sending a notification that says only "a call happened". OpenAI can be made
to answer with a forced tool call. ElevenLabs has no equivalent, so the bridge
injects a text turn instead — which means a *retained* transcript ends with a
line the caller never said. It is prefixed `[system, not spoken by the caller]`
to make that unmistakable. The reply also generates speech into a closed socket:
a few seconds of TTS spent to turn an empty notification into a useful one.

**Transcripts are free on ElevenLabs and still not kept.** It sends both sides of
the conversation as text at no extra cost, where OpenAI needs a second model
billed separately. They are still discarded unless `RETAIN_TRANSCRIPTS=true` —
see [Data minimisation](#data-minimisation). Cheap to collect is not a reason to
keep.

## Lifecycle

Run from the install directory.

```bash
docker compose up -d          # start
docker compose down           # stop
docker compose logs -f        # follow
docker compose build && docker compose up -d   # rebuild after a code change
```

The prompt at `prompts/receptionist.md` is bind-mounted and re-read on every call,
so wording changes take effect on the next call with no rebuild and no restart.

## Container hardening

Read-only root filesystem, 16 MB tmpfs for `/tmp`, all capabilities dropped,
`no-new-privileges`, non-root user with `nologin` as its shell, and its own bridge
network that is not joined to any other project. Log rotation is capped at 3 × 10 MB.

There is no Docker `HEALTHCHECK`: a CLI probe that boots a Python interpreter on a
short interval burns measurable idle CPU for very little signal. Probe `/health`
externally instead.

## Call guards

A call ends automatically after `MAX_CALL_SECONDS` (default 300) or after
`SILENCE_HANGUP_SECONDS` of no activity (default 25). The agent can also hang up
itself via `end_call` — it says its closing line first, then the service sends a
Twilio `mark` and closes only once Twilio echoes it back, so the goodbye is never
cut off mid-word.

## The supervisor

Optional. It does two things, both **after the caller has hung up**, so it is
never on the critical path of a live call:

1. **Writes the call summary** that gets delivered to you.
2. **Turns your reply into a spoken script**, when you reply to a summary and
   the caller is rung back.

With `SUPERVISOR_ENABLED=false` the service composes summaries locally from the
call record and relays your reply verbatim. That is a supported way to run this,
not a degraded one — it makes no outbound call and costs nothing.

### It is just a chat-completions endpoint

`SUPERVISOR_URL` is an **origin**; `/v1/chat/completions` is appended to it. The
client sends a system prompt and one JSON user message, with `stream: false`, and
reads `choices[0].message.content`. Anything that speaks that will work:

| Setup | `SUPERVISOR_URL` | `SUPERVISOR_MODEL` | Key |
|---|---|---|---|
| None | — | — | `SUPERVISOR_ENABLED=false` |
| OpenAI | `https://api.openai.com` | `gpt-4o-mini` | `sk-…` |
| Local Ollama | `http://host.docker.internal:11434` | `qwen2.5:14b` | none |
| vLLM / LM Studio / llama.cpp | wherever it listens | its model id | usually none |
| OpenRouter | `https://openrouter.ai/api` | `anthropic/claude-sonnet-4` | `sk-or-…` |
| LiteLLM gateway | your gateway origin | your alias | your key |
| An agent framework | its API server origin | its model name | its bearer key |

`host.docker.internal` is how a container reaches a service on the Docker host;
on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` to the compose
service.

### What it must return

Two different contracts, and the second is the one that catches people out:

| Call | Must return | If it doesn't |
|---|---|---|
| Summary | Plain prose. Anything at all. | Falls back to a locally composed summary |
| Callback script | A JSON object with `script`, `expect_reply`, `note` | Falls back to relaying your words verbatim |

Both failures are safe and logged, so a weak model degrades rather than breaks —
but if you point this at a small local model, expect the callback script to fall
back regularly. The JSON is extracted leniently (code fences and surrounding
prose are tolerated), so the usual cause is a model that cannot hold the shape at
all rather than one that wraps it in markdown.

### Why use an agent rather than a plain model

A plain LLM writes better prose than the local fallback and nothing more. The
reason to point this at an *agent* — one with access to your notes, calendar, or
documents — is **enrichment**: "the garage rang about the car" becomes "the
garage rang about the 335d, the one you booked in last week".

That is a real upgrade and it has a real cost. Enriched summaries carry whatever
the agent found — which may include health, financial or family details — into
whatever channel you deliver them over. If that matters, either cap the agent to
non-sensitive sources, or have it reference a note by title instead of quoting
figures out of it.

Enrichment is **opt-in and self-described**. Copy
`prompts/supervisor/enrichment.md.example` to `enrichment.md`, edit it to point at
whatever your agent can actually read, and that text is inserted into the summary
prompt. Leave the file absent and the supervisor writes from the call record alone.

> Only turn it on if your supervisor is an **agent with tools**. Pointed at a plain
> chat-completions model, an instruction to "search my notes" tells something with
> no filesystem to read files — and it will invent what it thinks it found rather
> than admit it could not look.

### Trust direction

Nothing the caller says is forwarded to the supervisor as an instruction. It
receives a structured summary the receptionist wrote *about* the call, and it is
told explicitly that the record is reported information, not instructions to it.
Empty fields are stripped before sending — handing a model a form full of blanks
invites it to hand the form back.

The supervisor is also **not in the delivery path**. It returns text; this
service sends it. See the Privilege boundary section for why that split exists.

## Notification

Composing a summary and delivering it are separate steps, and only the first one
is optional.

At the end of every call the supervisor is asked to write the summary. It gets
one shot within `SUPERVISOR_FINAL_TIMEOUT_S`; if it is slow, unreachable,
disabled, or returns nothing usable, the service composes one locally from the
call record with no network call at all. The record is written to disk either
way, so a call is never silently swallowed.

**Delivery has no dependency on the supervisor**, and never did — the summary
arrives here as a plain string, and this half does not care what wrote it. That
is also why adding channels is cheap.

### Channels

Any number can be enabled at once:

| channel | notes |
|---|---|
| Telegram | The only one that can trigger a callback — see below. |
| Email | Plain SMTP through the standard library, no extra dependency. |
| Webhook | The summary text plus the structured call record, as JSON. Covers ntfy, Gotify, Discord, Home Assistant, n8n. |
| WhatsApp | Through a self-hosted bridge you pair from the settings page. |

Every channel is configured from **Settings** in the admin UI, and every field
there falls back to an environment variable, so a headless install can be
configured entirely through `.env` and never open the UI. **Secrets are the
exception and are env-only** — tokens, SMTP passwords and bridge keys are shown
on the settings page as present or missing, never as values, and are never
written to `settings.json`.

### Routing

A table decides which channels each call category reaches. Untick every box for
a category and you are simply not told about that kind of call — spam going
nowhere is the obvious use, and the call is still recorded and still visible in
the admin UI. With no routing configured, every enabled channel gets everything.

Delivery failure is logged at ERROR. A category that is deliberately routed
nowhere is logged at INFO, because that is a setting rather than a fault.

### Why replies are Telegram-only

Sending fans out; replying does not. Replying to a summary is what makes the
service **ring a real person**, so it needs two things a channel must actually
provide: a reliable way to tell which call a reply answers, and a sender
identity that is not trivially forged. Telegram has both
(`reply_to_message.message_id`, and a numeric user id checked against an
allowlist). Email has neither — a forged `From` header that places a phone call
is not a boundary worth defending. So other channels deliver the summary and
stop there.

### WhatsApp needs a bridge, and a dedicated number

There is no official WhatsApp API that will let a service send arbitrary prose
to its owner. The Business Platform requires a pre-approved template for
anything business-initiated outside a 24-hour window opened *by the user*, and
template parameters cannot contain newlines — which a summary does, deliberately,
so the callback number lands on its own tappable line.

So this talks to a self-hosted WhatsApp Web gateway (WAHA, Evolution API, or
anything similar) over HTTP, paired by scanning a QR from the settings page.
`custom` takes a URL and a JSON body template with `{to}` and `{text}`
placeholders, for a bridge these adapters do not fit.

**Pair a second number, not your personal account.** The bridge signs in as a
real WhatsApp account and can message anyone that account can reach. Pointing it
at your own account changes this container's worst case from "can send you
messages" to "can impersonate you to all your contacts". The receptionist only
ever needs to message one person.

## Known limitations

- **Transfer is off by default.** `TRANSFER_ENABLED=1` plus a `TRANSFER_TO_NUMBER`
  turns it on, and the agent is only given the tool when both are set — an agent
  that cannot see the tool cannot be talked into using it.
- **You are told after the call, not during it.** The summary is composed once the
  caller has hung up. With no supervisor that is immediate; with one it is a few
  seconds, and longer if it searches your notes first (`SUPERVISOR_FINAL_TIMEOUT_S`
  defaults to 150s). If you need to know while the phone is still ringing, transfer
  is the mechanism, not the notification.
- **Only Telegram can trigger a callback.** Other channels deliver the summary and
  stop there. See "Why replies are Telegram-only" under Notification.
- **Caller history is keyed on caller ID, which is spoofable.** Someone dialling
  from a faked number would be told what that number discussed before. The
  disclosure rules do not relax for a recognised caller, but the history does
  surface. Set `HISTORY_ENABLED=false` if that trade is wrong for you.
- **Barge-in relies on Twilio's media timestamps.** If the agent ever talks over an
  interrupting caller, that truncation arithmetic is the place to look.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

Permissive, so you can use this commercially and privately without obligation. The
patent grant is the reason for Apache over MIT: telephony and voice-agent patents
are thick on the ground, and an explicit grant from contributors is worth having if
anyone builds a product on this.

## A word on what this is

This was built for one household and then generalised, which shows in places: the
prompt is opinionated, the default locale is British, and a few design decisions
(no mid-call supervisor consult, Telegram-only replies) are the result of specific
things going wrong on real calls rather than of a survey of alternatives. Those
stories are in the code comments, and they are the most useful thing here.

It answers real phone calls, and it has been wrong in production in ways that cost
money — a mistranscribed digit once dialled a stranger. Read the privilege boundary
and the guards before pointing it at a number that matters.
