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
    raise_on_invalid: bool = False,
) -> OptimizationResult:
    """Calculates the optimal combination of vouchers to cover a target amount.

    Args:
        region: Store region ('TR' or 'US').
        currency: Store currency ('TRY' or 'USD').
        target_amount_minor: Purchase target in minor currency units (> 0).
        denominations: Sequence of available Denomination items.
        raise_on_invalid: If True, raises ValidationError on invalid inputs;
                          if False, returns OptimizationResult with status='no_solution'.

    Returns:
        OptimizationResult representing either success or no_solution.
    """
    # 1. Input Validation
    try:
        reg_enum, curr_enum, target, validated_denoms = validate_input_contract(
            region=region,
            currency=currency,
            target_amount_minor=target_amount_minor,
            denominations=denominations,
        )
    except ValidationError as err:
        if raise_on_invalid:
            raise
        return OptimizationResult(
            status="no_solution",
            reason=f"Input validation failed: {str(err)}",
        )

    # 2. Filter active denominations (enabled=True and stock_limit > 0)
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
            take = min(d.stock_limit, rem // d.face_value_minor)
            if take > 0:
                combo[i] = take
                cost_acc += take * d.cost_rub_kopecks
                cnt_acc += take
                rem -= take * d.face_value_minor

        # If not covered yet, complete with the smallest possible single overshoot
        if rem > 0:
            for i in reversed(range(n)):
                d = active_denoms[i]
                cur = combo.get(i, 0)
                avail = d.stock_limit - cur
                if avail > 0:
                    take = min(avail, (rem - 1) // d.face_value_minor + 1)
                    combo[i] = cur + take
                    cost_acc += take * d.cost_rub_kopecks
                    cnt_acc += take
                    rem -= take * d.face_value_minor
                    if rem <= 0:
                        break

        if rem <= 0:
            tot_face = sum(combo[i] * active_denoms[i].face_value_minor for i in combo)
            leftover = tot_face - target
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
        nonlocal best_candidate

        # Candidate check: target reached or exceeded
        if current_face >= target:
            leftover = current_face - target
            tb = tuple(-current_combo.get(i, 0) for i in range(n))
            cand = (current_cost, leftover, current_count, tb, dict(current_combo))
            if best_candidate is None or cand < best_candidate:
                best_candidate = cand
            return

        if idx >= n:
            return

        denom = active_denoms[idx]

        # Prune 1: Suffix stock reachability
        # If even taking 100% of remaining stock cannot reach target, backtrack
        if current_face + suffix_max_face[idx] < target:
            return

        # Prune 2: Cost lower bound using integer arithmetic
        # The remaining face value needed is `needed`
        needed = target - current_face
        if best_candidate is not None:
            # Theoretical absolute lower bound on cost to cover `needed` using available suffix
            # For each denomination j >= idx: minimum integer ratio (needed * c_j) // v_j
            min_extra_cost = min(
                (needed * active_denoms[j].cost_rub_kopecks) // active_denoms[j].face_value_minor
                for j in range(idx, n)
            )
            if current_cost + min_extra_cost > best_candidate[0]:
                return

        # Prune 3: Redundant voucher elimination (Theorem 1)
        # We never take more vouchers of denomination `idx` than needed to overshoot once
        max_q = min(denom.stock_limit, (needed - 1) // denom.face_value_minor + 1)

        # Prune 4: Lower bound on q
        # If remaining stock after this denomination is insufficient to cover the rest,
        # we MUST take at least min_q of this denomination
        stock_after = suffix_max_face[idx + 1]
        needed_from_this = needed - stock_after
        min_q = 0
        if needed_from_this > 0:
            min_q = (needed_from_this + denom.face_value_minor - 1) // denom.face_value_minor
        if min_q > denom.stock_limit:
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
        return OptimizationResult(
            status="no_solution",
            region=reg_enum.value,
            currency=curr_enum.value,
            target_amount_minor=target,
            reason="No combination of available stock can cover the target amount.",
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
