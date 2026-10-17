"""Top-level calculator module facade as specified in the project context."""

from src.engine import (
    DEFAULT_MAX_VOUCHERS_PER_ORDER,
    MAX_PERMITTED_VOUCHERS_PER_ORDER,
    Currency,
    Denomination,
    OptimizationResult,
    Region,
    SelectedDenomination,
    ValidationError,
    calculate_optimal_vouchers,
    validate_input_contract,
)

__all__ = [
    "calculate_optimal_vouchers",
    "Denomination",
    "SelectedDenomination",
    "OptimizationResult",
    "Region",
    "Currency",
    "ValidationError",
    "validate_input_contract",
    "DEFAULT_MAX_VOUCHERS_PER_ORDER",
    "MAX_PERMITTED_VOUCHERS_PER_ORDER",
]

