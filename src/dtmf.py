"""Keypad tones, generated as audio.

Twilio Media Streams accept exactly three messages back from us - `media`, `mark`
and `clear` - so there is no way to *signal* "press 2" on a live stream. The way
to press a key is to play the tone itself: in-band DTMF, the dual-frequency
sound a keypad makes. G.711 carries it untouched, and it is what switchboards
and IVR menus listen for.

That keeps the voice session intact. The alternative - redirecting the call to
TwiML `<Play digits>` and reconnecting a new stream - drops the agent's
conversation mid-menu and loses whatever the menu said during the gap.

Nothing here touches the network or the call. It turns keys into u-law bytes;
the bridge decides when they are played.
"""

from __future__ import annotations

import base64
import math
import re

from .ulaw import SILENCE, linear_to_ulaw

SAMPLE_RATE = 8000
# 20 ms at 8 kHz: the frame size Twilio sends us, so the same size is sent back.
FRAME_BYTES = 160

# ITU-T Q.23 frequency pairs, (low group, high group) in Hz.
FREQUENCIES = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}

VALID_KEYS = re.compile(r"^[0-9*#]+$")

# Q.24 receivers must accept a 40 ms tone, but a PBX often wants more and a
# mobile or VoIP leg in the path eats into it. 120 ms on, 120 ms off is well
# clear of both and still quick enough that a four-digit extension takes a
# second.
TONE_MS = 120
GAP_MS = 120

# Per-frequency amplitude as a fraction of 16-bit full scale: roughly -7 dBm0
# low and -6 dBm0 high. Inside what receivers accept, with the high group a
# little louder (positive twist) as a real keypad produces, and a combined peak
# well short of clipping.
LOW_AMPLITUDE = 0.30
HIGH_AMPLITUDE = 0.35

# A few milliseconds of fade at each end, so a tone does not begin with a click
# a receiver could read as a transient.
RAMP_MS = 5


def tones(keys: str) -> bytes:
    """u-law audio for a key sequence, tone then gap for each key."""
    if not keys or not VALID_KEYS.match(keys):
        raise ValueError(f"not a keypad sequence: {keys!r}")

    tone_n = SAMPLE_RATE * TONE_MS // 1000
    gap = bytes([SILENCE]) * (SAMPLE_RATE * GAP_MS // 1000)
    ramp_n = SAMPLE_RATE * RAMP_MS // 1000

    out = bytearray()
    for key in keys:
        low, high = FREQUENCIES[key]
        for n in range(tone_n):
            envelope = min(1.0, n / ramp_n, (tone_n - 1 - n) / ramp_n)
            t = n / SAMPLE_RATE
            value = envelope * 32767 * (
                LOW_AMPLITUDE * math.sin(2 * math.pi * low * t)
                + HIGH_AMPLITUDE * math.sin(2 * math.pi * high * t)
            )
            out.append(linear_to_ulaw(round(value)))
        out.extend(gap)
    return bytes(out)


def frames(audio: bytes) -> list[str]:
    """Split audio into base64 frames the size Twilio expects."""
    if len(audio) % FRAME_BYTES:
        audio += bytes([SILENCE]) * (FRAME_BYTES - len(audio) % FRAME_BYTES)
    return [
        base64.b64encode(audio[i : i + FRAME_BYTES]).decode("ascii")
        for i in range(0, len(audio), FRAME_BYTES)
    ]


def duration_s(keys: str) -> float:
    """How long a sequence takes to play out."""
    return len(keys) * (TONE_MS + GAP_MS) / 1000
