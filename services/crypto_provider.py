"""
ChannelFlow AI - Crypto Payment Provider Abstraction
======================================================

Real crypto payments through a provider API - never fake links, never
client-side confirmation. The bot/frontend only ever sees the
provider-generated checkout URL; credentials stay server-side.

Provider interface (Prompt 3 section 24):

    create_payment()      -> PaymentCreation (id, url, expires_at)
    get_payment_status()  -> status string
    verify_payment()      -> normalized payment state
    cancel_payment()

Implemented providers:
    * OXAPAYMerchant - OXAPAY merchant invoice API
      Docs: https://docs.oxapay.com (merchant/request + merchants/inquiry)

Adding a new provider = implement CryptoPaymentProvider, register it in
get_provider(). Nothing else in the app touches provider APIs directly.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from config import OXAPAY_API_KEY

logger = logging.getLogger(__name__)

# Statuses normalized across providers
STATUS_WAITING = "WAITING"
STATUS_DETECTING = "DETECTING"
STATUS_CONFIRMING = "CONFIRMING"
STATUS_PAID = "PAID"
STATUS_FAILED = "FAILED"
STATUS_EXPIRED = "EXPIRED"


@dataclass
class PaymentCreation:
    """What a successful create_payment() returns - everything the rest
    of the app needs, without leaking provider-specific details."""

    provider_payment_id: str
    payment_url: str
    currency: str
    amount: float
    expires_at: datetime
    raw: dict = field(default_factory=dict)


class CryptoPaymentError(RuntimeError):
    """Raised with a USER-SAFE message. Technical details go to logs,
    never to the chat."""


class CryptoPaymentProvider:
    """Interface every crypto provider must implement."""

    name = "abstract"

    async def create_payment(self, amount_usd: float, order_id: str, description: str = "", lifetime_minutes: int = 15) -> PaymentCreation:
        raise NotImplementedError

    async def get_payment_status(self, provider_payment_id: str) -> str:
        raise NotImplementedError

    async def cancel_payment(self, provider_payment_id: str) -> bool:
        return True  # not all providers support cancellation


class OXAPAYMerchant(CryptoPaymentProvider):
    """OXAPAY merchant invoice flow.

    create: POST https://api.oxapay.com/merchants/request
            {merchant, amount, currency, lifeTime, orderId, description}
            -> {result:100, payLink, trackId}

    inquiry: POST https://api.oxapay.com/merchants/inquiry
             {merchant, trackId}
             -> {result:100, status: paid|waiting|expired|failed|cancel...}
    """

    name = "oxapay"

    CREATE_URL = "https://api.oxapay.com/merchants/request"
    INQUIRY_URL = "https://api.oxapay.com/merchants/inquiry"

    STATUS_MAP = {
        "paid": STATUS_PAID,
        "waiting": STATUS_WAITING,
        "checking": STATUS_CONFIRMING,
        "confirming": STATUS_CONFIRMING,
        "confirmed": STATUS_PAID,
        "expired": STATUS_EXPIRED,
        "canceled": STATUS_FAILED,
        "cancelled": STATUS_FAILED,
        "failed": STATUS_FAILED,
    }

    def _require_key(self):
        if not OXAPAY_API_KEY:
            raise CryptoPaymentError(
                "Crypto payments aren't configured right now. Please use UPI or contact support."
            )

    async def create_payment(self, amount_usd: float, order_id: str, description: str = "", lifetime_minutes: int = 15) -> PaymentCreation:

        self._require_key()

        payload = {
            "merchant": OXAPAY_API_KEY,
            "amount": round(float(amount_usd), 2),
            "currency": "USD",
            "lifeTime": lifetime_minutes,
            "orderId": order_id,
            "description": description or "ChannelFlow subscription",
        }

        try:
            async with httpx.AsyncClient(timeout=25) as http:
                resp = await http.post(self.CREATE_URL, json=payload)
        except httpx.HTTPError as e:
            logger.error("OXAPAY create network error: %s", e)
            raise CryptoPaymentError(
                "Couldn't reach the crypto payment provider. Please try again in a minute."
            )

        try:
            data = resp.json()
        except ValueError:
            logger.error("OXAPAY create returned non-JSON: %s", resp.text[:300])
            raise CryptoPaymentError("Payment provider returned an invalid response. Try again shortly.")

        if data.get("result") != 100:
            # 135 = merchant not found / bad key etc.
            logger.error("OXAPAY create failed (%s): %s", data.get("result"), data.get("message"))
            raise CryptoPaymentError("Payment provider rejected the request. Please try again shortly.")

        pay_link = data.get("payLink")
        track_id = data.get("trackId")

        if not pay_link or track_id is None:
            logger.error("OXAPAY create missing fields: %s", list(data.keys()))
            raise CryptoPaymentError("Payment provider returned incomplete data. Try again shortly.")

        return PaymentCreation(
            provider_payment_id=str(track_id),
            payment_url=pay_link,
            currency="USD",
            amount=round(float(amount_usd), 2),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes),
            raw=data,
        )

    async def get_payment_status(self, provider_payment_id: str) -> str:

        self._require_key()

        try:
            async with httpx.AsyncClient(timeout=20) as http:
                resp = await http.post(
                    self.INQUIRY_URL,
                    json={"merchant": OXAPAY_API_KEY, "trackId": provider_payment_id},
                )
        except httpx.HTTPError as e:
            logger.error("OXAPAY inquiry network error: %s", e)
            raise CryptoPaymentError("Couldn't check payment status. Try again in a moment.")

        try:
            data = resp.json()
        except ValueError:
            logger.error("OXAPAY inquiry returned non-JSON")
            raise CryptoPaymentError("Couldn't read payment status. Try again shortly.")

        if data.get("result") != 100:
            # result 124 typically means unknown/removed trackId -> treat as expired
            logger.info("OXAPAY inquiry non-100 (%s) for %s", data.get("result"), provider_payment_id)
            return STATUS_EXPIRED if data.get("result") == 124 else STATUS_WAITING

        raw_status = (data.get("status") or "").lower()
        return self.STATUS_MAP.get(raw_status, STATUS_WAITING)


_PROVIDER = None


def get_provider() -> CryptoPaymentProvider:
    """Returns the configured provider singleton. Currently only OXAPAY
    is wired; add more by extending this factory."""

    global _PROVIDER

    if _PROVIDER is None:
        _PROVIDER = OXAPAYMerchant()

    return _PROVIDER