"""Comprehensive test suite for the Optimal Knapsack Engine.

Covers all test requirements and acceptance criteria from:
FINAL BUILD BRIEF — PHASE 1.1: Optimal Knapsack Engine
"""

import itertools
import random
import unittest
from typing import List, Optional, Tuple

from src.engine import (
    Currency,
    Denomination,
    OptimizationResult,
    Region,
    ValidationError,
    calculate_optimal_vouchers,
    validate_input_contract,
)


def brute_force_reference_solver(
    target_amount_minor: int,
    denominations: List[Denomination],
) -> Optional[Tuple[int, int, int, Tuple[int, ...], dict]]:
    """Exhaustive brute-force oracle for independent verification on small catalogs.

    Explores 100% of the Cartesian product space of all available stock.
    Applies the exact same objective and tie-breaking order:
    1. Min total procurement cost
    2. Min leftover wallet balance
    3. Min voucher count
    4. Deterministic tie-breaker (prefer larger denominations)

    Returns:
        Tuple of (cost, leftover, count, tie_breaker, combo_dict) or None if no solution.
    """
    # Filter active denominations
    active = [d for d in denominations if d.enabled and d.stock_limit > 0]
    if not active:
        return None

    # Canonical sort identical to engine
    active.sort(key=lambda d: (-d.face_value_minor, d.cost_rub_kopecks))
    n = len(active)

    best = None
    ranges = [range(d.stock_limit + 1) for d in active]

    for combo in itertools.product(*ranges):
        tot_face = sum(q * d.face_value_minor for q, d in zip(combo, active))
        if tot_face >= target_amount_minor:
            tot_cost = sum(q * d.cost_rub_kopecks for q, d in zip(combo, active))
            leftover = tot_face - target_amount_minor
            count = sum(combo)
            tb = tuple(-q for q in combo)
            cand = (tot_cost, leftover, count, tb, {i: q for i, q in enumerate(combo) if q > 0})
            if best is None or cand < best:
                best = cand

    return best


