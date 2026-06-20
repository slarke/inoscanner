"""Conversion between IEEE-754 floats and Modbus 16-bit register words.

The PLC stores a 32-bit ``REAL`` as two consecutive holding registers in
**low-word-first** order: ``[low_word, high_word]``.
"""

from __future__ import annotations

import struct
from typing import List, Sequence


def float_to_words(value: float) -> List[int]:
    """Encode a float as ``[low_word, high_word]`` (two unsigned 16-bit words)."""
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    return [bits & 0xFFFF, (bits >> 16) & 0xFFFF]


def words_to_float(words: Sequence[int]) -> float:
    """Decode ``[low_word, high_word]`` back into a float (0.0 if malformed)."""
    if len(words) < 2:
        return 0.0
    bits = ((words[1] & 0xFFFF) << 16) | (words[0] & 0xFFFF)
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def floats_to_words(*values: float) -> List[int]:
    """Encode several floats into one contiguous word list."""
    words: List[int] = []
    for value in values:
        words.extend(float_to_words(value))
    return words
