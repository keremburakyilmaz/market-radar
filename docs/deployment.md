# Market Radar deployment

Market Radar publishes data without committing it. GitHub stores code and run
logs; Cloudflare R2 stores mutable state and public snapshots; the personal
website fetches the latest snapshot at runtime.

```text
Refresh Action
  -> restore private state from market-radar-state
  -> collect, build, and validate
  -> write immutable JSON to market-radar-public
  -> conditionally advance v1/latest.json
  -> conditionally advance private state

Personal website
  -> GET https://radar-data.keremburakyilmaz.com/v1/latest.json
  -> verify and GET the referenced immutable snapshot
```

The workflows have `contents: read`, disable persisted checkout credentials,
and contain no `git add`, `git commit`, `git push`, or cross-repository checkout.
Files beneath `out/` are sanitized and uploaded only as expiring workflow
diagnostics.

## Cloudflare resources

Create two R2 buckets in the same Cloudflare account:

| Bucket | Access | Purpose |
| --- | --- | --- |
| `market-radar-public` | Public through one custom domain | `v1/latest.json` and immutable public snapshots |
| `market-radar-state` | Private | Content-addressed checkpoints and `state/latest.json` |

Create two R2 S3 API tokens with **Object Read & Write**, each scoped to only
its corresponding bucket. Do not use an account-wide Admin token. R2's S3
region is `auto`; the endpoint is
`https://<CLOUDFLARE_ACCOUNT_ID>.r2.cloudflarestorage.com`.

Connect `market-radar-public` to the R2 custom domain
`radar-data.keremburakyilmaz.com`. Do not point a CNAME at an `r2.dev` URL.
Disable the public `r2.dev` development URL after the custom domain is active.
The private state bucket must have no public hostname.

### CORS

Apply this CORS policy to the public bucket. Add the `www` origin only if the
website actually serves from it; CORS origin matching is exact.

