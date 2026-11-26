"""Centralized operational defaults for Stage 3.1+ persistence and delivery primitives.

All timeouts, limits, and security thresholds live here so they are auditable,
immutable at runtime, and cleanly overridable in unit tests (OD-08, OD-09).
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationalDefaults:
    """Immutable operational configuration defaults (OD-08, OD-09, AR-P3-001, AR-P3-006).

    Note on OD-09:
        `voucher_denominations.cost_rub_kopecks` represents the customer retail
        selling price in RUB kopecks (preserved without altering Phase 1 contracts).
    """

    # OD-08 checkout / order lifecycle defaults
    max_open_pending_payment_orders_per_user: int = 3
    pending_payment_ttl_seconds: int = 900

    # OD-08 & AR-P3-001 delivery attempt lease defaults
    delivery_lease_seconds: int = 60
    delivery_min_send_budget_seconds: float = 20.0
    delivery_http_timeout_seconds: float = 15.0
    delivery_safety_margin_seconds: float = 5.0
    redelivery_cooldown_seconds: int = 60

    # AR-P3-006 idempotency retention & stale lock recovery defaults
    idempotency_ttl_seconds: int = 86_400
    idempotency_in_progress_stale_seconds: int = 120

    # SG-P2-012 cryptographic key/pepper defaults
    min_fingerprint_pepper_bytes: int = 16

    # Receipt payload limit (aligned with 001_initial_schema.sql CHECK <= 10485760)
    max_receipt_size_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        int_fields = (
            "max_open_pending_payment_orders_per_user",
            "pending_payment_ttl_seconds",
            "delivery_lease_seconds",
            "redelivery_cooldown_seconds",
            "idempotency_ttl_seconds",
            "idempotency_in_progress_stale_seconds",
            "min_fingerprint_pepper_bytes",
            "max_receipt_size_bytes",
        )
        for field_name in int_fields:
            val = getattr(self, field_name)
            if type(val) is not int or val <= 0:
                raise ValueError(f"{field_name} must be a positive integer (got {val!r}).")

        float_fields = (
            "delivery_min_send_budget_seconds",
            "delivery_http_timeout_seconds",
            "delivery_safety_margin_seconds",
        )
        float_values: dict[str, float] = {}
        for field_name in float_fields:
            val = getattr(self, field_name)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"{field_name} must be a finite positive number (got {val!r}).")
            try:
                fval = float(val)
            except OverflowError as exc:
                raise ValueError(f"{field_name} must be a finite positive number (value overflows float).") from exc
            if not math.isfinite(fval) or fval <= 0:
                raise ValueError(f"{field_name} must be a finite positive number (got {val!r}).")
            float_values[field_name] = fval

        min_send_budget = float_values["delivery_min_send_budget_seconds"]
        http_timeout = float_values["delivery_http_timeout_seconds"]
        safety_margin = float_values["delivery_safety_margin_seconds"]

        if min_send_budget < (http_timeout + safety_margin):
            raise ValueError(
                "delivery_min_send_budget_seconds must be >= delivery_http_timeout_seconds + delivery_safety_margin_seconds."
            )
        if min_send_budget >= self.delivery_lease_seconds:
            raise ValueError(
                "delivery_min_send_budget_seconds must be strictly less than delivery_lease_seconds."
            )
        if (http_timeout + safety_margin) >= self.delivery_lease_seconds:
            raise ValueError(
                "delivery_http_timeout_seconds + delivery_safety_margin_seconds must be strictly less than delivery_lease_seconds."
            )
        if self.idempotency_in_progress_stale_seconds > self.idempotency_ttl_seconds:
            raise ValueError(
                "idempotency_in_progress_stale_seconds cannot exceed idempotency_ttl_seconds."
            )


DEFAULT_OPERATIONAL_CONFIG = OperationalDefaults()
