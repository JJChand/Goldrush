# -*- coding: utf-8 -*-
"""
GoldRush 2.0 — low-latency Python strategy.

Interface (per the rules):
    class Player:
        def MoveDecision(self, game_input) -> [a0..a5, k, order, vp]   # 9 ints

LATENCY IS A GAME MECHANIC
--------------------------
Moves resolve in ascending order of decision time, and the faster player's
whole turn resolves before the NPCs and before the opponent. In self-play
between two *identical* copies of this strategy, forcing one to move first was
worth roughly +40% to +100% gold. That dwarfs every decision-quality knob
measured here: a beam width of 48 makes slightly better plans than 16, but is
slow enough to move second every round and loses 1 game in 10 because of it.

The leaderboard reports P90 in microseconds.  This version therefore keeps the
normal path allocation-light and bounded: two compact beams, one joint scoring
pass, and an O(17*17) max-decay transform.  Low latency also wins the P90
tiebreak when gold totals are equal.

Design summary
--------------
1. A persistent map separates exact visible gold from expected fog gold, learns
   outer-region hotspots online, and uses global snapshots as raid signals.
2. Bomb knowledge expires at each observed 20-round refresh.  Unknown-bomb risk
   depends on region, cycle phase, and the individual unit's wallet.
3. A compact beam produces one marginal path curve per unit.  A cheap joint
   simulator then considers all seven move splits and both internal orders, so
   shared gold, bombs, trampling, and collisions are priced once rather than by
   two additional beam searches.
4. A linear max-decay transform replaces the per-round heap Dijkstra.  It is the
   exact Manhattan potential on an open grid; the short beam handles known walls.
5. MoveDecision can never raise: a legal all-stay decision remains the fallback.

Every tunable lives in the CONFIG block and can be overridden per instance.
"""

import math

# ----------------------------------------------------------------------------
# Game constants (mirror the rulebook)
# ----------------------------------------------------------------------------
N = 17                 # board is 17x17
NN = N * N
S = 6                  # moves shared by the two units each round
PICK_RATE = 0.65       # C%: fraction of a cell's gold taken on entry
BOMB_PCT = 0.10        # X%: gold lost when stepping on a bomb
TRAMPLE_PCT = 0.05     # N%: gold lost entering a cell with >2 NPCs
TRAMPLE_NPC = 3        # ">2 NPCs" == 3 or more

FOG = -5
BOMB = -3
OBSTACLE = -1

# action encoding: 0=up 1=down 2=left 3=right 4=stay
DR = (-1, 1, 0, 0, 0)
DC = (0, 0, -1, 1, 0)
STAY = 4

CENTER_LO, CENTER_HI = 4, 12   # the "center 9x9" that regenerates gold each round

# ----------------------------------------------------------------------------
# CONFIG — tune these after watching real matches
# ----------------------------------------------------------------------------
# Width 4 is the latency-first setting: it cuts roughly 10% from P90 versus
# width 6 while the joint evaluator recovers much of the coordination quality
# that v1 bought with two additional width-16 beam calls.
BEAM_WIDTH = 4
GAMMA = 0.72           # potential decay per step of distance
POT_W = 0.35           # weight of the terminal potential in a plan's score

DECAY = 0.95           # remembered gold decays this much per unseen round
CENTER_REGEN = 0.1668  # measured gold/round/passable center cell (first-day logs)
OUTER_REGEN = 0.05     # same, outside the center 9x9
AGE_CAP = 14           # cap on how much regrowth we credit to an old memory
MAX_EST = 60.0         # clamp on any single-cell estimate
HOTSPOT_EMA = 0.08     # learn persistent outer high-yield cells online
HOTSPOT_MAX = 2.0      # robust cap on learned per-round outer generation

# Discount for gold a rival is likely to reach first. Measured as a *loss* in
# self-play and disabled at 1.0: because we decide fast we usually move before
# the NPCs, so writing off cells they are near mostly throws away real gold.
# Lower it (0.7-0.85) only if the field turns out to be slower than us.
COMP_DISCOUNT = 1.0
COMP_MAX = 3           # cap on modelled competitors per cell
MY_SPEED = 5.0         # effective cells/round for one of our units (6 shared)
NPC_SPEED = 3.0        # B: NPC steps per round
OPP_SPEED = 5.0        # opponent unit effective speed

# Penalty (in gold) for stepping into a cell we have never observed. A move
# into fog can turn out to be a wall, in which case the move is silently burned.
# Mostly this breaks ties in favour of known-passable ground, e.g. when picking
# which neighbour to bounce off while milking a pile.
FOG_STEP = 0.6
OPENING_SCOUT_ROUNDS = 25
ENDGAME_ROUNDS = 15

# Observed standing bomb rates by official region.  A phase multiplier accounts
# for bombs disappearing after being triggered within a 20-round cycle.
BOMB_RATE = (0.0, 0.0300, 0.0579, 0.0635, 0.0535, 0.0600)
BOMB_PHASE = (1.12, 1.08, 1.05, 1.03, 1.02,
              1.00, 0.99, 0.98, 0.97, 0.96,
              0.95, 0.93, 0.92, 0.90, 0.88,
              0.86, 0.87, 0.88, 0.89, 0.91)

