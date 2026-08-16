# GoldRush 竞赛策略仓库

本文档汇总：**比赛规则** → **由规则推导出的关键结论** → **日志实证规律** → **原策略 v1 的缺陷** → **改进路线图** → **论文阅读清单**。

相关文件：

- `strategy.py` — 当前低延迟 v2 策略实现
- `strategy.cpp` — 固定内存、C ABI 导出的 C++17 v2.2 移植
- `game_api_compat.h` — 本地测试用 ABI 镜像；正式编译必须优先用官方头文件
- `test_strategy_cpp.cpp` — 通过 `dlopen/dlsym` 验证 `.so` 的500轮测试器
- `OFFICIAL_SO_GUIDE.md` — 官方开发机编译、sanitizer、符号与上传检查清单
- `LOG_FINDINGS_GOLD_BOMB.md` — 首日 16 份日志的金币/炸弹规律分析
- `benchmark_strategy.py` — 500 回合热路径延迟基准
- `test_strategy.py` — 规则、输入兼容性与合法输出回归测试

## v2.3：beam 换成精确 DFS + 分支限界（2026-08-16）

`Strategy::plan` 里的 beam search 已被**深度 6 的精确 DFS + 分支限界**完全取代。
分支因子 ≤ 4（`STAY` 由单调 carry-forward 处理，不进搜索），所以整棵树最多
`(4^7-1)/3 = 5461` 个节点——**"好路线在 width 3 被剪掉"这个失效模式现在结构上
不可能发生**。递归时每个深度直接写入对应的 `result[depth]` 槽位，
`plan()` 本来就要返回的**每预算边际曲线因此是免费的**，不需要按预算各搜一次。

可采纳的上界（`Strategy::dfs`）：

```text
bound = utility
      + remaining_steps × reach_gain_[remaining][pos]     // 障碍感知的曼哈顿膨胀
      + POT_W_eff × pot_[pos] × GAMMA^(-remaining)        // max-decay 变换的性质
      + DENIAL_CAP                                        // 仅侦察角色
```

- `reach_gain_[k][c]` = 距 `c` 不超过 `k` 步（绕障碍）的格子里最大的单步拾取量，
  每回合用 6 层单跳膨胀算一次，`6 × 289 × 4` 次比较。
- 第二项的合法性来自 `pot_` 是**精确**的曼哈顿 max-decay 变换：
  对任意距离 `k` 的 `c'` 有 `pot_[c'] ≤ pot_[c] × GAMMA^(-k)`。
- 炸弹、踩踏、迷雾都是负项，忽略它们只会抬高上界，方向是安全的。

剪枝条件是 `bound ≤ min(result[depth+1..budget].score)`——因为后代可能改进的是
任意一个深度槽位，不能只跟最终预算比。

### 验证（真实对局输入，见 `LOG_ANALYSIS_460712.md`）

回放台用 `game_460712.log` 里我方 500 回合的真实 `GameInput` 驱动 `.so`：

| | median | P90 | P99 | max | 占 300ms |
|---|---:|---:|---:|---:|---:|
| beam width 3（v2.2） | 15.1 us | 18.2 us | 31.6 us | 147 us | 0.006% |
| **DFS + 分支限界（v2.3）** | **41.8 us** | **67.3 us** | **97.5 us** | **339 us** | **0.022%** |
| 仓库合成负载 `sotest` | 101 us | 133 us | 156 us | — | 0.044% |

**三项等价性证明（checksum 逐位相同）：**

1. **剪枝无损**：开/关分支限界，checksum 都是 `381506086`，即上界确实可采纳，
   B&B 与完全枚举给出同一组决策，速度约 2x（无剪枝 median 85 us）。
2. **回溯正确**：把原地 undo 换成"整体快照 + 赋值还原"的朴素实现，checksum 不变。
   实测朴素快照**更快**（`SearchState` 约 170 字节 POD，向量化拷贝比分支式
   逐字段 undo 便宜），故最终采用快照版。
3. **ASan / UBSan**：500 回合零报错，checksum 不变。

严格编译零 warning：

```bash
g++ -std=c++17 -O2 -fPIC -Wall -Wextra -Warray-bounds=2 -Wstringop-overflow=4 \
    -shared -o player.so strategy.cpp     # 0 error 0 warning
g++ -std=c++17 -O3 -march=native -fPIC -Wall -Wextra -shared -o /dev/null strategy.cpp
```

### 收益：真实但很小，**不要高估**

联合决策得分逐回合对比（同一批输入）：

```text
DFS 严格更优 163 回合 / 更差 4 回合 / 持平 333 回合
500 轮总得分增益 +178.8（均值 +0.358/轮）
```

得分含 `POT_W` 势场项，折算成真实金币大约 **+3~4%**。这和另一条独立测算吻合：
用当时可见信息做精确 DFS 求上限，**beam 全局只漏了 83 金币（捕获率 96.7%）**。

**因此 README 五附 A 的判断需要修正**：`BEAM_WIDTH=3` 剪掉好路线在理论上成立，
但在真实对局里几乎没有发生。换 DFS 是一次干净的"消除失效模式"，不是提分手段。
真正的瓶颈见 `LOG_ANALYSIS_460712.md`：**信息（21.8% 的回合视野内一枚金币都没有、
视野购买 500 轮零触发）和跨回合路由（中心重访间隔 P90 = 148 轮、
一个角色在 `(2,16)` 蹲了 66 轮）**，不是回合内的搜索。

移除的符号：`BEAM_WIDTH`、`MAX_BEAM_WIDTH`、`MAX_CANDIDATES`、`BeamState`、
`better_beam`；`plan()` 去掉了 `beam_width` 参数。
新增：`reach_bounds()`、`dfs()`、`SearchState`、`reach_gain_`、`inv_gamma_`、
`pow5_`、`scout_max_`、`Mask289::reset`。

> 注意 `pow5_[depth] - 1` 是"连续 depth 个 STAY"的动作编码。`result[depth].code`
> 必须初始化成它——否则被完全堵死时会留下全 0 编码，解码成**六个向上移动**。

---

## 当前实现：低延迟 v2.2 + Reset-Process Index（2026-08-14）

本轮修改以“先于 NPC 执行”为第一目标，同时把首日总结中已经有强证据的
建模问题落到代码。使用仓库内固定的 500 回合合成负载、Python 3.9、同一台
机器进行配对测量：

