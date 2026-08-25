# Setup

Getting from a clone to a phone number that answers itself. Budget about an hour,
most of it waiting for a Twilio number and a TLS certificate.

There are two ways to run this and the order matters — **do the local walkthrough
first**. It costs nothing, needs no phone number, and every mistake you are going to
make is cheaper to find there.

---

## What you need before you start

| | Why | Cost |
|---|---|---|
| **A voice provider account** — *either* an OpenAI API key with Realtime access *or* an ElevenLabs API key | The agent itself | ~$0.06/min on `gpt-realtime-mini` at time of writing; ElevenLabs bills its own way |
| **A Twilio account and a voice number** | The phone line | ~£1/month plus per-minute |
| **Somewhere to run Docker** | A always-on box, a VPS, a Pi 5 | — |
| **A public HTTPS hostname** | Twilio has to reach your webhook and open a WebSocket to it | — |
| A supervisor endpoint | *Optional.* Writes nicer summaries | — |

The public hostname is the part people underestimate. Twilio needs to reach **your
box** over HTTPS, and it needs to open a **WebSocket**, so a tunnel that only
forwards plain HTTP will not do. Options, easiest first:

- **Cloudflare Tunnel** — free, no port forwarding, WebSockets work.
- **A reverse proxy on a box you already expose** — Caddy or nginx with
  `proxy_set_header Upgrade`/`Connection` set.
- **ngrok** — fine for the walkthrough, but the URL changes on every restart and you
  will have to update Twilio each time.

---

## 0. Or skip most of this: the setup wizard

Once the container is up with an `ADMIN_PASSWORD` set, `http://127.0.0.1:5051/setup`
walks the same ground in five steps, with every list read live from your accounts
rather than typed. It can set the voice-provider key, Twilio credentials, the
phone number, the notification credentials, and provision the ElevenLabs agent.

Three things it deliberately cannot do, because they change the security posture
rather than a destination — set them in `.env` yourself:
`TRANSFER_TO_NUMBER`, `VALIDATE_TWILIO_SIGNATURE`, and `ADMIN_PASSWORD` itself.
`PUBLIC_BASE_URL` is also `.env`-only: it has to match the Twilio webhook
character for character, and a trailing slash typed into a box breaks every call.

The walkthrough below is still worth reading once — it explains *why* each piece
is there, which the wizard does not.

---

## 1. Local walkthrough (no phone number, no cost)

```bash
git clone https://github.com/lucvan/ai-receptionist.git
cd ai-receptionist
cp .env.example .env
```

Open `.env` and set four things:

```bash
OPENAI_API_KEY=sk-...
OWNER_NAME=Sam                      # whose calls this answers
PUBLIC_BASE_URL=http://localhost:5050
STREAM_TOKEN_SECRET=                # generate it, see below
VALIDATE_TWILIO_SIGNATURE=false     # ONLY for local testing
ADMIN_PASSWORD=something-you-pick
```

Generate the stream secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Start it:

```bash
mkdir -p logs
docker compose up -d
curl -s localhost:5050/health
```

You want:

```json
{"status":"degraded", "missing_config":[], "notification_channels":[]}
```

`degraded` with an empty `missing_config` means **the service is fine but nothing
would tell you about a call**. That is the next step.

### Add a notification channel

Email is the quickest to prove. Add to `.env` and restart:

```bash
SMTP_HOST=smtp.fastmail.com
SMTP_PORT=465
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=...
SMTP_SENDER=you@example.com
EMAIL_TO=you@example.com
```

```bash
docker compose up -d
curl -s localhost:5050/health     # -> "status":"ok", channels:["email"]
```

Now open the admin UI at **http://localhost:5051**, sign in with `ADMIN_PASSWORD`,
go to **Settings**, and press **Send test** on the Email card. If a message arrives,
the whole notification path works.

> **Do not skip the test button.** SMTP fails in quiet ways — wrong port for the TLS
> mode, an app-password requirement, a provider blocking logins from new IPs. Finding
> that out now beats finding it out from a call you missed.

---

## 2. Give it a phone number

### Buy a number

In the Twilio console, buy a **voice-capable** number in the country you want to be
reachable in. Note your **Account SID** and **Auth Token** from Account → API keys &
tokens.

> **It must be the Auth Token, not an API Key secret.** Only the Auth Token signs
> webhooks, and it cannot be read back through the API — it only ever appears in the
> console. Getting this wrong produces a 403 on every single call.

### Expose your box

