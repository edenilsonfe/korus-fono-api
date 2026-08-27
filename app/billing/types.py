"""Canonical billing event types."""

from enum import Enum


class InternalBillingEventType(str, Enum):
    SUBSCRIPTION_CREATED = "subscription.created"
    SUBSCRIPTION_UPDATED = "subscription.updated"
    SUBSCRIPTION_CANCELED = "subscription.canceled"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_DELETED = "payment.deleted"
    CHECKOUT_COMPLETED = "checkout.completed"
    UNKNOWN = "unknown"