| 版本 | median | P90 | 相对 P90 |
|---|---:|---:|---:|
| 原 v1（git HEAD 基线） | 1795.9 us | 1944.3 us | 1.00x |
| v2.1 动态角色（beam width 4） | ~760 us | ~890 us | ~2.18x faster |
| 当前 v2.2 Whittle-like（beam width 3） | **~855 us** | **~985 us** | **~1.97x faster** |

这里测的是可复现的策略热路径，不是官方服务器成绩，也不是胜率回测；不同
机器的绝对值不可直接横比。README 原先记录 v1 在另一台机器约 0.9 ms P90，
按本地相对加速比估算，v2.2 在同机仍应处于微秒级；绝对数仍应以上传后的
官方 P90 为准。Whittle 查表带来约 90 us P90 成本，因此默认 beam 从 4 收窄
到 3，使当前固定负载的 P90 保持在 1 ms 内。

宽度 3 是按“延迟第一”选择的默认值。端点多样性剪枝和条件式 residual
replan 仍然保留；尚未用真实固定先手自对弈证明 Whittle-like 目标优先级的
金币提升足以覆盖其约 90 us P90 成本。

复现命令：

```bash
python3 -B benchmark_strategy.py --matches 7
python3 -B -m unittest -v test_strategy.py
```

已完成：

- 按 Liu、Weber、Zhao 的 reset-process 分解，为单个金币格建立“采集/等待”
  被动补贴 Bellman 模型；状态覆盖 0–60 存量、6 档生成率和 3 档竞争风险。
  数值求解结果离线固化为 quarter-unit 常量表，运行时只做 O(1) 查表。
- 把 Whittle-like index 定义为“推迟采集的机会成本”，与 `est` 混合后只用于
  目标势场；beam 和 joint simulator 仍用官方 `ceil(0.65*G)` 结算真实金币，
  不重复把指数当收益。NPC/对手近距和 snapshot 周转/拥挤度只提高等待丢失
  风险，不直接折损我们本轮可以先手拿到的金币。
- 最后 15 轮把无限期 continuation premium 平滑退火到即时拾取价值；已加入
  库存单调性、竞争紧迫性、生成率等待价值、终局退化和势场接入回归测试。
- 每个 20 轮炸弹周期动态定义“侦察/扫雷/干扰者”和“收割者”：优先让低
  持币角色承担风险，周期内保持身份稳定，财富明显反转时换岗；炸弹刷新后
  前 5 轮奖励有用探路和低成本清雷，可见对手附近只加入小幅高置信堵路价值。
  角色效用不禁止拾金，避免为了保持贫穷而浪费全队真实收益。
- 四次无条件 beam 搜索改为“两条边际曲线 + 最多 14 个 `k/order` 联合轻量
  评分”；路径独立时跳过等价的另一执行顺序，仅在争抢/碰撞时做最多两次
  条件式 residual replan，共享金币、炸弹清除、踩踏和己方碰撞只结算一次。
- heap Dijkstra 改为四趟线性 Manhattan max-decay transform；动作压成整数，
  只解码最终候选；beam 默认宽度从 16 降至 4。
- 修复从未见过的格子在第 0 轮凭空累积 14 轮金币，以及对期望浮点金币先
  `ceil`、把 0.05 期望误算成必得 1 金币的问题。
- `CENTER_REGEN` 按首日日志从 0.70 校准为 0.1668；外围格在线学习生成率；
  snapshot 只缩放不确定的新生成量，并把区域耗尽证据持久写回，避免放大
  已观测存量或长期追逐幽灵金币。
- 炸弹记忆在 20 回合边界失效；迷雾炸弹风险按区域、周期相位、角色持币量
  计算；前 25 轮低成本探索，最后 15 轮衰减未来势场并提高风险权重。
- 计划中的拾取、通行与扫雷只在下一轮实际终点吻合后写入 belief，避免慢手
  时被 NPC/对手改变路径而污染长期记忆。
- 读取 snapshot 的 `enter/leave/occupants/gold_collected`；按 FAQ 修正为“包含
  NPC 的总拥挤度”，不再误当作对手精确位置。
- 支持 list、flat buffer、ctypes 17x17 grid；保留任何异常时的合法全停兜底。

尚未声称完成：真实固定先手自对弈、中心 12 轮巡逻环的 A/B、正式地图的
热点学习收益。

### 官方 `game_api.h` 编译验证（2026-08-14，已通过）

已用官方 `game_api.h` + 官方 `Makefile` 完成编译验证，修复了 3 个会在官方
服务器上直接失败、但本地完全看不出来的问题。详见下面「提交打包」一节。

```text
g++ -std=c++17 -O2 -march=native -fPIC -Wall -shared -o player.so player.cpp
→ 0 error，0 warning
nm -D --defined-only player.so | grep moveDecision  →  T moveDecision
./sotest ./player.so  →  PASS calls=500 median=7.1us p90=8.4us p99=12us
python3 -m unittest test  →  Ran 25 tests，OK
```

C++ 版 P90 约 **8.4us**，比 Python v2.2 快约两个数量级，先手几乎稳拿。
（该数字来自 aarch64 沙箱，与 README 上方 Python 表格不同机，不可横比；
正式数据仍以官方机 `make test` 与上传后的官方 P90 为准。）

仍需在官方开发机按 `OFFICIAL_SO_GUIDE.md` 跑一次 ASan/UBSan 与 x86-64 下的
`make test`，再把 `player.so` 视为可上传产物。

---

## 提交打包（官方 game.zip）

官方 zip 含 7 个文件：`game_api.h`、`game_api.py`、`Makefile`、`player.cpp`、
`player.py`、`README.md`、`test.py`。本仓库的对应关系：

| 仓库文件 | zip 内文件名 |
|---|---|
| `strategy.cpp` | `player.cpp` |
| `strategy.py` | `player.py` |
| `test_strategy.py` | `test.py` |

**其余文件（`game_api.h`、`game_api.py`、`Makefile`）保持官方原样，不要替换。**

### 曾经踩过的 3 个坑（已全部修复，勿回退）

**坑 1：官方 `game_api.h` 在全局作用域定义了 `S` 和 `MAX_NPCS`。**
`strategy.cpp` 的匿名 namespace 里原本也有 `constexpr int S = 6;` 和
`constexpr int MAX_NPCS = 16;`。匿名 namespace 的成员在全局作用域可见，于是
所有裸写的 `S` 都变成二义性引用：

