import os
import pytz

BOT_VERSION = "1.0"

# Telegram
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", 0)) or None

# Google Sheets
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# Football API
FOOTBALL_API_KEY = os.environ["FOOTBALL_API_KEY"]
FOOTBALL_API_BASE = "https://api.football-data.org/v4"
WC_COMPETITION = "WC"

# Anthropic API (for Katerina)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Tournament stages (UTC dates) ─────────────────────────────────────────────
from datetime import date as _date

TOURNAMENT_STAGES = [
    {"name": "Group Stage",  "start": _date(2026, 6, 11), "end": _date(2026, 6, 27)},
    {"name": "Round of 32",  "start": _date(2026, 6, 28), "end": _date(2026, 7,  3)},
    {"name": "Round of 16",  "start": _date(2026, 7,  4), "end": _date(2026, 7,  7)},
    {"name": "Quarterfinals","start": _date(2026, 7,  9), "end": _date(2026, 7, 11)},
    {"name": "Semifinals",   "start": _date(2026, 7, 14), "end": _date(2026, 7, 15)},
    {"name": "Third Place",  "start": _date(2026, 7, 18), "end": _date(2026, 7, 18)},
    {"name": "Final",        "start": _date(2026, 7, 19), "end": _date(2026, 7, 19)},
]
TOURNAMENT_FINAL_DATE = _date(2026, 7, 19)

# ── Prize pool ────────────────────────────────────────────────────────────────
PRIZE_PLAYER_COUNT = 5
PRIZE_PER_PLAYER = 80  # SGD
PRIZE_POOL = PRIZE_PLAYER_COUNT * PRIZE_PER_PLAYER
PRIZE_INFO = (
    f"Prize pool: {PRIZE_PLAYER_COUNT} players × $80 = ${PRIZE_POOL} total. "
    "1st place wins the World Cup champion jersey plus dining vouchers (remainder after jerseys). "
    "2nd place wins the World Cup runner-up jersey. "
    "Winner is determined by highest credits at the end of the tournament (after the World Cup Final on July 19, 2026). "
    "No tiebreaker rule — credits standings are final."
)

# Timezone
SGT = pytz.timezone("Asia/Singapore")
UTC = pytz.utc

# Game settings
DAILY_CREDITS = 100
STARTING_CREDITS = 100
RESULT_MARKET_PAYOUT = 1.0
OU_MARKET_PAYOUT = 1.0

# Bet lock: seconds after kickoff
BET_LOCK_BUFFER = 30

# Polling: seconds after expected match end before first poll
POLL_START_OFFSET = 120 * 60   # 120 minutes (90min match + 15min HT + 15min buffer)
POLL_INTERVAL = 5 * 60        # 5 minutes in seconds

# Group stage match duration (seconds)
GROUP_STAGE_DURATION = 90 * 60
KNOCKOUT_DURATION = 120 * 60

# Scheduler times (SGT)
NIGHT_REMINDER_HOUR = 23
NIGHT_REMINDER_MINUTE = 0
MORNING_CATCHUP_HOUR = 7
MORNING_CATCHUP_MINUTE = 30
PREMATCH_SUMMARY_MINUTES = 15

# Cache refresh interval (seconds)
CACHE_REFRESH_INTERVAL = 5 * 60

# Session expiry (seconds)
SESSION_EXPIRY = 120

# Fuzzy match threshold
FUZZY_THRESHOLD = 70

# Sheet tab names
SHEET_USERS = "users"
SHEET_MATCHES = "matches"
SHEET_BETS = "bets"
SHEET_LEDGER = "ledger"
SHEET_EVENTS = "events"

