# GoldRush — Reading List & Improvement Roadmap

Status: written 2026-08-13. Rules section of `README.md` still pending (Feishu doc
is JS-rendered; needs the Chrome extension or a paste).

Inputs used: `strategy.py` (v1), `LOG_FINDINGS_GOLD_BOMB.md` (16 logs, 3 maps).

---

## Part 0 — The number that should drive everything

From `LOG_FINDINGS`, board-wide gold generation per 5-round window:

| Region | gold / 5 rounds | gold / round |
|---|---:|---:|
| 1 (center 9x9) | 44.75 | **8.95** |
| 2–5 (outer) | 48.73 | 9.75 |
| **Total board** | **93.48** | **18.70** |

Two implications:

1. **Perfect uncontested play caps at ~18.7 gold/round.** In a 2-player match a
   dominant agent should be targeting somewhere in the 9–14 range. Instrument
   `strategy.py` to log realised gold/round and compare against this ceiling —
   that ratio, not win/loss, is the metric to optimise. Right now nobody knows
   whether v1 is at 40% or 80% of ceiling, which makes every tuning decision blind.

2. **Center density: 0.1668 gold per passable cell per round → ~54 passable
   center cells.** Sweeping all 54 with 6 moves/round takes ~9 rounds of pure
   movement, ~12 with obstacle detours. At a 12-round revisit cycle each center
   cell holds ~1.95 gold.

Now the pickup rule bites. Pickup is `ceil(0.65 * G)`:

| Cell gold G | you take | left behind |
|---:|---:|---:|
| 1 | 1 | 0 |
| 2 | 2 | 0 |
| 3 | 2 | 1 |
| 5 | 4 | 1 |
| 20 | 13 | 7 |

**Piles of 1–2 gold are harvested 100% in a single step.** A ~12-round center
patrol keeps every center cell in exactly that regime. So the center is not a
"chase the big pile" problem at all — it's a **coverage/latency problem**, and a
disciplined sweep extracts ~100% of center regen, ~8.95 gold/round, on its own.

This is the strongest strategic claim in this document and it contradicts how v1
behaves. v1 is a greedy 6-step beam search pulled by a potential field toward the
single largest estimated pile. Greedy potential-following on a uniform low-value
field produces erratic, self-overlapping paths and revisit intervals with high
variance — some cells get hit every 3 rounds (harvesting 0.5 gold), others every
40 (capped by `MAX_EST`, and by then a rival took it). Milking (`on/off/on`)
costs 2 moves for `ceil(0.65*0.35*G)` which is **worth nothing below G=3** and
is a net loss below roughly G=20 versus spending those moves on fresh cells.
Worth checking whether the beam over-milks.

---

## Part 1 — Reading list, mapped to sub-problems

### A. The core structure: rewards that regrow where you aren't

This is the single best-matching literature. A cell resets to ~0 when you visit
and accumulates linearly otherwise — that is exactly a **restless bandit with
reset processes**, and it has a closed-form index.