VIS_FRAC = 0.15        # fraction of income we are willing to spend on vision
VIS_WARMUP = 30        # no vision purchases before this round
VIS_EMA = 0.05         # income EMA smoothing
BLIND_BONUS = 10.0     # income level above which we buy 7x7 when we see nothing

# Region ids 1..5 used by the snapshot: 1 is the center 9x9, and 2..5
# are the windmill-shaped outer bands published in the FAQ.
def _region_of(r, c):
    if CENTER_LO <= r <= CENTER_HI and CENTER_LO <= c <= CENTER_HI:
        return 1
    if r <= 3 and c <= 12:
        return 2
    if r >= 4 and c <= 3:
        return 3
    if r >= 13 and c >= 4:
        return 4
    return 5


REGION_OF = [_region_of(i // N, i % N) for i in range(NN)]
IS_CENTER = [CENTER_LO <= (i // N) <= CENTER_HI and CENTER_LO <= (i % N) <= CENTER_HI
             for i in range(NN)]
ROW = [i // N for i in range(NN)]
COL = [i % N for i in range(NN)]

# Pre-baked in-bounds neighbours: NEIGH[cell] = ((action, target_cell), ...).
# Decision latency decides who grabs contested gold, so the hot loop avoids
# recomputing bounds checks every round.
NEIGH = []
for _i in range(NN):
    _r, _c = ROW[_i], COL[_i]
    _lst = []
    for _a in range(4):
        _nr, _nc = _r + DR[_a], _c + DC[_a]
        if 0 <= _nr < N and 0 <= _nc < N:
            _lst.append((_a, _nr * N + _nc))
    NEIGH.append(tuple(_lst))

# Direct action lookup for the joint simulator; -1 means out of bounds.
NEXT = []
for _i in range(NN):
    _nxt = [-1] * 5
    for _a, _j in NEIGH[_i]:
        _nxt[_a] = _j
    _nxt[STAY] = _i
    NEXT.append(tuple(_nxt))

BIT = tuple(1 << i for i in range(NN))

# Flat traversal orders for the separable Manhattan max-decay transform.
POT_H_FWD = tuple(i for i in range(NN) if i % N)
POT_H_BACK = tuple(i for i in range(NN - 1, -1, -1) if i % N < N - 1)
POT_V_FWD = tuple(range(N, NN))
POT_V_BACK = tuple(range(NN - N - 1, -1, -1))

# Manhattan distance table; the plan scorer indexes it millions of times.
DIST = [[abs(ROW[_j] - ROW[_i]) + abs(COL[_j] - COL[_i]) for _j in range(NN)]
        for _i in range(NN)]
SNAP_LO, SNAP_HI = 0.15, 10.0  # allow a real outer burst to trigger a raid


# ----------------------------------------------------------------------------
# Tolerant accessors — the judge may hand us structs, dicts or plain tuples.
# ----------------------------------------------------------------------------
def _f(obj, name, default=None):
    v = getattr(obj, name, None)
    if v is not None:
        return v
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _pos(p):
    """Return (row, col) from a Position-like value; (-1, -1) when absent."""
    if p is None:
        return (-1, -1)
    if isinstance(p, (tuple, list)):
        return (int(p[0]), int(p[1])) if len(p) >= 2 else (-1, -1)
    if isinstance(p, dict):
        return int(p.get("row", -1)), int(p.get("col", -1))
    return int(getattr(p, "row", -1)), int(getattr(p, "col", -1))


class Player:
    # ---------------------------------------------------------------- setup --
    def __init__(self, **tune):
        # Tunables are copied onto the instance so they can be overridden per
        # player (handy for offline self-play sweeps): Player(POT_W=0.5, ...)
        self.BEAM_WIDTH = BEAM_WIDTH
        self.GAMMA = GAMMA
        self.POT_W = POT_W
        self.DECAY = DECAY
        self.CENTER_REGEN = CENTER_REGEN
        self.OUTER_REGEN = OUTER_REGEN
        self.AGE_CAP = AGE_CAP
        self.MAX_EST = MAX_EST
        self.HOTSPOT_EMA = HOTSPOT_EMA
        self.HOTSPOT_MAX = HOTSPOT_MAX
        self.COMP_DISCOUNT = COMP_DISCOUNT
        self.FOG_STEP = FOG_STEP
        self.OPENING_SCOUT_ROUNDS = OPENING_SCOUT_ROUNDS
        self.ENDGAME_ROUNDS = ENDGAME_ROUNDS
        self.VIS_FRAC = VIS_FRAC
        self.VIS_WARMUP = VIS_WARMUP
        self.BLIND_BONUS = BLIND_BONUS
        for key, val in tune.items():
            setattr(self, key, val)
        # gamma^d lookup, used to discount gold we have already banked out of
        # the terminal potential term
        gpow = [self.GAMMA ** d for d in range(2 * N + 2)]
        self.gpow = gpow
        # GP[a][b] = GAMMA ** manhattan(a, b), precomputed once
        self.gp = [[gpow[d] for d in row] for row in DIST]
        # Legal rounds are 0..499 and never-seen cells start at timestamp -1.
        # Stock keeps decaying for the full game; AGE_CAP limits only how much
        # speculative regrowth we credit, not how long stale stock survives.
        self.decay_pow = [self.DECAY ** d for d in range(501)]

        self.obstacle = [False] * NN
        self.terrain_known = [False] * NN
        self.bomb = [False] * NN
        self.gold_known = [0.0] * NN      # physical stock posterior at gold_round
        # -1 gives never-seen cells one round of prior at round 0; v1 used
        # -1e6 and accidentally credited AGE_CAP rounds immediately.
        self.last_seen = [-1] * NN
        self.gold_round = [-1] * NN       # direct/snapshot observation or harvest
        self.bomb_safe_round = [-1] * NN # seen/traversed safe in this bomb cycle
        self.outer_rate = [self.OUTER_REGEN] * NN
        self.est = [0.0] * NN             # current per-cell estimate
        self.growth_work = [0.0] * NN     # snapshot-only scratch buffer
        self.pot = [0.0] * NN             # max-decayed target value
        self.pot_source = [-1] * NN       # source cell responsible for pot[i]
        self.region_scale = [1.0] * 6     # scales uncertain growth, never stock
        self.region_remaining = [0.0] * 6
        self.region_generated = [0.0] * 6
        self.region_collected = [0.0] * 6
        self.region_congestion = [0.0] * 6
        self.last_bomb_reset = -1
        self.prev_total = 0
        self.income = 0.0                 # EMA of gold gained per round
        self.vision_spend = 0
        self.pending = None               # confirmed from next round's endpoints
        self.round = 0

    # --------------------------------------------------------- entry point --
    def MoveDecision(self, game_input):
        try:
            return self._decide(game_input)
        except Exception:
            # A malformed decision is an instant loss; a wasted round is not.
            return [STAY] * S + [S // 2, 0, 0]

    # -------------------------------------------------------------- driver --
    def _decide(self, gi):
        rnd = int(_f(gi, "round", 0) or 0)
        self.round = rnd
        grid = _f(gi, "grid")

        units = _f(gi, "my_units") or []
        u = [_pos(units[0]) if len(units) > 0 else (0, 0),
             _pos(units[1]) if len(units) > 1 else (0, 0)]
        golds = list(_f(gi, "my_units_gold") or [0, 0])
        while len(golds) < 2:
            golds.append(0)
        idx = [u[0][0] * N + u[0][1], u[1][0] * N + u[1][1]]
        self._confirm_pending(rnd, idx)

        npcs = self._npc_cells(gi)
        enemies = [p for p in (_pos(x) for x in (_f(gi, "visible_enemies") or []))
                   if p[0] >= 0]

        self._observe(grid, rnd)
        self._estimate(rnd, gi, u, npcs, enemies)
        self._potential()

        # ------------------------------------------------ plan the 6 moves --
        enemy_mask = 0
        for r, c in enemies:
            if 0 <= r < N and 0 <= c < N:
                enemy_mask |= BIT[r * N + c]
        npc_cnt = [0] * NN
        for cell in npcs:
            npc_cnt[cell] += 1

        future_rounds = 499 - rnd
        pot_w = self.POT_W
        if future_rounds < self.ENDGAME_ROUNDS:
            pot_w *= max(0.0, future_rounds / self.ENDGAME_ROUNDS)
        risk_mult = 0.0 if rnd < self.OPENING_SCOUT_ROUNDS else 1.0
        if future_rounds < self.ENDGAME_ROUNDS:
            risk_mult *= (1.0 +
                          (self.ENDGAME_ROUNDS - future_rounds) /
                          self.ENDGAME_ROUNDS)
        phase = BOMB_PHASE[rnd % 20] * risk_mult
        self.turn_risk_rate = tuple(rate * phase for rate in BOMB_RATE)
        self.turn_fog_penalty = (0.0 if rnd < self.OPENING_SCOUT_ROUNDS
                                 else self.FOG_STEP)

        # Two marginal curves replace v1's four beam calls.  The evaluator
        # below jointly prices every k/order pair, including shared harvests,
        # bomb removal and the moving own-unit collision constraint.
        curve0 = self._plan(idx[0], golds[0], S, enemy_mask, npc_cnt, pot_w)
        curve1 = self._plan(idx[1], golds[1], S, enemy_mask, npc_cnt, pot_w)
        paths0 = [self._decode_actions(curve0[d][2], d) for d in range(S + 1)]
        paths1 = [self._decode_actions(curve1[d][2], d) for d in range(S + 1)]

        best = None
        for k in range(S + 1):
            acts = (paths0[k], paths1[S - k])
            # Execution order can only change the result when a route touches
            # the other route or the other unit's starting square.  Most
            # rounds are independent, so avoid a duplicate joint simulation.
            route0, route1 = curve0[k][5], curve1[S - k][5]
            order_matters = bool(route0 & route1 or
                                 route0 & BIT[idx[1]] or
                                 route1 & BIT[idx[0]])
            for order in ((0, 1) if order_matters else (0,)):
                result = self._joint_score(idx, golds, acts, order, enemy_mask,
                                           npc_cnt, pot_w)
                if best is None or result[0] > best[0]:
                    best = (result[0], k, order, acts, result)

        _, best_k, order, acts, result = best

        # Only pay for a residual replan when the independently-generated paths
        # actually compete or collide.  This recovers complementary routes in
        # the important shared-pile cases without restoring v1's two unconditional
        # tail searches on every round.
        h0, h1 = curve0[best_k][3], curve1[S - best_k][3]
        interaction = any(cell in h1 for cell in h0)
        if (result[1][0] != curve0[best_k][4] or
                result[1][1] != curve1[S - best_k][4]):
            interaction = True
        first, second = order, 1 - order
        if interaction:
            # Also transfer one move from the leader to the residual planner;
            # this fixes the common case where both marginal paths spend their
            # budgets on one central pile and starve a nearby side route.
            coordinated_ks = [best_k]
            shifted_k = best_k + (1 if second == 0 else -1)
            if 0 <= shifted_k <= S:
                coordinated_ks.append(shifted_k)
            for coordinated_k in coordinated_ks:
                budgets = (coordinated_k, S - coordinated_k)
                if budgets[second] <= 0:
                    continue
                seed_actions = (paths0[coordinated_k],
                                paths1[S - coordinated_k])
                lead_actions = [(), ()]
                lead_actions[first] = seed_actions[first]
                lead_result = self._joint_score(
                    idx, golds, tuple(lead_actions), order, enemy_mask,
                    npc_cnt, pot_w)
                lead_final = lead_result[1][first]
                tail_curve = self._plan(
                    idx[second], golds[second], budgets[second],
                    enemy_mask | BIT[lead_final], npc_cnt, pot_w,
                    pre_harv=lead_result[2], initial_cleared=lead_result[4],
                    beam_width=max(6, self.BEAM_WIDTH))
                tail = self._decode_actions(
                    tail_curve[budgets[second]][2], budgets[second])
                coordinated = [seed_actions[0], seed_actions[1]]
                coordinated[second] = tail
                coordinated = tuple(coordinated)
                coordinated_result = self._joint_score(
                    idx, golds, coordinated, order, enemy_mask, npc_cnt, pot_w)
                if coordinated_result[0] > result[0]:
                    best_k, acts, result = (coordinated_k, coordinated,
                                            coordinated_result)

        finals, harvested = result[1], result[2]
        self.pending = (rnd, finals, dict(harvested), result[3], result[4])

        vp = self._vision(rnd, golds, finals, int(_f(gi, "gold_opp", 0) or 0))
        return list(acts[0]) + list(acts[1]) + [best_k, order, vp]

    @staticmethod
    def _decode_actions(code, depth):
        actions = [STAY] * depth
        for i in range(depth - 1, -1, -1):
            actions[i] = code % 5
            code //= 5
        return tuple(actions)

    def _confirm_pending(self, rnd, actual_finals):
        """Commit last turn's belief changes only after endpoints confirm it."""
        pending = self.pending
        self.pending = None
        if pending is None:
            return
        action_round, predicted, harvested, known_bombs, traversed = pending
        if rnd != action_round + 1 or tuple(actual_finals) != tuple(predicted):
            return

        for cell, taken in harvested.items():
            left = self.est[cell] - taken
            # `est` is always expressed in physical expected gold.  Regional
            # snapshot scaling applies only to uncertain *regrowth*, never to
            # remembered stock, so a confirmed residual can be stored as-is.
            self.gold_known[cell] = left if left > 0.0 else 0.0
            self.gold_round[cell] = action_round
            rid = REGION_OF[cell]
            remain = self.region_remaining[rid] - taken
            self.region_remaining[rid] = remain if remain > 0.0 else 0.0

        while traversed:
            bit = traversed & -traversed
            cell = bit.bit_length() - 1
            self.terrain_known[cell] = True
            self.obstacle[cell] = False
            self.bomb_safe_round[cell] = action_round
            traversed ^= bit
        while known_bombs:
            bit = known_bombs & -known_bombs
            self.bomb[bit.bit_length() - 1] = False
            known_bombs ^= bit

    # -------------------------------------------------------- observation --
    def _npc_cells(self, gi):
        out = []
        lst = _f(gi, "visible_npcs") or []
        n = int(_f(gi, "num_visible_npcs", len(lst)) or 0)
        for i, npc in enumerate(lst):
            if i >= n:
                break
            if isinstance(npc, (tuple, list)):
                if len(npc) >= 3:
                    if int(npc[0] or 0) == 0:
                        continue
                    r, c = int(npc[1]), int(npc[2])
                else:
                    r, c = _pos(npc)
            else:
                if int(_f(npc, "id", 0) or 0) == 0:
                    continue
                p = _f(npc, "pos")
                if p is None:
                    p = _f(npc, "position")
                if p is not None:
                    r, c = _pos(p)
                else:
                    r, c = int(_f(npc, "row", -1)), int(_f(npc, "col", -1))
            if 0 <= r < N and 0 <= c < N:
                out.append(r * N + c)
        return out

    def _observe(self, grid, rnd):
        """Fold everything currently visible into the persistent belief map."""
        if grid is None:
            return
        # Official wrappers may expose list rows, ctypes rows, or a flat 289
        # buffer.  Shape detection by row length covers all three without a
        # numpy/ctypes dependency (v1 silently all-STAYed on ctypes 2-D grids).
        try:
            first = grid[0]
        except (IndexError, TypeError):
            return
        try:
            nested = len(first) >= N
        except (AttributeError, TypeError):
            nested = False

        if rnd % 20 == 0 and self.last_bomb_reset != rnd:
            self.bomb = [False] * NN
            self.last_bomb_reset = rnd

        obstacle, terrain, bomb = self.obstacle, self.terrain_known, self.bomb
        known, seen, outer_rate = self.gold_known, self.last_seen, self.outer_rate
        gold_round, bomb_safe = self.gold_round, self.bomb_safe_round
        alpha, rate_max = self.HOTSPOT_EMA, self.HOTSPOT_MAX
        for r in range(N):
            base = r * N
            row = grid[r] if nested else grid
            off = 0 if nested else base
            for c in range(N):
                v = row[off + c]
                if v == FOG:
                    continue
                i = base + c
                old_gold = known[i]
                old_gold_round = gold_round[i]
                seen[i] = rnd
                gold_round[i] = rnd
                terrain[i] = True
                if v == OBSTACLE:
                    obstacle[i] = True
                    bomb[i] = False
                    known[i] = 0.0
                    bomb_safe[i] = rnd
                elif v == BOMB:
                    bomb[i] = True
                    obstacle[i] = False
                    known[i] = 0.0
                else:
                    obstacle[i] = False
                    bomb[i] = False
                    known[i] = float(v) if v > 0 else 0.0
                    bomb_safe[i] = rnd
                    if not IS_CENTER[i]:
                        if old_gold_round >= 0:
                            elapsed = rnd - old_gold_round
                            if elapsed < 1:
                                elapsed = 1
                            sample = max(0.0, float(v) - old_gold) / elapsed
                        else:
                            elapsed = rnd + 1
                            sample = float(v) / (elapsed if elapsed > 0 else 1)
                        if sample > rate_max:
                            sample = rate_max
                        learned = outer_rate[i] + alpha * (sample - outer_rate[i])
                        outer_rate[i] = learned if learned > 0.01 else 0.01

    # ---------------------------------------------------------- estimation --
    def _estimate(self, rnd, gi, units, npcs, enemies):
        """Estimate ground gold without rounding expected values into certainty."""
        est = self.est
        known, seen, gold_round = self.gold_known, self.last_seen, self.gold_round
        obstacle, bomb = self.obstacle, self.bomb
        outer_rate, region_scale = self.outer_rate, self.region_scale
        age_cap, max_est = self.AGE_CAP, self.MAX_EST
        decay_pow, c_regen = self.decay_pow, self.CENTER_REGEN
        cycle_start = rnd - rnd % 20

        snap = _f(gi, "snapshot")
        snap_valid = bool(int(_f(gi, "snapshot_valid", 0) or 0) and
                          snap is not None)
        if snap_valid:
            vis_sum = [0.0] * 6
            fixed_sum = [0.0] * 6
            growth_sum = [0.0] * 6
            stock_scale = [1.0] * 6
            snap_growth_scale = list(region_scale)
            growth_work = self.growth_work

        for i in range(NN):
            if obstacle[i] or (bomb[i] and seen[i] >= cycle_start):
                fixed = growth = 0.0
            else:
                age = rnd - gold_round[i]
                if age <= 0:
                    fixed, growth = known[i], 0.0
                else:
                    a = age if age < age_cap else age_cap
                    regen = c_regen if IS_CENTER[i] else outer_rate[i]
                    fixed = known[i] * decay_pow[age]
                    growth = regen * a

            if snap_valid:
                reg = REGION_OF[i]
                growth_work[i] = growth
                # Keep the fixed component in `est` as scratch space.  Once
                # all five scales are known, the second pass only needs one
                # multiply per fog cell instead of rebuilding its age model.
                est[i] = fixed
                if seen[i] == rnd:
                    vis_sum[reg] += known[i]
                else:
                    # A snapshot can tell us how surprising current
                    # generation was, but it cannot turn a physically observed
                    # pile into ten copies of itself.  Calibrate only growth.
                    fixed_sum[reg] += fixed
                    growth_sum[reg] += growth
            elif seen[i] == rnd:
                # Current vision is exact and must not be capped or scaled.
                est[i] = fixed
            else:
                value = fixed + growth * region_scale[REGION_OF[i]]
                est[i] = max_est if value > max_est else value

        # Snapshot remaining gold is an immediate raid signal.  Learned
        # outer_rate makes the rescaling concentrate on observed hotspots
        # instead of smearing a burst uniformly across an entire region.
        if snap_valid:
            for reg_stat in (_f(snap, "regions") or []):
                rid = int(_f(reg_stat, "id", 0) or 0)
                if not 1 <= rid <= 5:
                    continue
                remain = float(_f(reg_stat, "gold_remaining", 0) or 0)
                generated = float(_f(reg_stat, "gold_generated", 0) or 0)
                collected = float(_f(reg_stat, "gold_collected", 0) or 0)
                occupants = float(_f(reg_stat, "occupants", 0) or 0)
                entered = float(_f(reg_stat, "enter", 0) or 0)
                left = float(_f(reg_stat, "leave", 0) or 0)
                # FAQ: these counts include NPCs, so treat them only as total
                # congestion—not as exact opponent locations.
                congestion = occupants + 0.25 * max(0.0, entered - left)
                self.region_remaining[rid] = remain
                self.region_generated[rid] = generated
                self.region_collected[rid] = collected
                self.region_congestion[rid] = congestion

                available = remain - vis_sum[rid]
                if available < 0.0:
                    available = 0.0
                fixed_total = fixed_sum[rid]
                growth_total = growth_sum[rid]
                if fixed_total >= available:
                    # Negative evidence may attenuate stale fog stock, but a
                    # snapshot can never amplify a pile we physically saw.
                    stock_scale[rid] = (available / fixed_total
                                        if fixed_total > 1e-9 else 0.0)
                    snap_growth_scale[rid] = 0.0
                    region_scale[rid] = SNAP_LO
                elif growth_total > 1e-6:
                    s = (available - fixed_total) / growth_total
                    # Fit this snapshot down to zero when needed.  Keep a
                    # small prior for generation in subsequent rounds.
                    snap_growth_scale[rid] = min(SNAP_HI, max(0.0, s))
                    region_scale[rid] = min(SNAP_HI, max(SNAP_LO, s))

            for i in range(NN):
                if seen[i] == rnd:
                    continue
                reg = REGION_OF[i]
                value = (est[i] * stock_scale[reg] +
                         growth_work[i] * snap_growth_scale[reg])
                value = max_est if value > max_est else value
                est[i] = value
                # Commit the aggregate observation as a physical posterior.
                # This preserves depletion evidence between five-round
                # snapshots without introducing a second latent unit system.
                known[i] = value
                gold_round[i] = rnd

        # -- competition discount: cells someone else reaches first are worth --
        # -- only what they leave behind (35% per prior visitor).             --
        if self.COMP_DISCOUNT >= 1.0:
            return                      # discount disabled: skip the scan
        rivals = list(npcs) + [r * N + c for r, c in enemies]
        if not rivals:
            return
        speeds = [NPC_SPEED] * len(npcs) + [OPP_SPEED] * len(enemies)
        d0 = DIST[units[0][0] * N + units[0][1]]
        d1 = DIST[units[1][0] * N + units[1][1]]
        rdist = [DIST[cell] for cell in rivals]
        disc = self.COMP_DISCOUNT
        for i in range(NN):
            if est[i] < 0.5:
                continue
            a, b = d0[i], d1[i]
            dme = (a if a < b else b) / MY_SPEED - 0.15
            comp = 0
            for j in range(len(rivals)):
                if rdist[j][i] / speeds[j] < dme:
                    comp += 1
                    if comp >= COMP_MAX:
                        break
            if comp:
                est[i] *= disc ** comp

    def _potential(self):
        """Linear max-product Manhattan distance transform.

        Four one-dimensional passes compute max_g(est[g] * GAMMA^L1(x,g)).
        This removes the heap and hundreds of tuple allocations from every
        turn.  Known walls are zeroed as destinations; the six-step planner is
        responsible for routing around them.
        """
        pot = self.est.copy()
        source = [i if pot[i] > 0.0 else -1 for i in range(NN)]
        gamma = self.GAMMA

        for i in POT_H_FWD:
            value = pot[i - 1] * gamma
            if value > pot[i]:
                pot[i], source[i] = value, source[i - 1]
        for i in POT_H_BACK:
            value = pot[i + 1] * gamma
            if value > pot[i]:
                pot[i], source[i] = value, source[i + 1]
        for i in POT_V_FWD:
            value = pot[i - N] * gamma
            if value > pot[i]:
                pot[i], source[i] = value, source[i - N]
        for i in POT_V_BACK:
            value = pot[i + N] * gamma
            if value > pot[i]:
                pot[i], source[i] = value, source[i + N]

        obstacle = self.obstacle
        for i in range(NN):
            if obstacle[i]:
                pot[i], source[i] = 0.0, -1
        self.pot, self.pot_source = pot, source

    # ------------------------------------------------------- move planning --
    def _plan(self, start, unit_gold, budget, blocked_mask, npc_cnt, pot_w,
              pre_harv=None, initial_cleared=0, beam_width=None):
        """Return the best marginal path for every depth from 0..budget.

        Expected fog gold stays fractional; only currently visible integer
        piles use the official ceil pickup.  `utility` includes information/
        wall penalties while `wealth` tracks the simulated wallet used as the
        later bomb and trampling base.
        """
        obstacle, bombs_map = self.obstacle, self.bomb
        est, seen = self.est, self.last_seen
        terrain, safe_round = self.terrain_known, self.bomb_safe_round
        pot, pot_source, gp = self.pot, self.pot_source, self.gp
        width = self.BEAM_WIDTH if beam_width is None else beam_width
        rnd = self.round
        cycle_start = rnd - rnd % 20

        risk_rate = self.turn_risk_rate
        fog_penalty = self.turn_fog_penalty

        init = dict(pre_harv) if pre_harv else {}
        sig0 = 0
        for cell in init:
            sig0 |= BIT[cell]
        initial_potential = pot[start]
        src = pot_source[start]
        if src >= 0:
            taken = init.get(src, 0.0)
            if taken:
                left = est[src] - taken
                initial_potential = ((left if left > 0.0 else 0.0) *
                                     gp[start][src])
        base = pot_w * initial_potential
        # result: score, wallet_delta, packed_actions, harvest, final_cell,
        #         traversed/cleared mask
        res = [(base, 0.0, 0, init, start, initial_cleared)] * (budget + 1)
        # state: score, uid, utility, wallet_delta, cell, harvest,
        #        harvest_mask, cleared_bomb_mask, packed_actions, blind_terminal
        beam = [(base, 0, 0.0, 0.0, start, init, sig0,
                 initial_cleared, 0, False)]
        uid = 1

        for depth in range(1, budget + 1):
            cand = {}
            for (_, _, utility, wealth, pos, harv, sig, cleared, code,
                 blind) in beam:
                if blind:
                    continue
                held = unit_gold + wealth
                bomb_pen = math.ceil(BOMB_PCT * held) if held > 0.0 else 0
                for action, nxt in NEIGH[pos]:
                    bit = BIT[nxt]
                    if obstacle[nxt] or blocked_mask & bit:
                        continue

                    taken = harv.get(nxt, 0.0) if sig & bit else 0.0
                    avail = est[nxt] - taken
                    if avail > 1e-9:
                        if seen[nxt] == rnd:
                            pickup = math.ceil(PICK_RATE * avail)
                        elif avail < 1.0:
                            pickup = avail
                        else:
                            pickup = PICK_RATE * avail + 0.35
                            if pickup > avail:
                                pickup = avail
                        nharv = dict(harv)
                        nharv[nxt] = taken + pickup
                        nsig = sig | bit
                    else:
                        pickup = 0.0
                        nharv, nsig = harv, sig

                    nutility = utility + pickup
                    nwealth = wealth + pickup
                    ncleared = cleared | bit
                    if not cleared & bit:
                        loss = 0.0
                        if bombs_map[nxt]:
                            loss = bomb_pen
                        elif safe_round[nxt] < cycle_start and bomb_pen:
                            loss = risk_rate[REGION_OF[nxt]] * bomb_pen
                        if loss:
                            nutility -= loss
                            nwealth -= loss

                    if npc_cnt[nxt] >= TRAMPLE_NPC:
                        after_pickup = unit_gold + nwealth
                        loss = (math.ceil(TRAMPLE_PCT * after_pickup)
                                if after_pickup > 0.0 else 0)
                        nutility -= loss
                        nwealth -= loss

                    never_seen = not terrain[nxt]
                    if never_seen:
                        nutility -= fog_penalty

                    # Correct only the source that generated this max field.
                    # v1 subtracted every harvested cell from a max, which was
                    # both mathematically wrong and one of its hottest loops.
                    p = pot[nxt]
                    src = pot_source[nxt]
                    if src >= 0:
                        source_taken = nharv.get(src, 0.0)
                        if source_taken:
                            left = est[src] - source_taken
                            p = (left if left > 0.0 else 0.0) * gp[nxt][src]
                    score = nutility + pot_w * p

                    key = (nxt, nsig, ncleared)
                    previous = cand.get(key)
                    if previous is None or score > previous[0]:
                        cand[key] = (score, uid, nutility, nwealth, nxt,
                                     nharv, nsig, ncleared,
                                     code * 5 + action, never_seen)
                        uid += 1

            if not cand:
                for rest in range(depth, budget + 1):
                    prev = res[rest - 1]
                    res[rest] = (prev[0], prev[1], prev[2] * 5 + STAY,
                                 prev[3], prev[4], prev[5])
                break

            ranked = sorted(cand.values(), reverse=True)
            # A narrow global top-k can fill with several loop histories that
            # end on the same square and discard every route toward a second
            # pile.  Reserve the beam for distinct endpoints first; this keeps
            # width 4 useful for two-unit residual coordination.
            if width <= 4 and len(ranked) > width:
                beam = []
                endpoint_mask = 0
                for state in ranked:
                    endpoint = state[4]
                    bit = BIT[endpoint]
                    if not endpoint_mask & bit:
                        beam.append(state)
                        endpoint_mask |= bit
                        if len(beam) == width:
                            break
                if len(beam) < width:
                    for state in ranked:
                        duplicate = False
                        for selected in beam:
                            if state is selected:
                                duplicate = True
                                break
                        if not duplicate:
                            beam.append(state)
                            if len(beam) == width:
                                break
            else:
                beam = ranked[:width]
            best = beam[0]
            res[depth] = (best[0], best[3], best[8], best[5], best[4], best[7])

            # Staying is represented only on the output curve, never expanded.
            prev = res[depth - 1]
            if prev[0] > res[depth][0]:
                res[depth] = (prev[0], prev[1], prev[2] * 5 + STAY,
                              prev[3], prev[4], prev[5])
        return res

    def _joint_score(self, starts, golds, actions, order, enemy_mask,
                     npc_cnt, pot_w):
        """Score a complete output with both units sharing mutable board state."""
        positions = [starts[0], starts[1]]
        wallets = [float(golds[0]), float(golds[1])]
        harvested = {}
        harvest_mask = 0
        cleared = 0
        known_bombs = 0
        utility = 0.0

        est, seen = self.est, self.last_seen
        obstacle, bombs_map = self.obstacle, self.bomb
        terrain, safe_round = self.terrain_known, self.bomb_safe_round
        risk_rate, fog_penalty = self.turn_risk_rate, self.turn_fog_penalty
        rnd = self.round
        cycle_start = rnd - rnd % 20

        for unit in (order, 1 - order):
            other = 1 - unit
            for action in actions[unit]:
                if action == STAY:
                    continue
                nxt = NEXT[positions[unit]][action]
                if nxt < 0:
                    continue
                bit = BIT[nxt]
                if (obstacle[nxt] or enemy_mask & bit or
                        nxt == positions[other]):
                    continue
                positions[unit] = nxt
                wallet_before = wallets[unit]

                taken = (harvested.get(nxt, 0.0)
                         if harvest_mask & bit else 0.0)
                avail = est[nxt] - taken
                if avail > 1e-9:
                    if seen[nxt] == rnd:
                        pickup = math.ceil(PICK_RATE * avail)
                    elif avail < 1.0:
                        pickup = avail
                    else:
                        pickup = PICK_RATE * avail + 0.35
                        if pickup > avail:
                            pickup = avail
                    harvested[nxt] = taken + pickup
                    harvest_mask |= bit
                    utility += pickup
                    wallets[unit] += pickup

                if not cleared & bit:
                    bomb_pen = (math.ceil(BOMB_PCT * wallet_before)
                                if wallet_before > 0.0 else 0)
                    loss = 0.0
                    if bombs_map[nxt]:
                        loss = bomb_pen
                        known_bombs |= bit
                    elif safe_round[nxt] < cycle_start and bomb_pen:
                        loss = risk_rate[REGION_OF[nxt]] * bomb_pen
                    if loss:
                        utility -= loss
                        wallets[unit] -= loss
                cleared |= bit

                if npc_cnt[nxt] >= TRAMPLE_NPC:
                    loss = (math.ceil(TRAMPLE_PCT * wallets[unit])
                            if wallets[unit] > 0.0 else 0)
                    utility -= loss
                    wallets[unit] -= loss
                if not terrain[nxt]:
                    utility -= fog_penalty

        future = [0.0, 0.0]
        future_source = [-1, -1]
        for unit in (0, 1):
            cell = positions[unit]
            value = self.pot[cell]
            src = self.pot_source[cell]
            if src >= 0:
                taken = harvested.get(src, 0.0)
                if taken:
                    left = self.est[src] - taken
                    value = (left if left > 0.0 else 0.0) * self.gp[cell][src]
            future[unit], future_source[unit] = value, src

        if future_source[0] >= 0 and future_source[0] == future_source[1]:
            high = future[0] if future[0] >= future[1] else future[1]
            low = future[1] if future[0] >= future[1] else future[0]
            future_value = high + 0.35 * low
        else:
            future_value = future[0] + future[1]

        return (utility + pot_w * future_value, tuple(positions), harvested,
                known_bombs, cleared)

    # ------------------------------------------------------------- vision --
    def _vision(self, rnd, golds, idx, gold_opp=0):
        """Buy sight only when the income it protects exceeds its 2/3 gold cost."""
        total = golds[0] + golds[1]
        delta = total - self.prev_total
        self.prev_total = total
        if rnd > 0:
            if delta < 0:
                delta = 0
            self.income += VIS_EMA * (delta - self.income)
        # A purchase applies on the next round; round 499 has no next round.
        if rnd < self.VIS_WARMUP or rnd >= 499:
            return 0
        lead = total - self.vision_spend - gold_opp
        posture = 1.20 if lead < 0 else (0.75 if lead > 50 else 1.0)
        budget = posture * self.VIS_FRAC * self.income
        if budget >= 3.0:
            self.vision_spend += 3
            return 2
        if budget >= 2.0:
            self.vision_spend += 2
            return 1
        # Blind and reasonably rich: one round of wider sight is worth 2 gold.
        if self.income >= self.BLIND_BONUS:
            est = self.est
            for cell in idx:
                r0, c0 = cell // N, cell % N
                for dr in range(-3, 4):
                    r = r0 + dr
                    if r < 0 or r >= N:
                        continue
                    for dc in range(-3, 4):
                        c = c0 + dc
                        if 0 <= c < N and est[r * N + c] >= 2.0:
                            return 0
            self.vision_spend += 2
            return 1
        return 0
