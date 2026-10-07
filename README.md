# hermesi

Server-side client for [Hermesi](https://github.com/hermesihq): publish events and read what became of them, keep your
subscribers in sync, send a direct message, mint subscriber tokens and preference links.
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

`actor=Actor(external_id=..., name=...)`, `delay="PT15M"`, `send_at=datetime(..., tzinfo=timezone.utc)`, `override={...}`,
`tenant="..."`. A payload may hold `datetime`, `date`, `Decimal` and `UUID` values; they are serialised as ISO text and strings.

### Scheduling

`delay` holds the event back for an ISO 8601 duration (`PT15M`, `PT1H30M`, `P1D`: **not** `15m`) and `send_at` until an instant
(a `datetime` with a timezone: a naive one is refused as ambiguous). Give one, not both, at most 30 days ahead. A time already past runs at once. The run starts within about a minute after
its time, not at the second. A request the server cannot honour is refused with `422 invalid_schedule`: it is never sent
immediately instead. Pass your own idempotency key and retrying a scheduled event does not schedule it twice.

`override` and `tenant` are accepted by the API but not acted on yet.

## Read back what became of an event

```python
run = hermesi.events.get(event.event_id)

run.status  # "processed", "no_workflow" (nothing matched) or "invalid" (a strict payload schema refused it)
for notification in run.notifications:  # one per recipient
    notification.external_id, notification.workflow, notification.status
    for message in notification.messages:
        message.channel, message.status, message.provider, message.failure_code
        message.is_final  # True once nothing more will happen to it
```

A message's status moves on after the event was accepted (`queued`, `sent`, `delivered`, ...), so poll it rather than treating the
first answer as final. It never returns what was sent or the recipient's address, and an event of another environment is a
`NotFoundError`.

## Keep your subscribers in sync

```python
hermesi.subscribers.put("user_8821", email="amina@example.cm", phone_e164="+237690000000", first_name="Amina", locale="fr",
                        timezone="Africa/Douala", data={"plan": "pro"})
hermesi.subscribers.put("user_8821", locale="en")           # only the locale changes: the rest is left alone
hermesi.subscribers.put("user_8821", phone_e164=None)       # None clears one field
profile = hermesi.subscribers.get("user_8821")              # profile, channel identities, stored preference overrides
hermesi.subscribers.patch("user_8821", locale="fr")         # like put, but NotFoundError if the subscriber does not exist
hermesi.subscribers.delete("user_8821")                     # erase the personal data; idempotent
```

**An argument you give is set, `None` clears the field, and one you leave out is left alone**, so a sync job that knows half a
profile does not blank the other half. (`None` and "not given" are different things here, which is what `hermesi.UNSET` is.) `data`
replaces the stored attributes, up to 32 KB; it is not merged. The server checks the shapes (an email looks like one, `phone_e164`
is E.164, `locale` a language tag, `timezone` an IANA name) and refuses what it does not know, as a `ValidationError` naming the field.

`delete` removes the email, phone, names, attributes, channel identities and preferences, and replaces the address on every message
the person received by `[deleted]`, keeping the messages and their status for your statistics. Inbox items, the stored text of
messages and event payloads are **not** erased yet.

```python
hermesi.subscribers.register_channel("user_8821", "push", device_token, {"platform": "android"})  # on every app start
hermesi.subscribers.remove_channel("user_8821", "push", device_token)

hermesi.subscribers.update_preferences("user_8821", global_={"sms": False}, categories={"marketing": {"email": False, "push": None}})
hermesi.subscribers.preferences("user_8821").categories  # {"marketing": {"email": False}}
```

`register_channel` refreshes the identity and makes it active again if a provider had marked it invalid; it never duplicates it.
In `update_preferences`, `True` or `False` sets an override and `None` removes it, so the category's default applies again. It is all
or nothing: an unknown category (`NotFoundError`) or a critical one (`ValidationError`) refuses the whole update.

## Send one message on a channel you choose

Almost everything should be an event: you say what happened and Hermesi decides the channels. When the channel is a requirement
instead (an OTP that must be an SMS), send one message through one template:

```python
result = hermesi.messages.send(
    "sms", "user_8821", "otp-code",
    data={"code": "480219"}, category="security", priority="critical",
    idempotency_key="otp-user_8821-482",
)
result.status      # "queued", or "skipped" / "suppressed" if the recipient's preferences or a suppression refused it
result.messages    # one per destination: a push to three devices is three messages
hermesi.messages.get(result.message_id).is_final
```

It skips the workflow and nothing else: preferences, suppressions and the audit trail still apply, and a refused message is a
result you can read, not an exception. A mistake (an unknown template, a template with no variant for the channel, an unknown
recipient) is raised and creates nothing. The wording lives in a published template; `data` becomes its `payload.*` and is not kept
once a provider has the message. There is no inline `content`: it would put copy back in your code.

**Pass your own `idempotency_key` when your code can run twice.** One is generated and kept across the retries if you give none, so a
timeout cannot send a second SMS, but only your own key survives your code running again.

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

`AuthenticationError` (401), `ForbiddenError` (403), `NotFoundError` (404), `ConflictError` (409: an idempotency key already used
with another body), `ValidationError` (400, 422; see `.detail`),
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

Writes that are not events (`subscribers.put`, `messages.send`, `register_channel`, ...) are recorded in `hermesi.simulated_calls`
(method, path, body, idempotency key) and answered with a plausible result (`status == "simulated"` for a message). **Reads
(`events.get`, `subscribers.get`, `messages.get`, ...) raise `HermesiSimulationError`**: nothing was sent, so there is nothing to
read, and an invented answer would make a test pass for the wrong reason.

## Not included

Bulk subscriber import (a later phase of Hermesi), the dashboard's Management API (workflows, templates, providers) and inline
`content` for a direct message (Hermesi refuses it on purpose). Outbound webhooks are not implemented in Hermesi yet either, so
there is nothing to verify.

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