- Liu, Weber & Zhao (2011), *Indexability and Whittle Index for Restless Bandit
  Problems Involving Reset Processes* — [PDF](http://www.statslab.cam.ac.uk/~rrw1/publications/Liu%20-%20Weber%20-%20Zhao%202011%20Indexability%20and%20Whittle%20Index%20for%20restless%20bandit%20problems%20involving%20reset%20processes.pdf).
  **Read this one first.** Closed-form Whittle index for exactly your dynamics.
- Avrachenkov & Borkar, *Whittle index based Q-learning for restless bandits with
  average reward* — [arXiv:2004.14427](https://arxiv.org/pdf/2004.14427). Learn
  the index online when you don't know the regen rates (your outer regions).
- Nakhleh et al., *NeurWIN: Neural Whittle Index Network* — [NeurIPS 2021](https://proceedings.neurips.cc/paper/2021/file/0768281a05da9f27df178b5c39a51263-Paper.pdf).
  Only if you go the learned-policy route.

**Takeaway to steal:** replace the `est[]` + potential-field scoring with a per-cell
index `λ(i) = f(regen_rate_i, time_since_visit_i)` and plan a route that maximises
index-per-move. An index is a *scalar priority* — it composes with a route planner
far more cleanly than a max-discount potential field, and it naturally produces
patrol-like behaviour without hard-coding a cycle.

### B. Route planning under a move budget

Your per-round problem — "pick a walk of ≤6 steps from here maximising collected
prize" — is a Team Orienteering Problem with 2 vehicles.

- Chao, Golden & Wasil (1996), *The team orienteering problem* — [EJOR](https://www.sciencedirect.com/science/article/abs/pii/0377221794002894). The original.
- Vansteenwegen et al. (2011), *The orienteering problem: A survey* — [EJOR](https://www.sciencedirect.com/science/article/abs/pii/S0377221710002973).
- 2025 survey, model evolution + algorithmic advances — [arXiv:2512.16865](https://arxiv.org/pdf/2512.16865).
- Hammami, Rekik & Coelho, *Hybrid ALNS for the TOP* — [C&OR 2020](https://www.sciencedirect.com/science/article/abs/pii/S0305054820301519).
  ALNS destroy/repair is a good fit for a 300 ms anytime budget.

**Takeaway:** the 6-move budget is small enough that you can solve the TOP nearly
exactly. The hard part isn't the route, it's the *prizes* — which is Part A.

### C. Persistent monitoring / patrolling (the center-sweep question)

- Cassandras, Lin & Ding, *Optimal control for multi-agent persistent monitoring* —
  [Automatica 2013](https://www.sciencedirect.com/science/article/abs/pii/S0005109814001411).
- *A sub-modular receding horizon solution for mobile multi-agent persistent
  monitoring* — [arXiv:1908.04425](https://arxiv.org/pdf/1908.04425). Closest to
  what you'd actually implement: receding horizon + submodular greedy, bounded time.
- *ε-Optimal Multi-Agent Patrol using Recurrent Strategy* — [arXiv:2509.11640](https://arxiv.org/pdf/2509.11640).
- Portugal & Rocha, *A Survey on Multi-robot Patrolling Algorithms* — [link](https://www.researchgate.net/publication/220832895_A_Survey_on_Multi-robot_Patrolling_Algorithms).
  Cyclic vs. partition strategies — directly relevant to how you split 2 units.

**Takeaway:** the literature's answer to "cyclic sweep or partition the area?" is
*partition when agents are few and the area is compact* — i.e. give each unit half
the center 9x9 and let it run its own tight cycle. Cheap to implement, easy to test.

### D. Planning under partial observability

- Silver & Veness (2010), *Monte-Carlo Planning in Large POMDPs* (POMCP) —
  [NeurIPS PDF](https://papers.neurips.cc/paper_files/paper/2010/file/edfbe1afcf9246bb0d40eb4d8027d90f-Paper.pdf).
- Ye, Somani, Hsu & Lee, *DESPOT: Online POMDP Planning with Regularization* —
  [arXiv:1609.03250](https://arxiv.org/pdf/1609.03250). Better than POMCP under a
  **fixed real-time budget** and has regularisation against overfitting to sampled
  scenarios — which matters when your belief over fogged cells is mostly prior.

### E. Real-time budgeted search in games (the 300 ms question)

- Gaina, Lucas & Perez-Liebana, *Rolling Horizon Evolutionary Algorithms for
  General Video Game Playing* — [arXiv:2003.12331](https://arxiv.org/pdf/2003.12331) /
  [ToG'21 PDF](http://www.diego-perez.net/papers/RHEAforGVGP_ToG21.pdf).
  RHEA evolves an action *sequence* — which is literally your output format
  (6 moves + split + order). Strong anytime properties: interrupt it whenever and
  it hands back its best individual. **This is the most directly transplantable
  algorithm in the list.**
- Perez-Liebana et al., *Rolling Horizon NEAT* — [arXiv:2005.06764](https://arxiv.org/pdf/2005.06764).

### F. Closest published analogue: Pommerman

Partially observable grid, bombs, 2-agent teams, real-time budget. Top NeurIPS
2018 finishers were MCTS-based, not deep RL — a useful prior for where to spend
your effort.

- *Developing a Successful Bomberman Agent* — [arXiv:2203.09608](https://arxiv.org/pdf/2203.09608).
- Gao et al., *Skynet: A Top Deep RL Agent in the Inaugural Pommerman Team Competition* — [link](https://www.researchgate.net/publication/332897569_Skynet_A_Top_Deep_RL_Agent_in_the_Inaugural_Pommerman_Team_Competition).
- *Know your Enemy: Investigating MCTS with Opponent Models in Pommerman* — [arXiv:2305.13206](https://arxiv.org/pdf/2305.13206).
- [MultiAgentLearning/playground](https://github.com/MultiAgentLearning/playground) — reference env if you want a sandbox.

### G. Where to look (exploration & vision purchases)

- Singh, Krause, Guestrin, Kaiser & Batalin, *Nonmyopic Adaptive Informative Path
  Planning for Multiple Robots* — [IJCAI'09](https://www.ijcai.org/Proceedings/09/Papers/306.pdf).
  Multi-robot submodular orienteering — your 2-unit exploration problem, exactly.
- Krause & Golovin, *Submodular Function Maximization* survey — [PDF](https://viterbi-web.usc.edu/~shanghua/teaching/Fall2023-670/krause12survey.pdf).
  Gives you the `(1-1/e)` greedy guarantee that justifies a cheap greedy
  information-gain term instead of anything fancy.
- *Informative Path Planning with Limited Adaptivity* — [arXiv:2311.12698](https://arxiv.org/pdf/2311.12698).

---

## Part 2 — Ranked improvement roadmap

Ranked by (expected gold gain) / (implementation cost). Each item states the
concrete v1 defect it fixes.

### Tier 1 — do these first, all cheap

**1. Phase-dependent risk. Bombs are FREE early.**
Bomb damage is `ceil(0.10 * that_unit's_gold)`. At round 5 with 3 gold you lose 1.
At round 150 with 200 gold you lose 20. v1 applies the same `bomb_pen` logic
throughout and pays `FOG_STEP=0.6` on unknown cells from round 0.
*Fix:* make risk aversion an explicit function of held gold (it partly is) **and**
drive `FOG_STEP → 0` for the first ~25 rounds. Early game should be a deliberate,
reckless mapping sprint — you are buying map knowledge at near-zero cost, and that
knowledge feeds items 3 and 4 for the remaining 90% of the game.
*Cost:* ~10 lines. *Expected:* large, via compounding into hotspot learning.

**2. Bomb memory must reset on the 20-round boundary.**
`self.bomb[i]` is set `True` on sight and only cleared by re-observing the cell.
`LOG_FINDINGS` shows bombs fully resample at `round % 20 == 0` with a Jaccard
overlap of ~2.5% between cycles. So after a boundary, essentially **every
remembered bomb is wrong** — v1 is routing around ~40 phantom bombs and, far worse,
treating cells it "cleared" last cycle as safe when they now hold live bombs.
*Fix:* wipe `bomb[]` (and drop `last_seen` confidence) at every `round % 20 == 0`.
*Cost:* 3 lines. *Expected:* meaningful — this is a live bug, not a tuning knob.

**3. Fogged cells carry real, unmodelled bomb risk.**
`risk[i]` is set to `0.0` permanently the first time a cell is observed. A cell
last seen 30 rounds ago is treated as certainly bomb-free. It isn't: per
`LOG_FINDINGS` the standing bomb rate is ~3.0% in the center and 5.3–6.4% outer,
varying with cycle phase (6.39% at `%20==0` decaying to ~4.8% by `%20==15`).
*Fix:* `risk[i] = P_bomb(region, cycle_phase) * BOMB_PCT * unit_gold` for any cell
not seen this round. Note this makes risk **unit-specific and gold-scaled**, which
falls out of the existing `bomb_pen` machinery.
*Cost:* ~20 lines. *Expected:* large late-game, where 0.05 × 0.10 × 200 = 1.0 gold
per fogged step currently goes unpriced — comparable to a whole cell's pickup.

**4. Endgame: decay `POT_W` to 0.**
The potential field values *future* reachability. In the last ~10 rounds there is
no future; gold that is 8 steps away is worth exactly 0. v1 keeps `POT_W=0.35`
until the final round and will happily end the game walking toward a pile.
*Fix:* `POT_W_effective = POT_W * min(1, rounds_remaining / 15)`. Simultaneously
ramp risk aversion up — a bomb in the last 5 rounds is pure unrecoverable loss.
*Cost:* 5 lines. *Expected:* small but free and strictly positive.

### Tier 2 — the two big strategic wins

**5. Snapshot-driven outer-region raids.**
This is probably the largest single upside. Outer gold is ~52% of the board and
**~82% of it arrives in "burst" windows** (>20 gold in 5 rounds, ~11.5% of windows,
often 80–112 gold at once). v1 uses `snapshot.gold_remaining` only to *rescale
fogged priors uniformly across a region* — i.e. an 80-gold burst gets smeared over
~130 outer cells as +0.6 each, which the potential field then almost ignores.
*Fix:* treat the snapshot as a **mode trigger**, not a prior tweak. When region k's
`gold_remaining` crosses a threshold, dispatch a unit on a raid. Budget the
decision honestly: a center→outer round trip is ~12–16 moves ≈ 2–3 unit-rounds,
costing ~10–13 gold of foregone center income against an 80-gold prize. That is a
6–8x return and v1 is leaving it on the table almost every burst.
*Cost:* moderate — a mode/role state machine on top of the existing planner.
*Expected:* very large.

**6. Online hotspot learning for outer regions.**
`OUTER_REGEN = 0.05` is uniform. Reality: 5 template-`2` cells per outer region
carry gold **~12x** more often than ordinary outer cells (19.5% vs 1.65% seen-with-gold,
27.6x the mean value). A uniform prior can never steer a unit to them. And per
`LOG_FINDINGS`, the coordinates *must not be hard-coded* — the finals use new maps.
*Fix:* per-cell Beta/Gamma posterior over "gold arrival rate", updated on every
observation, with slow decay. This is exactly the Whittle-index-with-learning setup
from reading A. Then spread the snapshot rescaling **proportional to posterior
weight** instead of uniformly, which also sharpens item 5's raids from "wander the
region" into "hit 5 known cells".
*Cost:* moderate. *Expected:* large, and it multiplies item 5.

### Tier 3 — time optimisation (you asked for this specifically)

**7. You are spending 0.3% of your compute budget. Fix the controller, not the search.**
v1's header is right that move order is the strongest lever, and right that a
beam of 48 loses on latency. But the conclusion drawn — "stay at 0.9 ms forever" —
is over-corrected. You have a 300 ms budget and use ~0.9 ms. The binding
constraint is not absolute time, it is **being faster than this specific
opponent**, which is unknown and varies by match.
*Fix:* an **AIMD controller on the compute budget**. Additively raise the budget
(+15% per round) while you observe yourself resolving first; multiplicatively cut
it (×0.5) the moment you resolve second. You converge to just under the opponent's
latency and bank all the decision quality below that line, in every match, without
ever having to guess. Requires reading move order back from the round result —
confirm the API exposes it (rules doc pending).
*Guardrails:* hard ceiling well under the limit (say 60 ms), and the search must be
genuinely **anytime** so it can be cut off mid-iteration and still return a legal
decision. Keep the existing all-stay fallback.
*Cost:* moderate. *Expected:* unlocks Tier 4 entirely; near-zero risk if guarded.

**8. Spend the unlocked time on an anytime planner.**
Once item 7 gives you 10–50 ms instead of 0.9 ms, the beam is the wrong shape.
Two candidates, in order of preference:
- **RHEA** (reading E): evolves the exact 9-int output vector, anytime by
  construction, and handles the `k`-split and `order` fields as part of the genome
  rather than as a separate outer loop over 7×2 combinations.
- **DESPOT** (reading D): if you want honest belief-space planning over fog and
  bomb uncertainty rather than planning against point estimates.
Also extend the horizon: v1 plans exactly 6 moves = 1 round. A 2-round (12-move)
horizon is where patrol-like behaviour starts to emerge on its own.

**9. Micro-optimisations to buy headroom cheaply.**
`_estimate` and `_potential` both do full 289-cell passes with a Dijkstra every
round. `DIST` is a 289×289 table and `self.gp` duplicates it as floats (~167 KB of
Python floats, rebuilt per instance). If item 7's controller ever runs tight,
convert the hot arrays to `array('d')`/`bytearray`, and skip the `_potential`
recompute on rounds where the belief barely changed.

### Tier 4 — infrastructure (do it early, it makes everything above measurable)

**10. Paired self-play harness with fixed move order.**
v1's docstring says quality knobs were measured while latency was confounded —
that makes every reported number suspect. Build: N≥200 games, paired seeds, both
map-sides swapped, **move order forced** so decision quality is isolated, then a
second pass with real latency to price the order effect separately. Report gold/round
against the 18.7 ceiling from Part 0, with confidence intervals.

**11. Tune the CONFIG block with CMA-ES rather than by hand.**
There are ~15 tunables. Hand-sweeping them one at a time finds local optima and
misses interactions (e.g. `GAMMA` × `POT_W` are strongly coupled). Once item 10
gives a low-noise fitness signal, CMA-ES or SMAC over the block is a weekend job
with a real payoff.

---

## Part 3 — Open questions blocking further work

Answer these from the rules doc before implementing Tier 2–3:

1. **Game length** (total rounds). Item 4 needs it; Part 0's totals need it.
2. **Does the API report resolved move order / your decision time?** Item 7 depends
   on this. If not, fall back to a fixed conservative budget (~20 ms).
3. **Is gold per-unit or pooled?** If per-unit and bomb damage is 10% of *that
   unit's* holdings, there's an unexploited asymmetric play: keep one unit
   deliberately poor as a scout that walks through unknown/bomb-risky terrain at
   near-zero cost, while the rich unit farms only cleared center cells.
4. **Do NPCs harvest gold, or only trample?** v1 models trampling only. If NPCs
   collect, the competition discount (`COMP_DISCOUNT`, currently disabled at 1.0)
   was disabled for the wrong reason and needs re-deriving.
5. **Exact vision costs and radii** (v1 assumes 2 gold → 7x7, 3 gold → 9x9).
6. **Is pickup `ceil` or `floor`?** The whole Part 0 argument — that 1–2 gold piles
   are taken 100% — flips if it's `floor`. v1 assumes `ceil`. **Verify this first;
   it is the highest-leverage single fact in this document.**
7. **Are bomb refresh boundaries exactly `round % 20 == 0`,** and does round 0
   start with bombs already placed?