Whatever you choose, you need a stable `https://` origin that reaches port 5050 and
passes WebSocket upgrades. With Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://localhost:5050
```

Take the `https://something.trycloudflare.com` URL it prints.

### Point Twilio at it

Set these in `.env`:

```bash
PUBLIC_BASE_URL=https://something.trycloudflare.com
VALIDATE_TWILIO_SIGNATURE=true
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+15551234567
```

Then in the Twilio console, on your number: **A call comes in → Webhook →
`https://something.trycloudflare.com/incoming-call`**, method **POST**.

> **`PUBLIC_BASE_URL` must match the webhook URL character for character.** The
> signature is computed against this value, not against the URL the app sees — behind
> a proxy those differ, and rebuilding it from the request would fail every call. A
> trailing slash difference is enough to break it.

Restart and ring the number.

### Set a fallback

While you are in the console, set **Primary handler fails** to a static TwiML URL
that apologises and hangs up. If your box is down, the caller otherwise gets Twilio's
"an application error has occurred", which is worse than a polite failure.

---

## 3. Make it yours

### The prompt

`prompts/receptionist.md` is bind-mounted and **re-read on every call** — edit it and
the next call picks it up. No rebuild, no restart.

It is written with placeholders so it ships usable:

| placeholder | set by | notes |
|---|---|---|
| `{{owner_name}}` | `OWNER_NAME` | |
| `{{owner_them}}` | `OWNER_PRONOUN_OBJECT` | `them` / `him` / `her` — defaults to `them` |
| `{{owner_their}}` | `OWNER_PRONOUN_POSSESSIVE` | `their` / `his` / `her` |
| `{{assistant_name}}` | `ASSISTANT_NAME` | defaults to `<owner>'s assistant` |
| `{{locale_note}}` | `LOCALE_NOTE` | accent and number-reading guidance |

`{{locale_note}}` defaults to British English, including reading "oh" for zero and
"zed" for Z. **If you are not in the UK, replace it** — otherwise your receptionist
will have an accent you did not ask for. Set it to a paragraph describing how you
want the agent to sound.

Only object and possessive pronouns are exposed, because those are the two that read
correctly for every pronoun set without changing the verb around them. If you rewrite
prompt text, use `{{owner_name}}` where a subject pronoun would go.

### The greeting

`GREETING` is spoken **verbatim** on an unknown caller. Left to improvise, the model
opens with something chatty that never identifies itself — this was observed as
*"Hi there! How's your day going so far?"*. Keep it short and make it say who is
speaking.

Callers matching a saved contact get greeted by name instead.

### Contacts

Copy `config/contacts.example.json` to `config/contacts.json`, or just add people in
the admin UI. Recognition changes the agent's **tone only** — it greets by name and
stops interrogating. It deliberately does **not** relax any disclosure rule, because
caller ID is trivially spoofed.

---

## 4. Optional pieces

### Answering with ElevenLabs instead of OpenAI

`VOICE_PROVIDER=elevenlabs` swaps the voice, the turn-taking and the billing.
Nothing else changes — same tools, same summaries, same channels, same callbacks
and transfers.

**The easy way is the setup wizard** at `http://127.0.0.1:5051/setup/voice`:
paste a key (it is checked for the right scopes before it is stored), pick a
voice from your account, press *Create the agent*. It runs the same code as the
script below and verifies the result.

The command line does the same thing, and is what you want on a headless box.
An ElevenLabs agent references its tools by id, so they have to exist before the
first call:

```bash
python scripts/elevenlabs_setup.py --check     # is the key usable?
python scripts/elevenlabs_setup.py --voices    # list voice ids
python scripts/elevenlabs_setup.py --dry-run   # see exactly what it will send
python scripts/elevenlabs_setup.py             # create it, print the agent id
python scripts/elevenlabs_setup.py --agent-id agent_x --verify   # check it
```

Then in `.env`:

```bash
VOICE_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_AGENT_ID=agent_...       # printed by the script
```

Restart, and check `/health` reports `"voice_provider": "elevenlabs"`.

Re-run the script with `--agent-id agent_...` whenever `src/tools.py` changes —
the agent keeps its own copy of the tool surface and will not update itself. Add
`--include-transfer` if `TRANSFER_ENABLED` is on; it is left out by default so an
agent that should not be able to transfer cannot see the tool.

**Make the agent private and set `ELEVENLABS_API_KEY`.** A public agent can be
reached by anyone who learns its id, on your credits. The service warns at
startup if the key is missing.

