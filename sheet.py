import gspread
import json
import logging
import asyncio
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials
from config import (
    SPREADSHEET_ID, GOOGLE_CREDENTIALS_JSON,
    SHEET_USERS, SHEET_MATCHES, SHEET_BETS, SHEET_LEDGER, SHEET_EVENTS,
    STARTING_CREDITS, UTC
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ── In-memory cache ──────────────────────────────────────────────────────────
cache = {
    "users": {},
    "matches": {},
    "bets": [],
    "events": {},
    "last_refresh": None,
    "daily_credits_date": None,
    "eod_date": None,
    "paid_parlays": set(),          # parlay_ids already paid out, skip at EOD
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

# ── Sheet client — cached to avoid open_by_key() on every write ──────────────
_cached_client = None
_cached_spreadsheet = None


def get_client():
    global _cached_client
    if _cached_client is None:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _cached_client = gspread.authorize(creds)
    return _cached_client


def get_spreadsheet():
    global _cached_spreadsheet
    if _cached_spreadsheet is None:
        _cached_spreadsheet = get_client().open_by_key(SPREADSHEET_ID)
    return _cached_spreadsheet


def get_sheet(tab_name: str):
    global _cached_client, _cached_spreadsheet
    try:
        return get_spreadsheet().worksheet(tab_name)
    except Exception:
        # Force full reconnect on error (expired token, network drop, etc.)
        _cached_client = None
        _cached_spreadsheet = None
        return get_spreadsheet().worksheet(tab_name)

# ── Retry wrapper ────────────────────────────────────────────────────────────
async def with_retry(fn, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            # Never retry on rate limit — it makes the quota worse
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                raise
            if attempt < retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                raise

# ── Cache refresh ────────────────────────────────────────────────────────────
async def refresh_cache(notify_fn=None):
    """Rebuild in-memory cache from sheet. Also rebuilds row index cache."""
    try:
        spreadsheet = get_spreadsheet()

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
                "placed_at": row["placed_at"],
                "parlay_id": str(row.get("parlay_id", ""))
            })
            _bet_rows[row["bet_id"]] = i + 2

        cache["last_refresh"] = datetime.now(UTC)

        # Rebuild paid_parlays from ledger — any parlay_id that already has a payout
        # ledger entry was already credited. Prevents double-payout on restart.
        try:
            ledger_ws = spreadsheet.worksheet(SHEET_LEDGER)
            ledger_data = ledger_ws.get_all_records()
            paid = set()
            for row in ledger_data:
                notes = str(row.get("notes", ""))
                if row.get("type") == "payout" and notes.startswith("Parlay p_"):
                    # Extract parlay_id from notes like "Parlay p_123_456789 won (4 legs x10.0)"
                    parts = notes.split(" ")
                    if len(parts) >= 2:
                        paid.add(parts[1])
            # Also mark lost/voided parlays from bets cache — all legs settled, none won
            parlay_bets = [b for b in cache["bets"] if b.get("parlay_id")]
            parlay_ids = set(b["parlay_id"] for b in parlay_bets if b.get("parlay_id"))
            for pid in parlay_ids:
                if pid in paid:
                    continue
                legs = [b for b in parlay_bets if b["parlay_id"] == pid]
                if legs and all(b["status"] in ("lost", "void") for b in legs):
                    paid.add(pid)
            cache["paid_parlays"] = paid
            logger.info(f"Cache refreshed successfully — {len(paid)} paid/settled parlays loaded from ledger")
        except Exception as e:
            logger.warning(f"Could not rebuild paid_parlays from ledger: {e}")
            cache["paid_parlays"] = set()
            logger.info("Cache refreshed successfully")

        # Load events
        try:
            events_ws = spreadsheet.worksheet(SHEET_EVENTS)
            events_data = events_ws.get_all_records()
            cache["events"] = {}
            for row in events_data:
                if not row.get("event_id"):
                    continue
                eid = str(row["event_id"])
                cache["events"][eid] = {
                    "event_id": eid,
                    "question": row["question"],
                    "options": [o.strip() for o in str(row["options"]).split(",") if o.strip()],
                    "multiplier": float(row.get("multiplier", 1)),
                    "is_free": str(row.get("is_free", "false")).lower() == "true",
                    "reward": int(row.get("reward", 0)),
                    "status": row.get("status", "draft"),
                    "winner": row.get("winner", ""),
                    "created_at": row.get("created_at", "")
                }
        except Exception as e:
            logger.warning(f"Could not load events tab: {e}")
            cache["events"] = {}

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
        await with_retry(ws.update_cell, row_num, 4, new_credits)
        cache["users"][user_id]["credits"] = new_credits
    except Exception as e:
        logger.error(f"Failed to update credits for {user_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to update credits for user {user_id} after retries: {e}")
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
        await with_retry(ws.update_cell, row_num, 2, username or "")
        await with_retry(ws.update_cell, row_num, 3, first_name or "")
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
        ws.update(f"E{row_num}:I{row_num}", [["FINISHED", home_score, away_score, result, ou_result]])
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
async def place_bet(user_id: int, match_id: str, market: str, outcome: str, amount: int, notify_fn=None, parlay_id: str = "", deduct_credits: bool = True) -> str:
    """Deduct credits and write bet. Returns bet_id."""
    async with get_user_lock(user_id):
        user = cache["users"].get(user_id)
        if not user:
            raise ValueError("User not found")
        if deduct_credits and user["credits"] < amount:
            raise ValueError("Insufficient credits")

        bet_id = f"{user_id}_{match_id}_{market}_{datetime.now(UTC).strftime('%H%M%S%f')}"
        placed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        row = [bet_id, user_id, match_id, market, outcome, amount, "open", "", placed_at, parlay_id]

        try:
            ws = get_sheet(SHEET_BETS)
            await with_retry(ws.append_row, row)
            new_row_num = len(cache["bets"]) + 2
            _bet_rows[bet_id] = new_row_num

            if deduct_credits:
                new_credits = user["credits"] - amount
                await update_user_credits(user_id, new_credits, notify_fn)
                ledger_note = f"Parlay {parlay_id} stake on {match_id}" if parlay_id else f"Bet on {match_id} {market} {outcome}"
                await append_ledger(user_id, "bet", -amount, new_credits, ledger_note, notify_fn)

            cache["bets"].append({
                "bet_id": bet_id, "user_id": user_id, "match_id": match_id,
                "market": market, "outcome": outcome, "amount": amount,
                "status": "open", "payout": "", "placed_at": placed_at,
                "parlay_id": parlay_id
            })

            return bet_id
        except Exception as e:
            logger.error(f"Failed to place bet for {user_id}: {e}")
            if notify_fn:
                await notify_fn(f"⚠️ Failed to place bet for user {user_id}: {e}")
            raise


def get_parlay_bets(parlay_id: str) -> list:
    """Return all bets tagged with a given parlay_id."""
    return [b for b in cache["bets"] if b.get("parlay_id") == parlay_id]


def is_parlay_alive(parlay_id: str) -> bool:
    """Returns False if any leg is lost (parlay is bust) or all legs are settled without a win."""
    legs = get_parlay_bets(parlay_id)
    if not legs:
        return False
    if any(b["status"] == "lost" for b in legs):
        return False
    return True


def get_user_active_parlays(user_id: int) -> list:
    """Return list of distinct active parlay_ids for a user (bets still open)."""
    seen = set()
    result = []
    for b in cache["bets"]:
        pid = b.get("parlay_id", "")
        if pid and b["user_id"] == user_id and b["status"] == "open" and pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


def mark_parlay_paid(parlay_id: str):
    """Mark a parlay as already paid so EOD skips it."""
    cache["paid_parlays"].add(parlay_id)


async def settle_parlay(parlay_id: str, notify_fn=None) -> dict | None:
    """
    Check if all legs of a parlay are settled. If all won → credit payout.
    Returns payout dict {user_id, legs, stake, multiplier, payout, leg_labels}
    or None if not ready or already paid or didn't win.
    """
    from config import PARLAY_MULTIPLIERS, TEAM_DISPLAY
    if parlay_id in cache["paid_parlays"]:
        return None

    legs = get_parlay_bets(parlay_id)
    if not legs:
        return None

    # Must have no open legs remaining
    open_legs = [b for b in legs if b["status"] == "open"]
    if open_legs:
        return None

    settled_legs = [b for b in legs if b["status"] in ("won", "lost")]
    if not settled_legs:
        return None

    all_won = all(b["status"] == "won" for b in settled_legs)
    effective_legs = len(settled_legs)

    if not all_won or effective_legs < 2:
        mark_parlay_paid(parlay_id)  # lost or invalid — mark done so EOD skips
        return None

    multiplier = PARLAY_MULTIPLIERS.get(effective_legs)
    if not multiplier:
        return None

    uid = legs[0]["user_id"]
    stake = legs[0]["amount"]
    payout = int(stake * multiplier)

    # Build leg labels for display
    leg_labels = []
    for b in settled_legs:
        match = cache["matches"].get(str(b["match_id"]), {})
        if b["outcome"] == "draw":
            label = "Draw"
        elif b["outcome"] in ("home", "away"):
            team = match.get("home") if b["outcome"] == "home" else match.get("away")
            if team and team in TEAM_DISPLAY:
                code, flag = TEAM_DISPLAY[team]
                label = f"{flag} {code} Win"
            else:
                label = f"{(team or b['outcome'])[:3].upper()} Win"
        else:
            label = b["outcome"].capitalize()
        leg_labels.append(label)

    # Credit payout
    user = cache["users"].get(uid)
    if user:
        new_credits = user["credits"] + payout
        await update_user_credits(uid, new_credits, notify_fn)
        await append_ledger(uid, "payout", payout, new_credits,
                           f"Parlay {parlay_id} won ({effective_legs} legs x{multiplier})", notify_fn)

    mark_parlay_paid(parlay_id)
    return {
        "user_id": uid,
        "legs": effective_legs,
        "stake": stake,
        "multiplier": multiplier,
        "payout": payout,
        "leg_labels": leg_labels
    }

async def get_user_open_bets(user_id: int) -> list:
    return [b for b in cache["bets"] if b["user_id"] == user_id and b["status"] == "open" and not b.get("parlay_id", "")]

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

            # Parlay legs: mark won/lost but DO NOT pay out here.
            # Payout is handled at EOD via multiplier in job_post_standings.
            is_parlay_leg = str(bet.get("parlay_id", "")) not in ("", "0")

            row_num = _bet_rows.get(bet["bet_id"])
            if row_num:
                await with_retry(ws.update_cell, row_num, 7, status)
                await with_retry(ws.update_cell, row_num, 8, 0 if is_parlay_leg else payout)
            else:
                logger.warning(f"Bet {bet['bet_id']} row not in cache — sheet not updated, memory only")
                if notify_fn:
                    await notify_fn(f"⚠️ Bet {bet['bet_id']} row missing from cache — status set in memory only, sheet NOT updated. Run /admin_refresh and re-settle if needed.")
            bet["status"] = status
            bet["payout"] = 0 if is_parlay_leg else payout

            if won and not is_parlay_leg:
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
                "pl": pl,
                "parlay_id": bet.get("parlay_id", "")
            })

        return settlements

    except Exception as e:
        logger.error(f"Failed to settle bets for match {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to settle bets for match {match_id} after retries: {e}")
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
                await with_retry(ws.update_cell, row_num, 7, "void")
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

