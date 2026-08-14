# 命中率根因与 DSH 缓存设计复刻（v0.19 研究 + 修复记录）

> 日期：2026-08-14 ｜ 状态：核心修复已实施并真实验证 ✅

## 1. 现象与数据

修复前实测：57 条请求日志、总命中率 **13.5%**（hit 10112 / prompt 74710）。

逐条分析请求日志，发现一个极其规整的模式：
- **hit 永远是 384 / 512 / 1280 这种小块**，且同一会话内多轮请求 hit 值**不增长**（c84e66a3f：hit=512 → hit=512，miss 却从 3876 涨到 7716）
- hit 值在不同会话、不同天之间高度一致 → 命中内容 = 跨会话共享的**稳定区**（人设+技能 ≈ 384-512 token）
- **历史区（每轮增长的部分）从不贡献命中**；偶尔出现的 1280 是相邻块偶然对齐
- 缓存存活性证据：间隔 258 分钟的请求仍命中 384 → 缓存 TTL 至少数小时

结论：缓存机制本身工作正常，是我们的**线序结构**把命中率锁死在「只有稳定区」的水平。

## 2. 根因：波动区插位导致前缀链断裂

DeepSeek（及同类）缓存按**请求开头的 token 序列**逐块匹配，第一个不同的 token 之后全部作废。

修复前的组装顺序：

    [稳定区][低频区][历史…][环境时间戳][检索记忆][当前消息]
                         └──────┬──────┘
                            每轮必变的尾部

第 N 轮与第 N+1 轮对比：

    第 N 轮:  [S][L][历史1..N-1][volatile][retrieved][current=用户N]
    第 N+1 轮:[S][L][历史1..N-1][用户N][助手N][volatile'][retrieved'][current=用户N+1]
                              ↑ 断裂点

第 N 轮在该位置缓存的是 **volatile（时间戳）**，第 N+1 轮在该位置是**新历史** → 从这里开始全部不匹配 → 只有 [S][L]（稳定+低频）命中。每轮动态内容**插在历史与当前消息之间**，等于在前缀链上打了一堵墙。

数据验证：c84e66a3f 第 2 轮 hit=512 = 稳定区；miss=7716 = 历史+尾部全部重算。与机制预测**完全吻合**。

## 3. DSH 的对应设计（第一手材料）

研究 deepseek-harness 的 .agents/notes 设计记录与 docs/，其缓存哲学与我们踩的坑形成精确对照：

### 3.1 append-only 尾部原则（DSH 核心）

DSH note《session-prefix》与《unified-send-and-coalesced-user-messages》的原话：
- 「会话内冻结的内容走前缀（system 之后、历史之前）——*the provider's prompt cache holds by construction*」
- 「会话中途变化的内容走 **append-only 历史通道**——*paid once and prefix-cached thereafter*」
- 动态注入上下文一律作为追加的 user/message（尾部），**永不插到历史与当前轮之间**

我们的修复就是这条原则：volatile/检索结果从「历史与 current 之间」移到「current 之后」的绝对尾部。

### 3.2 辅助请求 = 主请求的 prefix-extension

DSH note《compaction-summary-prefix-cache-reuse》：摘要请求**复用主对话请求的前缀**（system+tools 逐字节一致），整理指令作为**最后一条 user 消息**追加；换一个 system 头 = 第一个 token 就变 = 全盘失效。备选方案全部被否（换头/去掉 tools/独立 system）。

对我们的启示（P2）：起标题、记忆摘要、抽取、建议这些辅助 LLM 调用目前都用独立 prompt → 每次全 miss。摘要指令尾部化成本高（输入是旧消息不是主前缀），但**起标题/建议**这类「读同一段历史」的调用可复用主前缀。

### 3.3 每个请求是日志的纯函数

DSH《reconstructable-requests》：所有请求必须能从会话日志字节级重建，缓存才成立。我们已有对应物：前缀指纹（稳定+低频 SHA-256）+ 确定性消息序列化 + 固定顺序合并。本次线序修复后这条性质才真正覆盖波动区。

### 3.4 命中率口径

DSH：cacheHitRate = cacheRead / (input + cacheRead + cacheWrite)（缓存写也算计费输入）。DeepSeek 目前只报 hit/miss 两桶，我们 hit/(hit+miss) 等价。

## 4. 复刻对照表

| DSH 设计 | Gili 现状 | 结论 |
|---|---|---|
| append-only 尾部（动态内容挂绝对尾部） | ❌ 插在历史与 current 之间 | **✅ 本次修复（根因）** |
| 会话冻结前缀（system+技能+工具） | ✅ 稳定区 + 前缀指纹守卫 | 已有；**✅ 指纹已升级为信封哈希**（稳定+低频 + model + tools 排序后 JSON）——换模型/换工具集 = 缓存时代切换，自动记账 |
| 辅助请求 prefix-extension（指令尾部化） | ❌ 辅助调用全是独立 prompt | **✅ 摘要已实施**（路由快照 replay + 指令尾部化 + 同 model）；起标题/建议 P2 |
| 请求=日志纯函数 | ✅ 指纹+确定性序列化 | 已有（修复后完整成立） |
| 缓存写也计费（hit rate 口径） | ✅ hit/(hit+miss) | 等价，无需改 |

## 5. 修复与验证

