#include "game_api_compat.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <dlfcn.h>
#include <iostream>
#include <string>
#include <vector>

namespace {

using MoveDecision = GameOutput (*)(const GameInput*);

bool legal(const GameOutput& output) {
    for (const int action : output.actions)
        if (action < 0 || action > 4) return false;
    return output.k >= 0 && output.k <= 6 &&
           (output.order == 0 || output.order == 1) &&
           output.vp >= 0 && output.vp <= 2;
}

void make_input(GameInput& input, int round) {
    std::memset(&input, 0, sizeof(input));
    input.round = round;
    for (auto& row : input.grid)
        for (int& value : row) value = -5;

    input.my_units[0] = Position{7 + (round / 40) % 2, 7};
    input.my_units[1] = Position{9, 9 - (round / 50) % 2};
    input.my_units_gold[0] = round / 3;
    input.my_units_gold[1] = round / 4;
    input.gold_opp = round / 2;
    input.visible_enemies[0] = Position{-1, -1};
    input.visible_enemies[1] = Position{-1, -1};

    for (const Position unit : input.my_units) {
        for (int row = unit.row - 2; row <= unit.row + 2; ++row) {
            for (int col = unit.col - 2; col <= unit.col + 2; ++col) {
                int value = (row * 17 + col + round) % 19 == 0 ? 2 : 0;
                if ((row * 11 + col) % 37 == 0) value = -1;
                if (round % 20 < 10 && (row * 23 + col) % 53 == 0) value = -3;
                input.grid[row][col] = value;
            }
        }
        input.grid[unit.row][unit.col] = 0;
    }

    input.num_visible_npcs = 2;
    input.visible_npcs[0] = NpcInfo{1, Position{8, 8}};
    input.visible_npcs[1] = NpcInfo{2, Position{8, 9}};
    input.snapshot_valid = round % 5 == 0;
    input.snapshot.window_begin = std::max(0, round - 4);
    input.snapshot.window_end = round;
    for (int region = 1; region <= 5; ++region) {
        const bool burst = region == 2 && round % 35 < 5;
        input.snapshot.regions[region - 1] = RegionStat{
            region,
            region == 1 ? 1 : 0,
            0,
            region == 1 ? 45 : burst ? 80 : 0,
            region == 1 ? 25 : 0,
            region == 1 ? 30 : burst ? 70 : 3,
            region == 1 ? 2 : 0,
        };
    }
}

}  // namespace

int main(int argc, char** argv) {
    const std::string library = argc > 1 ? argv[1] : "./player.so";
    void* handle = dlopen(library.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        std::cerr << "dlopen failed: " << dlerror() << '\n';
        return 1;
    }
    dlerror();
    void* symbol = dlsym(handle, "moveDecision");
    const char* error = dlerror();
    if (error || !symbol) {
        std::cerr << "dlsym(moveDecision) failed: "
                  << (error ? error : "missing symbol") << '\n';
        dlclose(handle);
        return 2;
    }
    MoveDecision move = nullptr;
    static_assert(sizeof(move) == sizeof(symbol), "POSIX function-pointer size mismatch");
    std::memcpy(&move, &symbol, sizeof(move));

    const GameOutput null_result = move(nullptr);
    if (!legal(null_result)) {
        std::cerr << "null-input fallback is illegal\n";
        dlclose(handle);
        return 3;
    }

    std::vector<long long> timings;
    timings.reserve(500);
    uint32_t checksum = 0;
    for (int round = 0; round < 500; ++round) {
        GameInput input{};
        make_input(input, round);
        const auto begin = std::chrono::steady_clock::now();
        const GameOutput output = move(&input);
        const auto end = std::chrono::steady_clock::now();
        if (!legal(output)) {
            std::cerr << "illegal output at round " << round << '\n';
            dlclose(handle);
            return 4;
        }
        timings.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(
            end - begin).count());
        for (const int action : output.actions)
            checksum = checksum * 131U + static_cast<uint32_t>(action);
        checksum = checksum * 131U + static_cast<uint32_t>(output.k);
        checksum = checksum * 131U + static_cast<uint32_t>(output.order);
        checksum = checksum * 131U + static_cast<uint32_t>(output.vp);
    }

    // Verify that a second round zero safely resets persistent match state.
    GameInput reset_input{};
    make_input(reset_input, 0);
    if (!legal(move(&reset_input))) {
        std::cerr << "cross-match reset returned illegal output\n";
        dlclose(handle);
        return 5;
    }

    std::sort(timings.begin(), timings.end());
    const auto percentile_us = [&](double percentile) {
        const std::size_t index = static_cast<std::size_t>(
            percentile * static_cast<double>(timings.size() - 1));
        return timings[index] / 1000.0;
    };
    std::cout << "PASS calls=500"
              << " median=" << percentile_us(0.50) << "us"
              << " p90=" << percentile_us(0.90) << "us"
              << " p99=" << percentile_us(0.99) << "us"
              << " checksum=" << checksum << '\n';
    dlclose(handle);
    return 0;
}