async def void_parlay_bets(parlay_id: str, user_id: int, notify_fn=None) -> int:
    """Void all open bets for a parlay and refund the stake. Returns count voided."""
    parlay_bets = [b for b in cache["bets"] if b.get("parlay_id") == parlay_id and b["status"] == "open"]
    if not parlay_bets:
        return 0

    try:
        ws = get_sheet(SHEET_BETS)
        for bet in parlay_bets:
            row_num = _bet_rows.get(bet["bet_id"])
            if row_num:
                ws.update_cell(row_num, 7, "void")
            bet["status"] = "void"

        # Refund total stake once — amount is the same on all legs, deducted only once
        stake = parlay_bets[0]["amount"]
        async with get_user_lock(user_id):
            user = cache["users"].get(user_id)
            if user:
                new_credits = user["credits"] + stake
                await update_user_credits(user_id, new_credits, notify_fn)
                await append_ledger(user_id, "refund", stake, new_credits, f"Cancelled parlay {parlay_id}", notify_fn)

        return len(parlay_bets)
    except Exception as e:
        logger.error(f"Failed to void parlay {parlay_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to void parlay {parlay_id}: {e}")
        raise


# ── Ledger operations ────────────────────────────────────────────────────────
async def append_ledger(user_id: int, type_: str, amount: int, balance_after: int, notes: str, notify_fn=None):
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    ledger_id = f"{user_id}_{timestamp.replace(' ', '_')}"
    row = [ledger_id, user_id, type_, amount, balance_after, timestamp, notes]
    try:
        await with_retry(get_sheet(SHEET_LEDGER).append_row, row)
    except Exception as e:
        logger.error(f"Failed to write ledger for {user_id} after retries: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to write ledger entry for user {user_id} after retries: {e}")

