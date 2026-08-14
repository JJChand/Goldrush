import ctypes
import random
import unittest

import strategy


N = strategy.N
NN = strategy.NN


def game_input(round_no=0, grid=None, **overrides):
    if grid is None:
        grid = [[strategy.FOG] * N for _ in range(N)]
    value = {
        "round": round_no,
        "grid": grid,
        "my_units": [(1, 1), (15, 15)],
        "my_units_gold": [0, 0],
        "gold_opp": 0,
        "visible_enemies": [(-1, -1), (-1, -1)],
        "num_visible_npcs": 0,
        "visible_npcs": [],
        "snapshot_valid": 0,
        "snapshot": None,
    }
    value.update(overrides)
    return value


class StrategyRuleTests(unittest.TestCase):
    def test_never_seen_cells_get_one_round_not_age_cap(self):
        player = strategy.Player()
        gi = game_input(0)
        player._observe(gi["grid"], 0)
        player._estimate(0, gi, [(1, 1), (15, 15)], [], [])
        center = 8 * N + 8
        outer = 1 * N + 1
        self.assertAlmostEqual(player.est[center], strategy.CENTER_REGEN)
        self.assertAlmostEqual(player.est[outer], strategy.OUTER_REGEN)

    def test_bomb_memory_expires_on_twenty_round_boundary(self):
        player = strategy.Player()
        cell = 8 * N + 8
        visible = [[strategy.FOG] * N for _ in range(N)]
        visible[8][8] = strategy.BOMB
        player._observe(visible, 19)
        self.assertTrue(player.bomb[cell])
        player._observe([[strategy.FOG] * N for _ in range(N)], 20)
        self.assertFalse(player.bomb[cell])

    def test_fractional_fog_expectation_is_not_rounded_to_one(self):
        player = strategy.Player()
        player.round = 30
        player.turn_risk_rate = (0.0,) * 6
        player.turn_fog_penalty = 0.0
        start = 8 * N + 8
        target = start + 1
        player.est = [0.0] * NN
        player.est[target] = 0.05
        player.last_seen[target] = 20  # known passable, currently fogged
        player.pot = [0.0] * NN
        player.pot_source = [-1] * NN
        curve = player._plan(start, 0, 1, 0, [0] * NN, 0.0)
        self.assertAlmostEqual(curve[1][1], 0.05)

    def test_visible_two_coin_pile_uses_official_ceiling(self):
        player = strategy.Player()
        player.round = 30
        player.turn_risk_rate = (0.0,) * 6
        player.turn_fog_penalty = 0.0
        start = 8 * N + 8
        target = start + 1
        player.est[target] = 2.0
        player.last_seen[target] = 30
        score = player._joint_score(
            [start, 0], [0, 0], ((3,), ()), 0,
            0, [0] * NN, 0.0,
        )[0]
        self.assertEqual(score, 2.0)

    def test_unknown_bomb_cost_scales_with_unit_wallet(self):
        player = strategy.Player()
        player.round = 30
        phase = strategy.BOMB_PHASE[10]
        player.turn_risk_rate = tuple(x * phase for x in strategy.BOMB_RATE)
        player.turn_fog_penalty = 0.0
        start = 8 * N + 8
        # Action 3 is right; the target was seen before the current bomb cycle.
        target = start + 1
        player.last_seen[target] = 0
        player.terrain_known[target] = True
        player.est[target] = 0.05
        poor = player._joint_score(
            [start, 0], [0, 0], ((3,), ()), 0, 0, [0] * NN, 0.0,
        )[0]
        rich = player._joint_score(
            [start, 0], [100, 0], ((3,), ()), 0, 0, [0] * NN, 0.0,
        )[0]
        self.assertAlmostEqual(poor, 0.05)
        self.assertLess(rich, poor)

    def test_pending_belief_changes_require_matching_endpoints(self):
        player = strategy.Player()
        cell = 8 * N + 8
        player.est[cell] = 10.0
        player.bomb[cell] = True
        player.pending = (5, (cell, 0), {cell: 7.0}, strategy.BIT[cell],
                          strategy.BIT[cell])
        player._confirm_pending(6, [cell, 0])
        self.assertEqual(player.gold_known[cell], 3.0)
        self.assertEqual(player.gold_round[cell], 5)
        self.assertEqual(player.bomb_safe_round[cell], 5)
        self.assertTrue(player.terrain_known[cell])
        self.assertFalse(player.bomb[cell])

        other = cell + 1
        player.est[other] = 10.0
        player.pending = (6, (other, 0), {other: 7.0}, 0,
                          strategy.BIT[other])
        player._confirm_pending(7, [cell, 0])
        self.assertEqual(player.gold_known[other], 0.0)
        self.assertEqual(player.gold_round[other], -1)

    def test_confirmed_fog_harvest_does_not_apply_region_scale_twice(self):
        player = strategy.Player()
        cell = N + 1
        rid = strategy.REGION_OF[cell]
        player.est[cell] = 10.0       # already includes the 10x region scale
        player.region_scale[rid] = 10.0
        player.outer_rate[cell] = 0.0
        player.pending = (5, (cell, 0), {cell: 7.0}, 0,
                          strategy.BIT[cell])
        player._confirm_pending(6, [cell, 0])
        self.assertAlmostEqual(player.gold_known[cell], 3.0)
        gi = game_input(6)
        player._estimate(6, gi, [(0, 0), (0, 1)], [], [])
        self.assertAlmostEqual(player.est[cell], 2.85)

    def test_region_scale_applies_only_to_unseen_regrowth(self):
        player = strategy.Player()
        grid = [[strategy.FOG] * N for _ in range(N)]
        grid[1][1] = 20
        cell = N + 1
        player._observe(grid, 10)
        player.outer_rate[cell] = strategy.OUTER_REGEN
        player.region_scale[strategy.REGION_OF[cell]] = 10.0
        gi = game_input(11)
        player._estimate(11, gi, [(0, 0), (0, 1)], [], [])
        # 20 observed coins decay to 19; only the 0.05 regrowth prior is 10x.
        self.assertAlmostEqual(player.est[cell], 19.5)

    def test_zero_snapshot_removes_stale_fog_stock(self):
        player = strategy.Player()
        grid = [[strategy.FOG] * N for _ in range(N)]
        grid[1][1] = 20
        cell = N + 1
        rid = strategy.REGION_OF[cell]
        player._observe(grid, 10)
        snap = {"regions": [{
            "id": rid,
            "gold_remaining": 0,
            "gold_generated": 0,
            "gold_collected": 20,
            "occupants": 0,
            "enter": 0,
            "leave": 0,
        }]}
        gi = game_input(11, snapshot_valid=1, snapshot=snap)
        player._estimate(11, gi, [(0, 0), (0, 1)], [], [])
        self.assertAlmostEqual(player.est[cell], 0.0)
        self.assertAlmostEqual(player.gold_known[cell], 0.0)
        self.assertEqual(player.gold_round[cell], 11)

    def test_stock_decay_continues_past_regrowth_age_cap(self):
        player = strategy.Player()
        cell = N + 1
        player.gold_known[cell] = 20.0
        player.gold_round[cell] = 0
        player.last_seen[cell] = 0
        player.outer_rate[cell] = 0.0
        gi = game_input(30)
        player._estimate(30, gi, [(0, 0), (0, 1)], [], [])
        self.assertAlmostEqual(player.est[cell], 20.0 * strategy.DECAY ** 30)

    def test_unchanged_outer_stock_is_not_learned_as_new_generation(self):
        player = strategy.Player()
        grid = [[strategy.FOG] * N for _ in range(N)]
        grid[1][1] = 20
        cell = N + 1
        player._observe(grid, 10)
        learned = player.outer_rate[cell]
        player._observe([[strategy.FOG] * N for _ in range(N)], 11)
        player._observe(grid, 12)
        self.assertLessEqual(player.outer_rate[cell], learned)

    def test_residual_replan_finds_complementary_side_routes(self):
        grid = [[0] * N for _ in range(N)]
        grid[8][8] = 20
        grid[8][5] = 18
        grid[8][11] = 18
        player = strategy.Player()
        output = player._decide(game_input(
            100, grid=grid, my_units=[(8, 7), (8, 9)],
        ))
        k, order = output[6], output[7]
        actions = (tuple(output[:k]), tuple(output[k:6]))
        score = player._joint_score(
            [8 * N + 7, 8 * N + 9], [0, 0], actions, order,
            0, [0] * NN, 0.0,
        )[0]
        self.assertEqual(score, 37.0)

    def test_ctypes_nested_and_flat_grids_are_supported(self):
        row_type = ctypes.c_int * N
        grid_type = row_type * N
        nested = grid_type(*[row_type(*([0] * N)) for _ in range(N)])
        flat = (ctypes.c_int * NN)(*([0] * NN))
        for grid in (nested, flat):
            output = strategy.Player()._decide(game_input(0, grid=grid))
            self.assert_legal(output)

    def test_final_round_ignores_terminal_future_value(self):
        player = strategy.Player()
        grid = [[0] * N for _ in range(N)]
        # This primarily guards the 499-rnd boundary; the decision must remain
        # legal when the effective potential weight is exactly zero.
        output = player._decide(game_input(499, grid=grid))
        self.assert_legal(output)
        self.assertEqual(player._vision(499, [0, 0], (0, 0)), 0)

    def test_randomized_outputs_are_always_legal(self):
        rng = random.Random(20260814)
        player = strategy.Player()
        for round_no in range(120):
            grid = [[strategy.FOG] * N for _ in range(N)]
            units = [(rng.randrange(2, 15), rng.randrange(2, 15)),
                     (rng.randrange(2, 15), rng.randrange(2, 15))]
            if units[0] == units[1]:
                units[1] = (units[1][0], (units[1][1] + 1) % N)
            for ur, uc in units:
                for r in range(ur - 2, ur + 3):
                    for c in range(uc - 2, uc + 3):
                        roll = rng.random()
                        grid[r][c] = (-1 if roll < 0.07 else
                                      -3 if roll < 0.10 else
                                      rng.randrange(1, 12) if roll < 0.18 else 0)
                grid[ur][uc] = 0
            output = player._decide(game_input(
                round_no, grid=grid, my_units=units,
                my_units_gold=[round_no // 2, round_no // 3],
            ))
            self.assert_legal(output)

    def assert_legal(self, output):
        self.assertEqual(len(output), 9)
        self.assertTrue(all(type(x) is int for x in output))
        self.assertTrue(all(0 <= x <= 4 for x in output[:6]))
        self.assertTrue(0 <= output[6] <= 6)
        self.assertIn(output[7], (0, 1))
        self.assertIn(output[8], (0, 1, 2))


if __name__ == "__main__":
    unittest.main()
