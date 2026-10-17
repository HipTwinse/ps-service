"""Optimal Knapsack Engine implementation for PlayStation Store digital vouchers.

Solves the Bounded Covering Knapsack / Change-Making problem with finite stock
and multi-tier deterministic tie-breakers:
1. Minimize total procurement cost in RUB kopecks.
2. Minimize leftover wallet balance (wallet change in minor currency units).
3. Minimize total count of vouchers.
4. Deterministic canonical tie-breaker (prefer larger denominations).

Strict constraint: All monetary values are pure integers.
No floating-point operations are used in financial calculations or bounds.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from src.engine.models import (
    DEFAULT_MAX_VOUCHERS_PER_ORDER,
    Currency,
    Denomination,
    OptimizationResult,
    Region,
    SelectedDenomination,
    ValidationError,
    validate_input_contract,
)



@dataclass(frozen=True)
class _ActiveDenom:
    """Internal representation of an active, in-stock denomination."""
    face_value_minor: int
    cost_rub_kopecks: int
    stock_limit: int
    original_index: int


def calculate_optimal_vouchers(
    region: Union[Region, str],
    currency: Union[Currency, str],
    target_amount_minor: int,
    denominations: Sequence[Denomination],
    max_vouchers_per_order: int = DEFAULT_MAX_VOUCHERS_PER_ORDER,
    max_allowed_overshoot_minor: Optional[int] = None,
    raise_on_invalid: bool = False,
) -> OptimizationResult:
    """Calculates the optimal combination of vouchers to cover a target amount.

    Args:
        region: Store region ('TR' or 'US').
        currency: Store currency ('TRY' or 'USD').
        target_amount_minor: Purchase target in minor currency units (> 0).
        denominations: Sequence of available Denomination items.
        max_vouchers_per_order: Max vouchers allowed in a single order (default 6).
        max_allowed_overshoot_minor: Optional ceiling on wallet change (leftover).
        raise_on_invalid: If True, raises ValidationError on invalid inputs;
                          if False, returns OptimizationResult with status='no_solution'.

    Returns:
        OptimizationResult representing either success or no_solution.
    """
    # 1. Input Validation
    try:
        (
            reg_enum,
            curr_enum,
            target,
            validated_denoms,
            max_vouchers,
            max_overshoot,
        ) = validate_input_contract(
            region=region,
            currency=currency,
            target_amount_minor=target_amount_minor,
            denominations=denominations,
            max_vouchers_per_order=max_vouchers_per_order,
            max_allowed_overshoot_minor=max_allowed_overshoot_minor,
        )
    except ValidationError as err:
        if raise_on_invalid:
            raise
        return OptimizationResult(
            status="no_solution",
            reason=f"Input validation failed: {str(err)}",
        )

    # 2. Filter active denominations (strictly enabled=True and stock_limit > 0)
    # Invariant: Denominations with stock_limit == 0 are completely excluded
    active_denoms: List[_ActiveDenom] = []
    for idx, d in enumerate(validated_denoms):
        if d.enabled and d.stock_limit > 0:
            active_denoms.append(
                _ActiveDenom(
                    face_value_minor=d.face_value_minor,
                    cost_rub_kopecks=d.cost_rub_kopecks,
                    stock_limit=d.stock_limit,
                    original_index=idx,
                )
            )

    if not active_denoms:
        return OptimizationResult(
            status="no_solution",
            region=reg_enum.value,
            currency=curr_enum.value,
            target_amount_minor=target,
            reason="No enabled denominations with available stock.",
        )

    # 3. Check total stock capacity
    total_stock_face = sum(d.face_value_minor * d.stock_limit for d in active_denoms)
    if total_stock_face < target:
        return OptimizationResult(
            status="no_solution",
            region=reg_enum.value,
            currency=curr_enum.value,
            target_amount_minor=target,
            reason=(
                f"Total available stock face value ({total_stock_face}) "
                f"is less than target amount ({target})."
            ),
        )

    # 4. Sort denominations in canonical descending order:
    # Primary: face value descending (explore larger vouchers first)
    # Secondary: procurement cost ascending (cheaper vouchers first)
    # Tertiary: original index ascending (preserves stability)
    active_denoms.sort(
        key=lambda d: (-d.face_value_minor, d.cost_rub_kopecks, d.original_index)
    )
    n = len(active_denoms)

    # Precompute suffix maximum face values for reachability pruning
    suffix_max_face: List[int] = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_max_face[i] = suffix_max_face[i + 1] + (
            active_denoms[i].face_value_minor * active_denoms[i].stock_limit
        )

    # 5. Branch and Bound optimization
    # Evaluation tuple: (cost, leftover, voucher_count, tie_breaker_tuple)
    # tie_breaker_tuple: (-q_0, -q_1, ..., -q_{n-1}) preferring larger denominations
    best_candidate: Optional[Tuple[int, int, int, Tuple[int, ...], Dict[int, int]]] = None
    had_voucher_overflow = False
    had_overshoot_overflow = False

    # Greedy initial upper bound to quickly constrain the search space
    def _seed_initial_solution() -> None:
        nonlocal best_candidate
        rem = target
        cost_acc = 0
        cnt_acc = 0
        combo: Dict[int, int] = {}

        # Greedily take largest vouchers without overshooting if possible
        for i, d in enumerate(active_denoms):
            if rem <= 0:
                break
            max_take = min(d.stock_limit, rem // d.face_value_minor, max_vouchers - cnt_acc)
            if max_take > 0:
                combo[i] = max_take
                cost_acc += max_take * d.cost_rub_kopecks
                cnt_acc += max_take
                rem -= max_take * d.face_value_minor

        # If not covered yet, complete with the smallest possible single overshoot
        if rem > 0 and cnt_acc < max_vouchers:
            for i in reversed(range(n)):
                d = active_denoms[i]
                cur = combo.get(i, 0)
                avail = min(d.stock_limit - cur, max_vouchers - cnt_acc)
                if avail > 0:
                    take = min(avail, (rem - 1) // d.face_value_minor + 1)
                    combo[i] = cur + take
                    cost_acc += take * d.cost_rub_kopecks
                    cnt_acc += take
                    rem -= take * d.face_value_minor
                    if rem <= 0:
                        break

        if rem <= 0 and cnt_acc <= max_vouchers:
            tot_face = sum(combo[i] * active_denoms[i].face_value_minor for i in combo)
            leftover = tot_face - target
            if max_overshoot is None or leftover <= max_overshoot:
                tb = tuple(-combo.get(i, 0) for i in range(n))
                best_candidate = (cost_acc, leftover, cnt_acc, tb, dict(combo))

    _seed_initial_solution()

    # Recursive branch and bound
    current_combo: Dict[int, int] = {}

    def _search(
        idx: int,
        current_face: int,
        current_cost: int,
        current_count: int,
    ) -> None:
        nonlocal best_candidate, had_voucher_overflow, had_overshoot_overflow

        # Candidate check: target reached or exceeded
        if current_face >= target:
            leftover = current_face - target
            # Check business limits
            if current_count > max_vouchers:
                had_voucher_overflow = True
                return
            if max_overshoot is not None and leftover > max_overshoot:
                had_overshoot_overflow = True
                return

            tb = tuple(-current_combo.get(i, 0) for i in range(n))
            cand = (current_cost, leftover, current_count, tb, dict(current_combo))
            if best_candidate is None or cand < best_candidate:
                best_candidate = cand
            return

        if idx >= n:
            return

        # Prune: Depth limit (max vouchers per order)
        if current_count >= max_vouchers:
            had_voucher_overflow = True
            return

        denom = active_denoms[idx]

        # Prune 1: Suffix stock reachability
        if current_face + suffix_max_face[idx] < target:
            return

        needed = target - current_face

        # Prune 2: Minimum vouchers needed bound against max_vouchers limit
        # The largest available voucher face value in this suffix is active_denoms[idx].face_value_minor
        min_vouchers_needed = (needed + denom.face_value_minor - 1) // denom.face_value_minor
        if current_count + min_vouchers_needed > max_vouchers:
            had_voucher_overflow = True
            return

        # Prune 3: Cost lower bound using integer arithmetic
        if best_candidate is not None:
            min_extra_cost = min(
                (needed * active_denoms[j].cost_rub_kopecks) // active_denoms[j].face_value_minor
                for j in range(idx, n)
            )
            if current_cost + min_extra_cost > best_candidate[0]:
                return

        # Prune 4: Redundant voucher elimination (Theorem 1)
        # We never take more vouchers of denomination `idx` than needed to overshoot once,
        # nor more than allowed by max_vouchers limit.
        max_q = min(
            denom.stock_limit,
            (needed - 1) // denom.face_value_minor + 1,
            max_vouchers - current_count,
        )

        # Prune 5: Maximum overshoot limit bound
        if max_overshoot is not None:
            max_allowed_q = (needed + max_overshoot) // denom.face_value_minor
            if max_allowed_q < max_q:
                had_overshoot_overflow = True
                max_q = max_allowed_q


        # Prune 6: Lower bound on q
        stock_after = suffix_max_face[idx + 1]
        needed_from_this = needed - stock_after
        min_q = 0
        if needed_from_this > 0:
            min_q = (needed_from_this + denom.face_value_minor - 1) // denom.face_value_minor
        if min_q > max_q:
            return

        # Branching: iterate from max_q down to min_q
        for q in range(max_q, min_q - 1, -1):
            next_cost = current_cost + q * denom.cost_rub_kopecks
            if best_candidate is not None and next_cost > best_candidate[0]:
                continue

            next_face = current_face + q * denom.face_value_minor
            next_count = current_count + q

            if q > 0:
                current_combo[idx] = q

            _search(idx + 1, next_face, next_cost, next_count)

            if q > 0:
                del current_combo[idx]

    _search(0, 0, 0, 0)

    if best_candidate is None:
        reason = "No combination of available stock can cover the target amount."
        if had_overshoot_overflow and not had_voucher_overflow:
            reason = (
                f"Smallest available combination produces wallet leftover exceeding "
                f"maximum allowed overshoot ({max_overshoot})."
            )
        elif had_voucher_overflow and not had_overshoot_overflow:
            reason = (
                f"No combination covers target within the maximum limit of "
                f"{max_vouchers} vouchers per order."
            )
        return OptimizationResult(
            status="no_solution",
            region=reg_enum.value,
            currency=curr_enum.value,
            target_amount_minor=target,
            reason=reason,
        )


    # 6. Format successful result
    opt_cost, opt_leftover, opt_count, _, opt_combo = best_candidate

    selected_list: List[SelectedDenomination] = []
    # Sort selected denominations by face value descending
    for idx in sorted(opt_combo.keys(), key=lambda i: -active_denoms[i].face_value_minor):
        qty = opt_combo[idx]
        if qty > 0:
            d = active_denoms[idx]
            selected_list.append(
                SelectedDenomination(
                    face_value_minor=d.face_value_minor,
                    quantity=qty,
                    cost_rub_kopecks=d.cost_rub_kopecks,
                    total_cost_rub_kopecks=qty * d.cost_rub_kopecks,
                )
            )

    total_face = sum(item.face_value_minor * item.quantity for item in selected_list)

    return OptimizationResult(
        status="success",
        region=reg_enum.value,
        currency=curr_enum.value,
        target_amount_minor=target,
        selected_denominations=selected_list,
        total_face_value_minor=total_face,
        wallet_change_minor=total_face - target,
        total_procurement_cost_rub_kopecks=opt_cost,
        voucher_count=opt_count,
    )
