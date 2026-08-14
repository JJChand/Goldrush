#pragma once

// Local-only mirror of the ABI published in the competition rulebook.
// On the official build server, place the organizer-provided game_api.h next
// to strategy.cpp; strategy.cpp will prefer that header automatically.

constexpr int GOLD_RUSH_N = 17;
constexpr int GOLD_RUSH_S = 6;
constexpr int GOLD_RUSH_A = 7;

struct Position {
    int row;
    int col;
};

struct NpcInfo {
    int id;
    Position pos;
};

struct RegionStat {
    int id;
    int enter;
    int leave;
    int gold_generated;
    int gold_collected;
    int gold_remaining;
    int occupants;
};

struct Snapshot {
    int window_begin;
    int window_end;
    RegionStat regions[5];
};

struct GameInput {
    int round;
    int grid[GOLD_RUSH_N][GOLD_RUSH_N];
    Position my_units[2];
    int my_units_gold[2];
    int gold_opp;
    Position visible_enemies[2];
    int num_visible_npcs;
    NpcInfo visible_npcs[GOLD_RUSH_A];
    int snapshot_valid;
    Snapshot snapshot;
};

struct GameOutput {
    int actions[GOLD_RUSH_S];
    int k;
    int order;
    int vp;
};

static_assert(sizeof(int) == 4, "GoldRush ABI requires 32-bit int");
static_assert(sizeof(Position) == 8, "unexpected Position layout");
static_assert(sizeof(NpcInfo) == 12, "unexpected NpcInfo layout");
static_assert(sizeof(RegionStat) == 28, "unexpected RegionStat layout");
static_assert(sizeof(Snapshot) == 148, "unexpected Snapshot layout");
static_assert(sizeof(GameInput) == 1444, "unexpected local GameInput layout");
static_assert(sizeof(GameOutput) == 36, "unexpected local GameOutput layout");
