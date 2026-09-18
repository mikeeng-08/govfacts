"""Three read-only US government financial data tools, served locally over MCP stdio."""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

from govfacts.data import Concept, GovDataError, GovFactsData, TreasuryIndicator

Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
CikOrTicker = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.\-]+$"
    ),
]
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

INSTRUCTIONS = """GovFacts is a read-only US government financial-data research assistant.
Use search_companies to find a SEC-registered company's exact ticker and CIK.
Use company_financials for one company's reported revenue, net income, diluted EPS,
or shares outstanding, taken directly from its own SEC filings over time.
Use treasury_indicator for US Treasury interest-rate, federal receipts/outlays, or
government reporting-exchange-rate data over a date range.
There is no stock price, ETF, cryptocurrency, or market-quote data of any kind here --
this tool cannot answer "what is X's stock price" or "compare X vs Y's performance".
It answers questions about a company's own reported financial statements and about
US federal government finances. Company figures are as reported by the company in
its own filings, not independently verified by GovFacts, and not a real-time or
forecast figure. Distinguish the reporting period (period_end) from when a filing
became public (filed). Treasury data is at whatever frequency the source dataset
uses (monthly or quarterly) -- state the actual dates returned, do not assume daily
granularity. Cite the returned source URLs, filing accession numbers, and dates.
"""

SOURCE_GUIDE = {
    "sources": [
        {
            "name": "SEC EDGAR (data.sec.gov, www.sec.gov)",
            "coverage": "Company identifiers and reported financial-statement facts from SEC filings.",
            "basis": "US federal government work; not subject to domestic copyright (17 U.S.C. 105).",
        },
        {
            "name": "US Treasury Fiscal Data (api.fiscaldata.treasury.gov)",
            "coverage": "Treasury interest rates, federal receipts/outlays, government reporting exchange rates.",
            "basis": "Published open for redistribution, commercial or non-commercial, no API key required.",
        },
    ],
    "limitations": [
        "No stock, ETF, cryptocurrency, futures, or options price data of any kind.",
        "No live or historical foreign-exchange quotes; Treasury's reporting rates are periodic government accounting rates, not tradable quotes.",
        "Company figures are exactly as the company reported them; GovFacts does not audit, restate, or normalize them.",
        "Treasury dataset frequency varies (monthly or quarterly); there is no daily series here.",
    ],
    "privacy": (
        "Only your search terms, company identifiers, concept choice, and requested date range "
        "are sent to SEC or Treasury. GovFacts stores no queries, conversation history, or credentials."
    ),
    "sec_privacy_policy": "https://www.sec.gov/privacy",
    "treasury_privacy_policy": "https://www.fiscal.treasury.gov/about-us/privacy-policy",
}


_CONTACT_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER_MARKERS = ("example.com", "example.org", "example.net", "replace", "you@", "yourname")


def _is_real_contact(value: str) -> bool:
    if not _CONTACT_PATTERN.match(value):
        return False
    lowered = value.casefold()
    return not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[GovFactsData]:
    contact = os.environ.get("GOVFACTS_CONTACT", "").strip()
    if not _is_real_contact(contact):
        raise RuntimeError(
            "GOVFACTS_CONTACT must be set to a real, non-placeholder contact email. "
            "SEC requires a declared identifying contact in the User-Agent for "
            "automated EDGAR access (see https://www.sec.gov/os/webmaster-faq). In "
            "Claude Desktop, set this under the extension's configuration; for "
            "manual/stdio setups, export GOVFACTS_CONTACT to your own real address "
            "before starting the server."
        )
    async with httpx.AsyncClient(
        headers={
            "User-Agent": f"GovFacts/1.0 {contact}",
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(15.0, connect=10.0),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        follow_redirects=False,
    ) as client:
        yield GovFactsData(client)


mcp = MCPServer(
    "GovFacts",
    version="1.0.0",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
    log_level="WARNING",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


@mcp.tool(title="Search SEC-registered companies", annotations=READ_ONLY)
async def search_companies(
    query: Query,
    ctx: Context[GovFactsData],
    limit: Annotated[int, Field(ge=1, le=20, description="Maximum companies to return.")] = 8,
) -> dict[str, Any]:
    """Find a SEC-registered company's exact ticker and CIK by name or partial ticker.

    Example: 'Nvidia' or 'NVDA'. Use the returned ticker or CIK with company_financials.
    """
    try:
        return await ctx.request_context.lifespan_context.search_companies(query, limit)
    except GovDataError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Get a company's reported financials", annotations=READ_ONLY)
async def company_financials(
    ticker_or_cik: CikOrTicker,
    concept: Concept,
    ctx: Context[GovFactsData],
    limit: Annotated[
        int, Field(ge=1, le=40, description="Maximum reported periods to return.")
    ] = 12,
) -> dict[str, Any]:
    """Return one financial concept's history for a company, exactly as it reported it to the SEC.

    concept is one of: revenue, net_income, eps_diluted, shares_outstanding.
    Use a ticker (e.g. 'AAPL') or a 10-digit CIK. No stock price or market data here --
    this is reported financial-statement data only, from the company's own SEC filings.
    """
    try:
        return await ctx.request_context.lifespan_context.company_financials(
            ticker_or_cik, concept, limit
        )
    except GovDataError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="Get a US Treasury fiscal indicator", annotations=READ_ONLY)
async def treasury_indicator(
    indicator: TreasuryIndicator,
    start_date: IsoDate,
    end_date: IsoDate,
    ctx: Context[GovFactsData],
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum observations to return.")] = 50,
) -> dict[str, Any]:
    """Return a US Treasury dataset's observations over a date range.

    indicator is one of: interest_rates, receipts_outlays, exchange_rates.
    Dates are YYYY-MM-DD. Not a live FX or bond-price feed -- periodic government
    accounting data at whatever frequency the underlying dataset publishes.
    """
    try:
        return await ctx.request_context.lifespan_context.treasury_indicator(
            indicator, start_date, end_date, limit
        )
    except GovDataError as exc:
        raise ToolError(str(exc)) from exc


@mcp.resource("govfacts://sources", mime_type="application/json")
def sources() -> dict[str, Any]:
    """GovFacts's data sources, coverage, privacy, and methodology limitations."""
    return SOURCE_GUIDE


@mcp.prompt(title="Research government and company financial data")
def research_brief(topic: Query) -> str:
    """Start a concise, source-cited research brief using only reported/government data."""
    return (
        f"Research this topic using GovFacts: {topic}\n"
        "Find the relevant company or Treasury dataset first, then pull reported figures "
        "over a meaningful date range. Write a concise brief: what the reported data shows, "
        "how it changed over time, and what's missing (there is no price/market data here). "
        "Cite source URLs, filing dates, and accession numbers. Distinguish the reporting "
        "period from the filing date. Do not infer stock performance or market reaction from "
        "reported fundamentals alone."
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
