# -*- coding: utf-8 -*-
"""
GoldRush 2.0 — Python strategy.

Interface (per the rules):
    class Player:
        def MoveDecision(self, game_input) -> [a0..a5, k, order, vp]   # 9 ints

THE BIG ONE: LATENCY IS THE STRONGEST LEVER IN THIS GAME
--------------------------------------------------------
Moves resolve in ascending order of decision time, and the faster player's
whole turn resolves before the NPCs and before the opponent. In self-play
between two *identical* copies of this strategy, forcing one to move first was
worth roughly +40% to +100% gold. That dwarfs every decision-quality knob
measured here: a beam width of 48 makes slightly better plans than 16, but is
slow enough to move second every round and loses 1 game in 10 because of it.

So this strategy deliberately spends ~0.3% of the 300 ms round budget. Keep it
that way. If you add cleverness, measure it under a *fixed* move order first to
see whether the quality gain is real, then check what it costs in latency.
(Low latency also wins the P90 tiebreak when gold totals are equal.)

Design summary
--------------
1. Persistent belief map (obstacles / bombs / gold) rebuilt every round from the
   5x5 (or bought 7x7 / 9x9) view, plus a decay+regrowth prior for fogged cells
   and a rescaling pass driven by the every-5-round global region snapshot.
2. A "potential field" (max over gold cells of value * GAMMA^distance) gives the
   units a gradient to follow when nothing is reachable inside the 6-move budget.
3. A beam search plans this round's moves. It models the real pickup rule
   (65% of the cell, so stepping off-and-back re-harvests the remaining 35% ->
   "milking": ~96% of a pile in one round instead of 65%), bomb damage (10% of
   *that unit's* gold, so bombs are free while poor and expensive while rich),
   and >2-NPC trampling (5%).
4. The 6 shared moves are split between the two units by maximising
   value0[k] + value1[6-k]; both execution orders are evaluated and the second
   unit re-plans against the first unit's harvest so they never double-count.
5. Vision is bought only when measured income justifies the 2/3 gold per round
   (turning it off entirely was the single worst config tested).
6. MoveDecision can never raise: it is wrapped and falls back to a legal
   all-stay decision, because a malformed decision loses the match outright.

Measured: ~0.9 ms p90 per round, ~2 ms worst case, on a 2026 laptop.
Every tunable lives in the CONFIG block below and can also be overridden per
instance, e.g. Player(BEAM_WIDTH=24), which is what the offline sweeps used.
"""

import heapq

# ----------------------------------------------------------------------------
# Game constants (mirror the rulebook)
# ----------------------------------------------------------------------------
N = 17                 # board is 17x17
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
# Beam width past ~16 buys almost no decision quality but costs latency, and
# latency loses contested gold outright (see the note on move order below).
BEAM_WIDTH = 16
GAMMA = 0.72           # potential decay per step of distance
POT_W = 0.35           # weight of the terminal potential in a plan's score

DECAY = 0.95           # remembered gold decays this much per unseen round
CENTER_REGEN = 0.70    # expected gold/round appearing on an unseen center cell
OUTER_REGEN = 0.05     # same, outside the center 9x9
AGE_CAP = 14           # cap on how much regrowth we credit to an old memory
MAX_EST = 60.0         # clamp on any single-cell estimate

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

VIS_FRAC = 0.15        # fraction of income we are willing to spend on vision
VIS_WARMUP = 30        # no vision purchases before this round
VIS_EMA = 0.05         # income EMA smoothing
BLIND_BONUS = 10.0     # income level above which we buy 7x7 when we see nothing

# Region ids 1..5 used by the snapshot. The rulebook does not pin the layout
# down, so this is the natural guess: 5 = center 9x9, 1..4 = the four outer
# quadrants (TL, TR, BL, BR). Snapshot influence is clamped, so a wrong guess
# degrades gracefully — fix REGION_OF once you see real snapshot data.
def _region_of(r, c):
    if CENTER_LO <= r <= CENTER_HI and CENTER_LO <= c <= CENTER_HI:
        return 5
    return 1 + (0 if r < 8 else 2) + (0 if c < 8 else 1)


