# hermesi

Server-side client for [Hermesi](https://github.com/hermesihq): publish events, mint subscriber tokens and preference links.
Thin on purpose: it builds the request, sends it with retries, and turns the answer into a typed result or an exception. It makes
no decision about notifications; that is the platform's job.

- **Retries** on connection failures, timeouts, `429` (honouring `Retry-After`) and `5xx`, with exponential backoff and jitter.
- **Idempotency built in.** Every event goes out with an `Idempotency-Key` (generated if you give none) that is kept across the
  retries, so a lost response cannot send a notification twice.
- **Typed**, with a `py.typed` marker, for synchronous and asynchronous code (`Hermesi`, `AsyncHermesi`).
- **A test mode** that sends nothing and records what you would have sent.
- Python 3.9 and later. One dependency, `httpx`.

## Install

```
pip install hermesi
```

## Publish an event

```python
import os
from hermesi import Hermesi

hermesi = Hermesi(
    api_key=os.environ["HERMESI_SECRET_KEY"],  # hm_sk_..., your SECRET key, server side only
    base_url="https://your-hermesi-host",
)

result = hermesi.events.trigger(
    "order.shipped",  # <noun>.<past-tense-verb>
    "user_8821",  # a subscriber's external_id
    {"order_id": "4821", "tracking_url": url},
    idempotency_key=f"order-{order.id}-shipped",  # see below
)
print(result.event_id, result.status)  # evt_..., accepted
```

`202` means the event is recorded and queued. Nothing has been delivered yet: watch the Activity Log in the dashboard.

`recipient` is a subscriber's `external_id`, a `Subscriber(...)` described inline (created or updated on the fly), or a list of up
to 100 of either:

```python
from hermesi import Subscriber

hermesi.events.trigger("order.shipped", Subscriber("cust_331", email="a@example.cm", locale="fr"), {"order_id": "4821"})
```

The client reads `HERMESI_SECRET_KEY` and `HERMESI_BASE_URL` from the environment if you do not pass them.

### Idempotency: pass your own key when your code can run twice

The SDK generates a key per call and reuses it across its own retries, which covers a lost response. It cannot cover **your**
code running twice for the same thing (a webhook handler that is retried, a queue consumer that redelivers): the second run
generates a new key. For that, give the event a key derived from what happened, such as `order-4821-shipped`. Replaying a key
with the same body within 24 hours returns the original answer, and `result.replayed` is `True`.

### Other fields

`actor=Actor(external_id=..., name=...)`, `delay="15m"`, `send_at=datetime(..., tzinfo=timezone.utc)` (a naive datetime is
refused as ambiguous), `override={...}`, `tenant="..."`. A payload may hold `datetime`, `date`, `Decimal` and `UUID` values; they
are serialised as ISO text and strings.

## Subscriber tokens

A browser or an app talks to Hermesi's client API as one subscriber, with a token minted on **your** server:

```python
token = hermesi.tokens.mint("user_8821", environment_id="env_01...")  # valid for an hour at most
# hand it to your frontend, which sends it with the public key
```

`environment_id` is the `env_...` id shown in the dashboard; the key itself does not carry it. Minting makes no request. The
function is also available on its own: `from hermesi import mint_subscriber_token`.

## Preference links

```python
link = hermesi.subscribers.preference_link("user_8821")
link.url  # a hosted page that needs no login and works for about a year
```

## Errors

```python
from hermesi import HermesiAPIError, HermesiConnectionError, NotFoundError, RateLimitError

try:
    hermesi.events.trigger("order.shipped", "user_8821", {...})
except NotFoundError:
    ...  # that recipient is not a subscriber in this environment
except RateLimitError as e:
    e.retry_after  # seconds the server asked to wait (it was too long to wait out)
except HermesiAPIError as e:
    e.code, e.request_id  # branch on `code`; quote `request_id` to whoever runs Hermesi
except HermesiConnectionError:
    ...  # could not reach Hermesi, after the retries
```

`AuthenticationError` (401), `ForbiddenError` (403), `NotFoundError` (404), `ValidationError` (400, 422; see `.detail`),
`RateLimitError` (429) and `ServerError` (5xx) all derive from `HermesiAPIError`, which derives from `HermesiError`. Branch on the
class or on `code`, never on `message`. A response that is not the API's error envelope has `type == "sdk_error"` and
`code == "unexpected_response"`, which the API never sends.

### Retries

`RetryPolicy(max_retries=3, backoff_base=0.5, backoff_cap=8.0, max_retry_after=30.0)`:

```python
from hermesi import RetryPolicy

Hermesi(..., retry=RetryPolicy(max_retries=0))  # no retrying
```

A `Retry-After` is waited out exactly, up to `max_retry_after` seconds; a server that asks for more gets its error raised, because
a request handler that sleeps for ten minutes is worse than one that fails.

## Async

```python
from hermesi import AsyncHermesi

async with AsyncHermesi(api_key=..., base_url=...) as hermesi:
    await hermesi.events.trigger("order.shipped", "user_8821", {"order_id": "4821"})
```

## Testing your code

```python
hermesi = Hermesi(simulate=True)  # no key, no URL, no network
hermesi.events.trigger("order.shipped", "user_1", {"order_id": "4821"})

assert hermesi.simulated[0].name == "order.shipped"
assert hermesi.simulated[0].payload == {"order_id": "4821"}
```

A simulated call validates and serialises exactly as a real one does, so a payload that would fail in production fails in your
test. It does not know your workflows: it cannot tell you whether an event matches one.

## Not included

There is no method for messages or subscribers beyond the preference link, because Hermesi's secret-key API does not have them
yet: those are the dashboard's (Management) API. Outbound webhooks are not implemented in Hermesi yet either, so there is nothing
to verify.

## Development

```
pip install -e ".[dev]"
pytest
mypy
ruff check .
```

`tests/test_live.py` runs the SDK against a real Hermesi and is skipped unless you point it at one; read its docstring.

## License

MIT
