"""NovelAI voice preset lookup tables.

V1 voices: each preset maps to a numeric voice id. The custom-seed
sentinel maps to ``voice=-1`` with a free-form seed string supplied by
the caller.

V2 voices: ``voice`` is always ``-1`` upstream; the seed string drives
the voice. Each preset name maps to a fixed seed string (note Ligeia's
quirk: name "Ligeia" → seed "Anananan").

The same tables are mirrored in ``static/constants.js`` for the
frontend dropdown — keep both in sync when adding / removing voices.
"""
from __future__ import annotations


# preset name -> numeric voice id
NAI_V1_VOICE_IDS: dict[str, int] = {
    "Cyllene":  17,
    "Leucosia": 95,
    "Crina":    44,
    "Hespe":    80,
    "Ida":      106,
    "Alseid":   6,
    "Daphnis":  10,
    "Echo":     16,
    "Thel":     41,
    "Nomios":   77,
}


# preset name -> seed string
NAI_V2_VOICE_SEEDS: dict[str, str] = {
    "Ligeia": "Anananan",
    "Aini":   "Aini",
    "Orea":   "Orea",
    "Claea":  "Claea",
    "Lim":    "Lim",
    "Aurae":  "Aurae",
    "Naia":   "Naia",
    "Aulon":  "Aulon",
    "Elei":   "Elei",
    "Ogma":   "Ogma",
    "Raid":   "Raid",
    "Pega":   "Pega",
    "Lam":    "Lam",
}