```text
error: reference to 'S' is ambiguous
  candidates are: 'constexpr const int {anonymous}::S'
                  'constexpr const int S'   // game_api.h:7
```

已把内部常量改名为 `STEPS` 和 `NPC_CAP`。**新增全局常量前务必先确认没和官方
头文件的 `GRID_SIZE / MAX_NPCS / S / REGION_COUNT` 撞名。**

**坑 2：`moveDecision` 定义带了 `noexcept`，官方声明没有。**

```text
error: declaration of 'GameOutput moveDecision(const GameInput*) noexcept'
       has a different exception specifier
```

C++ 不允许在已有无 `noexcept` 声明后再加 `noexcept`。已去掉 `noexcept`，函数体
内的 `try/catch(...)` 全停兜底保留不变，行为不受影响。

**为什么本地没发现坑 1/2**：`game_api_compat.h` 当初为了「不污染全局命名空间」
用了 `GOLD_RUSH_N/S/A` 这套私有名字，且没有声明 `moveDecision`，正好把两个错误
全遮住了。现已改为与官方头文件**逐字同名**的镜像。**不要再为了整洁改这些名字。**

**坑 3：`make` 之后 `player.so` 会遮蔽 `player.py`。**
Python 的 FileFinder 先找扩展模块再找源码，所以目录里同时有 `player.so` 和
`player.py` 时，`import player` 会加载 `.so` 并报：

```text
ImportError: dynamic module does not define module export function (PyInit_player)
```

`test_strategy.py` / `benchmark_strategy.py` 已改为先试 `import strategy`，失败时
用 `importlib` **按路径**加载 `player.py`，两种目录布局都能跑。
**推论：如果最终提交 Python 版，务必确保包里没有 `player.so`。**

**坑 4：GCC 14 对 `std::sort` 小数组报 `-Warray-bounds` 假阳性。**
官方开发机（`8.153.76.120`）是 **GCC 14**，比本地新。`classify_roles` 里对
`target[4]` 调 `std::sort`：

```text
warning: array subscript 16 is outside array bounds of 'Target [4]' [-Warray-bounds=]
note: at offset 128 into object 'target' of size 32
```

**这是 warning 不是 error，`player.so` 照常生成，代码本身也没有越界**——`count`
由 `for (action = 0; action < 4; ++action)` 限死 ≤ 4。但 GCC ≥ 12 无法穿过内联后的
`std::sort` 证明这一点：`__final_insertion_sort` 里有一条 `__first + 16` 分支，在
这里是死代码，静态分析仍会报。（同文件另一处 `std::sort` 作用于 32 元素数组，
`+16` 确实在界内，所以不报。）

已改为手写降序插入排序：消除 warning，且 n ≤ 4 时比 `std::sort` 更快。校验和
与改前完全一致（`3332927686`），行为未变。

**注意本地 GCC 版本可能低于官方机而漏报。** 打包前建议加严编译一次：

```bash
g++ -std=c++17 -O3 -fPIC -Wall -Wextra -Warray-bounds=2 -Wstringop-overflow=4 \
    -shared -o /dev/null player.cpp
```

### 打包前必跑的验证

```bash
make clean && make            # 0 error 0 warning
nm -D --defined-only player.so | grep moveDecision   # 必须是未改名的 T moveDecision
python3 -B -m unittest test   # 25 tests OK
```

另注：官方 `Makefile` 带 `-march=native`。FAQ 称编译机与运行机一致，因此可用；
但若上传后出现 illegal instruction，第一个要去掉的就是它。

---

## 一、比赛规则

### 1.1 战场

- 17x17 网格，两名玩家各控制 **2 个角色**，出生点在主对角线或副对角线两端。
- 地图含 **障碍物**、**炸弹**、**NPC**。
  - 障碍物格不可进入。
  - 炸弹：角色进入后损失当前持有金币的 **X% = 10%**（向上取整），**炸弹随即消失**，角色可继续行动。
  - 炸弹每 **Y = 20 轮**刷新一次。
- 视野：全局信息不公开，每个角色默认视野为以自身为中心的 **5x5**，可花金币扩展。
- 每 **D = 5 轮**发布一次全局区域快照。

### 1.2 NPC

- 开局全部出生在地图正中央，总数 **A = 7**（公测期）。
- 每个 NPC 每轮最多移动 **B = 3 步**。
- NPC 之间、NPC 与玩家角色之间**可重叠**。
- NPC **参与金币争夺**，但不计入胜负判定。

### 1.3 金币生成

- 中心 9x9 区域**每回合**随机生成金币。
- 9x9 区域外**每若干轮**随机生成金币。

### 1.4 移动与拾取

- 两个角色初始金币均为 0。
- 每轮两个角色共分配 **S = 6 次**移动，可任意分配给两人。
- **非法移动**（越界 / 撞障碍物 / 撞其他玩家角色）不执行，但**后续移动照常进行**（即浪费一步）。
- 进入 NPC 数 **> 2** 的格子，因踩踏损失当前 **N% = 5%** 金币（向上取整）。
- **执行顺序按决策耗时从小到大**。同一格的金币/炸弹由**先执行者**获取/触发。**NPC 的行动顺序位于两名玩家之间。**
- 拾取：从其他格移入含金币的格，获得该格 **C% = 65%** 金币，**向上取整**。

### 1.5 胜负

- 固定 **500 轮**，结束时金币总量多者胜。
- **金币相同时，P90 延迟更低者胜。**

### 1.6 编程与时限

- 语言：C++（提交 .so）或 Python（提交 .py）。
- **每轮必须返回合法决策**，格式非法或运行时错误**直接判负**。
- 时限：每轮标准 **300ms**，另有 **T = 60 秒**总思考时间池。单轮超出 300ms 的部分从池中扣除，**池耗尽判负**。

### 1.7 接口速查