# Team aliases — hardcoded lookup before fuzzy matching
# Value of None means ambiguous — bot will ask for clarification
TEAM_ALIASES = {
    # Ambiguous — always confirm
    "gui": None,
    "guinea": None,
    "con": None,
    "congo": None,

    # Guinea-Bissau
    "guib": "Guinea-Bissau",
    "guinea-bissau": "Guinea-Bissau",
    "bissau": "Guinea-Bissau",

    # Guinea
    "guin": "Guinea",
    "guinea rep": "Guinea",

    # DR Congo
    "drc": "DR Congo",
    "drcongo": "DR Congo",
    "dr congo": "DR Congo",

    # Common teams
    "bra": "Brazil",
    "brazil": "Brazil",
    "eng": "England",
    "england": "England",
    "mex": "Mexico",
    "mexico": "Mexico",
    "kor": "South Korea",
    "korea": "South Korea",
    "south korea": "South Korea",
    "korea republic": "South Korea",
    "jpn": "Japan",
    "jap": "Japan",
    "japan": "Japan",
    "usa": "United States",
    "us": "United States",
    "america": "United States",
    "ger": "Germany",
    "germany": "Germany",
    "fra": "France",
    "france": "France",
    "arg": "Argentina",
    "argentina": "Argentina",
    "por": "Portugal",
    "portugal": "Portugal",
    "esp": "Spain",
    "spa": "Spain",
    "spain": "Spain",
    "ned": "Netherlands",
    "neth": "Netherlands",
    "netherlands": "Netherlands",
    "holland": "Netherlands",
    "aus": "Australia",
    "australia": "Australia",
    "bel": "Belgium",
    "belgium": "Belgium",
    "uru": "Uruguay",
    "uruguay": "Uruguay",
    "sen": "Senegal",
    "senegal": "Senegal",
    "mor": "Morocco",
    "morocco": "Morocco",
    "civ": "Ivory Coast",
    "ivory coast": "Ivory Coast",
    "ivorycoast": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "ksa": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "saudi arabia": "Saudi Arabia",
    "cro": "Croatia",
    "croatia": "Croatia",
    "cze": "Czechia",
    "czech": "Czechia",
    "czechia": "Czechia",
    "par": "Paraguay",
    "paraguay": "Paraguay",
    "pan": "Panama",
    "panama": "Panama",
    "irn": "Iran",
    "iran": "Iran",
    "sco": "Scotland",
    "scotland": "Scotland",
    "swi": "Switzerland",
    "sui": "Switzerland",
    "switzerland": "Switzerland",
    "col": "Colombia",
    "colombia": "Colombia",
    "ecu": "Ecuador",
    "ecuador": "Ecuador",
    "tur": "Turkey",
    "turkey": "Turkey",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "nor": "Norway",
    "norway": "Norway",
    "pol": "Poland",
    "poland": "Poland",
    "can": "Canada",
    "canada": "Canada",
    "qat": "Qatar",
    "qatar": "Qatar",
    "rsa": "South Africa",
    "south africa": "South Africa",
    "nzl": "New Zealand",
    "new zealand": "New Zealand",
    "hai": "Haiti",
    "haiti": "Haiti",
    "mar": "Morocco",
    "tun": "Tunisia",
    "tunisia": "Tunisia",
    "egy": "Egypt",
    "egypt": "Egypt",
    "irq": "Iraq",
    "iraq": "Iraq",
    "alg": "Algeria",
    "algeria": "Algeria",
    "nig": "Nigeria",
    "nigeria": "Nigeria",
    "cmr": "Cameroon",
    "cameroon": "Cameroon",
    "bos": "Bosnia & Herzegovina",
    "bosnia": "Bosnia & Herzegovina",
    "srb": "Serbia",
    "serbia": "Serbia",
    "aut": "Austria",
    "austria": "Austria",
    "bel": "Belgium",
    "swe": "Sweden",
    "sweden": "Sweden",
    "den": "Denmark",
    "denmark": "Denmark",
    "hun": "Hungary",
    "hungary": "Hungary",
    "wal": "Wales",
    "wales": "Wales",
    "gre": "Greece",
    "greece": "Greece",
    "ven": "Venezuela",
    "venezuela": "Venezuela",
    "per": "Peru",
    "peru": "Peru",
    "bol": "Bolivia",
    "bolivia": "Bolivia",
    "cap": "Cape Verde",
    "cape verde": "Cape Verde",
    "cur": "Curaçao",
    "curacao": "Curaçao",
}

# Valid bet outcomes
RESULT_OUTCOMES = ["win", "loss", "draw"]
OU_OUTCOMES = ["over", "under"]
ALL_OUTCOMES = RESULT_OUTCOMES + OU_OUTCOMES

# Match status values
STATUS_SCHEDULED = "SCHEDULED"
STATUS_FINISHED = "FINISHED"
STATUS_CANCELLED = "CANCELLED"
STATUS_POSTPONED = "POSTPONED"

# Parlay multipliers by number of legs
PARLAY_MULTIPLIERS = {2: 2.5, 3: 5.0, 4: 10.0}

