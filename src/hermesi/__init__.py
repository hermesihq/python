"""Hermesi's server-side client: publish events, mint subscriber tokens and preference links.

>>> from hermesi import Hermesi  # doctest: +SKIP
>>> hermesi = Hermesi(api_key="hm_sk_...", base_url="https://your-hermesi-host")  # doctest: +SKIP
>>> hermesi.events.trigger("order.shipped", "user_8821", {"order_id": "4821"})  # doctest: +SKIP
"""

from ._client import VERSION, AsyncHermesi, Hermesi, SimulatedEvent
from ._errors import (
    AuthenticationError,
    ErrorDetail,
    ForbiddenError,
    HermesiAPIError,
    HermesiConnectionError,
    HermesiError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from ._models import Actor, EventResult, NotificationSummary, PreferenceLink, Recipient, Subscriber
from ._retry import RetryPolicy
from ._tokens import mint_subscriber_token

__version__ = VERSION

__all__ = [
    "Actor",
    "AsyncHermesi",
    "AuthenticationError",
    "ErrorDetail",
    "EventResult",
    "ForbiddenError",
    "Hermesi",
    "HermesiAPIError",
    "HermesiConnectionError",
    "HermesiError",
    "NotFoundError",
    "NotificationSummary",
    "PreferenceLink",
    "RateLimitError",
    "Recipient",
    "RetryPolicy",
    "ServerError",
    "SimulatedEvent",
    "Subscriber",
    "ValidationError",
    "__version__",
    "mint_subscriber_token",
]
