"""G.711 u-law, both directions, in plain Python.

The bridges pass call audio through as opaque base64 and never need this. Two
things do: generating keypad tones to play into a call (`dtmf.py`), and telling
whether the far end is making any sound at all, so a caller who is mid-sentence -
or a line playing hold music - is not mistaken for dead air.

`audioop` would have done both, but it was removed from the standard library in
Python 3.13, and these two functions are the whole of what is needed.
"""

from __future__ import annotations

_BIAS = 0x84
_CLIP = 32635


def linear_to_ulaw(sample: int) -> int:
    """One 16-bit signed sample to one u-law byte."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_linear(byte: int) -> int:
    """One u-law byte to a 16-bit signed sample."""
    u = ~byte & 0xFF
    t = (((u & 0x0F) << 3) + _BIAS) << ((u & 0x70) >> 4)
    return _BIAS - t if u & 0x80 else t - _BIAS


SILENCE = linear_to_ulaw(0)

# Magnitude of every possible byte, so measuring a frame is a table lookup per
# sample rather than a decode. Called on every 20 ms frame of a live call.
_MAGNITUDE = [abs(ulaw_to_linear(b)) for b in range(256)]


def mean_level(frame: bytes) -> float:
    """Mean absolute sample value of a u-law frame, on a 16-bit scale.

    A silent line reads in the tens; speech and hold music read in the
    thousands. Coarse on purpose: it answers "is anything there", not "is
    someone talking".
    """
    if not frame:
        return 0.0
    return sum(map(_MAGNITUDE.__getitem__, frame)) / len(frame)