# ── Daily credits ────────────────────────────────────────────────────────────
async def add_match_credits(match_amount: int, match_id: str, notify_fn=None):
    """Add post-match credits to all users. Uses match_id as dedup key."""
    cache_key = f"match_credits_{match_id}"
    if cache.get(cache_key):
        logger.info(f"Match credits already added for {match_id}, skipping.")
        return

    try:
        ws = get_sheet(SHEET_USERS)
        for user_id, user in cache["users"].items():
            row_num = _user_rows.get(user_id)
            if not row_num:
                continue
            new_credits = user["credits"] + match_amount
            await with_retry(ws.update_cell, row_num, 4, new_credits)
            cache["users"][user_id]["credits"] = new_credits
            await append_ledger(user_id, "match_credit", match_amount, new_credits, f"Post-match top-up ({match_id})", notify_fn)
        cache[cache_key] = True
        logger.info(f"Match credits added for match {match_id}")
    except Exception as e:
        logger.error(f"Failed to add match credits for {match_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to add match credits for match {match_id}: {e}")
        raise


async def add_tiered_daily_credits(tier_map: dict, notify_fn=None):
    """Add tiered daily credits. tier_map = {user_id: amount}. Skips if already credited today."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if cache.get("daily_credits_date") == today:
        logger.info(f"Daily credits already added today ({today}), skipping.")
        if notify_fn:
            await notify_fn("⚠️ Daily credits already added today — skipped.")
        return
    try:
        ws = get_sheet(SHEET_USERS)
        for user_id, amount in tier_map.items():
            row_num = _user_rows.get(user_id)
            if not row_num:
                continue
            user = cache["users"].get(user_id)
            if not user:
                continue
            new_credits = user["credits"] + amount
            await with_retry(ws.update_cell, row_num, 4, new_credits)
            cache["users"][user_id]["credits"] = new_credits
            await append_ledger(user_id, "daily_credit", amount, new_credits, "Daily top-up (tiered)", notify_fn)
        cache["daily_credits_date"] = today
        logger.info("Tiered daily credits added to all users")
    except Exception as e:
        logger.error(f"Failed to add tiered daily credits: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to add tiered daily credits: {e}")
        raise


# ── Standings ────────────────────────────────────────────────────────────────
def get_standings() -> list:
    return sorted(
        [{"user_id": uid, **data} for uid, data in cache["users"].items()],
        key=lambda x: x["credits"],
        reverse=True
    )

def get_daily_pl(match_ids: list) -> dict:
    from config import PARLAY_MULTIPLIERS
    match_ids_str = [str(m) for m in match_ids]
    pl = {}

    # Singles only — parlay legs handled separately
    for bet in cache["bets"]:
        if bet["match_id"] not in match_ids_str:
            continue
        if bet["status"] not in ("won", "lost"):
            continue
        if bet.get("parlay_id", ""):
            continue  # skip parlay legs here
        uid = bet["user_id"]
        pl[uid] = pl.get(uid, 0) + (bet["amount"] if bet["status"] == "won" else -bet["amount"])

    # Parlays — find all parlay_ids with any leg in today's matches
    today_parlay_ids = set(
        b["parlay_id"] for b in cache["bets"]
        if b.get("parlay_id") and b["match_id"] in match_ids_str
    )
    for pid in today_parlay_ids:
        legs = get_parlay_bets(pid)
        if not legs:
            continue
        uid = legs[0]["user_id"]
        stake = legs[0]["amount"]  # stake deducted once
        settled = [b for b in legs if b["status"] in ("won", "lost")]
        open_legs = [b for b in legs if b["status"] == "open"]
        if open_legs:
            continue  # parlay not fully settled yet — skip
        all_won = all(b["status"] == "won" for b in settled)
        effective = len(settled)
        if all_won and effective >= 2:
            multiplier = PARLAY_MULTIPLIERS.get(effective)
            if not multiplier:
                continue
            payout = int(stake * multiplier)
            pl[uid] = pl.get(uid, 0) + (payout - stake)
        else:
            pl[uid] = pl.get(uid, 0) - stake

    return pl

# ── Event row cache ───────────────────────────────────────────────────────────
_event_rows: dict[str, int] = {}  # event_id -> sheet row


# ── Auto-create events tab if missing ────────────────────────────────────────
def ensure_events_tab():
    """Create events tab with headers if it doesn't exist."""
    try:
        client = get_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        try:
            spreadsheet.worksheet(SHEET_EVENTS)
        except Exception:
            ws = spreadsheet.add_worksheet(title=SHEET_EVENTS, rows=100, cols=10)
            ws.append_row(["event_id", "question", "options", "multiplier", "is_free", "reward", "status", "winner", "created_at"])
            logger.info("Created events tab")
    except Exception as e:
        logger.error(f"Failed to ensure events tab: {e}")


