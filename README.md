# GovFacts

US Treasury and SEC filing data in Claude. Three read-only tools, no account, no paid API key:

- **`search_companies`** — find a SEC-registered company's exact ticker and CIK.
- **`company_financials`** — a company's reported revenue, net income, diluted EPS, or
  shares outstanding, exactly as it reported them in its own 10-K/10-Q filings.
- **`treasury_indicator`** — US Treasury interest-rate, federal receipts/outlays, or
  government reporting-exchange-rate data over a date range.

**This is not a market-data tool.** No stock price, ETF, cryptocurrency, futures, or
live FX quote of any kind, and no comparison of market performance.

## Configuration

SEC requires every automated request to declare a real contact identifier (see
[sec.gov/os/webmaster-faq](https://www.sec.gov/os/webmaster-faq)). GovFacts asks for
this on install (Claude Desktop prompts for it as part of adding the extension) and
refuses to start without it — there is no bundled default. For manual/stdio setups,
set `GOVFACTS_CONTACT=you@example.org` in the server's environment before starting it.

## Install

**Claude Desktop:** Settings → Extensions → Advanced settings → Extension Developer →
Install Extension… → select `GovFacts.mcpb` from a
[release](https://github.com/mikeeng-08/govfacts/releases). Requires a Claude
Desktop release with MCPB v0.4 UV-runtime support.

## Build from source

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python scripts/build_bundle.py   # writes dist/GovFacts.mcpb
uv run pytest -q                        # 23 tests
uv run ruff check .
```

## Data sources

- **SEC EDGAR** (data.sec.gov, www.sec.gov) — company filing facts. US federal
  government work, not subject to domestic copyright (17 U.S.C. § 105). SEC's own
  fair-access policy requires a declared identifying User-Agent and caps automated
  access at 10 requests/second; GovFacts respects both.
- **US Treasury Fiscal Data** (api.fiscaldata.treasury.gov) — interest rates,
  receipts/outlays, and government reporting exchange rates. Its own API
  documentation states the data is "offered free, without restriction, and available
  to copy, adapt, redistribute, or otherwise use for non-commercial or commercial
  purposes." No key required.

No caching, no persistence, no bulk downloads — every call reads live from the
source. Company figures are exactly as reported; GovFacts does not audit or restate
them. Treasury dataset frequency varies (monthly or quarterly), never daily. Treasury's
API has been observed to time out intermittently during development; GovFacts surfaces
that as an explicit tool error rather than empty data, but expect occasional retries.

## Privacy Policy

- **Data collection:** GovFacts sends only what a tool call needs to answer it —
  your search terms, ticker/CIK, concept choice, indicator choice, and date range —
  directly to SEC EDGAR or US Treasury Fiscal Data. Nothing else is collected.
- **Usage and storage:** requests are made live, on demand. GovFacts does not log,
  cache, or persist queries, results, or your configured contact email anywhere.
- **Third-party sharing:** SEC and US Treasury (the two data sources) receive your
  request parameters and your configured `GOVFACTS_CONTACT` value, sent only in the
  outgoing request's User-Agent header, exactly as SEC's own fair-access policy
  requires. See their own policies: [SEC](https://www.sec.gov/privacy),
  [Treasury](https://www.fiscal.treasury.gov/about-us/privacy-policy).
- **Data retention:** none. GovFacts has no database, cache, or log of past queries.
- **Contact:** open an issue at
  [github.com/mikeeng-08/govfacts/issues](https://github.com/mikeeng-08/govfacts/issues)
  for product or security concerns.

## License

MIT (see `LICENSE`).
