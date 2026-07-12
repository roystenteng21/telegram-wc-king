"""
WC Kings 2026 — Knockout Bracket Structure

Hardcoded bracket skeleton. Team names match football-data.org API exactly.

To update each round:
  R32  — add match_id as confirmed in /matches (optional, team-name search is fallback)
  R16  — add r16_match_id once scheduled in API
  QF   — add qf_match_id once scheduled in API
  SF/Final — add new structure blocks at the bottom
"""

BRACKET = [
    {
        "qf_label": "Boston QF",
        "qf_date": "Jul 9",
        "qf_match_id": None,
        "pairs": [
            {
                "r16_label": "Philadelphia R16",
                "r16_date": "Jul 4",
                "r16_match_id": None,
                "r32": [
                    {"home": "Germany",  "away": "Paraguay", "match_id": "537415"},
                    {"home": "France",   "away": "Sweden",   "match_id": None},
                ],
            },
            {
                "r16_label": "Houston R16",
                "r16_date": "Jul 4",
                "r16_match_id": None,
                "r32": [
                    {"home": "South Africa", "away": "Canada",    "match_id": "537417"},
                    {"home": "Netherlands",  "away": "Morocco",   "match_id": None},
                ],
            },
        ],
    },
    {
        "qf_label": "Miami QF",
        "qf_date": "Jul 11",
        "qf_match_id": None,
        "pairs": [
            {
                "r16_label": "New York R16",
                "r16_date": "Jul 5",
                "r16_match_id": None,
                "r32": [
                    {"home": "Brazil",      "away": "Japan",  "match_id": "537423"},
                    {"home": "Ivory Coast", "away": "Norway", "match_id": None},
                ],
            },
            {
                "r16_label": "Mexico City R16",
                "r16_date": "Jul 5",
                "r16_match_id": None,
                "r32": [
                    {"home": "Mexico",  "away": "Ecuador",  "match_id": None},
                    {"home": "England", "away": "Congo DR",  "match_id": None},
                ],
            },
        ],
    },
    {
        "qf_label": "Los Angeles QF",
        "qf_date": "Jul 10",
        "qf_match_id": None,
        "pairs": [
            {
                "r16_label": "Dallas R16",
                "r16_date": "Jul 6",
                "r16_match_id": None,
                "r32": [
                    {"home": "Portugal", "away": "Croatia", "match_id": None},
                    {"home": "Spain",    "away": "Austria", "match_id": None},
                ],
            },
            {
                "r16_label": "Seattle R16",
                "r16_date": "Jul 6",
                "r16_match_id": None,
                "r32": [
                    {"home": "United States",    "away": "Bosnia-Herzegovina", "match_id": None},
                    {"home": "Belgium",          "away": "Senegal",            "match_id": None},
                ],
            },
        ],
    },
    {
        "qf_label": "Kansas City QF",
        "qf_date": "Jul 11",
        "qf_match_id": None,
        "pairs": [
            {
                "r16_label": "Atlanta R16",
                "r16_date": "Jul 7",
                "r16_match_id": None,
                "r32": [
                    {"home": "Argentina", "away": "Cape Verde Islands", "match_id": None},
                    {"home": "Australia", "away": "Egypt",              "match_id": None},
                ],
            },
            {
                "r16_label": "Vancouver R16",
                "r16_date": "Jul 7",
                "r16_match_id": None,
                "r32": [
                    {"home": "Switzerland", "away": "Algeria", "match_id": None},
                    {"home": "Colombia",    "away": "Ghana",   "match_id": None},
                ],
            },
        ],
    },
]

# ── Semifinals, Third Place, Final ────────────────────────────────────────────
# Filled in as each stage's matchups become known. home/away as None means
# "not yet determined" (e.g. Third Place/Final depend on semifinal results).
SEMIFINALS = {
    "stage_label": "Semi-finals",
    "matches": [
        {"label": "Dallas SF", "date": "Jul 14", "home": "France", "away": "Spain", "match_id": None},
        {"label": "Atlanta SF", "date": "Jul 15", "home": "England", "away": "Argentina", "match_id": None},
    ],
}

THIRD_PLACE = {
    "stage_label": "Third Place",
    "matches": [
        {"label": "Miami — Third Place", "date": "Jul 18", "home": None, "away": None, "match_id": None},
    ],
}

FINAL = {
    "stage_label": "Final",
    "matches": [
        {"label": "New Jersey — Final", "date": "Jul 19", "home": None, "away": None, "match_id": None},
    ],
}