```c
struct Position { int row; int col; };
struct NpcInfo  { int id; Position pos; };          // id 跨回合不变，空槽为 0，不可见时 (-1,-1)

struct RegionStat {
    int id;              // 区域编号 1-5
    int enter;           // 窗口内进入该区域的角色次数
    int leave;           // 窗口内离开该区域的角色次数
    int gold_generated;  // 窗口内该区域生成的金币总量
    int gold_collected;  // 窗口内该区域被拾取的金币总量
    int gold_remaining;  // 该区域地面当前剩余金币
    int occupants;       // 该区域当前角色数
};
struct Snapshot { int window_begin; int window_end; RegionStat regions[5]; };

struct GameInput {
    int round;                    // 从 0 开始
    int grid[17][17];             // -5 迷雾 / -3 炸弹 / -1 障碍 / 0 空地 / >=1 金币数量
    Position my_units[2];
    int my_units_gold[2];         // 两角色各自持有金币
    int gold_opp;                 // 对手两角色金币总和
    Position visible_enemies[2];
    int num_visible_npcs;
    NpcInfo visible_npcs[A];
    int snapshot_valid;           // 1=本轮有新快照
    Snapshot snapshot;
};

struct GameOutput {
    int actions[S];  // [0,4]  0=上 1=下 2=左 3=右 4=不动
    int k;           // [0,S]  角色0走 actions[0:k]，角色1走 actions[k:S]
    int order;       // {0,1}  0=角色0先执行
    int vp;          // {0,1,2} 0=不买 / 1=买7x7(2金币) / 2=买9x9(3金币)，费用对局结束后结算
};
```

注：**NPC 不在 grid 中标记**，只通过 `visible_npcs` 提供；视野外统一为 `-5`。

---

## 二、由规则推导出的关键结论

这一节是规则的直接数学推论，是制定策略的地基。

### 2.1 拾取取整规则决定了"广度优先"

拾取为 `ceil(0.65 × G)`：

| 格上金币 G | 拿走 | 剩余 |
|---:|---:|---:|
| 1 | 1 | 0 |
| 2 | 2 | 0 |
| 3 | 2 | 1 |
| 5 | 4 | 1 |
| 20 | 13 | 7 |

**结论：1–2 金币的小堆一步即可 100% 拿走。**

推论 A：**"挤奶"（踩上-退开-再踩）在 G < 3 时收益为零**，2 步换 `ceil(0.65×0.35×G)`，在 G ≲ 20 之前都不如把这 2 步用在新格子上。需要检查 v1 的 beam search 是否过度挤奶。

推论 B：中心区应当按**覆盖问题**而非**追大堆问题**来解（见 2.2）。

### 2.2 中心区的理论上限与最优巡逻周期

由日志：区域 1 生成率 0.1668 金币/可通行格/轮，每 5 轮共 44.75 → **8.95 金币/轮**，反推中心可通行格约 **54 格**。

- 6 步/轮走完 54 格 ≈ 9 轮（纯移动），计入绕障约 **12 轮/圈**。
- 12 轮后每格积累 `0.1668 × 12 ≈ 2.0` 金币 → `ceil(0.65×2) = 2`，**恰好整堆拿走**。

**即：一个纪律严明的 ~12 轮中心巡逻圈，可以把中心区产出几乎 100% 收走。** 这与 v1 的行为直接冲突——v1 是被势场牵引的 6 步贪心 beam search，重访间隔方差极大，有些格 3 轮踩一次（只拿 0.5 金币），有些 40 轮才回去（早被 NPC 拿走）。

### 2.3 全场金币上限与 NPC 的支配性

| 区域 | 金币/5轮 | 金币/轮 |
|---|---:|---:|
| 1（中心 9x9） | 44.75 | **8.95** |
| 2–5（外围合计） | 48.73 | 9.75 |
| **全场合计** | **93.48** | **18.70** |

500 轮全场总产出 ≈ **9350 金币**。

但注意移动量对比：

| 方 | 每轮步数 | 占比 |
|---|---:|---:|
| NPC（7 个 × 3 步） | 21 | **64%** |
| 我方（2 角色共享） | 6 | 18% |
| 对手 | 6 | 18% |

**NPC 的总移动量是我方的 3.5 倍。** 无法在覆盖率上赢过 NPC，只能在**目标选择**（去价值密度最高处）和**执行顺序**（比 NPC 先到）上赢。且 NPC 全部出生于中心——中心是 NPC 密度最高的区域。

**这条大幅提升了外围突袭的相对价值**（见 5.5），也意味着中心区实际可得远低于 8.95/轮。

### 2.4 执行顺序：比 NPC 先动是核心杠杆

顺序为 `快的玩家 → NPC → 慢的玩家`。决策快 = 在 7 个 NPC 之前拿走同一格的金币。v1 注释中实测"强制先手价值 +40%~+100% 金币"，与此机制吻合，该判断应当保留。

### 2.5 时间池的 P90 漏洞（可利用）

- 可持续预算：`300ms + 60s/500轮 = 420ms/轮`。
- 但胜负平局判定看 **P90 延迟**，即第 450 慢的那一轮。

**因此：可以有 10% 的回合任意慢，而 P90 完全不受影响。** 保守取 8%（40 轮），每轮可用 `60s/40 = 1.5s`（300ms 标准 + 1.2s 扣池），总扣池 48s < 60s，安全。

代价是这 40 轮会后手。所以应当**只在"后手不吃亏"的回合下重注**——即 `visible_enemies` 全不可见、且身边没有被争夺的大堆时。此时深搜免费。

### 2.6 炸弹损失是百分比 → 越早挨炸越便宜，且应当把金币集中

- 损失 = `ceil(0.10 × 该角色当前持有金币)`。开局持有 0 时，**踩炸弹完全免费**。
- 炸弹触发后**消失**，即角色可以主动扫雷。

**推论 A（时序）：前 ~25 轮应当彻底无视炸弹**，用零成本的鲁莽换取地图信息，这些信息在剩下 475 轮里持续变现。

**推论 B（分工）：金币是按角色分开记录的（`my_units_gold[2]`），而胜负看总和。** 所以最优是让**一个角色刻意保持贫穷**，专职探路/扫雷/闯高风险区（挨炸几乎不损失），另一个角色富有并且只走已清干净的安全格。总损失 = `Σ 0.1 × 挨炸时的持有量`，把持有量集中到挨炸次数最少的角色上即可最小化。这是规则里一个明确但不显然的可利用点。

### 2.7 快照里有 4 个字段 v1 完全没读

`RegionStat` 提供 `enter` / `leave` / `occupants` / `gold_collected`，v1 只用了 `gold_remaining`。

