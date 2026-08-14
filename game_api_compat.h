#pragma once

// Local-only mirror of the official GoldRush 2.0 game_api.h.
//
// IMPORTANT: this file must stay a NAME-FOR-NAME mirror of the organizer's
// header, including the global constants GRID_SIZE / MAX_NPCS / S /
// REGION_COUNT. An earlier version used private names (GOLD_RUSH_N/S/A) to
// "avoid polluting the global namespace". That hid a real build break: the
// official header puts `S` and `MAX_NPCS` at global scope, which collided with
// same-named constants in strategy.cpp's anonymous namespace and only failed on
// the organizer's build server. Never rename these to be tidy.
//
// On the official build server the organizer-provided game_api.h sits next to
// player.cpp and strategy.cpp picks it up automatically via __has_include.

constexpr int GRID_SIZE = 17;   // 地图 17x17
constexpr int MAX_NPCS  = 7;    // 最多可见 NPC 数
constexpr int S         = 6;    // 每回合总步数
constexpr int REGION_COUNT = 5; // 快照区域数

struct Position {
    int row;
    int col;
};

struct NpcInfo {
    int      id;
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
    int        window_begin;
    int        window_end;
    RegionStat regions[REGION_COUNT];
};

struct GameInput {
    int      round;
    int      grid[GRID_SIZE][GRID_SIZE];
    Position my_units[2];
    int      my_units_gold[2];
    int      gold_opp;
    Position visible_enemies[2];
    int      num_visible_npcs;
    NpcInfo  visible_npcs[MAX_NPCS];
    int      snapshot_valid;
    Snapshot snapshot;
};

struct GameOutput {
    int actions[S];
    int k;
    int order;
    int vp;
};

// Declared exactly as the official header does — in particular WITHOUT
// noexcept. Adding noexcept to the definition in strategy.cpp is a hard
// compile error against the official header ("different exception specifier").
extern "C" GameOutput moveDecision(const GameInput* input);

static_assert(sizeof(int) == 4, "GoldRush ABI requires 32-bit int");
static_assert(sizeof(Position) == 8, "unexpected Position layout");
static_assert(sizeof(NpcInfo) == 12, "unexpected NpcInfo layout");
static_assert(sizeof(RegionStat) == 28, "unexpected RegionStat layout");
static_assert(sizeof(Snapshot) == 148, "unexpected Snapshot layout");
static_assert(sizeof(GameInput) == 1444, "unexpected local GameInput layout");
static_assert(sizeof(GameOutput) == 36, "unexpected local GameOutput layout");
