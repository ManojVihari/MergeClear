# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org);
a release is made by pushing the tag `vX.Y.Z` (see README "Releasing").

## [0.3.0] - unreleased

### Added
- C# / ASP.NET Core at the depth of Spring and FastAPI (new dependency: `tree-sitter-c-sharp`). Controllers (attribute and
  conventional routing, `[controller]`/`[action]` tokens, API versions, inherited attributes) and Minimal APIs (`MapGroup`
  chains and conventions, endpoint extension methods, `UseEndpoints`, method groups, lambdas) get parameters with their
  binding source, request and response schemas (DataAnnotations, FluentValidation, records, generics, `System.Text.Json`
  naming), status codes (`Ok`/`NotFound`/`TypedResults`, `[ProducesResponseType]`, exception middleware and
  `IExceptionHandler`), `[Authorize]`/policy/`RequireAuthorization` security, and the call chain through the DI container
  (interfaces, generic repositories bound to `IRepository<Order>`, MediatR handlers, MassTransit consumers) down to
  EF Core tables, Dapper SQL, MongoDB, Redis, brokers, HTTP clients (`BaseAddress`, `IOptions<T>` resolved from
  `appsettings.json`), storage, e-mail and search. `architecture.aspnet` mirrors the other models and the server shows it,
  alone or together with Spring and FastAPI models of the same repository.
- FastAPI at the depth of Spring. The scanner builds a model of the application (apps, routers,
  `include_router` prefixes and dependencies, mounts, factories) and reports for every route:
  parameters including those of `Depends` chains, Pydantic/SQLModel request and response schemas with
  constraints, status codes (decorator, `responses`, `HTTPException` in the call chain, exception
  handlers), security schemes and scopes, and what the call chain reaches (SQLAlchemy/SQLModel/Mongo/
  Tortoise, Redis, Celery, Kafka, RabbitMQ, httpx/requests, S3, background tasks...). `--commit`/`--since`
  map changed lines to functions, models, entities, dependencies and router configuration.
  `architecture.fastapi` mirrors `architecture.spring`; the server shows it on the architecture pages,
  also together with a Spring model of the same repository.
- Projects: `mergeclear scan --project NAME` (or `MERGECLEAR_PROJECT`, `project:` in
  `mergeclear.yml`). API keys can carry a default project, used when the pipeline
  names none; the key form explains this and shows the pipeline step to paste.
- Uploading requires both the server URL and an API key; the scanner names everything
  missing before it sends anything.
- Limits: request bodies over `MERGECLEAR_MAX_UPLOAD_MB` (25) get 413; uploads per
  account over `MERGECLEAR_UPLOADS_PER_MINUTE` (30) get 429 with `Retry-After`, which
  the scanner waits for and retries (up to 3 times).
- HTTPS: `docker-compose.https.yml` puts Caddy (automatic Let's Encrypt certificates,
  HSTS, HTTP→HTTPS) in front of the server; `MERGECLEAR_TRUSTED_PROXIES` for your own proxy.
- Backups: `scripts/backup.sh` (verified `pg_dump`, keeps the newest 14) and
  `scripts/restore.sh`; the smoke test does a backup → wipe → restore round trip.
- Releases: pushing a `vX.Y.Z` tag publishes `ghcr.io/<owner>/mergeclear-server` and
  `-scanner` (amd64 + arm64) and a GitHub release with the scanner's wheel and sdist.
  `scripts/build_dist.sh` builds and checks the PyPI files.

### Changed
- FastAPI route fields now follow the Spring shape: `errors` hold status names (`NOT_FOUND`) and one
  `VALIDATION_ERROR` entry instead of bare integers, `handler` is `module.function`, and `status_codes`,
  `request_body`, `security` and `integrations` are filled. Reports of older scanners stay readable.
- `docker compose up` pulls the released server image; `docker-compose.build.yml`
  builds from the checkout instead.
- The scanner is packaged with `pyproject.toml` (PyPI metadata, one version source).
- `--name` / `MERGECLEAR_NAME` / `name:` are now spelled `--project` /
  `MERGECLEAR_PROJECT` / `project:` (the old spellings still work).

### Fixed
- CI now runs on pushes to `master` (it only listened to `main`).

## [0.2.0]

First version under the Mergeclear name: Spring Boot and FastAPI scanner with
`scan`, `push`, `diff` and `check`; self-hosted server with versioned API docs,
history and diffs, architecture and dependency views, impact analysis, QA plans,
accounts with admin/viewer roles and API keys, Postgres with migrations and a job queue.