```json
[
  {
    "AllowedOrigins": ["https://keremburakyilmaz.com"],
    "AllowedMethods": ["GET"],
    "AllowedHeaders": [],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

The bucket is intentionally public, so CORS is a browser policy, not an access
control boundary. Never put API keys, private state, licensed article bodies,
or raw provider responses in it.

### Cache and bot settings

Create hostname-scoped Cloudflare rules for
`radar-data.keremburakyilmaz.com`:

- Bypass cache for `/v1/latest.json`. The object also carries
  `Cache-Control: no-store`, but the explicit bypass prevents an old pointer
  from surviving an overwrite at the edge.
- Cache `/v1/snapshots/*` for up to one year. Snapshot names contain their full
  SHA-256 and are never overwritten.
- Do not apply managed challenges, Bot Fight Mode, Browser Integrity Check,
  Hotlink Protection, or interactive access rules to this hostname. Public JSON
  fetches must receive JSON, not a challenge page. If a zone-wide bot feature
  cannot exempt this hostname, turn that feature off or serve the data from a
  Cloudflare configuration where the hostname can be exempted.
- Keep TLS enforced. The exception is for bot/challenge behavior, not HTTPS.

After changing CORS or cache rules, purge cached objects for the JSON hostname
before testing.

## GitHub environment

Create two environments, both restricted to the `main` branch:

- `market-radar-production` is used by scheduled refreshes. Do not require a
  human approval here, or every scheduled run will wait indefinitely.
- `market-radar-operations` is used only by manual pause, resume, and rollback.
  Require at least one reviewer and prevent self-review if the repository plan
  supports it.

Both workflows share the `market-radar-production` concurrency group, so an
operation and a refresh cannot mutate pointers simultaneously.

Configure these variables in both environments, or as repository variables:

| Name | Value |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ENDPOINT` | Optional explicit S3 endpoint; leave empty to derive it from the account ID |
| `R2_PUBLIC_BUCKET` | `market-radar-public` |
| `R2_STATE_BUCKET` | `market-radar-state` |

Configure these secrets in `market-radar-production`:

| Name | Scope |
| --- | --- |
| `R2_PUBLIC_ACCESS_KEY_ID` | Public-bucket token access-key ID |
| `R2_PUBLIC_SECRET_ACCESS_KEY` | Public-bucket token secret |
| `R2_STATE_ACCESS_KEY_ID` | State-bucket token access-key ID |
| `R2_STATE_SECRET_ACCESS_KEY` | State-bucket token secret |
| `FRED_API_KEY` | FRED API key; never exposed in public output |

Configure the four R2 secrets in `market-radar-operations` as well. The
workflow exposes state-bucket credentials to pause/resume steps and adds the
public-bucket credentials only to rollback. It never receives `FRED_API_KEY`.

The weekly smoke workflow does not enter the production environment and never
receives R2 credentials. If FRED coverage is desired there, add a separate
repository-level `FRED_API_KEY` secret or a dedicated smoke environment. It is
optional for the smoke because the run is diagnostic and nonblocking.

## Workflows

### CI

`.github/workflows/ci.yml` runs on pull requests and pushes to `main` with
Python 3.12. It installs `.[dev]`, runs Ruff lint and format checks, strict mypy,
the contract/source-fixture suites, and the complete unittest suite. CI receives
no provider or R2 secrets.

### Refresh

`.github/workflows/refresh.yml` runs at 00:17, 08:17, 13:17, and 18:17
Europe/Istanbul time. GitHub evaluates cron in UTC, so the workflow uses
`17 5,10,15,21 * * *`; Turkey remains UTC+3 year-round. The non-zero minute
avoids GitHub's busiest cron boundary. Production runs are serialized by the
`market-radar-production` concurrency group.

Scheduled runs execute:

```sh
python -m market_radar refresh \
  --publish \
  --target r2 \
  --slot "gha-${GITHUB_RUN_ID}" \
  --output-dir out
```

The run ID makes retries of the same Actions run slot-idempotent. A manual
dispatch defaults `publish` to false and instead uses the local target. It
collects and validates a real candidate but cannot reach R2.

Before a publishing run collects any source, it reads
`control/publication.json` from the private state bucket. A missing control
object means enabled. A valid paused object produces a no-op report without
collecting, publishing, or advancing state; a corrupt control object fails the
run closed.

### Live-source smoke

`.github/workflows/live-source-smoke.yml` runs every Monday at 06:43 UTC and on
manual dispatch. It performs a local dry run with live network sources, has no
R2 credentials, and is deliberately nonblocking. Failures remain visible in
the step result, warning annotation, summary, and seven-day diagnostic artifact.

### Operations

`.github/workflows/ops.yml` runs only by manual dispatch from `main`, through
the protected `market-radar-operations` environment.

- `pause` conditionally writes a private, bounded control object. Scheduled
  publication then stops before collection.
- `resume` conditionally enables publication again and records the operator and
  reason privately.
- `rollback` first pauses publication, then reads an existing immutable public
  snapshot, verifies its schema, canonical bytes, SHA-256 key, path timestamp,
  and object metadata, and conditionally moves `v1/latest.json` to it. If any
  verification or pointer write fails, publication remains paused.

Rollback changes only the public pointer; it does not rewind the latest private
analysis checkpoint. This is deliberate. The pause prevents a scheduled run
from immediately superseding the rollback, and a later validated refresh
continues from the newest durable engine state after an explicit resume.

Do not perform operations with raw `aws s3 cp`, Wrangler, or dashboard pointer
edits. Those paths bypass contract validation, conditional writes, and the
private audit record.

## First publication

1. Merge the tested engine and workflows to `main`.
2. Create both R2 buckets and the two bucket-scoped tokens.
3. Activate the custom domain, disable `r2.dev`, and configure CORS, cache
   bypass, immutable snapshot caching, and bot/challenge exemptions.
4. Create and populate both GitHub environments. Protect
   `market-radar-operations` with required review.
5. Confirm CI is green on the exact `main` commit.
6. Manually run **Refresh Market Radar** with `publish=false`. Download the
   diagnostic artifact and inspect the candidate, source coverage, freshness,
   and report.
7. Manually run it again with `publish=true` from `main`.
8. Confirm that `market-radar-public` contains an immutable
   `v1/snapshots/YYYY/MM/DD/<timestamp>-<sha256>.json` and `v1/latest.json`.
9. Fetch `https://radar-data.keremburakyilmaz.com/v1/latest.json`, fetch the
   referenced path, and verify its byte length and SHA-256 against the manifest.
10. Confirm `market-radar-state/state/latest.json` references an existing private
    checkpoint and that no generated-data commit appeared in Git history.
11. Dispatch **Market Radar Operations** with `pause`, confirm a publishing
    refresh becomes a no-op, then dispatch `resume` before enabling the schedule.

Only after those checks should the personal website switch from mock data to
runtime fetching.

## Rollback runbook

1. Copy the exact prior immutable key from R2 or an older `v1/latest.json`.
2. Dispatch **Market Radar Operations** with `rollback`, that key, and a concise
   audit reason. Approve the protected environment request.
3. Fetch public `v1/latest.json`; verify that it references the selected key and
   that the referenced bytes match its size and SHA-256.
4. Leave publication paused while repairing and validating the cause. Manual
   refreshes with `publish=false` remain available for live diagnostics.
5. Dispatch `resume`, then run one manual publishing refresh and verify the
   public pointer, private checkpoint, source coverage, and workflow report.

## Known operational limitation

GitHub may delay or drop scheduled work during load. It also disables schedules
in a public repository after 60 days without repository activity. Before this is
treated as unattended production, add an external freshness monitor that parses
`v1/latest.json`, alerts when it is stale, and dispatches `refresh.yml` through a
fine-grained GitHub token if necessary. Do not add synthetic heartbeat commits;
that would recreate the history-thrashing problem this architecture removes.
