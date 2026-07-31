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
import webbrowser
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
        self._write(data)

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

        return OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata.model_validate({
                "client_name": "Trade Recorder",
                "redirect_uris": [f"http://localhost:{CALLBACK_PORT}/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }),
            storage=FileTokenStorage(TOKEN_PATH),
            redirect_handler=_open_browser,
            callback_handler=_await_callback,
        )

    async def _fetch_orders_async(self) -> List[dict]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if not self.account_number:
            raise RuntimeError(
                "ROBINHOOD_ACCOUNT_NUMBER is not set in .env — the order-history "
                "tool requires it. Use the account whose trades you want to record."
            )

        async with streamablehttp_client(self.url, auth=self._build_auth()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # --- Discover the server's tools -------------------------
                tools = (await session.list_tools()).tools
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

    async def _fetch_all_pages(self, session, tool_name: str) -> List[dict]:
        """
        Walk every page of order history.

        One page holds a couple hundred orders, so a single call would
        silently truncate the history. We keep following the cursor until
        the server stops handing one back.
        """
        orders: List[dict] = []
        cursor = None

        for page in range(1, self.MAX_PAGES + 1):
            args = {"account_number": self.account_number, "state": "filled"}
            if self.since:
                args["created_at_gte"] = self.since
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


if __name__ == "__main__":
    run_sync()
