import gspread
import json
import logging
import asyncio
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials
from config import (
    SPREADSHEET_ID, GOOGLE_CREDENTIALS_JSON,
    SHEET_USERS, SHEET_MATCHES, SHEET_BETS, SHEET_LEDGER,
    STARTING_CREDITS, UTC
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ── In-memory cache ──────────────────────────────────────────────────────────
cache = {
    "users": {},        # keyed by user_id (int)
    "matches": {},      # keyed by match_id (str)
    "bets": [],         # list of bet dicts
    "last_refresh": None
}

# Row index cache — tracks sheet row numbers to avoid re-reading
# row numbers are 1-indexed sheet rows (header = row 1, first data = row 2)
_user_rows: dict[int, int] = {}       # user_id -> sheet row
_match_rows: dict[str, int] = {}      # match_id -> sheet row
_bet_rows: dict[str, int] = {}        # bet_id -> sheet row

# Per-user asyncio locks to prevent race conditions on credit writes
_user_locks: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]

# ── Sheet client ─────────────────────────────────────────────────────────────
def get_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(tab_name: str):
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.worksheet(tab_name)

# ── Retry wrapper ────────────────────────────────────────────────────────────
async def with_retry(fn, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                raise e

# ── Cache refresh ────────────────────────────────────────────────────────────
async def refresh_cache(notify_fn=None):
    """Rebuild in-memory cache from sheet. Also rebuilds row index cache."""
    try:
        client = get_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        # Users
        users_ws = spreadsheet.worksheet(SHEET_USERS)
        users_data = users_ws.get_all_records()
        cache["users"] = {}
        _user_rows.clear()
        for i, row in enumerate(users_data):
            if not row.get("user_id"):
                continue
            uid = int(row["user_id"])
            cache["users"][uid] = {
                "username": row["username"],
                "first_name": row["first_name"],
                "credits": int(row["credits"]),
                "joined_date": row["joined_date"],
                "is_admin": str(row["is_admin"]).lower() == "true"
            }
            _user_rows[uid] = i + 2  # +2 for header row + 0-index

        # Matches
        matches_ws = spreadsheet.worksheet(SHEET_MATCHES)
        matches_data = matches_ws.get_all_records()
        cache["matches"] = {}
        _match_rows.clear()
        for i, row in enumerate(matches_data):
            if not row.get("match_id"):
                continue
            mid = str(row["match_id"])
            cache["matches"][mid] = {
                "match_id": mid,
                "home": row["home"],
                "away": row["away"],
                "kickoff_utc": row["kickoff_utc"],
                "status": row["status"],
                "home_score": row["home_score"],
                "away_score": row["away_score"],
                "result": row["result"],
                "ou_result": row["ou_result"],
                "matchday": row["matchday"],
                "round": row["round"]
            }
            _match_rows[mid] = i + 2

        # Bets
        bets_ws = spreadsheet.worksheet(SHEET_BETS)
        bets_data = bets_ws.get_all_records()
        cache["bets"] = []
        _bet_rows.clear()
        for i, row in enumerate(bets_data):
            if not row.get("bet_id"):
                continue
            cache["bets"].append({
                "bet_id": row["bet_id"],
                "user_id": int(row["user_id"]),
                "match_id": str(row["match_id"]),
                "market": row["market"],
                "outcome": row["outcome"],
                "amount": int(row["amount"]),
                "status": row["status"],
                "payout": row["payout"],
                "placed_at": row["placed_at"]
            })
            _bet_rows[row["bet_id"]] = i + 2

        cache["last_refresh"] = datetime.now(UTC)
        logger.info("Cache refreshed successfully")

    except Exception as e:
        logger.error(f"Cache refresh failed: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Cache refresh failed: {e}")

# ── User operations ──────────────────────────────────────────────────────────
async def get_user(user_id: int) -> dict | None:
    return cache["users"].get(user_id)

async def register_user(user_id: int, username: str, first_name: str, is_admin: bool = False, notify_fn=None) -> dict:
    """Register new user if not exists. Returns user dict."""
    if user_id in cache["users"]:
        return cache["users"][user_id]

    joined_date = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    row = [user_id, username or "", first_name or "", STARTING_CREDITS, joined_date, str(is_admin)]

    try:
        ws = get_sheet(SHEET_USERS)
        await with_retry(ws.append_row, row)
        # New row is header + existing users + 1
        new_row_num = len(cache["users"]) + 2
        user = {
            "username": username or "",
            "first_name": first_name or "",
            "credits": STARTING_CREDITS,
            "joined_date": joined_date,
            "is_admin": is_admin
        }
        cache["users"][user_id] = user
        _user_rows[user_id] = new_row_num
        logger.info(f"Registered user {user_id} ({first_name})")
        return user
    except Exception as e:
        logger.error(f"Failed to register user {user_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to register user {user_id} ({first_name}): {e}")
        raise

async def update_user_credits(user_id: int, new_credits: int, notify_fn=None):
    """Update credits in sheet and cache using cached row number."""
    new_credits = max(0, new_credits)
    try:
        row_num = _user_rows.get(user_id)
        if not row_num:
            raise ValueError(f"User {user_id} row not in cache — run refresh")
        ws = get_sheet(SHEET_USERS)
        ws.update_cell(row_num, 4, new_credits)  # col 4 = credits
        cache["users"][user_id]["credits"] = new_credits
    except Exception as e:
        logger.error(f"Failed to update credits for {user_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to update credits for user {user_id}: {e}")
        raise

async def refresh_display_name(user_id: int, username: str, first_name: str, notify_fn=None):
    """Update username and first_name if changed, using cached row number."""
    try:
        if user_id not in cache["users"]:
            return
        cached = cache["users"][user_id]
        if cached["username"] == (username or "") and cached["first_name"] == (first_name or ""):
            return  # no change, skip write
        row_num = _user_rows.get(user_id)
        if not row_num:
            return
        ws = get_sheet(SHEET_USERS)
        ws.update_cell(row_num, 2, username or "")
        ws.update_cell(row_num, 3, first_name or "")
        cache["users"][user_id]["username"] = username or ""
        cache["users"][user_id]["first_name"] = first_name or ""
    except Exception as e:
        logger.error(f"Failed to refresh display name for {user_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to refresh display name for {user_id}: {e}")

# ── Match operations ─────────────────────────────────────────────────────────
async def get_matches_for_date(date_str: str) -> list:
    return [
        m for m in cache["matches"].values()
        if m["kickoff_utc"].startswith(date_str)
    ]

async def get_match_by_id(match_id: str) -> dict | None:
    return cache["matches"].get(str(match_id))

async def upsert_match(match: dict, notify_fn=None):
    """Insert or update a match in sheet and cache using cached row number."""
    match_id = str(match["match_id"])
    try:
        row = [
            match["match_id"], match["home"], match["away"],
            match["kickoff_utc"], match["status"],
            match.get("home_score", ""), match.get("away_score", ""),
            match.get("result", ""), match.get("ou_result", ""),
            match.get("matchday", ""), match.get("round", "")
        ]
        ws = get_sheet(SHEET_MATCHES)
        if match_id in _match_rows:
            row_num = _match_rows[match_id]
            ws.update(f"A{row_num}:K{row_num}", [row])
        else:
            await with_retry(ws.append_row, row)
            _match_rows[match_id] = len(cache["matches"]) + 2
        cache["matches"][match_id] = {**match, "match_id": match_id}
    except Exception as e:
        logger.error(f"Failed to upsert match {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to upsert match {match_id}: {e}")
        raise

async def update_match_result(match_id: str, home_score: int, away_score: int, notify_fn=None):
    """Set final score and result using cached row number."""
    match_id = str(match_id)
    try:
        if home_score > away_score:
            result = "home"
        elif away_score > home_score:
            result = "away"
        else:
            result = "draw"

        total_goals = home_score + away_score
        ou_result = "over" if total_goals > 2 else "under"

        row_num = _match_rows.get(match_id)
        if not row_num:
            raise ValueError(f"Match {match_id} row not in cache")
        ws = get_sheet(SHEET_MATCHES)
        ws.update(f"F{row_num}:J{row_num}", [[home_score, away_score, result, ou_result, "FINISHED"]])
        if match_id in cache["matches"]:
            cache["matches"][match_id].update({
                "home_score": home_score,
                "away_score": away_score,
                "result": result,
                "ou_result": ou_result,
                "status": "FINISHED"
            })
        return result, ou_result
    except Exception as e:
        logger.error(f"Failed to update match result {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to update match result {match_id}: {e}")
        raise

# ── Bet operations ───────────────────────────────────────────────────────────
async def place_bet(user_id: int, match_id: str, market: str, outcome: str, amount: int, notify_fn=None) -> str:
    """Deduct credits and write bet. Returns bet_id."""
    async with get_user_lock(user_id):
        user = cache["users"].get(user_id)
        if not user:
            raise ValueError("User not found")
        if user["credits"] < amount:
            raise ValueError("Insufficient credits")

        bet_id = f"{user_id}_{match_id}_{market}_{datetime.now(UTC).strftime('%H%M%S%f')}"
        placed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        row = [bet_id, user_id, match_id, market, outcome, amount, "open", "", placed_at]

        try:
            ws = get_sheet(SHEET_BETS)
            await with_retry(ws.append_row, row)
            new_row_num = len(cache["bets"]) + 2
            _bet_rows[bet_id] = new_row_num

            new_credits = user["credits"] - amount
            await update_user_credits(user_id, new_credits, notify_fn)
            await append_ledger(user_id, "bet", -amount, new_credits, f"Bet on {match_id} {market} {outcome}", notify_fn)

            cache["bets"].append({
                "bet_id": bet_id, "user_id": user_id, "match_id": match_id,
                "market": market, "outcome": outcome, "amount": amount,
                "status": "open", "payout": "", "placed_at": placed_at
            })

            return bet_id
        except Exception as e:
            logger.error(f"Failed to place bet for {user_id}: {e}")
            if notify_fn:
                await notify_fn(f"⚠️ Failed to place bet for user {user_id}: {e}")
            raise

async def get_user_open_bets(user_id: int) -> list:
    return [b for b in cache["bets"] if b["user_id"] == user_id and b["status"] == "open"]

async def get_bets_for_match(match_id: str) -> list:
    return [b for b in cache["bets"] if b["match_id"] == str(match_id)]

async def cancel_bet(bet_id: str, user_id: int, notify_fn=None):
    """Void bet and refund credits using cached row number."""
    async with get_user_lock(user_id):
        bet = next((b for b in cache["bets"] if b["bet_id"] == bet_id), None)
        if not bet:
            raise ValueError("Bet not found")
        if bet["status"] != "open":
            raise ValueError("Bet is not open")

        try:
            row_num = _bet_rows.get(bet_id)
            if not row_num:
                raise ValueError(f"Bet {bet_id} row not in cache")
            ws = get_sheet(SHEET_BETS)
            ws.update_cell(row_num, 7, "void")
            bet["status"] = "void"

            user = cache["users"][user_id]
            new_credits = user["credits"] + bet["amount"]
            await update_user_credits(user_id, new_credits, notify_fn)
            await append_ledger(user_id, "refund", bet["amount"], new_credits, f"Cancelled bet {bet_id}", notify_fn)

        except Exception as e:
            logger.error(f"Failed to cancel bet {bet_id}: {e}")
            if notify_fn:
                await notify_fn(f"⚠️ Failed to cancel bet {bet_id}: {e}")
            raise

async def settle_bets_for_match(match_id: str, result: str, ou_result: str, notify_fn=None) -> list:
    """Settle all open bets for a match using cached row numbers."""
    match_id = str(match_id)
    settlements = []

    open_bets = [b for b in cache["bets"] if b["match_id"] == match_id and b["status"] == "open"]
    if not open_bets:
        return settlements

    try:
        ws = get_sheet(SHEET_BETS)

        for bet in open_bets:
            won = False
            if bet["market"] == "result":
                won = bet["outcome"] == result
            elif bet["market"] == "ou":
                won = bet["outcome"] == ou_result

            payout = bet["amount"] * 2 if won else 0
            status = "won" if won else "lost"
            pl = bet["amount"] if won else -bet["amount"]

            row_num = _bet_rows.get(bet["bet_id"])
            if row_num:
                ws.update_cell(row_num, 7, status)
                ws.update_cell(row_num, 8, payout)
            bet["status"] = status
            bet["payout"] = payout

            if won:
                async with get_user_lock(bet["user_id"]):
                    user = cache["users"].get(bet["user_id"])
                    if user:
                        new_credits = user["credits"] + payout
                        await update_user_credits(bet["user_id"], new_credits, notify_fn)
                        await append_ledger(bet["user_id"], "payout", payout, new_credits, f"Won bet {bet['bet_id']}", notify_fn)

            settlements.append({
                "user_id": bet["user_id"],
                "market": bet["market"],
                "outcome": bet["outcome"],
                "amount": bet["amount"],
                "status": status,
                "pl": pl
            })

        return settlements

    except Exception as e:
        logger.error(f"Failed to settle bets for match {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to settle bets for match {match_id}: {e}")
        raise

async def void_all_bets_for_match(match_id: str, notify_fn=None):
    """Void and refund all open bets for a match using cached row numbers."""
    match_id = str(match_id)
    open_bets = [b for b in cache["bets"] if b["match_id"] == match_id and b["status"] == "open"]

    try:
        ws = get_sheet(SHEET_BETS)

        for bet in open_bets:
            row_num = _bet_rows.get(bet["bet_id"])
            if row_num:
                ws.update_cell(row_num, 7, "void")
            bet["status"] = "void"

            async with get_user_lock(bet["user_id"]):
                user = cache["users"].get(bet["user_id"])
                if user:
                    new_credits = user["credits"] + bet["amount"]
                    await update_user_credits(bet["user_id"], new_credits, notify_fn)
                    await append_ledger(bet["user_id"], "refund", bet["amount"], new_credits, f"Match {match_id} cancelled", notify_fn)

        return len(open_bets)

    except Exception as e:
        logger.error(f"Failed to void bets for match {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to void bets for match {match_id}: {e}")
        raise

# ── Ledger operations ────────────────────────────────────────────────────────
async def append_ledger(user_id: int, type_: str, amount: int, balance_after: int, notes: str, notify_fn=None):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    ledger_id = f"{user_id}_{timestamp.replace(' ', '_')}"
    row = [ledger_id, user_id, type_, amount, balance_after, timestamp, notes]
    try:
        await with_retry(get_sheet(SHEET_LEDGER).append_row, row)
    except Exception as e:
        logger.error(f"Failed to write ledger for {user_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to write ledger entry for user {user_id}: {e}")

# ── Daily credits ────────────────────────────────────────────────────────────
async def add_daily_credits(daily_amount: int, notify_fn=None):
    """Add daily credits to all users using cached row numbers."""
    try:
        ws = get_sheet(SHEET_USERS)
        for user_id, user in cache["users"].items():
            row_num = _user_rows.get(user_id)
            if not row_num:
                continue
            new_credits = user["credits"] + daily_amount
            ws.update_cell(row_num, 4, new_credits)
            cache["users"][user_id]["credits"] = new_credits
            await append_ledger(user_id, "daily_credit", daily_amount, new_credits, "Daily top-up", notify_fn)
        logger.info("Daily credits added to all users")
    except Exception as e:
        logger.error(f"Failed to add daily credits: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to add daily credits: {e}")
        raise

# ── Standings ────────────────────────────────────────────────────────────────
def get_standings() -> list:
    return sorted(
        [{"user_id": uid, **data} for uid, data in cache["users"].items()],
        key=lambda x: x["credits"],
        reverse=True
    )

def get_daily_pl(match_ids: list) -> dict:
    pl = {}
    for bet in cache["bets"]:
        if bet["match_id"] not in [str(m) for m in match_ids]:
            continue
        if bet["status"] not in ("won", "lost"):
            continue
        uid = bet["user_id"]
        if uid not in pl:
            pl[uid] = 0
        pl[uid] += bet["amount"] if bet["status"] == "won" else -bet["amount"]
    return pl
