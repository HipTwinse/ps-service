"""Data models and validation definitions for the Optimal Knapsack Engine.

All monetary values are strictly represented as integers in minor units:
- TRY: kuruş (1 TRY = 100 kuruş)
- USD: cents (1 USD = 100 cents)
- RUB: kopecks (1 RUB = 100 kopecks)

Floating-point arithmetic is strictly forbidden for financial amounts.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Union


class Region(str, Enum):
    """Supported PlayStation Store regions."""
    TR = "TR"
    US = "US"


class Currency(str, Enum):
    """Supported store currencies."""
    TRY = "TRY"
    US = "USD"
    USD = "USD"


# Canonical mapping between regions and their valid currency
REGION_CURRENCY_MAP = {
    Region.TR: Currency.TRY,
    Region.US: Currency.USD,
}

# Realistic PlayStation Store limits to prevent Algorithmic DoS and protect resources
MAX_TARGET_AMOUNT_MINOR = 5_000_000    # 50,000 TRY or 50,000 USD (PSN wallet max is ~5,000 TRY)
MAX_FACE_VALUE_MINOR = 5_000_000       # Up to 50,000 TRY / USD
MAX_COST_RUB_KOPECKS = 50_000_000      # 500,000 RUB
MAX_STOCK_LIMIT = 1_000                # Up to 1,000 vouchers per denomination
MAX_DENOMINATIONS_COUNT = 20           # Up to 20 distinct denominations in catalog
DEFAULT_MAX_VOUCHERS_PER_ORDER = 6     # Maximum vouchers per order to prevent toxic UX (PSN flood control)
MAX_PERMITTED_VOUCHERS_PER_ORDER = 20  # Hard ceiling for order voucher count


class ValidationError(ValueError):
    """Raised when input parameters violate the input contract."""
    pass


@dataclass(frozen=True)
class Denomination:
    """Represents a single voucher denomination available in the catalog.

    Attributes:
        face_value_minor: Face value of one voucher in minor currency units (> 0).
        cost_rub_kopecks: Procurement cost of one voucher in Russian kopecks (>= 0).
        stock_limit: Maximum available quantity of this denomination (>= 0).
        enabled: Whether this denomination can be selected during optimization.
    """
    face_value_minor: int
    cost_rub_kopecks: int
    stock_limit: int
    enabled: bool = True

    def __post_init__(self) -> None:
        # Validate face_value_minor
        if isinstance(self.face_value_minor, bool) or not isinstance(self.face_value_minor, int):
            raise ValidationError(f"face_value_minor must be an integer, got {type(self.face_value_minor).__name__}")
        if self.face_value_minor <= 0:
            raise ValidationError(f"face_value_minor must be strictly positive (> 0), got {self.face_value_minor}")
        if self.face_value_minor > MAX_FACE_VALUE_MINOR:
            raise ValidationError(f"face_value_minor exceeds maximum permitted limit ({MAX_FACE_VALUE_MINOR})")

        # Validate cost_rub_kopecks
        if isinstance(self.cost_rub_kopecks, bool) or not isinstance(self.cost_rub_kopecks, int):
            raise ValidationError(f"cost_rub_kopecks must be an integer, got {type(self.cost_rub_kopecks).__name__}")
        if self.cost_rub_kopecks < 0:
            raise ValidationError(f"cost_rub_kopecks cannot be negative, got {self.cost_rub_kopecks}")
        if self.cost_rub_kopecks > MAX_COST_RUB_KOPECKS:
            raise ValidationError(f"cost_rub_kopecks exceeds maximum permitted limit ({MAX_COST_RUB_KOPECKS})")

        # Validate stock_limit
        if isinstance(self.stock_limit, bool) or not isinstance(self.stock_limit, int):
            raise ValidationError(f"stock_limit must be an integer, got {type(self.stock_limit).__name__}")
        if self.stock_limit < 0:
            raise ValidationError(f"stock_limit cannot be negative, got {self.stock_limit}")
        if self.stock_limit > MAX_STOCK_LIMIT:
            raise ValidationError(f"stock_limit exceeds maximum permitted limit ({MAX_STOCK_LIMIT})")

        # Validate enabled
        if not isinstance(self.enabled, bool):
            raise ValidationError(f"enabled must be a boolean, got {type(self.enabled).__name__}")


@dataclass(frozen=True)
class SelectedDenomination:
    """Represents a denomination and quantity chosen in the optimal solution.

    Attributes:
        face_value_minor: Face value of one voucher in minor currency units.
        quantity: Number of vouchers selected of this denomination (> 0).
        cost_rub_kopecks: Procurement cost of one voucher in Russian kopecks.
        total_cost_rub_kopecks: Total procurement cost for this denomination (quantity * cost).
    """
    face_value_minor: int
    quantity: int
    cost_rub_kopecks: int
    total_cost_rub_kopecks: int


@dataclass(frozen=True)
class OptimizationResult:
    """Structured result returned by the Optimal Knapsack Engine.

    Can represent either a successful calculation (status='success') or
    a failure where no valid combination could be found (status='no_solution').
    """
    status: str
    region: Optional[str] = None
    currency: Optional[str] = None
    target_amount_minor: Optional[int] = None
    selected_denominations: List[SelectedDenomination] = field(default_factory=list)
    total_face_value_minor: int = 0
    wallet_change_minor: int = 0
    total_procurement_cost_rub_kopecks: int = 0
    voucher_count: int = 0
    reason: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.status == "success"


def validate_input_contract(
    region: Union[Region, str],
    currency: Union[Currency, str],
    target_amount_minor: int,
    denominations: Sequence[Denomination],
    max_vouchers_per_order: int = DEFAULT_MAX_VOUCHERS_PER_ORDER,
    max_allowed_overshoot_minor: Optional[int] = None,
) -> tuple[Region, Currency, int, list[Denomination], int, Optional[int]]:
    """Strictly validates all input fields according to the specification.

    Raises:
        ValidationError: If any parameter violates contract rules or types.

    Returns:
        Validated tuple of (Region, Currency, target_amount_minor, list of Denominations, max_vouchers, max_overshoot).
    """
    # 1. Validate Region
    if isinstance(region, str):
        try:
            reg_enum = Region(region.upper())
        except ValueError:
            raise ValidationError(f"Invalid region '{region}'. Must be one of {[r.value for r in Region]}")
    elif isinstance(region, Region):
        reg_enum = region
    else:
        raise ValidationError(f"region must be a string or Region enum, got {type(region).__name__}")

    # 2. Validate Currency
    if isinstance(currency, str):
        try:
            curr_enum = Currency(currency.upper())
        except ValueError:
            raise ValidationError(f"Invalid currency '{currency}'. Must be one of {[c.value for c in Currency]}")
    elif isinstance(currency, Currency):
        curr_enum = currency
    else:
        raise ValidationError(f"currency must be a string or Currency enum, got {type(currency).__name__}")

    # 3. Consistency between Region and Currency
    expected_curr = REGION_CURRENCY_MAP[reg_enum]
    if curr_enum != expected_curr:
        raise ValidationError(
            f"Region/currency mismatch: Region '{reg_enum.value}' requires currency '{expected_curr.value}', "
            f"but got '{curr_enum.value}'"
        )

    # 4. Validate target_amount_minor
    if isinstance(target_amount_minor, bool) or not isinstance(target_amount_minor, int):
        raise ValidationError(
            f"target_amount_minor must be an integer, got {type(target_amount_minor).__name__}"
        )
    if target_amount_minor <= 0:
        raise ValidationError(
            f"target_amount_minor must be strictly positive (> 0), got {target_amount_minor}"
        )
    if target_amount_minor > MAX_TARGET_AMOUNT_MINOR:
        raise ValidationError(
            f"target_amount_minor exceeds maximum permitted limit ({MAX_TARGET_AMOUNT_MINOR})"
        )

    # 5. Validate denominations collection
    if not isinstance(denominations, (list, tuple)):
        raise ValidationError(
            f"denominations must be a list or tuple, got {type(denominations).__name__}"
        )
    if len(denominations) > MAX_DENOMINATIONS_COUNT:
        raise ValidationError(
            f"denominations list size ({len(denominations)}) exceeds maximum permitted ({MAX_DENOMINATIONS_COUNT})"
        )

    validated_denoms: list[Denomination] = []
    for idx, d in enumerate(denominations):
        if not isinstance(d, Denomination):
            raise ValidationError(
                f"Denomination at index {idx} must be an instance of Denomination dataclass, got {type(d).__name__}"
            )
        validated_denoms.append(d)

    # 6. Validate max_vouchers_per_order
    if isinstance(max_vouchers_per_order, bool) or not isinstance(max_vouchers_per_order, int):
        raise ValidationError(
            f"max_vouchers_per_order must be an integer, got {type(max_vouchers_per_order).__name__}"
        )
    if max_vouchers_per_order <= 0:
        raise ValidationError(
            f"max_vouchers_per_order must be strictly positive (> 0), got {max_vouchers_per_order}"
        )
    if max_vouchers_per_order > MAX_PERMITTED_VOUCHERS_PER_ORDER:
        raise ValidationError(
            f"max_vouchers_per_order cannot exceed {MAX_PERMITTED_VOUCHERS_PER_ORDER}, got {max_vouchers_per_order}"
        )

    # 7. Validate max_allowed_overshoot_minor
    if max_allowed_overshoot_minor is not None:
        if isinstance(max_allowed_overshoot_minor, bool) or not isinstance(max_allowed_overshoot_minor, int):
            raise ValidationError(
                f"max_allowed_overshoot_minor must be an integer or None, got {type(max_allowed_overshoot_minor).__name__}"
            )
        if max_allowed_overshoot_minor < 0:
            raise ValidationError(
                f"max_allowed_overshoot_minor cannot be negative, got {max_allowed_overshoot_minor}"
            )

    return reg_enum, curr_enum, target_amount_minor, validated_denoms, max_vouchers_per_order, max_allowed_overshoot_minor