Once it is running, voice and language are editable in the admin UI. Everything
else about how the agent behaves — its model, its turn-taking, its knowledge base
— lives in the ElevenLabs dashboard, because that is where the agent lives.

### A supervisor

Writes better summaries than the built-in formatter, and can enrich them if it is an
agent with access to your notes. See **The supervisor** in the README for the endpoint
table and the response contract. Simplest version:

```bash
SUPERVISOR_ENABLED=true
SUPERVISOR_URL=https://api.openai.com
SUPERVISOR_KEY=sk-...
SUPERVISOR_MODEL=gpt-4o-mini
```

### Callbacks

Reply to a Telegram summary and the caller is rung back with what you said. This
**spends money and disturbs a real person**, so it is off by default and refuses to
start without an allowlist:

```bash
CALLBACKS_ENABLED=true
TELEGRAM_ALLOWED_USER_IDS=123456789      # your numeric Telegram user id
```

The sender is checked by **user id**, not just chat id — if the chat ever becomes a
group, membership alone would otherwise let anyone place calls.

### Transfer

Lets the agent patch a screened caller through to you.

```bash
TRANSFER_ENABLED=true
TRANSFER_TO_NUMBER=+15559876543
```

When this is off the agent is **not given the tool at all**, so it cannot be talked
into using it.

---

## 5. Before you leave it running

- [ ] Ring the number from a phone that is not in your contacts. Does it screen you
      sensibly, take a message, and hang up cleanly?
- [ ] Ring from a saved contact. Does it greet you by name?
- [ ] Try to talk it into telling you where the owner is. It should decline without
      hinting — "he's away" leaks occupancy just as much as an address does.
- [ ] Check `docker compose logs` for `summary delivered via ...` on each call.
- [ ] Confirm `/health` says `ok` and lists the channels you expect.
- [ ] Confirm your admin port is **not** reachable from outside the host.

That last one matters most. The admin UI holds the contact book, every call summary
and the settings page, and it is protected by one password. In Docker the boundary is
the compose publish:

```yaml
ports:
  - "127.0.0.1:5051:5051"     # do not widen this
```

To reach it from your other devices, put it behind Tailscale or a VPN rather than
opening the port.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every call 403s | `PUBLIC_BASE_URL` does not match the Twilio webhook exactly, or you used an API Key secret instead of the Auth Token |
| Call connects then drops immediately | WebSocket upgrade is not passing through your proxy or tunnel |
| Caller hears nothing (OpenAI) | Check `OPENAI_API_KEY` has Realtime access — `curl -s -H "Authorization: Bearer $KEY" https://api.openai.com/v1/models \| grep realtime` |
| Caller hears loud static or noise (ElevenLabs) | The agent is not set to `ulaw_8000` both ways. It cannot be fixed per call — re-run `scripts/elevenlabs_setup.py --agent-id ...`. The logs say so explicitly at `ERROR` |
| Agent ignores the caller's context and answers generically (ElevenLabs) | The prompt override is not allowlisted on the agent, so it is being dropped in silence and the fallback prompt is answering. Re-run the setup script |
| Calls drop after ~20 seconds (ElevenLabs) | Pings going unanswered. The bridge handles this; if you have forked it, check the `pong` reply still carries `event_id` |
| Nothing recorded on any call (ElevenLabs) | The client tools are missing from the agent. Run `scripts/elevenlabs_setup.py` |
| Setup script: `English Agents must use turbo or flash v2` | `eleven_flash_v2_5` is the *multilingual* line; an agent pinned to `language: en` needs `eleven_flash_v2`. `--tts-model auto` (the default) picks correctly from `--language` |
| Setup script: `Must set one of: description, dynamic_variable, ...` | A tool parameter has no description. ElevenLabs requires one on every property where OpenAI does not; the converter fills a fallback in, so this only appears if you have edited the script |
| ElevenLabs key returns 401 `missing_permissions` | The key is valid but unscoped. It needs **`convai_read`** and **`convai_write`** — note the signed-URL endpoint requires `convai_write` despite being a GET |
| `/health` says `degraded`, empty `missing_config` | No notification channel configured |
| Summaries arrive with nothing in them | The model ended the call without calling `take_message`; the end-of-call extraction backstop should catch this — check logs for `extraction` |
| Agent asks for a number it already has | `From` is not reaching the app; check your proxy is forwarding the POST body intact |
| Admin UI unreachable in Docker | `ADMIN_BIND` must stay `0.0.0.0` **inside** a container — Docker forwards to the container interface, not its loopback |