class TestKnapsackCoreRequirements(unittest.TestCase):
    """Tests core functional optimization and tie-breaker behaviors."""

    def assert_invariants(self, res: OptimizationResult, target: int, denoms: List[Denomination]) -> None:
        """Verifies calculation invariants for any successful optimization result."""
        self.assertEqual(res.status, "success")
        self.assertGreaterEqual(res.total_face_value_minor, target)
        self.assertEqual(res.wallet_change_minor, res.total_face_value_minor - target)

        calc_face = sum(v.face_value_minor * v.quantity for v in res.selected_denominations)
        self.assertEqual(res.total_face_value_minor, calc_face)

        calc_cost = sum(v.cost_rub_kopecks * v.quantity for v in res.selected_denominations)
        self.assertEqual(res.total_procurement_cost_rub_kopecks, calc_cost)

        calc_count = sum(v.quantity for v in res.selected_denominations)
        self.assertEqual(res.voucher_count, calc_count)

        # Verify stock and enabled constraints
        denom_map = {d.face_value_minor: d for d in denoms}
        for item in res.selected_denominations:
            orig = denom_map.get(item.face_value_minor)
            self.assertIsNotNone(orig, f"Selected unknown denomination {item.face_value_minor}")
            self.assertTrue(orig.enabled, f"Selected disabled denomination {item.face_value_minor}")
            self.assertLessEqual(item.quantity, orig.stock_limit, "Selected quantity exceeds stock limit")
            self.assertGreater(item.quantity, 0, "Selected item with zero quantity")

    def test_01_exact_denomination_match(self):
        """Case 1: Exact denomination match with target amount."""
        denoms = [
            Denomination(face_value_minor=10000, cost_rub_kopecks=35000, stock_limit=5),
            Denomination(face_value_minor=25000, cost_rub_kopecks=85000, stock_limit=5),
            Denomination(face_value_minor=50000, cost_rub_kopecks=160000, stock_limit=5),
        ]
        target = 25000  # Exact match for 250 ₺
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        self.assertEqual(res.total_face_value_minor, 25000)
        self.assertEqual(res.wallet_change_minor, 0)
        self.assertEqual(res.voucher_count, 1)
        self.assertEqual(len(res.selected_denominations), 1)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 25000)
        self.assertEqual(res.selected_denominations[0].quantity, 1)

    def test_02_combination_of_multiple_denominations(self):
        """Case 2: Combining multiple denominations to cover target."""
        # 1150 TRY target: 1000 TRY + 250 TRY (overshoot by 100) or 1000 + 100 + 100
        denoms = [
            Denomination(face_value_minor=100000, cost_rub_kopecks=310000, stock_limit=2),
            Denomination(face_value_minor=25000, cost_rub_kopecks=85000, stock_limit=2),
            Denomination(face_value_minor=10000, cost_rub_kopecks=35000, stock_limit=5),
        ]
        target = 115000
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        # Option A: 1000 + 250 = 1250 TRY (cost 3100 + 850 = 3950 RUB)
        # Option B: 1000 + 100 + 100 = 1200 TRY (cost 3100 + 350 + 350 = 3800 RUB)
        # Option B has lower cost (380000 kopecks) -> Option B wins!
        self.assertEqual(res.total_procurement_cost_rub_kopecks, 380000)
        self.assertEqual(res.total_face_value_minor, 120000)
        self.assertEqual(res.wallet_change_minor, 5000)

    def test_03_lower_procurement_cost_wins_even_with_more_vouchers(self):
        """Case 3: Lower procurement cost must win, even if it uses more vouchers."""
        # Target = 100 ₺
        # Voucher A: 100 ₺ for 400 ₽ (1 voucher)
        # Voucher B: 50 ₺ for 150 ₽ (2 vouchers = 300 ₽)
        denoms = [
            Denomination(face_value_minor=10000, cost_rub_kopecks=40000, stock_limit=5),
            Denomination(face_value_minor=5000, cost_rub_kopecks=15000, stock_limit=5),
        ]
        target = 10000
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        self.assertEqual(res.total_procurement_cost_rub_kopecks, 30000)
        self.assertEqual(res.voucher_count, 2)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 5000)
        self.assertEqual(res.selected_denominations[0].quantity, 2)

    def test_04_equal_cost_lower_leftover_balance_wins(self):
        """Case 4: Tie-breaker 1: Equal cost -> lower leftover balance wins."""
        # Target = 80
        # Combo A: 1 x 100 (leftover 20, cost 300)
        # Combo B: 1 x 90 (leftover 10, cost 300)
        denoms = [
            Denomination(face_value_minor=10000, cost_rub_kopecks=30000, stock_limit=1),
            Denomination(face_value_minor=9000, cost_rub_kopecks=30000, stock_limit=1),
        ]
        target = 8000
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        self.assertEqual(res.total_procurement_cost_rub_kopecks, 30000)
        self.assertEqual(res.wallet_change_minor, 1000)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 9000)

    def test_05_equal_cost_and_leftover_fewer_vouchers_wins(self):
        """Case 5: Tie-breaker 2: Equal cost and leftover -> fewer vouchers wins."""
        # Target = 100
        # Combo A: 1 x 100 (cost 200, leftover 0, count 1)
        # Combo B: 2 x 50 (cost 100 + 100 = 200, leftover 0, count 2)
        denoms = [
            Denomination(face_value_minor=10000, cost_rub_kopecks=20000, stock_limit=1),
            Denomination(face_value_minor=5000, cost_rub_kopecks=10000, stock_limit=2),
        ]
        target = 10000
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        self.assertEqual(res.total_procurement_cost_rub_kopecks, 20000)
        self.assertEqual(res.wallet_change_minor, 0)
        self.assertEqual(res.voucher_count, 1)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 10000)

    def test_06_complete_tie_deterministic_result(self):
        """Case 6: Tie-breaker 3: Complete tie produces a deterministic result."""
        # Combo 1: 1 x 70 + 1 x 30 = 100 (cost 200, count 2)
        # Combo 2: 1 x 60 + 1 x 40 = 100 (cost 200, count 2)
        # Tie-breaker rule prefers larger denominations -> Combo 1 (70 > 60)
        denoms = [
            Denomination(face_value_minor=7000, cost_rub_kopecks=10000, stock_limit=1),
            Denomination(face_value_minor=3000, cost_rub_kopecks=10000, stock_limit=1),
            Denomination(face_value_minor=6000, cost_rub_kopecks=10000, stock_limit=1),
            Denomination(face_value_minor=4000, cost_rub_kopecks=10000, stock_limit=1),
        ]
        target = 10000

        res1 = calculate_optimal_vouchers("TR", "TRY", target, denoms)
        res2 = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assertEqual(res1, res2)
        selected_faces = [d.face_value_minor for d in res1.selected_denominations]
        self.assertEqual(selected_faces, [7000, 3000])

    def test_07_no_valid_combination_exists_insufficient_total_stock(self):
        """Case 7: Target cannot be reached due to insufficient total stock."""
        denoms = [
            Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=2),  # 2000 total
        ]
        target = 3000
        res = calculate_optimal_vouchers("US", "USD", target, denoms)

        self.assertEqual(res.status, "no_solution")
        self.assertIn("less than target", res.reason)

    def test_08_insufficient_stock_prevents_combination(self):
        """Case 8: Desired cheap denomination has 0 stock, falls back to available stock."""
        denoms = [
            Denomination(face_value_minor=5000, cost_rub_kopecks=10000, stock_limit=0),  # Out of stock
            Denomination(face_value_minor=10000, cost_rub_kopecks=30000, stock_limit=2), # Available
        ]
        target = 5000
        res = calculate_optimal_vouchers("US", "USD", target, denoms)

        self.assert_invariants(res, target, denoms)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 10000)
        self.assertEqual(res.wallet_change_minor, 5000)

    def test_09_disabled_denomination_is_excluded(self):
        """Case 9: Denomination with enabled=False is completely ignored."""
        denoms = [
            Denomination(face_value_minor=5000, cost_rub_kopecks=5000, stock_limit=10, enabled=False),
            Denomination(face_value_minor=10000, cost_rub_kopecks=20000, stock_limit=10, enabled=True),
        ]
        target = 5000
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assert_invariants(res, target, denoms)
        # Should pick the 10000 voucher because 5000 is disabled
        self.assertEqual(res.selected_denominations[0].face_value_minor, 10000)
        for item in res.selected_denominations:
            self.assertNotEqual(item.face_value_minor, 5000)

    def test_10_empty_catalog(self):
        """Case 10: Empty denominations catalog returns structured no_solution."""
        res = calculate_optimal_vouchers("TR", "TRY", 5000, [])
        self.assertEqual(res.status, "no_solution")
        self.assertIn("No enabled denominations", res.reason)

    def test_11_all_denominations_zero_stock(self):
        """Case 11: Catalog where all items have stock_limit=0."""
        denoms = [
            Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=0),
            Denomination(face_value_minor=2000, cost_rub_kopecks=1000, stock_limit=0),
        ]
        res = calculate_optimal_vouchers("US", "USD", 1000, denoms)
        self.assertEqual(res.status, "no_solution")


