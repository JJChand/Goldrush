CXX ?= g++

CPPFLAGS := -I.
CXXFLAGS_COMMON := -std=c++17 -Wall -Wextra -Wpedantic -Wconversion
CXXFLAGS_RELEASE := -O3 -DNDEBUG -fPIC -fvisibility=hidden \
	-fstack-protector-strong -flto
LDFLAGS_RELEASE := -shared -flto -Wl,-z,defs -Wl,-z,relro -Wl,-z,now

.PHONY: all test benchmark check-symbol asan clean

all: player.so test_strategy_cpp check-symbol

player.so: strategy.cpp game_api_compat.h $(wildcard game_api.h)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS_COMMON) $(CXXFLAGS_RELEASE) \
		$(LDFLAGS_RELEASE) strategy.cpp -o $@

test_strategy_cpp: test_strategy_cpp.cpp game_api_compat.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS_COMMON) -O2 test_strategy_cpp.cpp \
		-ldl -o $@

check-symbol: player.so
	@nm -D --defined-only player.so | awk '$$3 == "moveDecision" { found=1 } END { exit !found }'
	@echo "PASS exported symbol: moveDecision"

test: player.so test_strategy_cpp check-symbol
	./test_strategy_cpp ./player.so

benchmark: test

player_asan.so: strategy.cpp game_api_compat.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS_COMMON) -O1 -g -fPIC -fvisibility=hidden \
		-fno-omit-frame-pointer -fsanitize=address,undefined \
		-shared strategy.cpp -o $@

test_strategy_cpp_asan: test_strategy_cpp.cpp game_api_compat.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS_COMMON) -O1 -g -fno-omit-frame-pointer \
		-fsanitize=address,undefined test_strategy_cpp.cpp -ldl -o $@

asan: player_asan.so test_strategy_cpp_asan
	ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 \
	UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
	./test_strategy_cpp_asan ./player_asan.so

clean:
	rm -f player.so player_asan.so test_strategy_cpp test_strategy_cpp_asan
