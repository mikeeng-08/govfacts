"""Boundaries where a plausible mistake would misrepresent government/company data."""

import httpx
import pytest

from govfacts.data import GovDataError, GovFactsData

TICKERS_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "2": {"cik_str": 2012383, "ticker": "AAPLW", "title": "Apple Warrant Co"},
}


def concept_payload(entity, values):
    return {
        "entityName": entity,
        "units": {
            "USD": [
                {
                    "start": start,
                    "end": end,
                    "val": val,
                    "accn": accn,
                    "fy": fy,
                    "fp": fp,
                    "form": form,
                    "filed": filed,
                }
                for start, end, val, accn, fy, fp, form, filed in values
            ]
        },
    }


def provider(*, tickers=TICKERS_PAYLOAD, concept_status=200, concept_body=None, treasury_body=None):
    def respond(request):
        path = request.url.path
        if path.endswith("/company_tickers.json"):
            return httpx.Response(200, json=tickers)
        if "/xbrl/companyconcept/" in path:
            if concept_status == 404:
                return httpx.Response(404, json={"error": "no data"})
            return httpx.Response(concept_status, json=concept_body)
        if "/fiscal_service/" in path:
            return httpx.Response(200, json=treasury_body)
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_search_matches_ticker_and_name_but_not_unrelated_entries():
    async with provider() as client:
        result = await GovFactsData(client).search_companies("apple")
    tickers = {c["ticker"] for c in result["companies"]}
    assert tickers == {"AAPL", "AAPLW"}
    assert all(c["cik"] == f"{cik:010d}" for c, cik in zip(result["companies"], [320193, 2012383]))


@pytest.mark.anyio
async def test_ticker_resolves_case_insensitively_to_correct_cik():
    async with provider(
        concept_body=concept_payload(
            "Apple Inc.", [("2023-10-01", "2023-12-30", 100, "a", 2024, "Q1", "10-Q", "2024-02-01")]
        )
    ) as client:
        result = await GovFactsData(client).company_financials("aapl", "revenue")
    assert result["cik"] == "0000320193"
    assert result["entity_name"] == "Apple Inc."


@pytest.mark.anyio
async def test_unreported_concept_is_explicit_not_silently_empty():
    async with provider(concept_status=404) as client:
        result = await GovFactsData(client).company_financials("AAPL", "eps_diluted")
    assert result["observations"] == []
    assert "has not reported" in result["caveats"][0]


@pytest.mark.anyio
async def test_observations_carry_period_and_filing_dates_not_conflated():
    payload = concept_payload(
        "NVIDIA CORP",
        [
            ("2023-02-01", "2024-01-28", 60922000000, "a1", 2024, "FY", "10-K", "2024-02-21"),
            ("2024-01-29", "2024-04-28", 26044000000, "a2", 2024, "Q1", "10-Q", "2024-05-22"),
        ],
    )
    async with provider(concept_body=payload) as client:
        result = await GovFactsData(client).company_financials("NVDA", "revenue")
    first, second = result["observations"]
    assert first["period_end"] == "2024-01-28"
    assert first["filed"] == "2024-02-21"
    assert first["period_end"] != first["filed"]
    assert second["form"] == "10-Q"


@pytest.mark.anyio
async def test_limit_keeps_most_recent_periods_not_earliest():
    values = [
        (f"{y}-01-01", f"{y}-12-31", y * 1000, f"a{y}", y, "FY", "10-K", f"{y + 1}-02-01")
        for y in range(2015, 2024)
    ]
    async with provider(concept_body=concept_payload("Test Co", values)) as client:
        result = await GovFactsData(client).company_financials("AAPL", "revenue", limit=3)
    years = [obs["fiscal_year"] for obs in result["observations"]]
    assert years == [2021, 2022, 2023]


@pytest.mark.anyio
async def test_bad_ticker_rejected_before_any_network_call():
    async with provider() as client:
        with pytest.raises(GovDataError, match="12"):
            await GovFactsData(client).company_financials("way-too-long-ticker-name", "revenue")


@pytest.mark.anyio
async def test_unknown_ticker_is_explicit_error_not_empty_result():
    async with provider() as client:
        with pytest.raises(GovDataError, match="No SEC-registered company"):
            await GovFactsData(client).company_financials("NOTREAL", "revenue")


@pytest.mark.anyio
async def test_invalid_concept_rejected_before_network_call():
    async with provider() as client:
        with pytest.raises(GovDataError, match="Concept must be one of"):
            await GovFactsData(client).company_financials("AAPL", "stock_price")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_treasury_rejects_inverted_date_range():
    async with provider() as client:
        with pytest.raises(GovDataError, match="start_date must not be after"):
            await GovFactsData(client).treasury_indicator(
                "interest_rates", "2024-06-01", "2024-01-01"
            )