# Bet status values
BET_OPEN = "open"
BET_WON = "won"
BET_LOST = "lost"
BET_VOID = "void"

# Ledger types
LEDGER_BET = "bet"
LEDGER_PAYOUT = "payout"
LEDGER_DAILY = "daily_credit"
LEDGER_ADMIN = "admin_adjustment"
LEDGER_REFUND = "refund"

# Team display: API name → (short code, flag emoji)
TEAM_DISPLAY = {
    "Mexico": ("MEX", "🇲🇽"),
    "South Africa": ("RSA", "🇿🇦"),
    "Korea Republic": ("KOR", "🇰🇷"),
    "South Korea": ("KOR", "🇰🇷"),
    "Czechia": ("CZE", "🇨🇿"),
    "Canada": ("CAN", "🇨🇦"),
    "Bosnia-Herzegovina": ("BIH", "🇧🇦"),
    "Bosnia & Herzegovina": ("BIH", "🇧🇦"),
    "Bosnia and Herzegovina": ("BIH", "🇧🇦"),
    "USA": ("USA", "🇺🇸"),
    "United States": ("USA", "🇺🇸"),
    "Paraguay": ("PAR", "🇵🇾"),
    "Qatar": ("QAT", "🇶🇦"),
    "Switzerland": ("SUI", "🇨🇭"),
    "Brazil": ("BRA", "🇧🇷"),
    "Morocco": ("MAR", "🇲🇦"),
    "Haiti": ("HAI", "🇭🇹"),
    "Scotland": ("SCO", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"),
    "Australia": ("AUS", "🇦🇺"),
    "Türkiye": ("TUR", "🇹🇷"),
    "Turkey": ("TUR", "🇹🇷"),
    "Germany": ("GER", "🇩🇪"),
    "Côte d'Ivoire": ("CIV", "🇨🇮"),
    "Ivory Coast": ("CIV", "🇨🇮"),
    "Ecuador": ("ECU", "🇪🇨"),
    "Curaçao": ("CUW", "🇨🇼"),
    "Tunisia": ("TUN", "🇹🇳"),
    "Japan": ("JPN", "🇯🇵"),
    "Spain": ("ESP", "🇪🇸"),
    "Saudi Arabia": ("KSA", "🇸🇦"),
    "Belgium": ("BEL", "🇧🇪"),
    "Iran": ("IRN", "🇮🇷"),
    "Uruguay": ("URU", "🇺🇾"),
    "Cape Verde": ("CPV", "🇨🇻"),
    "New Zealand": ("NZL", "🇳🇿"),
    "Egypt": ("EGY", "🇪🇬"),
    "Netherlands": ("NED", "🇳🇱"),
    "Sweden": ("SWE", "🇸🇪"),
    "France": ("FRA", "🇫🇷"),
    "England": ("ENG", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    "Argentina": ("ARG", "🇦🇷"),
    "Peru": ("PER", "🇵🇪"),
    "Poland": ("POL", "🇵🇱"),
    "Austria": ("AUT", "🇦🇹"),
    "Portugal": ("POR", "🇵🇹"),
    "Colombia": ("COL", "🇨🇴"),
    "DR Congo": ("COD", "🇨🇩"),
    "Serbia": ("SRB", "🇷🇸"),
    "Hungary": ("HUN", "🇭🇺"),
    "Panama": ("PAN", "🇵🇦"),
    "Croatia": ("CRO", "🇭🇷"),
    "Senegal": ("SEN", "🇸🇳"),
    "Nigeria": ("NGA", "🇳🇬"),
    "Algeria": ("ALG", "🇩🇿"),
    "Cameroon": ("CMR", "🇨🇲"),
    "Norway": ("NOR", "🇳🇴"),
    "Venezuela": ("VEN", "🇻🇪"),
    "Bolivia": ("BOL", "🇧🇴"),
    "Iraq": ("IRQ", "🇮🇶"),
    "Wales": ("WAL", "🏴󠁧󠁢󠁷󠁬󠁳󠁿"),
    "Greece": ("GRE", "🇬🇷"),
    "Guinea": ("GUI", "🇬🇳"),
    "Guinea-Bissau": ("GNB", "🇬🇼"),
    "Denmark": ("DEN", "🇩🇰"),
    "Ukraine": ("UKR", "🇺🇦"),
    "Chile": ("CHI", "🇨🇱"),
    "Costa Rica": ("CRC", "🇨🇷"),
}