# ── Event operations ──────────────────────────────────────────────────────────
def get_next_event_id() -> str:
    existing = [int(k.replace("event", "")) for k in cache["events"] if k.startswith("event") and k[5:].isdigit()]
    next_num = max(existing, default=0) + 1
    return f"event{next_num}"


async def create_event(question: str, options: list, multiplier: float, is_free: bool, reward: int, notify_fn=None) -> str:
    """Create a new event in draft status. Returns event_id."""
    event_id = get_next_event_id()
    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    row = [
        event_id, question, ",".join(options),
        multiplier, str(is_free).lower(), reward,
        "draft", "", created_at
    ]
    try:
        ws = get_sheet(SHEET_EVENTS)
        await with_retry(ws.append_row, row)
        new_row_num = len(cache["events"]) + 2
        _event_rows[event_id] = new_row_num
        cache["events"][event_id] = {
            "event_id": event_id, "question": question,
            "options": options, "multiplier": multiplier,
            "is_free": is_free, "reward": reward,
            "status": "draft", "winner": "", "created_at": created_at
        }
        return event_id
    except Exception as e:
        logger.error(f"Failed to create event: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to create event: {e}")
        raise


async def update_event_status(event_id: str, status: str, winner: str = "", notify_fn=None):
    """Update event status and optionally set winner."""
    try:
        row_num = _event_rows.get(event_id)
        if not row_num:
            # Fallback: refresh to find row
            await refresh_cache()
        row_num = _event_rows.get(event_id)
        if not row_num:
            raise ValueError(f"Event {event_id} row not in cache")
        ws = get_sheet(SHEET_EVENTS)
        await with_retry(ws.update_cell, row_num, 7, status)   # col 7 = status
        if winner:
            await with_retry(ws.update_cell, row_num, 8, winner)  # col 8 = winner
        cache["events"][event_id]["status"] = status
        if winner:
            cache["events"][event_id]["winner"] = winner
    except Exception as e:
        logger.error(f"Failed to update event {event_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to update event {event_id}: {e}")
        raise


