"""US Treasury and SEC EDGAR data access for GovFacts.

Two sources, both used strictly within their documented open-reuse terms:

- SEC EDGAR (data.sec.gov, www.sec.gov): US federal government work, not subject to
  domestic copyright (17 U.S.C. Section 105). SEC's own fair-access policy requires a
  declared identifying User-Agent and caps automated access at 10 requests/second
  (https://www.sec.gov/os/webmaster-faq). Company filing facts only -- no market
  prices, no quotes.
- US Treasury Fiscal Data (api.fiscaldata.treasury.gov): explicitly "offered free,
  without restriction, and available to copy, adapt, redistribute, or otherwise use
  for non-commercial or commercial purposes" (fiscaldata.treasury.gov/api-documentation).
  No API key. Government interest-rate, receipts/outlays, and reporting-exchange-rate
  datasets only -- no live FX quotes.

Both sources are read live on every call. No caching, no persistence, no bulk
downloads -- each tool call makes exactly the HTTP requests it needs.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx

TreasuryIndicator = Literal["interest_rates", "receipts_outlays", "exchange_rates"]
Concept = Literal["revenue", "net_income", "eps_diluted", "shares_outstanding"]

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/{cik}/{taxonomy}/{tag}.json"
_TREASURY_ORIGIN = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
# SEC's documented cap is 10 requests/second; keep comfortably under it.
_SEC_MIN_INTERVAL = 0.15

_CONCEPT_TAGS: dict[Concept, tuple[str, str]] = {
    "revenue": ("us-gaap", "Revenues"),
    "net_income": ("us-gaap", "NetIncomeLoss"),
    "eps_diluted": ("us-gaap", "EarningsPerShareDiluted"),
    "shares_outstanding": ("dei", "EntityCommonStockSharesOutstanding"),
}

_TREASURY_DATASETS: dict[TreasuryIndicator, dict[str, Any]] = {
    "interest_rates": {
        "path": "v2/accounting/od/avg_interest_rates",
        "fields": ["record_date", "security_type_desc", "security_desc", "avg_interest_rate_amt"],
        "label": "Average Interest Rates on U.S. Treasury Securities (monthly, by security type)",
    },
    "receipts_outlays": {
        "path": "v1/accounting/mts/mts_table_1",
        "fields": [
            "record_date",
            "classification_desc",
            "current_month_gross_rcpt_amt",
            "current_month_gross_outly_amt",
            "current_month_dfct_sur_amt",
        ],
        "label": "Monthly Treasury Statement Table 1 (federal receipts, outlays, deficit/surplus)",
    },
    "exchange_rates": {
        "path": "v1/accounting/od/rates_of_exchange",
        "fields": ["record_date", "country_currency_desc", "exchange_rate"],
        "label": "Treasury Reporting Rates of Exchange (quarterly government reporting rates)",
    },
}

_CIK_PATTERN = re.compile(r"\d{1,10}\Z")


class GovDataError(Exception):
    """An actionable problem retrieving or interpreting government data."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _finite_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise GovDataError(f"Provider returned a non-numeric {field} value.")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise GovDataError(f"Provider returned a non-numeric {field} value.") from None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GovDataError(f"Provider returned a non-finite {field} value.")
    return float(value)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _valid_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (ValueError, TypeError):
        raise GovDataError(f"{field} must be an ISO date (YYYY-MM-DD).") from None
    return value


def _period_type(start: str | None, end: str | None) -> str:
    if start is None or end is None:
        return "instant"
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return "other"
    if days <= 100:
        return "quarterly"
    if 350 <= days <= 380:
        return "annual"
    return "other"


