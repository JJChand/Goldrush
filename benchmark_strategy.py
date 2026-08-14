"""Reproducible warm-path latency benchmark for strategy.Player.

This is not a game simulator.  It exercises representative 5x5 visibility,
snapshots, bombs, NPCs, persistent beliefs, and all 500 calls without including
input construction in the timing.
"""

import argparse
import statistics
import time

import strategy


def percentile(sorted_values, q):
    return sorted_values[int(q * (len(sorted_values) - 1))]


def make_input(round_no):
    n = strategy.N
    grid = [[strategy.FOG] * n for _ in range(n)]
    units = [
        {"row": 7 + (round_no // 40) % 2, "col": 7},
        {"row": 9, "col": 9 - (round_no // 50) % 2},
    ]
    for unit in units:
        for row in range(unit["row"] - 2, unit["row"] + 3):
            for col in range(unit["col"] - 2, unit["col"] + 3):
                value = 2 if (row * 17 + col + round_no) % 19 == 0 else 0
                if (row * 11 + col) % 37 == 0:
                    value = strategy.OBSTACLE
                if round_no % 20 < 10 and (row * 23 + col) % 53 == 0:
                    value = strategy.BOMB
                grid[row][col] = value

    burst = round_no % 35 < 5
    regions = []
    for region_id in range(1, 6):
        outer_burst = region_id == 2 and burst
        regions.append({
            "id": region_id,
            "enter": int(region_id == 1),
            "leave": 0,
            "gold_generated": 45 if region_id == 1 else (80 if outer_burst else 0),
            "gold_collected": 25 if region_id == 1 else 0,
            "gold_remaining": 30 if region_id == 1 else (70 if outer_burst else 3),
            "occupants": 2 if region_id == 1 else 0,
        })

    return {
        "round": round_no,
        "grid": grid,
        "my_units": units,
        "my_units_gold": [round_no // 3, round_no // 4],
        "gold_opp": round_no // 2,
        "visible_enemies": [(-1, -1), (-1, -1)],
        "num_visible_npcs": 2,
        "visible_npcs": [
            {"id": 1, "pos": {"row": 8, "col": 8}},
            {"id": 2, "pos": {"row": 8, "col": 9}},
        ],
        "snapshot_valid": int(round_no % 5 == 0),
        "snapshot": {"regions": regions},
    }


def benchmark(matches, rounds, width):
    inputs = [make_input(i) for i in range(rounds)]
    samples = []
    match_p90 = []
    match_p95 = []
    match_p99 = []
    checksum = 0
    for _ in range(matches):
        player = strategy.Player(BEAM_WIDTH=width)
        match_samples = []
        for game_input in inputs:
            started = time.perf_counter_ns()
            output = player.MoveDecision(game_input)
            elapsed = time.perf_counter_ns() - started
            samples.append(elapsed)
            match_samples.append(elapsed)
            checksum = (checksum * 131 + sum(output)) & 0xFFFFFFFF
        match_samples.sort()
        match_p90.append(percentile(match_samples, 0.90))
        match_p95.append(percentile(match_samples, 0.95))
        match_p99.append(percentile(match_samples, 0.99))
    samples.sort()
    return {
        "calls": len(samples),
        "median": statistics.median(samples) / 1_000,
        # Official ranking aggregates the per-match percentile by median.
        "p90": statistics.median(match_p90) / 1_000,
        "p95": statistics.median(match_p95) / 1_000,
        "p99": statistics.median(match_p99) / 1_000,
        "max": samples[-1] / 1_000,
        "checksum": checksum,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=500)
    parser.add_argument("--width", type=int, default=strategy.BEAM_WIDTH)
    args = parser.parse_args()
    result = benchmark(args.matches, args.rounds, args.width)
    print(
        "calls={calls} width={width} median={median:.1f}us "
        "p90={p90:.1f}us p95={p95:.1f}us p99={p99:.1f}us "
        "max={max:.1f}us checksum={checksum}".format(width=args.width, **result)
    )


if __name__ == "__main__":
    main()