async def update_event_fields(event_id: str, question: str, options: list, multiplier: float, is_free: bool, reward: int, notify_fn=None):
    """Edit event fields (only valid in draft status)."""
    try:
        row_num = _event_rows.get(event_id)
        if not row_num:
            raise ValueError(f"Event {event_id} row not in cache")
        ws = get_sheet(SHEET_EVENTS)
        await with_retry(ws.update, f"B{row_num}:G{row_num}", [[question, ",".join(options), multiplier, str(is_free).lower(), reward, "draft"]])
        event = cache["events"][event_id]
        event.update({"question": question, "options": options, "multiplier": multiplier, "is_free": is_free, "reward": reward})
    except Exception as e:
        logger.error(f"Failed to edit event {event_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to edit event {event_id}: {e}")
        raise


async def settle_event_bets(event_id: str, winner_option: str, notify_fn=None) -> list:
    """Settle all bets for an event. winner_option is the option string e.g. 'MEX'."""
    event = cache["events"].get(event_id)
    if not event:
        raise ValueError(f"Event {event_id} not found")

    # Find winner index (1-based)
    try:
        winner_idx = str(event["options"].index(winner_option) + 1)
    except ValueError:
        raise ValueError(f"Option {winner_option} not in event options")

    event_bets = [b for b in cache["bets"] if b["match_id"] == event_id and b["status"] == "open"]
    settlements = []

    try:
        ws = get_sheet(SHEET_BETS)
        for bet in event_bets:
            won = bet["outcome"] == winner_idx
            if event["is_free"]:
                payout = event["reward"] if won else 0
            else:
                payout = int(bet["amount"] * event["multiplier"]) if won else 0
            status = "won" if won else "lost"

            row_num = _bet_rows.get(bet["bet_id"])
            if row_num:
                ws.update_cell(row_num, 7, status)
                ws.update_cell(row_num, 8, payout)
            bet["status"] = status
            bet["payout"] = payout

            if won and payout > 0:
                async with get_user_lock(bet["user_id"]):
                    user = cache["users"].get(bet["user_id"])
                    if user:
                        new_credits = user["credits"] + payout
                        await update_user_credits(bet["user_id"], new_credits, notify_fn)
                        await append_ledger(bet["user_id"], "payout", payout, new_credits, f"Won event {event_id}", notify_fn)

            settlements.append({
                "user_id": bet["user_id"],
                "outcome": bet["outcome"],
                "amount": bet["amount"],
                "status": status,
                "payout": payout
            })

        return settlements
    except Exception as e:
        logger.error(f"Failed to settle event {event_id}: {e}")
        if notify_fn:
            await notify_fn(f"⚠️ Failed to settle event {event_id}: {e}")
        raise


