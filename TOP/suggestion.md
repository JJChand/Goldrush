会有时延影响，但可以控制到很小；而且你说得对，**6 步让完整 TOP 的发挥空间有限**。所以我不建议上“完整路线池 TOP”，而建议上一个很薄的 **target-route supplement**：只补 beam 容易漏掉的“高 priority 目标导向路线”。

**为什么仍可能有用**
6 步短，确实意味着复杂插入、2-opt、ALNS 这些大招基本用不上。但 TOP 思路在这里的价值不是深度优化，而是纠正 beam 的一个偏差：

> beam 是从当前位置一步步扩展，容易被局部收益和 terminal potential 牵引；  
> target-route 是先锁定高净值点，再问“有没有一条 6 步内能吃到它的路线”。

也就是说，它不是替代 beam，而是给规划器加几条“直奔高价值点”的备选路线。

**时延怎么控**
不要为每个目标跑 BFS，也不要枚举很多组合。我们有 17x17、固定 6 步，可以非常便宜地做：

```text
每个角色：
  选 top 6-8 个候选目标
  只保留 dist[start][target] <= budget + 1 的目标
  对每个目标生成 1 条最短路方向路线
  可选：对 top 2 目标尝试一个二目标串联
```

这样每个角色最多新增十来条路线。真正耗时的 `_joint_score` 也可以限制组合数：

```text
旧 beam 每个 k 只有 1 条
target routes 每个 k 最多 2 条
总组合大约 7 * 3 * 3 * 2 = 126 次 joint_score 上限
```

如果担心 P90，可以更保守：

```text
只在以下情况启用：
1. snapshot 显示外围剩余/生成很高；
2. top priority 目标距离 <= 6 且明显高于 beam 当前路线收益；
3. 两个角色 beam 路线发生收获重叠；
4. 当前回合不是高压终局。
```

第一版甚至可以只做：

```text
每个角色生成 top 4 direct-target routes
只和 beam best 做竞争
不做二目标插入
```

这个开销很低。

**6 步下 TOP 的合理形态**
完整 TOP 假设路线可以经过很多点；GoldRush 6 步下，一条路线通常只解决三类事：

```text
1. 直达一个高价值目标；
2. 顺路吃一个中价值点后到目标；
3. 在中心小范围扫 2-4 个相邻格。
```

所以不要做复杂路线生成。最适合的是：

```text
direct route:
  start -> target

one-insertion route:
  start -> side_point -> target
  要求 side_point 基本顺路，额外步数很小

cluster route:
  在目标附近 1-2 格内继续找 priority 高的邻居
```

**我的建议**
在 `strategy4.cpp` 里先不要大改。加一个小模块即可：

```text
build_target_routes(unit, budget):
  从 priority_ 选 top targets
  对每个 target 生成一条 greedy shortest path
  沿路用现有 joint_score/harvest 逻辑评分
  每个 budget 只保留 best 1-2 条
```

然后组合阶段变成：

```text
candidate_routes[unit][budget] =
  {beam_route} + {最多 1 条 target_route}
```

这就够了。它不会让问题变成“枚举情景”，也不会明显拖慢时延，但能测试 B 方向是否真的有收益。

预期效果我会保守估计：平均收益可能只是小幅提升，但在“高 priority 点离当前位置 3-6 步、beam 被近处小收益吸走”的局面，提升会明显。若实测 P90 或收益不好，直接关掉这个 supplement，损失很小。