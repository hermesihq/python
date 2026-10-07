"""Hermesi's server-side client: publish events and read what became of them, keep your subscribers in sync, send a direct message,
mint subscriber tokens and preference links.

>>> from hermesi import Hermesi  # doctest: +SKIP
>>> hermesi = Hermesi(api_key="hm_sk_...", base_url="https://your-hermesi-host")  # doctest: +SKIP
>>> hermesi.events.trigger("order.shipped", "user_8821", {"order_id": "4821"})  # doctest: +SKIP
"""

from ._calls import HermesiSimulationError
from ._client import VERSION, AsyncHermesi, Hermesi, SimulatedEvent
from ._errors import (
    AuthenticationError,
    ConflictError,
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
from ._server_models import (
    UNSET,
    ChannelIdentity,
    EventRun,
    Message,
    MessageCreated,
    MessageResult,
    Preferences,
    RunNotification,
    SimulatedCall,
    SubscriberProfile,
)
from ._tokens import mint_subscriber_token

__version__ = VERSION

__all__ = [
    "UNSET",
    "Actor",
    "AsyncHermesi",
    "AuthenticationError",
    "ChannelIdentity",
    "ConflictError",
    "ErrorDetail",
    "EventResult",
    "EventRun",
    "ForbiddenError",
    "Hermesi",
    "HermesiAPIError",
    "HermesiConnectionError",
    "HermesiError",
    "HermesiSimulationError",
    "Message",
    "MessageCreated",
    "MessageResult",
    "NotFoundError",
    "NotificationSummary",
    "PreferenceLink",
    "Preferences",
    "RateLimitError",
    "Recipient",
    "RetryPolicy",
    "RunNotification",
    "ServerError",
    "SimulatedCall",
    "SimulatedEvent",
    "Subscriber",
    "SubscriberProfile",
    "ValidationError",
    "__version__",
    "mint_subscriber_token",
]