- **`occupants` = 该区域当前玩家角色与 NPC 总数。** 它能衡量区域拥挤度，
  但不能直接还原对手位置；需扣除我方和可见 NPC 后仍只得到混合残差。
- **`gold_collected` 减去我方已知拾取量 = 对手+NPC 在该区域的拾取量**，可用于估计对手活动强度与 NPC 分布。
- 恒等式 `gold_remaining[t] = gold_remaining[t-1] + gold_generated - gold_collected` 可用来校验并反推不可见量。

---

## 三、日志实证规律（摘要）

完整分析见 `LOG_FINDINGS_GOLD_BOMB.md`。要点：

- **区域划分**：区域 1 = 中心 9x9 `[4,4]–[12,12]`；区域 2–5 为风车状外围带。
- **中心稳定、外围爆发**：中心 99.9% 的 5 轮窗口都有产出；外围约 65% 窗口不产，11–12% 窗口大产（>20），**大产窗口贡献了外围 ~82% 的金币**，单次常见 80–112 金币。
- **外围热点**：地图模板值 `2` 的格子，每个外围区固定 5 个，被看见时有金币的概率 **19.5%**，而普通外围空地仅 **1.65%**（约 12 倍），平均金币量约 27.6 倍。**但初赛换图，坐标必须在线学习，不可硬编码。**
- **炸弹 20 轮整体重采样**：周期内不新增炸弹；跨 `round%20: 19→0` 后旧位置保留率仅 8.4%，相邻周期 Jaccard 重合度约 2.5%。
- **炸弹密度**：中心 3.00%，外围 5.35–6.35%；周期内从刷新时 6.39% 衰减到 `%20==15` 时 4.77%。

---

## 四、原策略 v1 的缺陷清单（历史基线）

下表记录首日复盘时的 v1 状态；本页顶部“低延迟 v2”列出了本轮已落地项。

| # | 缺陷 | 位置 | 性质 |
|---|---|---|---|
| 1 | 炸弹记忆不随 20 轮周期清空，只在重新看见时才更新 | `_observe` | **Bug** |
| 2 | 格子一旦被看过 `risk[i]` 永久置 0，迷雾格不计炸弹风险 | `_observe` / `_estimate` | **Bug** |
| 3 | `OUTER_REGEN = 0.05` 均匀，无法体现 12 倍热点 | CONFIG | 建模缺失 |
| 4 | 快照 `gold_remaining` 被均摊到整个区域，80 金币爆发被稀释成每格 +0.6 | `_estimate` | 建模缺失 |
| 5 | `enter`/`leave`/`occupants`/`gold_collected` 完全未使用 | — | 信息浪费 |
| 6 | `POT_W` 到最后一轮仍为 0.35，终局仍在"走向"远处金币 | `_plan` | 建模缺失 |
| 7 | 风险厌恶不随持有金币/回合数变化，开局免费的鲁莽没有利用 | CONFIG | 建模缺失 |
| 8 | 只用 300ms 预算的 0.3%（~0.9ms），时间池 60s 完全未动 | 全局 | 资源浪费 |
| 9 | 规划视野恰好 1 轮（6 步），产生不了巡逻行为 | `_plan` | 结构限制 |
| 10 | 两角色对称，未利用"贫穷角色挨炸免费"的分工 | `_decide` | 策略缺失 |
| 11 | 视野购买用收入 EMA 拍脑袋，非信息价值（VOI）驱动 | `_vision` | 建模缺失 |
| 12 | `COMP_DISCOUNT` 被关掉（=1.0），但 NPC 占 64% 移动量，竞争是真实的 | CONFIG | 待重推 |

---

## 五、改进路线图（按 ROI 排序）

### 第一梯队：便宜且必做

**5.1 炸弹记忆按 20 轮周期清空**（缺陷 1）
日志显示跨周期重合度仅 2.5%，即每次跨越 `round%20==0` 后，几乎**所有**记忆中的炸弹都是错的。v1 既在绕开幻影炸弹，更糟的是把上周期"排查干净"的格子当成安全格，而那里现在可能有雷。
*改动*：`round % 20 == 0` 时清空 `bomb[]`。约 3 行。

**5.2 迷雾格必须计炸弹风险**（缺陷 2）
`risk[i]` 首次观测后永久归零，30 轮前看过的格子被当作确定无雷。实际常驻雷率中心 3.0%、外围 5.3–6.4%，且随周期相位变化。
*改动*：对非本轮可见格，`risk[i] = P_bomb(区域, 周期相位) × 0.10 × 该角色持有金币`。注意这使风险**依赖角色且随金币缩放**。
*收益*：终局尤其大——持有 200 金币时每步迷雾的隐含成本是 `0.05 × 0.10 × 200 = 1.0` 金币，与一整格的拾取量相当，目前完全未计价。

**5.3 阶段化风险：开局免费鲁莽 + 终局势场归零**（缺陷 6、7）
- 前 ~25 轮：`FOG_STEP → 0`，彻底无视炸弹，做一次高强度地图测绘冲刺（依据 2.6）。
- 最后 ~15 轮：`POT_W_eff = POT_W × min(1, 剩余轮数/15)`，同时抬高风险厌恶。
*改动*：约 15 行，纯收益。

**5.4 读取快照的另外 4 个字段**（缺陷 5，v2 已完成）
FAQ 已确认这些字段包含 NPC，因此不能当作对手精确位置。v2 将
`occupants + 0.25 × max(enter-leave, 0)` 作为区域总拥挤度，并保留生成、拾取
和剩余量。

### 第二梯队：两个战略级增益

**5.5 快照驱动的外围突袭模式**（缺陷 4）
外围占全场 52% 的产出，其中 82% 来自爆发窗口（单次 80–112 金币）。而 NPC 主要盘踞中心（2.3），外围爆发的金币会**站在地上等人来拿**。
*改动*：把快照当作**模式切换信号**而非先验微调。当某外围区 `gold_remaining` 越过阈值时派遣角色突袭。
*账要算清*：中心→外围往返约 12–16 步 ≈ 2–3 个角色轮，代价约 10–13 金币的中心机会成本，博 80 金币的奖励——**6–8 倍回报**。
*这大概率是单项最大的提升。*