REGION_OF = [_region_of(i // N, i % N) for i in range(N * N)]
IS_CENTER = [CENTER_LO <= (i // N) <= CENTER_HI and CENTER_LO <= (i % N) <= CENTER_HI
             for i in range(N * N)]
ROW = [i // N for i in range(N * N)]
COL = [i % N for i in range(N * N)]

# Pre-baked in-bounds neighbours: NEIGH[cell] = ((action, target_cell), ...).
# Decision latency decides who grabs contested gold, so the hot loop avoids
# recomputing bounds checks every round.
NEIGH = []
for _i in range(N * N):
    _r, _c = ROW[_i], COL[_i]
    _lst = []
    for _a in range(4):
        _nr, _nc = _r + DR[_a], _c + DC[_a]
        if 0 <= _nr < N and 0 <= _nc < N:
            _lst.append((_a, _nr * N + _nc))
    NEIGH.append(tuple(_lst))

# Manhattan distance table; the plan scorer indexes it millions of times.
DIST = [[abs(ROW[_j] - ROW[_i]) + abs(COL[_j] - COL[_i]) for _j in range(N * N)]
        for _i in range(N * N)]
SNAP_LO, SNAP_HI = 0.20, 5.0   # clamp on snapshot-derived rescaling


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
        self.COMP_DISCOUNT = COMP_DISCOUNT
        self.FOG_STEP = FOG_STEP
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

        n2 = N * N
        self.obstacle = [False] * n2
        self.bomb = [False] * n2
        self.gold_known = [0.0] * n2      # gold value when the cell was last seen
        self.last_seen = [-10 ** 6] * n2  # round of last observation
        self.risk = [self.FOG_STEP] * n2  # cleared once a cell has been seen
        self.est = [0.0] * n2             # current per-cell estimate
        self.pot = [0.0] * n2             # potential field
        self.region_scale = [1.0] * 6
        self.prev_total = 0
        self.income = 0.0                 # EMA of gold gained per round
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

        npcs = self._npc_cells(gi)
        enemies = [p for p in (_pos(x) for x in (_f(gi, "visible_enemies") or []))
                   if p[0] >= 0]

        self._observe(grid, rnd)
        self._estimate(rnd, gi, u, npcs, enemies)
        self._potential()

        # ------------------------------------------------ plan the 6 moves --
        idx = [u[0][0] * N + u[0][1], u[1][0] * N + u[1][1]]
        enemy_cells = set(r * N + c for r, c in enemies)
        npc_cnt = {}
        for cell in npcs:
            npc_cnt[cell] = npc_cnt.get(cell, 0) + 1

        blocked0 = enemy_cells | {idx[1]}
        blocked1 = enemy_cells | {idx[0]}
        curve0 = self._plan(idx[0], golds[0], S, blocked0, self.est, npc_cnt)
        curve1 = self._plan(idx[1], golds[1], S, blocked1, self.est, npc_cnt)

        best_k, best_sum = S // 2, -1e18
        for k in range(S + 1):
            tot = curve0[k][0] + curve1[S - k][0]
            if tot > best_sum:
                best_sum, best_k = tot, k

        best = None
        for order in (0, 1):
            first, second = (0, 1) if order == 0 else (1, 0)
            budget = (best_k, S - best_k)
            lead = (curve0 if first == 0 else curve1)[budget[first]]
            # The second unit plans against what the leader already took, and
            # must not walk into the leader's final cell.
            blk = enemy_cells | {lead[4]}
            tail = self._plan(idx[second], golds[second], budget[second], blk,
                              self.est, npc_cnt, pre_harv=lead[3])[budget[second]]
            score = lead[0] + tail[0]
            if best is None or score > best[0]:
                acts = [None, None]
                acts[first] = lead[2]
                acts[second] = tail[2]
                best = (score, order, acts)

        _, order, acts = best
        a0 = list(acts[0])[:best_k]
        a1 = list(acts[1])[:S - best_k]
        a0 += [STAY] * (best_k - len(a0))
        a1 += [STAY] * (S - best_k - len(a1))

        vp = self._vision(rnd, golds, idx)
        return a0 + a1 + [best_k, order, vp]

    # -------------------------------------------------------- observation --
    def _npc_cells(self, gi):
        out = []
        lst = _f(gi, "visible_npcs") or []
        n = int(_f(gi, "num_visible_npcs", len(lst)) or 0)
        for i, npc in enumerate(lst):
            if i >= n:
                break
            if isinstance(npc, (tuple, list, dict)) and not isinstance(npc, dict):
                r, c = _pos(npc)
            else:
                if int(_f(npc, "id", 0) or 0) == 0:
                    continue
                r, c = _pos(_f(npc, "pos"))
            if 0 <= r < N and 0 <= c < N:
                out.append(r * N + c)
        return out

    def _observe(self, grid, rnd):
        """Fold everything currently visible into the persistent belief map."""
        if not grid:
            return
        flat = not isinstance(grid[0], (list, tuple))
        obstacle, bomb = self.obstacle, self.bomb
        known, seen, risk = self.gold_known, self.last_seen, self.risk
        for r in range(N):
            base = r * N
            row = grid if flat else grid[r]
            off = base if flat else 0
            for c in range(N):
                v = row[off + c]
                if v == FOG:
                    continue
                i = base + c
                seen[i] = rnd
                risk[i] = 0.0        # observed once == known passable/blocked
                if v == OBSTACLE:
                    obstacle[i] = True
                    bomb[i] = False
                    known[i] = 0.0
                elif v == BOMB:
                    bomb[i] = True
                    obstacle[i] = False
                    known[i] = 0.0
                else:
                    obstacle[i] = False
                    bomb[i] = False
                    known[i] = float(v) if v > 0 else 0.0

    # ---------------------------------------------------------- estimation --
    def _estimate(self, rnd, gi, units, npcs, enemies):
        """Per-cell expected gold: exact where visible, prior where fogged."""
        est = self.est
        known, seen, obstacle = self.gold_known, self.last_seen, self.obstacle
        age_cap, decay, max_est = self.AGE_CAP, self.DECAY, self.MAX_EST
        c_regen, o_regen = self.CENTER_REGEN, self.OUTER_REGEN
        for i in range(N * N):
            if obstacle[i]:
                est[i] = 0.0
                continue
            age = rnd - seen[i]
            if age <= 0:
                est[i] = known[i]
                continue
            a = age if age < age_cap else age_cap
            regen = c_regen if IS_CENTER[i] else o_regen
            v = known[i] * (decay ** a) + regen * a
            est[i] = v if v < max_est else max_est

        # -- snapshot rescaling: make each region's fogged mass match reality --
        snap = _f(gi, "snapshot")
        if int(_f(gi, "snapshot_valid", 0) or 0) and snap is not None:
            vis_sum = [0.0] * 6
            fog_sum = [0.0] * 6
            for i in range(N * N):
                reg = REGION_OF[i]
                if seen[i] == rnd:
                    vis_sum[reg] += known[i]
                else:
                    fog_sum[reg] += est[i]
            for reg_stat in (_f(snap, "regions") or []):
                rid = int(_f(reg_stat, "id", 0) or 0)
                if not 1 <= rid <= 5:
                    continue
                remain = float(_f(reg_stat, "gold_remaining", 0) or 0)
                target = remain - vis_sum[rid]
                if fog_sum[rid] > 1e-6 and target > 0.0:
                    s = target / fog_sum[rid]
                elif target <= 0.0:
                    s = SNAP_LO
                else:
                    continue
                self.region_scale[rid] = min(SNAP_HI, max(SNAP_LO, s))

        for i in range(N * N):
            if seen[i] != rnd and est[i] > 0.0:
                est[i] *= self.region_scale[REGION_OF[i]]

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
        for i in range(N * N):
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
        """pot[x] = max_g est[g] * GAMMA^dist(x, g), via a max-first Dijkstra."""
        est, obstacle, GAMMA = self.est, self.obstacle, self.GAMMA
        pot = [0.0] * (N * N)
        heap = []
        top = 0.0
        for i in range(N * N):
            v = est[i]
            if v > 0.2:
                heap.append((-v, i))
                if v > top:
                    top = v
        heapq.heapify(heap)
        # Stop propagating once a source has decayed into irrelevance; this
        # bounds the Dijkstra without changing any decision that matters.
        cut = top * 0.02
        if cut < 0.15:
            cut = 0.15
        while heap:
            negv, i = heapq.heappop(heap)
            v = -negv
            if v <= pot[i]:
                continue
            pot[i] = v
            nv = v * GAMMA
            if nv < cut:
                continue
            r, c = i // N, i % N
            if r > 0:
                j = i - N
                if not obstacle[j] and nv > pot[j]:
                    heapq.heappush(heap, (-nv, j))
            if r < N - 1:
                j = i + N
                if not obstacle[j] and nv > pot[j]:
                    heapq.heappush(heap, (-nv, j))
            if c > 0:
                j = i - 1
                if not obstacle[j] and nv > pot[j]:
                    heapq.heappush(heap, (-nv, j))
            if c < N - 1:
                j = i + 1
                if not obstacle[j] and nv > pot[j]:
                    heapq.heappush(heap, (-nv, j))
        self.pot = pot

    # ------------------------------------------------------- move planning --
    def _plan(self, start, unit_gold, budget, blocked, est, npc_cnt,
              pre_harv=None):
        """Beam search over move sequences.

        Returns res[d] = (score, gold, actions, harvest, final_cell) for every
        depth d in 0..budget, so the caller can price any move split for free.

        score = gold banked this round + POT_W * (potential of the end cell,
        minus the gold this plan already banked, distance-discounted). That
        subtraction matters: without it a plan that ends standing on the pile
        it just emptied is credited with that pile twice, which makes the
        search chase single big piles and get *worse* as the beam widens.

        `pre_harv` lets the second unit plan against gold its partner has
        already taken this round, on both the pickup and the potential side.
        """
        obstacle, bombs_map, pot = self.obstacle, self.bomb, self.pot
        POT_W, gp, width, risk = self.POT_W, self.gp, self.BEAM_WIDTH, self.risk
        init = dict(pre_harv) if pre_harv else {}
        nlargest = heapq.nlargest

        # terminal potential at `start`, discounting gold already banked
        p0 = pot[start]
        if init:
            row = gp[start]
            for g, taken in init.items():
                p0 -= taken * row[g]
            if p0 < 0.0:
                p0 = 0.0
        base = POT_W * p0

        res = [(base, 0.0, (), init, start)] * (budget + 1)
        # Harvested cells and triggered bombs are tracked as bitmasks so that
        # de-duplication keys are plain ints (hashing dicts/frozensets here was
        # the single hottest line in the profile).
        sig0 = 0
        for g in init:
            sig0 |= 1 << g
        # state: (score, uid, gold, cell, harvest, sig, bomb_mask, actions)
        beam = [(base, 0, 0.0, start, init, sig0, 0, ())]
        uid = 1

        for depth in range(1, budget + 1):
            cand = {}
            for _, _, val, pos, harv, sig, hit, acts in beam:
                held = unit_gold + val
                bomb_pen = BOMB_PCT * held
                tramp_pen = TRAMPLE_PCT * held
                for a, nxt in NEIGH[pos]:
                    if obstacle[nxt] or nxt in blocked:
                        continue      # an illegal move is a burned move
                    bit = 1 << nxt
                    if sig & bit:
                        taken = harv[nxt]
                        avail = est[nxt] - taken
                    else:
                        taken = 0.0
                        avail = est[nxt]
                    if avail > 0.0:
                        gain = PICK_RATE * avail
                        nharv = dict(harv)
                        nharv[nxt] = taken + gain
                        nsig = sig | bit
                    else:
                        gain = 0.0
                        nharv, nsig = harv, sig
                    nhit = hit
                    if bombs_map[nxt] and not hit & bit:
                        gain -= bomb_pen
                        nhit = hit | bit
                    if npc_cnt.get(nxt, 0) >= TRAMPLE_NPC:
                        gain -= tramp_pen
                    gain -= risk[nxt]
                    nval = val + gain

                    p = pot[nxt]
                    if nharv:
                        grow = gp[nxt]
                        for g, tk in nharv.items():
                            p -= tk * grow[g]
                        if p < 0.0:
                            p = 0.0
                    score = nval + POT_W * p

                    key = (nxt, nsig)
                    prev = cand.get(key)
                    if prev is None or score > prev[0]:
                        cand[key] = (score, uid, nval, nxt, nharv, nsig, nhit,
                                     acts + (a,))
                        uid += 1
            if not cand:
                break
            beam = nlargest(width, cand.values())
            b = beam[0]
            res[depth] = (b[0], b[2], b[7], b[4], b[3])
            # "Stay" is never expanded (it wastes a move); instead, if using
            # fewer moves scored better, carry that plan forward padded with 4.
            prev = res[depth - 1]
            if prev[0] > res[depth][0]:
                res[depth] = (prev[0], prev[1], prev[2] + (STAY,), prev[3],
                              prev[4])
        return res

    # ------------------------------------------------------------- vision --
    def _vision(self, rnd, golds, idx):
        """Buy sight only when the income it protects exceeds its 2/3 gold cost."""
        total = golds[0] + golds[1]
        delta = total - self.prev_total
        self.prev_total = total
        if rnd > 0:
            if delta < 0:
                delta = 0
            self.income += VIS_EMA * (delta - self.income)
        if rnd < self.VIS_WARMUP:
            return 0
        budget = self.VIS_FRAC * self.income
        if budget >= 3.0:
            return 2
        if budget >= 2.0:
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
            return 1
        return 0