class TestSecurityAndInputValidation(unittest.TestCase):
    """Tests security boundaries, type safety, and error handling."""

    def test_reject_zero_or_negative_target_amount(self):
        """Reject target amounts <= 0."""
        denoms = [Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=1)]
        # Default mode returns no_solution
        res_zero = calculate_optimal_vouchers("TR", "TRY", 0, denoms)
        self.assertEqual(res_zero.status, "no_solution")
        self.assertIn("must be strictly positive", res_zero.reason)

        res_neg = calculate_optimal_vouchers("TR", "TRY", -500, denoms)
        self.assertEqual(res_neg.status, "no_solution")
        self.assertIn("must be strictly positive", res_neg.reason)

        # raise_on_invalid=True raises ValidationError
        with self.assertRaises(ValidationError):
            calculate_optimal_vouchers("TR", "TRY", 0, denoms, raise_on_invalid=True)

        with self.assertRaises(ValidationError):
            calculate_optimal_vouchers("TR", "TRY", -100, denoms, raise_on_invalid=True)

    def test_reject_float_target_amount(self):
        """Reject float numbers to enforce strict integer arithmetic."""
        denoms = [Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=1)]
        with self.assertRaises(ValidationError):
            validate_input_contract("TR", "TRY", 100.5, denoms) # type: ignore

    def test_reject_invalid_region_or_currency(self):
        """Reject unknown regions, unknown currencies, and region-currency mismatches."""
        denoms = [Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=1)]

        # Unknown region
        with self.assertRaises(ValidationError):
            validate_input_contract("UK", "TRY", 1000, denoms)

        # Unknown currency
        with self.assertRaises(ValidationError):
            validate_input_contract("TR", "EUR", 1000, denoms)

        # Mismatch: TR with USD
        with self.assertRaises(ValidationError):
            validate_input_contract("TR", "USD", 1000, denoms)

        # Mismatch: US with TRY
        with self.assertRaises(ValidationError):
            validate_input_contract("US", "TRY", 1000, denoms)

    def test_reject_invalid_denomination_attributes(self):
        """Reject zero or negative face values, negative costs, negative stock."""
        # Face value <= 0
        with self.assertRaises(ValidationError):
            Denomination(face_value_minor=0, cost_rub_kopecks=100, stock_limit=1)

        with self.assertRaises(ValidationError):
            Denomination(face_value_minor=-50, cost_rub_kopecks=100, stock_limit=1)

        # Cost < 0
        with self.assertRaises(ValidationError):
            Denomination(face_value_minor=100, cost_rub_kopecks=-1, stock_limit=1)

        # Stock < 0
        with self.assertRaises(ValidationError):
            Denomination(face_value_minor=100, cost_rub_kopecks=100, stock_limit=-5)

        # Float values
        with self.assertRaises(ValidationError):
            Denomination(face_value_minor=100.5, cost_rub_kopecks=100, stock_limit=1) # type: ignore

    def test_try_and_usd_remain_separate(self):
        """Verify Turkish and US currencies are distinct and validated strictly."""
        tr_denoms = [Denomination(face_value_minor=25000, cost_rub_kopecks=85000, stock_limit=2)]
        us_denoms = [Denomination(face_value_minor=2500, cost_rub_kopecks=250000, stock_limit=2)]

        tr_res = calculate_optimal_vouchers(Region.TR, Currency.TRY, 25000, tr_denoms)
        self.assertEqual(tr_res.status, "success")
        self.assertEqual(tr_res.currency, "TRY")

        us_res = calculate_optimal_vouchers(Region.US, Currency.USD, 2500, us_denoms)
        self.assertEqual(us_res.status, "success")
        self.assertEqual(us_res.currency, "USD")

    def test_large_permitted_inputs(self):
        """Verify performance and correct behavior on large permitted inputs."""
        denoms = [
            Denomination(face_value_minor=500000, cost_rub_kopecks=1500000, stock_limit=100),
            Denomination(face_value_minor=250000, cost_rub_kopecks=800000, stock_limit=100),
            Denomination(face_value_minor=100000, cost_rub_kopecks=350000, stock_limit=100),
        ]
        target = 12_700_000  # 127,000 TRY
        res = calculate_optimal_vouchers("TR", "TRY", target, denoms)

        self.assertEqual(res.status, "success")
        self.assertGreaterEqual(res.total_face_value_minor, target)
        self.assertEqual(res.wallet_change_minor, res.total_face_value_minor - target)

    def test_repeated_runs_identical_results(self):
        """Verify identical inputs across 50 repeated runs yield 100% deterministic identical outputs."""
        denoms = [
            Denomination(face_value_minor=100000, cost_rub_kopecks=310000, stock_limit=3),
            Denomination(face_value_minor=50000, cost_rub_kopecks=160000, stock_limit=5),
            Denomination(face_value_minor=25000, cost_rub_kopecks=85000, stock_limit=5),
            Denomination(face_value_minor=10000, cost_rub_kopecks=35000, stock_limit=10),
        ]
        target = 235000  # 2,350 ₺

        first_res = calculate_optimal_vouchers("TR", "TRY", target, denoms)
        for _ in range(50):
            res = calculate_optimal_vouchers("TR", "TRY", target, denoms)
            self.assertEqual(first_res, res)

    def test_resource_limits_exceeded(self):
        """Reject inputs that exceed resource safety bounds."""
        denoms = [Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=1)]
        # Target amount > 10^12
        with self.assertRaises(ValidationError):
            validate_input_contract("TR", "TRY", 10**13, denoms)

        # Denomination count > 100
        too_many = [Denomination(face_value_minor=1000, cost_rub_kopecks=500, stock_limit=1) for _ in range(101)]
        with self.assertRaises(ValidationError):
            validate_input_contract("TR", "TRY", 1000, too_many)


