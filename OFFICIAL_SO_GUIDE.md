# GoldRush C++ `.so` 官方机编译与测试

## 1. 准备文件

从官方材料取得正式 `game_api.h`，与以下文件放在同一目录：

```text
strategy.cpp
game_api.h                 # 官方正式 ABI，优先使用
game_api_compat.h          # 仅供本地无官方头文件时测试
test_strategy_cpp.cpp
Makefile
```

`strategy.cpp` 会通过 `__has_include("game_api.h")` 优先包含官方头文件。不要修改
或重命名官方结构体字段；若编译报字段差异，应修改薄适配层，不要猜测 ABI。

## 2. 在官方开发/编译机首次验证

登录组委会提供的开发机（FAQ 当前给出的地址为 `8.153.76.120`，账号和认证方式
以群内通知为准），进入代码目录：

```bash
make clean
make CXX=g++
make test
```

`make` 会：

1. 用 C++17、`-O3`、PIC、隐藏默认符号和 LTO 构建 `player.so`；
2. 检查动态符号表中恰能找到 `moveDecision`；
3. 构建一个通过 `dlopen/dlsym` 调用 `.so` 的本地测试器；
4. 连续调用 500 回合，检查每回合动作、`k/order/vp` 合法并输出本机延迟。

预期输出形如：

```text
PASS exported symbol: moveDecision
PASS calls=500 median=...us p90=...us p99=...us checksum=...
```

任何编译 warning、缺失符号、非法输出或崩溃都不要上传。

## 3. 内存与未定义行为检查

正式优化构建前至少运行一次：

```bash
make asan CXX=g++
```

该目标同时为测试器和 `.so` 开启 AddressSanitizer、UndefinedBehaviorSanitizer。
必须以零 sanitizer 报告退出。Sanitizer 版本只用于测试，不能上传。

## 4. 检查正式 `.so`

重新生成无 sanitizer 的发布文件：

```bash
make clean
make CXX=g++
make test
file player.so
ldd player.so
nm -D --defined-only player.so | grep moveDecision
stat -c '%n %s bytes' player.so
sha256sum player.so
```

检查要点：

- `file` 显示 x86-64 ELF shared object；
- `moveDecision` 是未改名的动态导出符号；
- 文件小于官方16MB限制；
- `ldd` 中没有自己电脑上的非官方绝对路径依赖；
- 保存 `sha256sum`，上传后与平台记录核对。

默认没有使用 `-march=native`，优先降低非法指令风险。FAQ 表示编译机与运行机
除超线程外一致，但只有确认官方要求后才建议额外尝试 CPU 专用指令；不要使用
`-ffast-math`，它可能改变 `ceil`、NaN 和比较语义。

## 5. 上传后的灰度测试顺序

只上传发布版 `player.so`，然后按以下顺序减少直接判负风险：

1. 发起少量公测对局，确认没有“格式非法/运行异常”；
2. 查看回放中的500轮输出是否完整；
3. 查看第一轮延迟是否异常——FAQ 明确首次 `moveDecision` 初始化会计入 P90；
4. 记录每局 P90 的中位数，而不是只看最快一局；
5. 与 Python v2.2 做固定地图、交换出生边的配对对局，比较金币而非只比较延迟；
6. C++金币行为确认一致后，再将 `.so` 设为正式版本。

## 6. 官方头文件不兼容时

最常见报错是 NPC 数量宏、数组维度或头文件路径不同。处理顺序：

1. 保留官方 `game_api.h` 原样；
2. 将完整编译错误与官方头文件中四个结构体定义进行对照；
3. 只修改 `strategy.cpp` 顶部 include/字段访问适配；
4. 重新运行 `make asan && make test`；
5. 不用 `reinterpret_cast` 猜结构体布局，也不要自行 `#pragma pack`。

正式 ABI 以官方 `game_api.h` 为唯一真源，`game_api_compat.h` 不能作为最终依据。