**改动**（2 处）：
1. prompt_builder.build_prompt 波动区顺序：[volatile][retrieved][current] → [current][volatile][retrieved]
2. main.py 知识引用注入：insert(len-1) → append（绝对尾部）

**真实验证**（同一会话 4 轮短消息，v4-flash）：

    第1轮: hit=0   miss=419   （首请求，无缓存可命中，正常）
    第2轮: hit=384 miss=82    → 命中率 82.4%
    第3轮: hit=384 miss=129   → 命中率 74.9%
    第4轮: hit=512 miss=70    → 命中率 88.0%  ← hit 逐轮增长，历史链开始累积命中

对照修复前（hit 恒 384-512、miss 涨到数千）。模拟器预测同会话连续请求对命中率 45.6% → 71.8%，实测单会话 82-88%（短消息历史占比小，长对话收益更大）。**整体命中率预期从 13.5% 提升到 40-60%+**（取决于聊天模式：首请求与跨天 TTL 过期仍会 miss，属机制固有）。


## 5.5 DSH 深挖补充发现（源码仓库级，2026-08-14 子代理报告合并）

1. **前缀稳定是涌现的，不是管理的**（DSH《reconstructable-requests》原话：Prefix-cache stability is corollary #1, not the headline: an append-only log projected by a per-node pure function yields requests that are append-extensions of their predecessors whenever the header is unchanged — stability is emergent, not managed）。我们的对应物：消息表 = 日志，build_prompt = 纯函数，指纹 = header 变化检测。本次线序修复后该性质成立。

2. **DSH 实为两区，不是三区**：低频内容要么稳定化进信封（system），要么 append 到历史尾部，没有中间态。我们的低频区（摘要+事实）本质是「会变的信封后缀」——摘要重写时其后历史整体失效一轮，但替换点之前（稳定区）仍命中，与 DSH 的 replace 语义等价。保留三区设计，但明确：低频区重写 = replace 点，语义正确、已记账。

3. **命中率分母核验 ✅**：DSH 提醒 DeepSeek 的 prompt_tokens 含命中、需减掉 hit 后再算。实测我们 57 条日志 prompt_tokens == hit + miss **恒成立**，且 hit/(hit+miss) 与 DSH 公式（cacheWrite=0 时）等价 → 我们的 13.5% 是真实值而非虚低，修复后 82-88% 同样真实。

4. **thinking 模型的工具轮必须回传 reasoning_content**（DSH 实践）→ 已修复：llm.complete 捕获并回传；工具轮 assistant 消息携带 reasoning_content；空 content 回传空串（非 null，字节稳定）。

5. **缓存域是 provider×model 级**：v4-flash 与 v4-pro 不共享缓存；切换模型后首个请求必然 miss。

6. **压缩阈值参考**（记录在案）：DSH 用 0.8×上下文触发、保留 0.16 尾部、裁最老整段、边界对齐闭合工具对；我们现用预算×0.35 保留窗口，个人场景够用，不迁移。

## 5.6 本轮追加实施（信封哈希 / 摘要 prefix-extension，2026-08-14 二批）

1. **信封哈希**：前缀指纹从「稳定+低频 SHA-256」升级为「稳定+低频 + model + tools（按名稳定排序后 JSON）」。原因：缓存域是 provider×model 级，换模型必须记账失效；tools 集合变化同理。容量参数（history_token_budget）按 DSH 纪律不进信封（容量不是请求内容，且裁剪由 history_trimmed 单独记账）。

2. **摘要请求复用 warm 前缀**（DSH 报告"投入产出比最高"项）：event_stream 把每次实际路由的 messages+model 存为会话快照 `_last_routed`；后台摘要任务 replay 该快照（逐字节一致）+ 末尾追加一条压缩指令 user 消息 + 使用同 model → 摘要请求成为主请求的 prefix-extension，命中刚热好的缓存。无快照时退回独立 prompt（正确性优先）。单测验证：前缀逐字节一致、指令尾部化、同 model、fallback 正常。

3. **tools 稳定排序**：按 function.name 字典序（当前仅 web_search，未来加工具不漂移）。

## 5.7 真实 AI 综合回归（2026-08-14 三批，全部通过）

8 轮同会话对话（v4-flash，思考关）：命中率 92% / 83% / 77% / 95% / 89% / 84% / 79% / 93%（均值 **86.5%**），hit 逐轮 384 → 512 → 640 持续累积——前缀链完全打通。

摘要请求复用 warm 前缀（二批实现）：hit=640 miss=99 → **86.6% 命中**，DSH 模式复刻成功（修复前独立 prompt 每次全 miss）。

顺带回归：动态问数一句话即确认（plan 抽取完整：subject/level_from/goal 全对）；思考模式+工具轮回传 reasoning_content 正常；笔记整理真实 LLM 生成成功。

## 6. 后续候选（P2）

1. **稳定区扩充**：把固定 few-shot / 回答格式约定并入稳定区（增大命中基数，顺带提升回复质量）
2. **辅助调用 prefix-extension**：起标题/学习建议复用主请求前缀（指令尾部化），摘要调用暂缓
3. **会话恢复预热**：重启应用后对活跃会话发一次「no-op 预热」——收益小，个人场景不建议
4. 观察 1-2 周真实命中率曲线，若 < 40% 再排查低频区摘要重写频率
