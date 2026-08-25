#!/usr/bin/env python3
"""Create or update the ElevenLabs agent this service talks to.

Run this once before setting `VOICE_PROVIDER=elevenlabs`, and again whenever
`src/tools.py` changes. **Or do the same thing from the admin UI's setup wizard**
(Settings → Setup wizard), which drives the identical code in
`src/elevenlabs_provision.py` - this file is the command-line front door to it,
not a second implementation.

## Why any of this is needed

An OpenAI Realtime session is handed its tool list at connect time, so the
service can define its own tools in code and nothing needs provisioning. An
ElevenLabs agent cannot: tools are workspace objects, the agent references them
by id, and a conversation can only use what the agent already has. Same for the
override allowlist, and same for the audio format. All three are configuration
that has to exist before the first call, and all three fail quietly when they are
wrong:

- **Missing tools** - the agent talks to the caller perfectly well and records
  nothing. The notification says a call happened and nothing else.
- **Overrides not allowlisted** - the override is *ignored, not rejected*, so the
  agent answers with its stored prompt and none of this call's context. It reads
  like a bad model rather than a bad config.
- **Wrong audio format** - the caller hears white noise.

## Usage

    export ELEVENLABS_API_KEY=...          # or put it in .env
    python scripts/elevenlabs_setup.py --check       # is the key usable?
    python scripts/elevenlabs_setup.py --voices      # list voice ids
    python scripts/elevenlabs_setup.py --dry-run
    python scripts/elevenlabs_setup.py

    # later, after editing src/tools.py:
    python scripts/elevenlabs_setup.py --agent-id agent_xxxx
    python scripts/elevenlabs_setup.py --agent-id agent_xxxx --verify

Stdlib only, deliberately: this runs on whatever machine is doing the setup,
before the container exists, and should not need a virtualenv first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.elevenlabs_provision import (  # noqa: E402
    ApiError,
    agent_body,
    check_key,
    list_voices,
    provision,
    to_client_tool,
    verify,
)
from src.tools import TOOL_SPECS, TRANSFER_TOOL  # noqa: E402


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader, so the key does not have to be exported by hand."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--agent-id", default="",
                        help="update this agent instead of creating one")
    parser.add_argument("--name", default="ai-receptionist")
    parser.add_argument("--voice-id", default="",
                        help="blank keeps whatever voice the agent has")
    parser.add_argument("--llm", default="",
                        help="blank uses the ElevenLabs default model")
    parser.add_argument("--tts-model", default="auto",
                        help="'auto' picks the flash model matching --language; "
                             "pass '' to leave the agent's own setting alone")
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-duration", type=int, default=600,
                        help="hard ceiling; the service also has MAX_CALL_SECONDS")
    parser.add_argument("--include-transfer", action="store_true",
                        help="also provision transfer_call (needs TRANSFER_ENABLED)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent and change nothing")
    parser.add_argument("--check", action="store_true",
                        help="check the key works, then stop")
    parser.add_argument("--voices", action="store_true",
                        help="list voices on the account, then stop")
    parser.add_argument("--verify", action="store_true",
                        help="read the agent back and check it, then stop")
    args = parser.parse_args()

    env = load_env_file(REPO_ROOT / ".env")
    key = os.environ.get("ELEVENLABS_API_KEY") or env.get("ELEVENLABS_API_KEY", "")
    if not key and not args.dry_run:
        print("ELEVENLABS_API_KEY is not set (env or .env)", file=sys.stderr)
        return 2

    agent_id = args.agent_id or env.get("ELEVENLABS_AGENT_ID", "")

    if args.check:
        ok, message = check_key(key)
        print(("OK   " if ok else "FAIL ") + message)
        return 0 if ok else 1

    if args.voices:
        voices = list_voices(key)
        if not voices:
            print("No voices returned - check the key has voices_read.")
            return 1
        for voice in voices:
            label = f"{voice['name'][:28]:<28}"
            print(f"{voice['id']}  {label} {voice['accent'] or '?':<12} "
                  f"{voice['gender'] or '?'}")
        return 0

    if args.verify:
        if not agent_id:
            print("--verify needs --agent-id (or ELEVENLABS_AGENT_ID in .env)",
                  file=sys.stderr)
            return 2
        ok, checks = verify(key, agent_id)
        for label, passed, detail in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {label} - {detail}")
        return 0 if ok else 1

    specs = list(TOOL_SPECS)
    if args.include_transfer:
        # Matches the runtime rule in `tools.py`: an agent that cannot see the
        # tool cannot be talked into using it, so it is opt-in here too.
        specs.append(TRANSFER_TOOL)

    if args.dry_run:
        print(f"Tools ({len(specs)}):")
        for spec in specs:
            print(f"  would create or update {spec['name']}")
            print("   ", json.dumps(to_client_tool(spec))[:160], "...")
        print("\nAgent payload:")
        print(json.dumps(
            agent_body(
                name=args.name,
                tool_ids=[f"<{s['name']}-id>" for s in specs],
                language=args.language,
                voice_id=args.voice_id,
                llm=args.llm,
                tts_model=args.tts_model,
                max_duration=args.max_duration,
            ),
            indent=2,
        ))
        return 0

    print(f"Tools ({len(specs)}):")
    agent_id = provision(
        key,
        specs,
        name=args.name,
        agent_id=agent_id,
        language=args.language,
        voice_id=args.voice_id,
        llm=args.llm,
        tts_model=args.tts_model,
        max_duration=args.max_duration,
        log_line=lambda line: print(f"  {line}"),
    )

    print("\nVerifying:")
    ok, checks = verify(key, agent_id)
    for label, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label} - {detail}")

    print(
        "\nDone. Put these in .env:\n"
        f"  VOICE_PROVIDER=elevenlabs\n"
        f"  ELEVENLABS_AGENT_ID={agent_id}\n"
        f"  ELEVENLABS_API_KEY=<the key you just used>\n"
        "\nThen restart the service and check /health reports "
        '"voice_provider": "elevenlabs".'
    )
    if not args.include_transfer:
        print(
            "\nNote: transfer_call was not provisioned. Re-run with "
            "--include-transfer if TRANSFER_ENABLED is on."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as exc:
        print(f"\nElevenLabs API error: {exc}", file=sys.stderr)
        sys.exit(1)