class TestIndependentExhaustiveOracle(unittest.TestCase):
    """Independent correctness verification against brute-force Cartesian product oracle."""


    def test_against_brute_force_oracle_randomized_scenarios(self):
        """Generates random small bounded instances and compares engine vs oracle."""
        rng = random.Random(42)  # Fixed seed for deterministic test suite

        for trial in range(30):
            num_denoms = rng.randint(2, 4)
            denoms = []
            for i in range(num_denoms):
                face = rng.randint(1, 10) * 100
                cost = rng.randint(50, 300)
                stock = rng.randint(1, 3)
                denoms.append(
                    Denomination(
                        face_value_minor=face,
                        cost_rub_kopecks=cost,
                        stock_limit=stock,
                    )
                )

            target = rng.randint(1, 15) * 100

            # Run engine
            engine_res = calculate_optimal_vouchers("US", "USD", target, denoms)

            # Run oracle
            oracle_res = brute_force_reference_solver(target, denoms)

            if oracle_res is None:
                self.assertEqual(engine_res.status, "no_solution")
            else:
                opt_cost, opt_leftover, opt_count, _, _ = oracle_res
                self.assertEqual(engine_res.status, "success")
                self.assertEqual(
                    engine_res.total_procurement_cost_rub_kopecks,
                    opt_cost,
                    f"Cost mismatch on trial {trial}: engine={engine_res.total_procurement_cost_rub_kopecks}, oracle={opt_cost}",
                )
                self.assertEqual(
                    engine_res.wallet_change_minor,
                    opt_leftover,
                    f"Leftover mismatch on trial {trial}: engine={engine_res.wallet_change_minor}, oracle={opt_leftover}",
                )
                self.assertEqual(
                    engine_res.voucher_count,
                    opt_count,
                    f"Count mismatch on trial {trial}: engine={engine_res.voucher_count}, oracle={opt_count}",
                )

    def test_coin_change_greedy_trap(self):
        """Classic coin-change trap where greedy choice is suboptimal."""
        # Denominations: 6, 5, 1
        # Target: 10
        # Greedy for 10: 6 + 1 + 1 + 1 + 1 = 5 vouchers (or 6 + 5 = 11, overshoot 1)
        # Optimal: 5 + 5 = 10 (2 vouchers, exactly 10, lower cost)
        denoms = [
            Denomination(face_value_minor=600, cost_rub_kopecks=60, stock_limit=5),
            Denomination(face_value_minor=500, cost_rub_kopecks=45, stock_limit=5),
            Denomination(face_value_minor=100, cost_rub_kopecks=15, stock_limit=5),
        ]
        target = 1000  # 10.00
        res = calculate_optimal_vouchers("US", "USD", target, denoms)

        self.assertEqual(res.status, "success")
        self.assertEqual(res.total_face_value_minor, 1000)
        self.assertEqual(res.wallet_change_minor, 0)
        self.assertEqual(res.total_procurement_cost_rub_kopecks, 90)
        self.assertEqual(res.voucher_count, 2)
        self.assertEqual(len(res.selected_denominations), 1)
        self.assertEqual(res.selected_denominations[0].face_value_minor, 500)
        self.assertEqual(res.selected_denominations[0].quantity, 2)


if __name__ == "__main__":
    unittest.main()