@pytest.mark.anyio
async def test_treasury_numeric_fields_are_actually_numeric_not_strings():
    treasury_body = {
        "data": [
            {
                "record_date": "2024-03-31",
                "security_type_desc": "Marketable",
                "security_desc": "Treasury Notes",
                "avg_interest_rate_amt": "3.456",
            }
        ]
    }
    async with provider(treasury_body=treasury_body) as client:
        result = await GovFactsData(client).treasury_indicator(
            "interest_rates", "2024-01-01", "2024-12-31"
        )
    rate = result["observations"][0]["avg_interest_rate_amt"]
    assert rate == pytest.approx(3.456)
    assert isinstance(rate, float)


@pytest.mark.anyio
async def test_treasury_empty_range_is_explicit_not_silent():
    async with provider(treasury_body={"data": []}) as client:
        result = await GovFactsData(client).treasury_indicator(
            "exchange_rates", "2024-01-01", "2024-01-02"
        )
    assert result["observations"] == []
    assert "No observations were found" in result["caveats"][-1]


@pytest.mark.anyio
async def test_entries_missing_required_fields_are_skipped_not_crashed():
    """A single malformed ticker entry (e.g. wrong-typed cik_str) is skipped, not fatal."""

    def respond(request):
        if request.url.path.endswith("/company_tickers.json"):
            return httpx.Response(
                200,
                json={
                    "0": {"cik_str": "not-an-int", "ticker": "X", "title": "Y"},
                    "1": {"cik_str": 320193, "ticker": "XGOOD", "title": "Good Co"},
                },
            )
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GovFactsData(client).search_companies("x")
    assert result["companies"] == [{"ticker": "XGOOD", "name": "Good Co", "cik": "0000320193"}]


@pytest.mark.anyio
async def test_non_dict_ticker_entry_raises_not_silently_skipped():
    """A non-dict entry means the whole payload shape is wrong, not one bad record."""

    def respond(request):
        if request.url.path.endswith("/company_tickers.json"):
            return httpx.Response(200, json={"0": "not-a-dict-entry"})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(GovDataError, match="malformed"):
            await GovFactsData(client).search_companies("x")


@pytest.mark.anyio
async def test_caveats_are_always_strings_never_null():
    async with provider() as client:
        search_result = await GovFactsData(client).search_companies("apple")
    assert search_result["companies"]
    assert all(isinstance(c, str) for c in search_result["caveats"])

    async with provider(
        treasury_body={
            "data": [
                {
                    "record_date": "2024-03-31",
                    "security_type_desc": "Marketable",
                    "security_desc": "Treasury Notes",
                    "avg_interest_rate_amt": "3.1",
                }
            ]
        }
    ) as client:
        treasury_result = await GovFactsData(client).treasury_indicator(
            "interest_rates", "2024-01-01", "2024-12-31"
        )
    assert treasury_result["observations"]
    assert all(isinstance(c, str) for c in treasury_result["caveats"])


@pytest.mark.anyio
async def test_period_type_distinguishes_annual_from_quarterly_not_mixed_silently():
    payload = concept_payload(
        "NVIDIA CORP",
        [
            ("2023-02-01", "2024-01-28", 60922000000, "a1", 2024, "FY", "10-K", "2024-02-21"),
            ("2024-01-29", "2024-04-28", 26044000000, "a2", 2024, "Q1", "10-Q", "2024-05-22"),
        ],
    )
    async with provider(concept_body=payload) as client:
        result = await GovFactsData(client).company_financials("NVDA", "revenue")
    annual, quarterly = result["observations"]
    assert annual["period_type"] == "annual"
    assert quarterly["period_type"] == "quarterly"
    assert any("period_type" in c for c in result["caveats"])


@pytest.mark.anyio
async def test_shares_outstanding_instant_fact_has_no_start_and_instant_type():
    payload = {
        "entityName": "Apple Inc.",
        "units": {
            "shares": [
                {
                    "end": "2024-06-30",
                    "val": 15000000000,
                    "accn": "a1",
                    "fy": 2024,
                    "fp": "Q3",
                    "form": "10-Q",
                    "filed": "2024-08-01",
                }
            ]
        },
    }
    async with provider(concept_body=payload) as client:
        result = await GovFactsData(client).company_financials("AAPL", "shares_outstanding")
    obs = result["observations"][0]
    assert obs["period_start"] is None
    assert obs["period_type"] == "instant"


@pytest.mark.anyio
async def test_sec_requests_are_actually_rate_limited():
    import time

    async with provider() as client:
        data = GovFactsData(client)
        start = time.monotonic()
        await data.search_companies("apple")
        await data.search_companies("nvidia")
        elapsed = time.monotonic() - start
    from govfacts.data import _SEC_MIN_INTERVAL

    assert elapsed >= _SEC_MIN_INTERVAL
