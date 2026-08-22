"""Normalize provider webhooks into internal billing events."""

from abc import ABC, abstractmethod
from typing import Any

from app.billing.types import InternalBillingEventType


class NormalizedBillingEvent:
    __slots__ = ("event_type", "external_event_id", "payload", "professional_hint")

    def __init__(
        self,
        *,
        event_type: InternalBillingEventType,
        external_event_id: str,
        payload: dict[str, Any],
        professional_hint: str | None = None,
    ):
        self.event_type = event_type
        self.external_event_id = external_event_id
        self.payload = payload
        self.professional_hint = professional_hint


def _parse_event_type(raw: Any) -> InternalBillingEventType:
    if raw is None:
        return InternalBillingEventType.UNKNOWN
    text = str(raw).strip().lower()
    for member in InternalBillingEventType:
        if member.value == text or member.name.lower() == text:
            return member
    return InternalBillingEventType.UNKNOWN


def _parse_external_reference(ref: Any) -> tuple[str | None, str | None]:
    if not ref:
        return None, None
    text = str(ref)
    if ":" not in text:
        return text, None
    professional_id, plan_slug = text.split(":", 1)
    return professional_id or None, plan_slug or None


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = source.get(key)
        if value:
            return value
    return None


class WebhookNormalizer(ABC):
    provider_key: str

    @abstractmethod
    def normalize(self, raw_body: dict[str, Any], headers: dict[str, str]) -> list[NormalizedBillingEvent]:
        raise NotImplementedError


class StubWebhookNormalizer(WebhookNormalizer):
    provider_key = "stub"

    def normalize(self, raw_body: dict[str, Any], headers: dict[str, str]) -> list[NormalizedBillingEvent]:
        eid = str(raw_body.get("id") or raw_body.get("event_id") or "stub-unknown")
        event_type = _parse_event_type(raw_body.get("event_type") or raw_body.get("type"))
        hint = raw_body.get("professional_id") or raw_body.get("account_id")
        return [
            NormalizedBillingEvent(
                event_type=event_type,
                external_event_id=eid,
                payload=raw_body,
                professional_hint=str(hint) if hint else None,
            )
        ]


class AsaasWebhookNormalizer(WebhookNormalizer):
    provider_key = "asaas"

    _PAYMENT_SUCCESS = frozenset({"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"})
    _PAYMENT_FAILURE = frozenset(
        {
            "PAYMENT_OVERDUE",
            "PAYMENT_DELETED",
            "PAYMENT_REFUNDED",
            "PAYMENT_CHARGEBACK_REQUESTED",
        }
    )
    _SUBSCRIPTION_CANCEL = frozenset({"SUBSCRIPTION_DELETED", "SUBSCRIPTION_INACTIVATED"})
    _ASAAS_SUB_STATUS_MAP = {
        "ACTIVE": "active",
        "INACTIVE": "canceled",
        "EXPIRED": "canceled",
    }

    def _subscription_payload(
        self, subscription: dict[str, Any], *, event_name: str
    ) -> tuple[str | None, dict[str, Any]]:
        ext_ref = subscription.get("externalReference")
        professional_id, plan_slug = _parse_external_reference(ext_ref)
        status_raw = str(subscription.get("status") or "").upper()
        payload: dict[str, Any] = {
            **subscription,
            "provider": "asaas",
            "external_reference": ext_ref,
            "plan_slug": plan_slug,
            "external_subscription_id": subscription.get("id"),
            "subscription_status": self._ASAAS_SUB_STATUS_MAP.get(status_raw),
        }
        current_period_end = _first_present(
            subscription,
            ("nextDueDate", "next_due_date", "currentPeriodEnd", "current_period_end"),
        )
        if current_period_end:
            payload["current_period_end"] = current_period_end
        if professional_id:
            payload["professional_id"] = professional_id
        return professional_id, payload

    def normalize(self, raw_body: dict[str, Any], headers: dict[str, str]) -> list[NormalizedBillingEvent]:
        event_name = str(raw_body.get("event") or "")

        checkout = raw_body.get("checkout") or {}
        if event_name == "CHECKOUT_PAID" and isinstance(checkout, dict) and checkout:
            checkout_id = checkout.get("id") or raw_body.get("id")
            ext_ref = checkout.get("externalReference")
            professional_id, plan_slug = _parse_external_reference(ext_ref)
            payload = {
                **checkout,
                "provider": "asaas",
                "external_reference": ext_ref,
                "plan_slug": plan_slug,
                "external_checkout_id": checkout_id,
                "checkout_session_id": checkout_id,
                "last_payment_at": raw_body.get("dateCreated")
                or checkout.get("dateCreated"),
                "subscription_status": "active",
            }
            if professional_id:
                payload["professional_id"] = professional_id
            return [
                NormalizedBillingEvent(
                    event_type=InternalBillingEventType.PAYMENT_SUCCEEDED,
                    external_event_id=f"asaas-{event_name}-{checkout_id or 'unknown'}",
                    payload=payload,
                    professional_hint=professional_id,
                )
            ]

        subscription = raw_body.get("subscription") or {}
        if isinstance(subscription, dict) and subscription and event_name.startswith("SUBSCRIPTION"):
            sub_id = subscription.get("id") or "unknown"
            external_event_id = f"asaas-{event_name}-{sub_id}"
            professional_id, payload = self._subscription_payload(subscription, event_name=event_name)

            if event_name in self._SUBSCRIPTION_CANCEL:
                event_type = InternalBillingEventType.SUBSCRIPTION_CANCELED
                payload["subscription_status"] = "canceled"
            elif event_name == "SUBSCRIPTION_UPDATED":
                event_type = InternalBillingEventType.SUBSCRIPTION_UPDATED
                if not payload.get("subscription_status"):
                    return []
            else:
                return []

            return [
                NormalizedBillingEvent(
                    event_type=event_type,
                    external_event_id=external_event_id,
                    payload=payload,
                    professional_hint=professional_id,
                )
            ]

        payment = raw_body.get("payment") or {}
        if not isinstance(payment, dict):
            payment = {}

        payment_id = payment.get("id") or raw_body.get("id")
        external_event_id = f"asaas-{event_name}-{payment_id or 'unknown'}"

        if event_name in self._PAYMENT_SUCCESS:
            event_type = InternalBillingEventType.PAYMENT_SUCCEEDED
        elif event_name in self._PAYMENT_FAILURE:
            event_type = InternalBillingEventType.PAYMENT_FAILED
        else:
            return []

        ext_ref = payment.get("externalReference")
        professional_id, plan_slug = _parse_external_reference(ext_ref)
        checkout_session_id = payment.get("checkoutSession")
        payload = {
            **payment,
            "provider": "asaas",
            "external_reference": ext_ref,
            "plan_slug": plan_slug,
            "external_checkout_id": checkout_session_id or payment_id,
            "checkout_session_id": checkout_session_id,
            "external_subscription_id": payment.get("subscription"),
            "last_payment_at": payment.get("paymentDate") or payment.get("clientPaymentDate"),
            "subscription_status": "active" if event_type == InternalBillingEventType.PAYMENT_SUCCEEDED else "past_due",
        }
        if professional_id:
            payload["professional_id"] = professional_id

        return [
            NormalizedBillingEvent(
                event_type=event_type,
                external_event_id=external_event_id,
                payload=payload,
                professional_hint=professional_id,
            )
        ]


def get_normalizer(provider: str) -> WebhookNormalizer:
    key = (provider or "stub").lower().strip()
    if key == "asaas":
        return AsaasWebhookNormalizer()
    return StubWebhookNormalizer()
