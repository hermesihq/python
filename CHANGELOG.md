# Changelog

All notable changes to `hermesi`. This file describes what a consumer gets.

**`0.x` means the public API can still change.** A minor bump may contain a breaking change; a patch bump will not. Each release
lists breaking changes first.

## 0.1.0 (2026-10-04)

First release.

- `Hermesi` and `AsyncHermesi`: `events.trigger`, `subscribers.preference_link`, `tokens.mint`.
- Retries on connection failures, timeouts, `429` (honouring `Retry-After`) and `5xx`, with exponential backoff and jitter; an
  idempotency key on every event, generated if none is given and kept across the retries.
- Typed exceptions: `AuthenticationError`, `ForbiddenError`, `NotFoundError`, `ValidationError`, `RateLimitError`,
  `ServerError`, `HermesiConnectionError`.
- `mint_subscriber_token`, and `simulate=True` to test code that publishes events without a network.
- A subscriber id of `.` or `..` is refused: a URL parser resolves it even when escaped, which would aim a request carrying your
  secret key at another endpoint.
