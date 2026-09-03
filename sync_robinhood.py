r"""
sync_robinhood.py — Pull recent orders and store filled trades in SQLite.

Design: an "adapter" pattern with two interchangeable data sources.

    +--------------------+     raw order dicts      +-----------+
    | MockDataSource     | ----.                    |           |
    +--------------------+      \    normalize      | trades.db |
                                 >---------------->  (SQLite)   |
    +--------------------+      /   (models.py)     |           |
    | RobinhoodMCPSource | ----'                    +-----------+
    +--------------------+

Which source runs is controlled by DATA_SOURCE in your `.env` file:
    DATA_SOURCE=mock        -> fake sample orders (works out of the box)
    DATA_SOURCE=robinhood   -> Robinhood's official Agentic Trading MCP

SAFETY / SCOPE (deliberate MVP constraints):
- READ-ONLY: this script only LISTS orders. It never places, cancels, or
  modifies anything.
- No secrets in code: the MCP URL and any auth token come from `.env`.
- No unofficial scraping libraries: the real adapter talks only to
  Robinhood's official MCP server.

Run it with:  python sync_robinhood.py
"""

import json
import os
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional, Protocol, Tuple
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

import db
from models import normalize_order

# Load variables from the .env file sitting next to this script.
load_dotenv()


# =====================================================================
# 1. The adapter interface
# =====================================================================

class TradeDataSource(Protocol):
    """Anything that can return a list of raw order dicts."""

    def fetch_orders(self) -> List[dict]:
        """Return recent orders as raw dicts (any status; we filter later)."""
        ...

    def fetch_portfolio(self) -> Tuple[List[dict], List[dict]]:
        """Return (open positions, realized P&L rows)."""
        ...

    def fetch_history(self, price_symbols: List[str], start: str) -> dict:
        """Return {"daily_prices": [...], "totals": {...}, "tax_lots": [...]}."""
        ...


# =====================================================================
# 2. Mock data source — lets you test everything with zero setup
# =====================================================================

class MockDataSource:
    """Returns a handful of realistic fake orders. No network needed."""

    def fetch_orders(self) -> List[dict]:
        return [
            {
                "id": "mock-order-001",
                "symbol": "AAPL",
                "side": "buy",
                "status": "filled",
                "filled_quantity": 10,
                "average_price": 227.35,
                "created_at": "2026-07-01T14:30:00Z",
                "filled_at": "2026-07-01T14:30:05Z",
                "asset_type": "stock",
            },
            {
                "id": "mock-order-002",
                "symbol": "NVDA",
                "side": "buy",
                "status": "filled",
                "filled_quantity": 5,
                "average_price": 172.10,
                "created_at": "2026-07-02T15:00:00Z",
                "filled_at": "2026-07-02T15:00:02Z",
                "asset_type": "stock",
            },
            {
                "id": "mock-order-003",
                "symbol": "AAPL",
                "side": "sell",
                "status": "filled",
                "filled_quantity": 4,
                "average_price": 231.80,
                "created_at": "2026-07-03T17:45:00Z",
                "filled_at": "2026-07-03T17:45:01Z",
                "asset_type": "stock",
            },
            {
                # This one is NOT filled — the sync should skip it.
                "id": "mock-order-004",
                "symbol": "TSLA",
                "side": "buy",
                "status": "cancelled",
                "filled_quantity": 0,
                "average_price": None,
                "created_at": "2026-07-03T18:00:00Z",
                "filled_at": None,
                "asset_type": "stock",
            },
        ]

    def fetch_portfolio(self) -> Tuple[List[dict], List[dict]]:
        """
        No mock positions or realized P&L.

        Both tables are replace-on-sync, and an empty result deliberately
        leaves them untouched — so running in mock mode can't clobber a real
        snapshot the way mock ORDERS would pollute the trades table.
        """
        return [], []

    def fetch_history(self, price_symbols: List[str], start: str) -> dict:
        """No mock price history — empty leaves every stored series alone."""
        return {"daily_prices": [], "totals": None, "tax_lots": []}