class GovFactsData:
    """Read-only US Treasury and SEC EDGAR data access using an injected HTTP client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._sec_lock = asyncio.Lock()
        self._sec_last_request = 0.0

    async def search_companies(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Find SEC-registered companies and their CIK identifiers by name or ticker."""
        if not isinstance(query, str) or not (query := query.strip()) or len(query) > 100:
            raise GovDataError("Search query must contain 1 to 100 non-whitespace characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise GovDataError("Search limit must be an integer from 1 to 20.")

        payload = await self._sec_get_json(_SEC_TICKERS_URL, subject="company ticker list")
        if not isinstance(payload, dict):
            raise GovDataError("SEC returned a malformed company ticker list.")

        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for entry in payload.values():
            if not isinstance(entry, dict):
                raise GovDataError("SEC returned a malformed company ticker entry.")
            ticker = _text(entry.get("ticker"))
            title = _text(entry.get("title"))
            cik = entry.get("cik_str")
            if ticker is None or title is None or not isinstance(cik, int):
                continue
            if needle in ticker.casefold() or needle in title.casefold():
                matches.append({"ticker": ticker, "name": title, "cik": f"{cik:010d}"})
            if len(matches) == limit:
                break

        return {
            "query": query,
            "retrieved_at": _now(),
            "source": _source("SEC company tickers", _SEC_TICKERS_URL, None),
            "companies": matches,
            "caveats": [
                "Matches only SEC-registered filers and their exact ticker/title text.",
                *([] if matches else ["No matching companies were found for this query."]),
            ],
        }

    async def company_financials(
        self, cik_or_ticker: str, concept: Concept, limit: int = 12
    ) -> dict[str, Any]:
        """Return one reported financial concept's history for one company, from its own filings."""
        if concept not in _CONCEPT_TAGS:
            raise GovDataError(f"Concept must be one of: {', '.join(_CONCEPT_TAGS)}.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 40:
            raise GovDataError("Limit must be an integer from 1 to 40.")
        cik = await self._resolve_cik(cik_or_ticker)

        taxonomy, tag = _CONCEPT_TAGS[concept]
        url = _SEC_CONCEPT_URL.format(cik=f"CIK{cik}", taxonomy=taxonomy, tag=tag)
        payload = await self._sec_get_json(url, subject=f"{concept} for CIK {cik}", allow_404=True)
        if payload is None:
            return {
                "cik": cik,
                "concept": concept,
                "retrieved_at": _now(),
                "source": _source("SEC XBRL company concept", url, None),
                "entity_name": None,
                "observations": [],
                "caveats": [
                    f"This company has not reported '{concept}' under tag {taxonomy}:{tag}."
                ],
            }
        if not isinstance(payload, dict):
            raise GovDataError(f"SEC returned a malformed concept response for {concept}.")

        entity_name = _text(payload.get("entityName"))
        units = payload.get("units")
        if not isinstance(units, dict) or not units:
            raise GovDataError(f"SEC returned no usable units for {concept}.")
        unit_label, series = next(iter(units.items()))
        if not isinstance(series, list):
            raise GovDataError(f"SEC returned a malformed {concept} series.")

        observations: list[dict[str, Any]] = []
        for item in series:
            if not isinstance(item, dict):
                raise GovDataError(f"SEC returned a malformed {concept} observation.")
            form = _text(item.get("form"))
            filed = _text(item.get("filed"))
            end = _text(item.get("end"))
            value = _finite_number(item.get("val"), concept)
            if form is None or filed is None or end is None or value is None:
                continue
            start = _text(item.get("start"))
            observations.append(
                {
                    "value": value,
                    "unit": unit_label,
                    "fiscal_year": item.get("fy") if isinstance(item.get("fy"), int) else None,
                    "fiscal_period": _text(item.get("fp")),
                    "period_start": start,
                    "period_end": end,
                    "period_type": _period_type(start, end),
                    "form": form,
                    "filed": filed,
                    "accession_number": _text(item.get("accn")),
                }
            )
        observations.sort(key=lambda row: (row["period_end"], row["filed"]))
        observations = observations[-limit:]

        return {
            "cik": cik,
            "concept": concept,
            "retrieved_at": _now(),
            "source": _source("SEC XBRL company concept", url, None),
            "entity_name": entity_name,
            "observations": observations,
            "caveats": [
                "Values are as reported in the company's own SEC filings, not independently audited by GovFacts.",
                "period_end marks the reporting period, not the filing date; use 'filed' for when it became public.",
                "Restated or amended figures may appear as separate observations for overlapping periods.",
                "period_type differs across observations (instant/quarterly/annual/other); never sum or compare across different period_type values.",
            ],
        }

    async def treasury_indicator(
        self, indicator: TreasuryIndicator, start_date: str, end_date: str, limit: int = 50
    ) -> dict[str, Any]:
        """Return a US Treasury fiscal dataset's observations for a date range."""
        if indicator not in _TREASURY_DATASETS:
            raise GovDataError(f"Indicator must be one of: {', '.join(_TREASURY_DATASETS)}.")
        start_date = _valid_date(start_date, "start_date")
        end_date = _valid_date(end_date, "end_date")
        if start_date > end_date:
            raise GovDataError("start_date must not be after end_date.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise GovDataError("Limit must be an integer from 1 to 200.")

        dataset = _TREASURY_DATASETS[indicator]
        url = f"{_TREASURY_ORIGIN}/{dataset['path']}"
        params = {
            "fields": ",".join(dataset["fields"]),
            "filter": f"record_date:gte:{start_date},record_date:lte:{end_date}",
            "sort": "-record_date",
            "page[size]": str(limit),
        }
        source_url = str(httpx.URL(url, params=params))
        payload = await self._get_json(url, params=params, subject=f"{indicator} data")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise GovDataError(f"Treasury returned a malformed {indicator} response.")

        observations: list[dict[str, Any]] = []
        for row in payload["data"]:
            if not isinstance(row, dict):
                raise GovDataError(f"Treasury returned a malformed {indicator} record.")
            clean: dict[str, Any] = {}
            for field in dataset["fields"]:
                value = row.get(field)
                if field.endswith(("_amt", "_rate")):
                    clean[field] = _finite_number(value, field)
                else:
                    clean[field] = _text(value)
            observations.append(clean)
        observations.sort(key=lambda row: row.get("record_date") or "")

        return {
            "indicator": indicator,
            "dataset": dataset["label"],
            "start_date": start_date,
            "end_date": end_date,
            "retrieved_at": _now(),
            "source": _source("US Treasury Fiscal Data", source_url, None),
            "observations": observations,
            "caveats": [
                "Reporting frequency varies by dataset (monthly or quarterly); check record_date spacing.",
                *([] if observations else ["No observations were found in this date range."]),
            ],
        }

    async def _resolve_cik(self, cik_or_ticker: str) -> str:
        if not isinstance(cik_or_ticker, str) or not (cik_or_ticker := cik_or_ticker.strip()):
            raise GovDataError("Provide a SEC ticker or 10-digit CIK.")
        if _CIK_PATTERN.fullmatch(cik_or_ticker):
            return f"{int(cik_or_ticker):010d}"
        if len(cik_or_ticker) > 12 or not re.fullmatch(r"[A-Za-z0-9.\-]+", cik_or_ticker):
            raise GovDataError("Ticker must be 1 to 12 letters, digits, '.' or '-'.")
        payload = await self._sec_get_json(_SEC_TICKERS_URL, subject="company ticker list")
        if not isinstance(payload, dict):
            raise GovDataError("SEC returned a malformed company ticker list.")
        needle = cik_or_ticker.casefold()
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            ticker = _text(entry.get("ticker"))
            cik = entry.get("cik_str")
            if ticker is not None and ticker.casefold() == needle and isinstance(cik, int):
                return f"{cik:010d}"
        raise GovDataError(f"No SEC-registered company found for ticker {cik_or_ticker!r}.")

    async def _sec_get_json(
        self, url: str, subject: str, params: dict[str, str] | None = None, allow_404: bool = False
    ) -> Any:
        # SEC's fair-access policy caps automated access at 10 requests/second
        # (https://www.sec.gov/os/webmaster-faq). Serialize SEC calls with a minimum
        # spacing safely under that cap; Treasury has no documented equivalent cap.
        async with self._sec_lock:
            wait = self._sec_last_request + _SEC_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._sec_last_request = time.monotonic()
        return await self._get_json(url, subject, params=params, allow_404=allow_404)

    async def _get_json(
        self,
        url: str,
        subject: str,
        params: dict[str, str] | None = None,
        allow_404: bool = False,
    ) -> Any:
        try:
            response = await self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise GovDataError(f"Timed out while retrieving {subject}. Try again later.") from exc
        except httpx.RequestError as exc:
            raise GovDataError(
                f"Request failed while retrieving {subject}: {exc.__class__.__name__}."
            ) from exc
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code == 429:
            raise GovDataError(f"Rate-limited while retrieving {subject}. Try again later.")
        if not response.is_success:
            raise GovDataError(
                f"Received HTTP {response.status_code} while retrieving {subject}. Try again later."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GovDataError(f"Received invalid JSON while retrieving {subject}.") from exc


def _source(name: str, url: str, as_of: str | None) -> dict[str, str | None]:
    return {"name": name, "url": url, "as_of": as_of}
