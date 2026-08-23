# Market Radar

Market Radar is a source-first macro intelligence engine. It collects a small
set of attributable macro observations and official release metadata, explains
how those inputs affect a deterministic macro-conditions score, and publishes
a bounded JSON snapshot for a separate website to render.

It is not a trading terminal, a price feed, or a buy/sell signal. The public
contract describes macro-financial pressure and makes every observation time,
retrieval time, source, freshness decision, and score contribution inspectable.

## Product contract

Each run follows one path:

```text
official/public sources
  -> normalize and sanitize
  -> restore eligible last-good observations
  -> score transparent macro conditions
  -> validate the closed v1 schema and semantic invariants
  -> write an immutable, content-addressed snapshot
  -> conditionally advance v1/latest.json
  -> advance private state only after publication smoke checks pass
```

The public bucket contains only:

- `v1/latest.json`, a no-cache manifest pointing to one validated snapshot.
- `v1/snapshots/YYYY/MM/DD/<timestamp>-<sha256>.json`, immutable for one year.

Private checkpoints, credentials, raw responses, internal error details, and
provider keys never enter the public contract. Generated data is stored in R2;
the refresh workflow never commits or pushes it to Git.

The schemas and canonical examples live in [`schemas/`](schemas/) and
[`examples/`](examples/).

## Sources

The [`public-apis/public-apis`](https://github.com/public-apis/public-apis)
catalog was used for discovery. Every selected service is integrated through
its direct endpoint rather than through the catalog itself.

| Source | v1 role | Credential |
| --- | --- | --- |
| U.S. Treasury | 2Y, 10Y, and derived 2s10s curve | None |
| Federal Reserve via FRED | Broad U.S. dollar index (`DTWEXBGS`) | `FRED_API_KEY` |
| CBRT | Official indicative USD/TRY buying rate | None |
| Federal Reserve, ECB, and CBRT | Official release metadata | None |
| BLS and BEA | Official economic calendars | None |
| GDELT | Discovery metadata only; no article bodies or images | None |

ECONDB was on the original candidate list but is intentionally not a v1 core or
fallback source. It duplicates first-party observations, requires a token, and
does not currently provide a sufficiently clear blanket redistribution grant
for its aggregated feed. It may later add non-scoring CPI, employment, or GDP
context after reuse terms are confirmed.

A source failure degrades coverage instead of fabricating data. Eligible
last-good official observations remain visibly stale, and public source health
uses sanitized messages.

## Local setup on macOS

Python 3.12 is the development and CI version.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,r2]"
```

If `python3.12` is not installed, install it with `brew install python@3.12`.

Run the complete local gate:

```sh
make check
```

Run a live dry run. It writes a candidate and report locally, but does not
publish or advance state:

```sh
python -m market_radar refresh \
  --target local \
  --output-dir out \
  --object-store-dir out/local-public \
  --state-file state/state.json
```

Exercise publication semantics against the local object store:

```sh
python -m market_radar refresh \
  --publish \
  --target local \
  --slot local-manual-1 \
  --output-dir out \
  --object-store-dir out/local-public \
  --state-file state/state.json
```

## Deployment

GitHub Actions validates every change, performs four serialized refreshes per
day, and runs a separate nonblocking weekly live-source smoke test. Cloudflare
R2 holds one public snapshot bucket and one private state bucket. The website
will fetch `v1/latest.json` at runtime; it does not need an API key and should
never receive a Cloudflare browser challenge.

See [`docs/deployment.md`](docs/deployment.md) for bucket policy, secrets, CORS,
cache rules, bot-protection exemptions, first publication, and rollback limits.

## License

Engine code is available under the [MIT License](LICENSE). Source data remains
subject to each publisher's own terms and attribution requirements.