# =====================================================================
# 3. OAuth plumbing for Robinhood's MCP
# =====================================================================
#
# Robinhood's MCP server is OAuth-protected. The MCP SDK drives the whole
# flow for us; we only supply three things:
#
#   1. somewhere to keep tokens          -> FileTokenStorage
#   2. a way to show the consent page    -> _open_browser
#   3. a way to catch the redirect back  -> _wait_for_callback
#
# The `mcp` imports stay inside functions so that mock mode still works
# when the SDK isn't installed (same reason fetch_orders imports lazily).
# =====================================================================

TOKEN_PATH = Path(__file__).parent / os.getenv("ROBINHOOD_TOKEN_FILE", ".rh_tokens.json")
CALLBACK_PORT = int(os.getenv("ROBINHOOD_CALLBACK_PORT", "3030"))
# Unattended runs (cron) should fail loudly rather than wait on a browser.
NO_BROWSER = os.getenv("ROBINHOOD_NO_BROWSER", "").strip() not in ("", "0")
# Print the server's full tool catalogue even when the tool name is pinned.
VERBOSE = os.getenv("ROBINHOOD_VERBOSE", "").strip() not in ("", "0")


class FileTokenStorage:
    """
    Keeps OAuth tokens in a local JSON file.

    The SDK's own example stores tokens in memory, which would force a
    browser login on EVERY run — useless for a scheduled sync. This file
    holds live credentials to your brokerage account, so it is written
    0600 (you-only) and must never be committed or shared.
    """

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))
        self.path.chmod(0o600)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        # Record WHEN we received them. `expires_in` is a duration, not a
        # deadline, so on its own it says nothing about a token read back
        # from disk hours later. See stored_expiry().
        data["obtained_at"] = time.time()
        self._write(data)

    def stored_expiry(self) -> Optional[float]:
        """
        Absolute epoch time at which the cached access token expires.

        Why this exists: the SDK only records an expiry for tokens it minted
        in the current process. Tokens loaded from disk get none, which makes
        its is_token_valid() answer True no matter how stale they are — so it
        sends an expired token, takes a 401, and restarts the full browser
        flow instead of quietly spending the refresh token. Feeding it this
        value makes an expired token look expired.

        Falls back to the file's mtime for token files written before
        `obtained_at` was recorded.
        """
        data = self._read()
        expires_in = (data.get("tokens") or {}).get("expires_in")
        if expires_in is None:
            return None

        obtained = data.get("obtained_at")
        if obtained is None:
            if not self.path.exists():
                return None
            obtained = self.path.stat().st_mtime

        return float(obtained) + float(expires_in)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