**5.6 外围热点在线学习**（缺陷 3，v2.2 已完成轻量版本）
当前每格用 capped EMA 维护金币到达率，并把它映射到 6 档 Whittle-like 模型；
快照重标定按该生成率权重分配而非均摊。后续有固定先手回测后，再判断是否
值得换成 Beta/Gamma 完整后验。
*与 5.5 相乘增益。*

**5.7 中心区改为分区巡逻**（缺陷 9）
依据 2.2，中心是覆盖问题。多机器人巡逻文献的结论是：**智能体少、区域紧凑时，分区（partition）优于共享环路（cyclic）**——即给每个角色分配中心 9x9 的一半，各自跑自己的紧凑巡逻圈，大堆出现时允许贪心偏离。实现便宜，容易 A/B。

**5.8 贫富分工**（缺陷 10，v2.1 已完成第一阶段）
v2.1 不固定角色编号，也不强迫侦察者放弃金币。每个炸弹周期优先选择低持币
角色承担探路、低成本扫雷与机会型堵路，高持币角色保持原收割目标；财富差
明显反转时才换岗，避免每回合抖动。仍需用固定先手自对弈验证扫雷收益是否
覆盖角色塑形带来的路径机会成本。

### 第三梯队：时间优化

**5.9 用 P90 漏洞做非对称时间分配**（缺陷 8）
不要改成"每轮都慢"——先手价值 +40~100% 的结论是对的，且 P90 还是平局判据。正确做法是 2.5 的非对称方案：

- **92% 的回合保持 ~1ms**（先手、低 P90）。
- **8% 的回合（约 40 轮）用到 1.5s 深搜**，触发条件为 `visible_enemies` 全不可见 **且** 附近无争夺中的大堆——此时后手不吃亏，深搜是净赚。
- 硬性护栏：总扣池上限设 50s（留 10s 余量），且搜索必须是 **anytime** 的，随时可截断并返回合法决策；保留现有的 all-stay 兜底。

**5.10 深搜回合上跑 anytime 规划器**
一旦 5.9 释放出 1.5s，beam search 的形状就不对了：

- **RHEA**（阅读清单 E）：直接演化 `actions[6] + k + order` 这个输出向量，天然 anytime，且把 `k` 和 `order` 作为基因组的一部分，而不是外层 7×2 的枚举循环。**最容易移植。**
- **DESPOT**（阅读清单 D）：如果要对迷雾和炸弹做真正的信念空间规划，而非对点估计做规划。

同时把规划视野从 6 步（1 轮）延长到 12 步（2 轮）——巡逻行为会自己涌现。

**5.11 常规回合的微优化（v2 第一阶段已完成）**
v2 已用四趟线性 max-decay transform 替换 heap Dijkstra，并将四次 beam 降为两次。
后续若仍需压缩 Python P90，可评估按信念变化跳过势场重算；几十到一百微秒
目标优先转 C++，不再以牺牲路径质量的窄 beam 作为主要手段。

### 第四梯队：基础设施（应尽早做，否则上面全部无法度量）

**5.12 配对自对弈评测台**
v1 注释承认调参时延迟是混杂因素，这让所有已报告的数字都存疑。建议：N ≥ 200 局，配对随机种子，交换出生角，**强制固定执行顺序**以隔离决策质量；再跑一遍带真实延迟的，单独给顺序效应定价。
**核心 KPI：金币/轮，对标 2.3 的 18.70 上限**，带置信区间。目前没人知道 v1 是发挥了上限的 40% 还是 80%，这让每个调参决定都是盲的。

**5.13 用 CMA-ES 替代手工调参**
CONFIG 块有 ~15 个可调项，逐个手扫会陷入局部最优且漏掉交互（`GAMMA` × `POT_W` 强耦合）。有了 5.12 的低噪声适应度信号后，CMA-ES 或 SMAC 是一个周末的工作量。

---

## 五附、`TOP/suggestion.md` target-route 补充方案可行性分析（2026-08-15）

**结论：诊断正确，方案方向可行，但提议的实现方式在本仓库当前代码上是不安全的，
且它的时延论证已经过时。建议先做两个更便宜的实验，再决定是否落地。**

> **2026-08-16 后记**：本节 A 的推导（好路线在 width 3 被剪掉）在理论上正确，
> 但用 `game_460712.log` 实测后发现**在真实对局里几乎不发生**——beam 全局只漏了
> 83 金币，捕获率 96.7%。D 节建议的"实验 1：宽度 3→8"已被更彻底的做法取代：
> 直接换成精确 DFS（见本文件顶部 v2.3 一节），失效模式被结构性消除，
> 但提分只有 +3~4%。**这一整节的优先级应下调。**

### A. 它诊断的偏差是真实的，可以量化

beam 打分为 `score = utility + POT_W * pot[终点]`，其中
`pot[c] = max_target priority[t] * GAMMA^dist(c,t)`，当前 `GAMMA=0.72`、`POT_W=0.35`。

朝一个价值 `V`、距离 `d` 的目标走一步，势场项只增加：

```text
ΔScore = POT_W · V · γ^d · (1/γ − 1) = 0.1361 · V · γ^d
```

| 目标距离 d | γ^d | 每步拉力（V=20） |
|---:|---:|---:|
| 2 | 0.518 | 1.41 |
| 3 | 0.373 | 1.02 |
| 4 | 0.269 | 0.73 |
| 6 | 0.139 | **0.38** |

而旁边一个 1 金币的格子，因为 `ceil(0.65×1)=1`，**立刻给 1.0 金币**。

于是在搜索中途（depth 3）比较两条路线：

```text
奔向 6 步外 V=20 大堆：utility≈0，势场 0.35×20×γ³ = 2.61  → 合计 2.61
沿途捡 3 个 1 金币小格：utility=3，势场 ≈0.50            → 合计 3.50
```

**贪心路线中途得分更高，而 `BEAM_WIDTH=3` 只保留 3 条**——奔向大堆的路线会在
真正吃到 `ceil(0.65×20)=13` 之前就被剪掉。suggestion 描述的偏差确实存在。

**根因**：`γ=0.72` 同时在做两件事——「本回合走过去的成本」和「跨回合的不确定性」。
但 6 步内的目标**本回合就能吃到**，`γ⁶=0.139` 把它折成面值的 14% 是严重低估。
这两种折扣需要不同的速率。

### B. 但提议的路线生成方式不安全（这是真正的可行性阻碍）

suggestion 写的是「用 `dist[start][target] <= budget+1` 过滤，再对每个目标生成
一条最短路方向路线」。问题在于本仓库里：