def get_event_bets(event_id: str) -> list:
    return [b for b in cache["bets"] if b["match_id"] == event_id and b["status"] in ("open", "won", "lost")]


async def place_event_bet(user_id: int, event_id: str, option_idx: str, amount: int, notify_fn=None) -> str:
    """Place a bet on an event. For free events amount=0."""
    async with get_user_lock(user_id):
        user = cache["users"].get(user_id)
        if not user:
            raise ValueError("User not found")

        event = cache["events"].get(event_id)
        if not event:
            raise ValueError("Event not found")

        if not event["is_free"] and user["credits"] < amount:
            raise ValueError("Insufficient credits")

        bet_id = f"{user_id}_{event_id}_{option_idx}_{datetime.now(UTC).strftime('%H%M%S%f')}"
        placed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        row = [bet_id, user_id, event_id, "event", option_idx, amount, "open", "", placed_at]

        try:
            ws = get_sheet(SHEET_BETS)
            await with_retry(ws.append_row, row)
            new_row_num = len(cache["bets"]) + 2
            _bet_rows[bet_id] = new_row_num

            if not event["is_free"] and amount > 0:
                new_credits = user["credits"] - amount
                await update_user_credits(user_id, new_credits, notify_fn)
                await append_ledger(user_id, "bet", -amount, new_credits, f"Event bet {event_id} option {option_idx}", notify_fn)

            cache["bets"].append({
                "bet_id": bet_id, "user_id": user_id, "match_id": event_id,
                "market": "event", "outcome": option_idx, "amount": amount,
                "status": "open", "payout": "", "placed_at": placed_at
            })
            return bet_id
        except Exception as e:
            logger.error(f"Failed to place event bet: {e}")
            if notify_fn:
                await notify_fn(f"⚠️ Failed to place event bet: {e}")
            raise