def _wait_for_callback() -> Tuple[str, Optional[str]]:
    """
    Block until Robinhood redirects back with an authorization code.

    Runs a one-shot local web server: it serves exactly one request (the
    redirect), grabs `code` and `state` from the query string, then stops.
    """
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            captured["code"] = params.get("code", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Authorized. You can close this tab.</h3>")

        def log_message(self, *args):
            pass  # keep the sync output clean

    server = HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.handle_request()
    server.server_close()

    if not captured.get("code"):
        raise RuntimeError("Robinhood did not return an authorization code.")
    return captured["code"], captured.get("state")


async def _open_browser(authorization_url: str) -> None:
    """Show the user Robinhood's consent page."""
    # On a scheduled run there is nobody to click "approve": opening a browser
    # would just block on the callback until the SDK's 300s timeout. Fail
    # immediately instead, so the log says what actually went wrong.
    if NO_BROWSER:
        raise RuntimeError(
            "Robinhood needs re-authorization, but ROBINHOOD_NO_BROWSER is set. "
            "Run `python sync_robinhood.py` by hand once to sign in again."
        )

    print("\nOpening your browser to authorize Robinhood access...")
    print(f"If it doesn't open, visit:\n  {authorization_url}\n")
    webbrowser.open(authorization_url)


async def _await_callback() -> Tuple[str, Optional[str]]:
    """Run the blocking callback server off the event loop."""
    import asyncio

    return await asyncio.to_thread(_wait_for_callback)


# =====================================================================
# 4. Robinhood MCP data source (official Agentic Trading MCP)
# =====================================================================
#
# Robinhood's official MCP server (launched May 2026) lives at:
#     https://agent.robinhood.com/mcp/trading
#
# It exposes read tools for positions, balances, orders, and transactions.
# The EXACT tool name for order history isn't hard-coded here — instead we
# LIST the server's tools at runtime and pick the one that looks like
# order history (or the one you set via ROBINHOOD_ORDERS_TOOL in .env).
# That way this code adapts if Robinhood renames things.
# =====================================================================

def _as_float(value) -> Optional[float]:
    """
    Robinhood sends every number as a string, sometimes with absurd
    precision ("249.2102941534238085909240487"). None and "" mean absent.
    """
    if value in (None, ""):
        return None
    return float(value)


class RobinhoodMCPSource:
    """
    Connects to Robinhood's official MCP server and fetches order history.

    Read-only by design: the only MCP tool this class will ever call is the
    order-history tool. Nothing here can place a trade.
    """

    # Name fragments we look for when guessing the order-history tool.
    ORDER_TOOL_HINTS = ("order", "history", "transaction")

    # Stop after this many pages, so a misbehaving cursor can't loop forever.
    MAX_PAGES = 100

    # With ROBINHOOD_SINCE=auto, start this many days BEFORE the newest trade
    # we already have. The overlap is deliberate: a fill can settle late and
    # land behind the newest timestamp we stored. Re-fetching it costs nothing
    # because the UNIQUE index throws duplicates away.
    OVERLAP_DAYS = 5

    def __init__(self):
        self.url = os.getenv(
            "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
        )
        # Optional: pin the exact tool name once you know it.
        self.tool_name = os.getenv("ROBINHOOD_ORDERS_TOOL", "")
        # Required: the orders tool refuses to run without an account number.
        self.account_number = os.getenv("ROBINHOOD_ACCOUNT_NUMBER", "")
        # Optional: only fetch orders created on/after this date (YYYY-MM-DD).
        # Leave blank for a full backfill; set it for cheap incremental syncs.
        self.since = os.getenv("ROBINHOOD_SINCE", "")

    def fetch_orders(self) -> List[dict]:
        # `mcp` is the official Model Context Protocol Python SDK.
        # Imported lazily so mock mode works even if it isn't installed.
        import asyncio
        return asyncio.run(self._fetch_orders_async())

    def _build_auth(self):
        """Assemble the OAuth handler the SDK will use for every request."""
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        storage = FileTokenStorage(TOKEN_PATH)
        provider = OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata.model_validate({
                "client_name": "Trade Recorder",
                "redirect_uris": [f"http://localhost:{CALLBACK_PORT}/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }),
            storage=storage,
            redirect_handler=_open_browser,
            callback_handler=_await_callback,
        )

        # Hand the SDK the cached token's real deadline. Without it, a token
        # loaded from disk is treated as valid forever, the refresh token is
        # never spent, and every expiry turns into a fresh browser login —
        # which on a cron job is a silent hang. See stored_expiry().
        provider.context.token_expiry_time = storage.stored_expiry()
        return provider

    async def _prime_oauth_metadata(self, provider) -> None:
        """
        Tell the provider where Robinhood actually issues tokens.

        Robinhood mints tokens at api.robinhood.com, NOT at the MCP host. The
        SDK only learns that during a full browser login, because discovery
        lives inside its 401 handler. On a run that starts from cached tokens
        it never discovers anything, so a refresh falls back to a guessed
        `<mcp-host>/token`, gets a 404, and collapses into a browser login.

        Fetching the well-known document up front (one small GET) makes the
        refresh go to the right place. A failure here is not fatal — it just
        restores the old fallback behaviour.
        """
        import httpx
        from mcp.shared.auth import OAuthMetadata

        parsed = urlparse(self.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        candidates = (
            f"{base}/.well-known/oauth-authorization-server{parsed.path}",
            f"{base}/.well-known/oauth-authorization-server",
        )

        async with httpx.AsyncClient(timeout=15) as client:
            for url in candidates:
                try:
                    resp = await client.get(url)
                except httpx.HTTPError:
                    continue
                if resp.status_code == 200:
                    provider.context.oauth_metadata = (
                        OAuthMetadata.model_validate_json(resp.content)
                    )
                    return

        print("  (warning: OAuth metadata discovery failed — a token refresh "
              "may fall back to a browser login)")

    async def _fetch_orders_async(self) -> List[dict]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if not self.account_number:
            raise RuntimeError(
                "ROBINHOOD_ACCOUNT_NUMBER is not set in .env — the order-history "
                "tool requires it. Use the account whose trades you want to record."
            )

        auth = self._build_auth()
        await self._prime_oauth_metadata(auth)

        async with streamablehttp_client(self.url, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # --- Discover the server's tools -------------------------
                tools = (await session.list_tools()).tools
                # Only dump the catalogue when we're actually hunting for the
                # right tool (or asked to). Printing ~5KB of tool descriptions
                # on every scheduled run just bloats sync.log.
                if not self.tool_name or VERBOSE:
                    print("Robinhood MCP tools available:")
                    for t in tools:
                        print(f"  - {t.name}: {(t.description or '')[:80]}")

                # --- Pick the order-history tool --------------------------
                tool = self._pick_orders_tool(tools)
                if tool is None:
                    raise RuntimeError(
                        "Couldn't identify the order-history tool. Look at the "
                        "list above and set ROBINHOOD_ORDERS_TOOL in .env to "
                        "the correct tool name."
                    )
                print(f"Using tool: {tool.name}")

                # --- Call it (READ-ONLY), following pagination -------------
                return await self._fetch_all_pages(session, tool.name)

    def _resolve_since(self) -> str:
        """
        Work out the `created_at` lower bound for this run.

        A literal date is used as-is. "auto" walks back OVERLAP_DAYS from the
        newest fill already stored, which keeps a scheduled sync to a single
        page instead of re-reading years of history. An empty database means
        there's nothing to be incremental about, so we do a full backfill.
        """
        raw = self.since.strip()
        if raw.lower() != "auto":
            return raw

        with db.get_connection() as conn:
            newest = db.newest_filled_at(conn)
        if not newest:
            print("  (since=auto: database is empty, doing a full backfill)")
            return ""

        newest_dt = datetime.fromisoformat(newest.replace("Z", "+00:00"))
        bound = (newest_dt - timedelta(days=self.OVERLAP_DAYS)).strftime("%Y-%m-%d")
        print(f"  (since=auto: newest stored fill {newest[:10]}, "
              f"asking from {bound})")
        return bound

    async def _fetch_all_pages(self, session, tool_name: str) -> List[dict]:
        """
        Walk every page of order history.

        One page holds a couple hundred orders, so a single call would
        silently truncate the history. We keep following the cursor until
        the server stops handing one back.
        """
        orders: List[dict] = []
        cursor = None
        since = self._resolve_since()

        for page in range(1, self.MAX_PAGES + 1):
            args = {"account_number": self.account_number, "state": "filled"}
            if since:
                args["created_at_gte"] = since
            if cursor:
                args["cursor"] = cursor

            result = await session.call_tool(tool_name, arguments=args)
            payload = self._payload(result)
            batch = payload.get("orders", [])
            orders.extend(batch)
            print(f"  page {page}: {len(batch)} order(s)")

            cursor = self._next_cursor(payload)
            if not cursor or not batch:
                break
        else:
            print(f"  (stopped at the {self.MAX_PAGES}-page safety limit)")

        return orders

    # -----------------------------------------------------------------
    # Portfolio: open positions (+ live prices) and realized P&L.
    #
    # These run in their OWN MCP session rather than sharing the orders
    # one. It costs a second connection (~15s), but the orders path is
    # verified and working and I'd rather not destabilise it. Tool names
    # are fixed here, not configurable like ROBINHOOD_ORDERS_TOOL —
    # they're confirmed against the live server and there's no ambiguity
    # to resolve the way there was for order history.
    # -----------------------------------------------------------------
    POSITIONS_TOOL = "get_equity_positions"
    QUOTES_TOOL = "get_equity_quotes"
    REALIZED_PNL_TOOL = "get_pnl_trade_history"

    # get_equity_quotes drops the `closes` block above 20 symbols.
    QUOTE_BATCH = 20

    def fetch_portfolio(self) -> Tuple[List[dict], List[dict]]:
        """Return (positions, realized_pnl) ready for the database."""
        import asyncio
        return asyncio.run(self._fetch_portfolio_async())

    def fetch_history(self, price_symbols: List[str], start: str) -> dict:
        """
        Fetch the Phase 1 extras: daily closes, account totals, tax lots.

        Separate from fetch_portfolio() so the existing two-tuple contract
        (and MockDataSource, which implements it) stays untouched. Returns
        {} for any source that can't supply them.
        """
        import asyncio
        return asyncio.run(self._fetch_history_async(price_symbols, start))

    async def _fetch_history_async(self, price_symbols: List[str],
                                   start: str) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if not self.account_number:
            raise RuntimeError("ROBINHOOD_ACCOUNT_NUMBER is not set in .env")

        auth = self._build_auth()
        await self._prime_oauth_metadata(auth)

        out: dict = {"daily_prices": [], "totals": None, "tax_lots": []}
        async with streamablehttp_client(self.url, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Each block is independently guarded: none of this is worth
                # losing an otherwise good sync over. Factories rather than
                # coroutines, so a failure in one doesn't leave the others
                # created-but-never-awaited.
                held = [p["symbol"] for p in await self._fetch_positions(session)]
                steps = (
                    ("prices", "daily_prices",
                     lambda: self._fetch_daily_prices(session, price_symbols, start)),
                    ("portfolio totals", "totals",
                     lambda: self._fetch_portfolio_totals(session)),
                    ("tax lots", "tax_lots",
                     lambda: self._fetch_tax_lots(session, held)),
                )
                for label, key, make in steps:
                    try:
                        out[key] = await make()
                    except Exception as exc:
                        print(f"  ({label} failed: {type(exc).__name__}: "
                              f"{str(exc)[:120]} — continuing)")
        return out

    async def _fetch_portfolio_async(self) -> Tuple[List[dict], List[dict]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if not self.account_number:
            raise RuntimeError("ROBINHOOD_ACCOUNT_NUMBER is not set in .env")

        auth = self._build_auth()
        await self._prime_oauth_metadata(auth)

        async with streamablehttp_client(self.url, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                positions = await self._fetch_positions(session)
                realized = await self._fetch_realized(session)
                return positions, realized

    async def _fetch_positions(self, session) -> List[dict]:
        """Open positions, priced with a live quote for each symbol."""
        raw: List[dict] = []
        cursor = None
        for _ in range(self.MAX_PAGES):
            args = {"account_number": self.account_number}
            if cursor:
                args["cursor"] = cursor
            payload = self._payload(await session.call_tool(self.POSITIONS_TOOL, args))
            raw.extend(payload.get("positions", []))
            cursor = self._next_cursor(payload)
            if not cursor:
                break

        if not raw:
            print("  positions: none returned")
            return []

        symbols = [p["symbol"] for p in raw if p.get("symbol")]
        quotes = await self._fetch_quotes(session, symbols)

        positions = []
        for p in raw:
            price, source = quotes.get(p["symbol"], (None, "unavailable"))
            positions.append({
                "symbol": p["symbol"],
                "quantity": float(p.get("quantity") or 0),
                "average_buy_price": _as_float(p.get("average_buy_price")),
                "last_price": price,
                "price_source": source,
            })
        print(f"  positions: {len(positions)}")
        return positions

    async def _fetch_quotes(self, session, symbols: List[str]) -> dict:
        """
        Map symbol -> (price, which_field_it_came_from).

        Robinhood returns both a regular-session price and an
        extended-hours one. Whichever carries the newer timestamp is the
        current price; we record which so the dashboard can say so rather
        than quietly mixing the two.
        """
        out: dict = {}
        for i in range(0, len(symbols), self.QUOTE_BATCH):
            batch = symbols[i:i + self.QUOTE_BATCH]
            payload = self._payload(
                await session.call_tool(self.QUOTES_TOOL, {"symbols": batch})
            )
            for entry in payload.get("results", []):
                q = entry.get("quote") or {}
                symbol = q.get("symbol")
                if not symbol:
                    continue

                regular = (_as_float(q.get("last_trade_price")),
                           q.get("venue_last_trade_time") or "")
                extended = (_as_float(q.get("last_non_reg_trade_price")),
                            q.get("venue_last_non_reg_trade_time") or "")

                # ISO-8601 UTC strings sort chronologically, so a plain
                # string comparison picks the fresher of the two.
                if extended[0] is not None and extended[1] > regular[1]:
                    out[symbol] = (extended[0], "extended_hours")
                elif regular[0] is not None:
                    out[symbol] = (regular[0], "regular_session")
        return out

    async def _fetch_realized(self, session) -> List[dict]:
        """Every realizing (closing) trade, with the gain/loss on each."""
        rows: List[dict] = []
        cursor = None
        for _ in range(self.MAX_PAGES):
            args = {"account_number": self.account_number, "span": "all"}
            if cursor:
                args["cursor"] = cursor
            payload = self._payload(
                await session.call_tool(self.REALIZED_PNL_TOOL, args)
            )
            for t in payload.get("trades", []):
                rows.append({
                    "timestamp": t.get("timestamp"),
                    "symbol": t.get("symbol"),
                    "quantity": _as_float(t.get("quantity")),
                    "price": _as_float(t.get("price")),
                    "realized_gain": _as_float(t.get("realized_gain")),
                })
            # This tool paginates with `next_cursor`, not the `next` URL
            # that the orders tool uses.
            cursor = payload.get("next_cursor") or None
            if not cursor:
                break

        total = sum(r["realized_gain"] or 0 for r in rows)
        print(f"  realized P&L: {len(rows)} closing trade(s), total ${total:,.2f}")
        return rows

    # -----------------------------------------------------------------
    # Phase 1: the extra reads that make performance measurable.
    #
    # These ride along in the portfolio session rather than opening a
    # third connection. Each is individually non-fatal — a failure here
    # must not cost you the orders and positions that already synced,
    # so callers wrap them and carry on.
    # -----------------------------------------------------------------
    HISTORICALS_TOOL = "get_equity_historicals"
    PORTFOLIO_TOOL = "get_portfolio"
    TAX_LOTS_TOOL = "get_equity_tax_lots"

    # get_equity_historicals accepts at most 10 symbols per call.
    HISTORICALS_BATCH = 10

    async def _fetch_daily_prices(self, session, symbols: List[str],
                                  start: str) -> List[dict]:
        """
        Daily closes for `symbols` from `start` (YYYY-MM-DD) to now.

        Asks for every symbol ever traded, not just those currently held:
        valuing the portfolio on a past date needs prices for positions
        since closed. Bars come back with string prices and a `begins_at`
        timestamp whose date is the trading day.
        """
        rows: List[dict] = []
        for i in range(0, len(symbols), self.HISTORICALS_BATCH):
            batch = symbols[i:i + self.HISTORICALS_BATCH]
            payload = self._payload(await session.call_tool(
                self.HISTORICALS_TOOL,
                {"symbols": batch,
                 "start_time": f"{start}T00:00:00Z",
                 "interval": "day"},
            ))
            for result in payload.get("results", []):
                symbol = result.get("symbol")
                if not symbol:
                    continue
                for bar in result.get("bars", []):
                    close = _as_float(bar.get("close_price"))
                    begins = bar.get("begins_at") or ""
                    if close is None or not begins:
                        continue
                    rows.append({"symbol": symbol,
                                 "date": begins[:10],
                                 "close": close})
            print(f"  prices: {len(batch)} symbol(s), {len(rows)} bar(s) so far")
        return rows

    async def _fetch_portfolio_totals(self, session) -> Optional[dict]:
        """
        Account-level equity and cash.

        Cash matters for return figures: uninvested money is capital you
        committed that earned nothing, and without this it is invisible.
        There is no historical version of this endpoint, so the series can
        only ever start from the first sync that records it.
        """
        payload = self._payload(await session.call_tool(
            self.PORTFOLIO_TOOL, {"account_number": self.account_number}))
        if not payload:
            return None
        totals = {
            "total_value": _as_float(payload.get("total_value")),
            "equity_value": _as_float(payload.get("equity_value")),
            "cash": _as_float(payload.get("cash")),
        }
        print(f"  portfolio: equity ${totals['equity_value'] or 0:,.2f}, "
              f"cash ${totals['cash'] or 0:,.2f}")
        return totals

    async def _fetch_tax_lots(self, session, symbols: List[str]) -> List[dict]:
        """
        Robinhood's own cost basis, one call per symbol (the tool takes a
        single `symbol`, not a list).

        Only open positions have lots; a fully-sold symbol returns none,
        which is why this is driven by current holdings rather than by
        everything ever traded.
        """
        rows: List[dict] = []
        for symbol in symbols:
            payload = self._payload(await session.call_tool(
                self.TAX_LOTS_TOOL,
                {"account_number": self.account_number, "symbol": symbol}))
            for lot in payload.get("tax_lots", []):
                lot_id = lot.get("open_lot_id")
                if not lot_id:
                    continue
                rows.append({
                    "open_lot_id": lot_id,
                    "symbol": symbol,
                    "quantity": _as_float(lot.get("quantity")),
                    "cost_per_share": _as_float(lot.get("cost_per_share")),
                    "cost_basis": _as_float(lot.get("tax_cost_basis")),
                    "open_date": lot.get("open_date"),
                    "term": lot.get("term"),
                })
        print(f"  tax lots: {len(rows)} open lot(s) across {len(symbols)} symbol(s)")
        return rows

    @staticmethod
    def _payload(result) -> dict:
        """
        Pull the {"orders": [...], "next": ...} object out of a tool result.

        The SDK hands us the already-parsed object in `structuredContent`
        when the server provides one, otherwise we fall back to JSON text
        blocks. Robinhood nests the useful part under a "data" key.
        """
        raw = result.structuredContent
        if raw is None:
            for block in result.content:
                if getattr(block, "type", "") == "text":
                    raw = json.loads(block.text)
                    break

        if not isinstance(raw, dict):
            return {}
        data = raw.get("data")
        return data if isinstance(data, dict) else raw

    @staticmethod
    def _next_cursor(payload: dict) -> Optional[str]:
        """
        Read the pagination cursor out of the payload's `next` URL.

        `next` points at an internal Robinhood host we can't fetch directly,
        so only its `cursor` query parameter is of any use to us.
        """
        nxt = payload.get("next")
        if not nxt:
            return None
        return parse_qs(urlparse(nxt).query).get("cursor", [None])[0]

    def _pick_orders_tool(self, tools):
        """Use the pinned name from .env, or guess by name."""
        if self.tool_name:
            for t in tools:
                if t.name == self.tool_name:
                    return t
            return None
        for t in tools:
            name = t.name.lower()
            # Never pick anything that could modify state.
            if any(bad in name for bad in ("place", "create", "cancel", "buy", "sell")):
                continue
            if any(hint in name for hint in self.ORDER_TOOL_HINTS):
                return t
        return None


# =====================================================================
# 5. The sync itself
# =====================================================================

def run_sync() -> None:
    """Fetch orders, keep the fills, insert new ones into SQLite."""
    source_name = os.getenv("DATA_SOURCE", "mock").lower()
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    if source_name == "robinhood":
        source: TradeDataSource = RobinhoodMCPSource()
    else:
        source = MockDataSource()
    print(f"Data source: {source_name}")

    raw_orders = source.fetch_orders()
    print(f"Fetched {len(raw_orders)} orders.")

    # Dry run: show what came back, write nothing. Use this the first time
    # you point the sync at a real account.
    if dry_run:
        print("\nDRY RUN — nothing will be written to the database.")
        if raw_orders:
            print("First order exactly as received:\n")
            print(json.dumps(raw_orders[0], indent=2, default=str))
            sample = normalize_order(raw_orders[0])
            print(f"\nNormalizes to: {sample}")
        return

    inserted = skipped_dupe = skipped_unfilled = 0
    with db.get_connection() as conn:
        for raw in raw_orders:
            trade = normalize_order(raw)   # returns None unless filled+valid
            if trade is None:
                skipped_unfilled += 1
                continue
            if db.insert_trade(conn, trade):
                inserted += 1
                print(f"  + {trade.side.upper():4} {trade.quantity} {trade.symbol}"
                      f" @ ${trade.average_price}")
            else:
                skipped_dupe += 1

    print(f"\nDone. Inserted {inserted} new trade(s), "
          f"skipped {skipped_dupe} duplicate(s), "
          f"skipped {skipped_unfilled} non-filled/invalid order(s).")

    # --- Portfolio snapshot: holdings and realized P&L ------------------
    # Separate from the trade history above: trades are immutable history,
    # these are point-in-time and get replaced on every sync.
    print("\nPortfolio snapshot:")
    positions, realized = source.fetch_portfolio()
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with db.get_connection() as conn:
        n_pos = db.replace_positions(conn, positions, as_of)
        n_pnl = db.replace_realized_pnl(conn, realized, as_of)

    if n_pos or n_pnl:
        print(f"  stored {n_pos} position(s) and {n_pnl} realized trade(s).")
    else:
        print("  nothing returned — previous snapshot left untouched.")

    # --- History: the series performance metrics are computed from --------
    # Positions are archived from what we just fetched rather than re-read,
    # so the archive and the current snapshot always agree.
    with db.get_connection() as conn:
        n_arch = db.archive_positions(conn, positions, as_of)
        symbols = sorted({r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM trades")})
    if n_arch:
        print(f"  archived {n_arch} position(s) to the history table.")

    print("\nPrice and account history:")
    # Only backfill the whole range once; later runs top up from the newest
    # close already stored.
    with db.get_connection() as conn:
        price_start = db.prices_start_from(conn, symbols, db.HISTORY_STARTS)
    if price_start != db.HISTORY_STARTS:
        print(f"  (prices: topping up from {price_start})")
    try:
        history = source.fetch_history(symbols, price_start)
    except Exception as exc:
        # The trades and snapshot above are already committed; this is
        # strictly additive, so a failure here is a warning, not a crash.
        print(f"  (unavailable: {type(exc).__name__}: {str(exc)[:150]})")
        history = {}

    with db.get_connection() as conn:
        n_px = db.upsert_daily_prices(conn, history.get("daily_prices") or [])
        n_tot = db.record_portfolio(conn, history.get("totals"), as_of)
        n_lots = db.replace_tax_lots(conn, history.get("tax_lots") or [], as_of)

    if n_px or n_tot or n_lots:
        print(f"  stored {n_px} daily close(s), {n_tot} account total(s), "
              f"{n_lots} tax lot(s).")
    else:
        print("  nothing returned — existing history left untouched.")


if __name__ == "__main__":
    run_sync()