- `dist_[i][j]` 是 **曼哈顿距离**（`strategy.cpp:347`），不绕障碍；
- `pot_` 是 **可分离的曼哈顿 max-decay 四趟扫描**（`strategy.cpp:669`），同样完全
  不感知障碍物（v1 的 obstacle-aware Dijkstra 在 v2.2 为了提速被换掉了）。

所以「沿曼哈顿方向贪心走向目标」会直接撞墙。而**非法移动不报错、只是静默浪费
一步**，即一次撞墙损失 6 步预算的 1/6。更糟的是目标可能真实不可达：曼哈顿距离 4，
中间隔一堵墙，真实图距离可能是 10 或无穷。

障碍密度不低，且恰好在最需要绕路的图上最高：

| 地图 | 可通行格 | 障碍数 | 障碍占比 |
|---|---:|---:|---:|
| 地图 1 | 249 | 40 | 13.8% |
| 地图 2 | 265 | 24 | 8.3% |
| 地图 3 | 211 | **78** | **27.0%** |

**修正方案**：不要用曼哈顿贪心，改成**从每个角色位置做一次深度 ≤6 的 BFS**。
半径 6 的菱形只有 `2·6·7+1 = 85` 格，两个角色合计约 170 次访问，亚微秒级。BFS 一次
性给你三样东西：真实绕障距离、父指针（直接得到真实最短路，不用猜方向）、以及
自动剔除不可达目标。这比 suggestion 的方案更简单**且**更正确。

### C. 它的时延论证是针对 Python 写的，在 C++ 版已经过时

suggestion 花了大量篇幅设计 4 条启用条件来控制时延（外围快照高、目标明显更优、
收获重叠、非终局）。那是 Python v2.2（P90 ≈ 985us）时代的顾虑。

C++ 版实测 **P90 ≈ 8us**，而单轮预算 300ms。实测 beam width 扫描：

| BEAM_WIDTH | median | P90 | 占 300ms 预算 |
|---:|---:|---:|---:|
| 3（当前） | 7.25us | 8.71us | 0.003% |
| 4 | 9.21us | 10.75us | 0.004% |
| 6 | 12.75us | 15.25us | 0.005% |
| 8 | 17.46us | 20.25us | **0.007%** |

四档的 checksum 各不相同，说明宽度确实改变决策，不是空转。

**因此 C++ 版不需要那 4 条启用条件**——直接常开，只留一个 A/B 开关即可。加上这
套 gating 只会增加分支和状态，还让 A/B 结果更难解释。

### D. 更便宜的两个实验，应该先做

**实验 1：把 `BEAM_WIDTH` 从 3 调到 8。** 改 1 个常量，代价 +11.5us P90（0.004%
预算）。当前的 3 是在 Python 时延压力下选的，那个约束在 C++ 里已经不存在。
注意 `MAX_BEAM_WIDTH = 8` 是数组上界，要超过 8 需要同时改它。
**这直接缓解 A 节里「好路线被剪掉」的问题，而且零新增代码。**

**实验 2：修根因，而不是加补丁。** 让 6 步内可达的目标不被 `γ^d` 过度折扣——
例如对 `d ≤ 6` 改用更接近 1 的折扣率，`d > 6` 保持 `γ=0.72`。这是改一个公式，
可能吃掉 target-route 模块的大部分收益，且不引入新模块。

只有当实验 1、2 都做完仍有明显缺口时，才值得上 target-route 模块。

### E. 一个已经存在、可以复用的东西

`pot_source_[NN]`（`strategy.cpp:284`）已经记录了每个格子的势场是由**哪个目标**
产生的。如果最终要做 target-route，候选目标的选取可以直接读 `pot_source_[start]`，
不必重新扫全图选 top-k。注意它同样是曼哈顿口径，只能当候选生成器，不能当可达性判据。

### F. 落地时的最小形态

若实验 1、2 之后仍要做，建议：

```text
1. 从两个角色位置各做一次 depth<=6 BFS（obstacle-aware，带父指针）
2. 在 BFS 可达集合内按 priority_ 取 top 6-8 个目标
3. 每个目标用父指针回溯出唯一一条真实最短路
4. 用现有 harvest/joint_score 逻辑给这些路线打分
5. candidate_routes[unit][budget] = {beam best} + {最多 1 条 target route}
6. 常开，不加 gating；用一个编译期开关做 A/B
```

预计新增 P90 约 5–15us，仍在 30us 量级，先手无忧。

**风险提示**：suggestion 结尾说「若实测不好直接关掉，损失很小」——这点成立，
前提是**必须先有 5.12 的配对自对弈评测台**。目前没有可信的金币指标，无法判断
这个模块（或任何改动）到底是赚是亏。**这仍然是整个路线图最关键的缺口。**

---

## 六、论文阅读清单

### A. 核心结构：离开后会重新生长的奖励 —— 最匹配的一类文献

格子无人访问时继续生成、访问后留下 35%，属于**带部分重置的 restless
bandit**。论文的二元状态/完整信息重置与赛题不完全相同，因此闭式公式不能
直接照搬；v2.2 使用相同的被动补贴定义，对多值库存做数值求解。

- Liu, Weber & Zhao (2011), *Indexability and Whittle Index for Restless Bandit Problems Involving Reset Processes* — [PDF](http://www.statslab.cam.ac.uk/~rrw1/publications/Liu%20-%20Weber%20-%20Zhao%202011%20Indexability%20and%20Whittle%20Index%20for%20restless%20bandit%20problems%20involving%20reset%20processes.pdf)。**优先读这篇**，动态与本赛题完全对应。
- Avrachenkov & Borkar, *Whittle index based Q-learning for restless bandits with average reward* — [arXiv:2004.14427](https://arxiv.org/pdf/2004.14427)。生成率未知（外围区）时在线学习指标。
- Nakhleh et al., *NeurWIN: Neural Whittle Index Network* — [NeurIPS 2021](https://proceedings.neurips.cc/paper/2021/file/0768281a05da9f27df178b5c39a51263-Paper.pdf)。仅在走学习策略路线时需要。

**v2.2 已实现**：每格指标
`λ(i)=f(预估存量_i, 生成率_i, 被竞争者采走的风险_i)`，作为标量目标优先级
进入 max-decay 势场；连续路径、双角色分步、炸弹与碰撞仍交给 beam/joint
simulator，不把论文的 Top-K 假设错误地套到六步路径上。

### B. 移动预算下的路径规划（每轮 6 步的子问题）

"从当前位置出发、长度 ≤6 的游走、最大化收集奖励"= 2 车辆的 Team Orienteering Problem。

- Chao, Golden & Wasil (1996), *The team orienteering problem* — [EJOR](https://www.sciencedirect.com/science/article/abs/pii/0377221794002894)
- Vansteenwegen et al. (2011), *The orienteering problem: A survey* — [EJOR](https://www.sciencedirect.com/science/article/abs/pii/S0377221710002973)
- 2025 综述，模型演进与算法进展 — [arXiv:2512.16865](https://arxiv.org/pdf/2512.16865)
- Hammami, Rekik & Coelho, *Hybrid ALNS for the TOP* — [C&OR 2020](https://www.sciencedirect.com/science/article/abs/pii/S0305054820301519)。ALNS 的破坏/修复结构很适合 anytime 预算。

**要点**：6 步预算小到几乎可以精确求解，难点不在路线而在**奖励估计**（即 A 类）。

### C. 持续监测 / 巡逻（对应 5.7 中心巡逻）

- Cassandras, Lin & Ding, *Optimal control for multi-agent persistent monitoring* — [Automatica 2013](https://www.sciencedirect.com/science/article/abs/pii/S0005109814001411)
- *A sub-modular receding horizon solution for mobile multi-agent persistent monitoring* — [arXiv:1908.04425](https://arxiv.org/pdf/1908.04425)。最接近可直接实现的形态：滚动视界 + 次模贪心 + 有界时间。
- *ε-Optimal Multi-Agent Patrol using Recurrent Strategy* — [arXiv:2509.11640](https://arxiv.org/pdf/2509.11640)
- Portugal & Rocha, *A Survey on Multi-robot Patrolling Algorithms* — [链接](https://www.researchgate.net/publication/220832895_A_Survey_on_Multi-robot_Patrolling_Algorithms)。环路 vs 分区策略的对比，直接对应两角色如何分工。

### D. 部分可观测下的规划

- Silver & Veness (2010), *Monte-Carlo Planning in Large POMDPs*（POMCP） — [NeurIPS PDF](https://papers.neurips.cc/paper_files/paper/2010/file/edfbe1afcf9246bb0d40eb4d8027d90f-Paper.pdf)
- Ye, Somani, Hsu & Lee, *DESPOT: Online POMDP Planning with Regularization* — [arXiv:1609.03250](https://arxiv.org/pdf/1609.03250)。在**固定实时预算**下优于 POMCP，且带正则化以防对采样场景过拟合——当你对迷雾格的信念主要来自先验时，这一点很关键。

### E. 实时预算内的博弈搜索（对应 5.10）

- Gaina, Lucas & Perez-Liebana, *Rolling Horizon Evolutionary Algorithms for General Video Game Playing* — [arXiv:2003.12331](https://arxiv.org/pdf/2003.12331) / [ToG'21 PDF](http://www.diego-perez.net/papers/RHEAforGVGP_ToG21.pdf)。**清单中最容易直接移植的算法**：演化的就是动作序列，正好是本赛题的输出格式。
- Perez-Liebana et al., *Rolling Horizon NEAT* — [arXiv:2005.06764](https://arxiv.org/pdf/2005.06764)

### F. 最接近的公开赛题：Pommerman

部分可观测网格、炸弹、双角色队伍、实时预算。NeurIPS 2018 前排是 **MCTS 而非深度 RL** —— 这对精力投向是个有用的先验。

- *Developing a Successful Bomberman Agent* — [arXiv:2203.09608](https://arxiv.org/pdf/2203.09608)
- Gao et al., *Skynet: A Top Deep RL Agent in the Inaugural Pommerman Team Competition* — [链接](https://www.researchgate.net/publication/332897569_Skynet_A_Top_Deep_RL_Agent_in_the_Inaugural_Pommerman_Team_Competition)
- *Know your Enemy: Investigating MCTS with Opponent Models in Pommerman* — [arXiv:2305.13206](https://arxiv.org/pdf/2305.13206)
- [MultiAgentLearning/playground](https://github.com/MultiAgentLearning/playground) — 参考环境

### G. 该往哪看：探索与视野购买（对应缺陷 11）

- Singh, Krause, Guestrin, Kaiser & Batalin, *Nonmyopic Adaptive Informative Path Planning for Multiple Robots* — [IJCAI'09](https://www.ijcai.org/Proceedings/09/Papers/306.pdf)。多机器人次模定向问题，正是本赛题的双角色探索问题。
- Krause & Golovin, *Submodular Function Maximization* 综述 — [PDF](https://viterbi-web.usc.edu/~shanghua/teaching/Fall2023-670/krause12survey.pdf)。提供 `(1-1/e)` 贪心保证，从而可以只用一个廉价的贪心信息增益项，不必上复杂方法。
- *Informative Path Planning with Limited Adaptivity* — [arXiv:2311.12698](https://arxiv.org/pdf/2311.12698)

---

## 七、下一步待办

### 应立即从日志中补做的分析

1. **各区域 `gold_collected / gold_generated` 比值。** 若中心接近 100%、外围仅约 30%，说明外围有站着不动的存量金币，5.5 的突袭价值还要再上调。这是当前最该跑的一个分析。
2. **NPC 移动模型**：是随机游走还是趋向金币？决定 `COMP_DISCOUNT`（缺陷 12）如何重推。
3. **v1 实测金币/轮**，对标 2.3 的 18.70 上限。没有这个数，路线图里所有优先级都只是推测。
4. **检查 beam search 是否过度挤奶**（依据 2.1 推论 A）。

### 规则核对状态

- 已确认：拾取按 65% **向上取整**；玩家和 NPC 使用同一拾取规则。
- 已确认：snapshot 的 `occupants/enter/leave` **包含 NPC**。
- 已确认：四名玩家角色不能重叠；一个角色整段走完后另一角色再执行。
- 已确认：`vp` 在**下一轮**生效、持续一轮，可连续购买。
- 仍属日志强先验而非正式承诺：炸弹是否在正式阶段严格按
  `round % 20 == 0` 整体重采样。v2 将该行为参数化为当前先验，换图后应复核。
