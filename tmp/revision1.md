# 信息学竞赛测试数据生成系统——动态 Branch 工作流设计

## 一、设计理念

整个系统并非传统的线性流水线，而是一个**持续发现信息、持续分叉探索、持续汇合沉淀**的动态工作流。

其演化过程可以类比为一条河流：

```text
题面 + 标程
      │
      ▼
大量信息发现（Findings）
      │
      ▼
不断产生 Branch（支流）
      │
      ▼
各 Branch 独立探索
      │
      ▼
不断汇合为 Generator、Test Cases、Knowledge
      │
      ▼
最终形成完整测试集与生成器
```

整个过程中，任何阶段都可能产生新的 Finding，并进一步衍生新的 Branch，因此整个系统天然支持持续演化，而不是固定的一次性流程。

---

# 二、总体架构

整个系统由两部分组成：

* 固定 LangGraph 主图（Meta Workflow）
* 动态产生的大量 Branch

固定主图负责：

* 信息分析
* Finding 管理
* Branch 管理
* Branch 调度
* 汇总
* 审计

动态 Branch 负责：

* 独立完成某一类测试目标的探索
* 贡献新的生成器能力
* 贡献新的测试点
* 贡献新的 Finding

因此整个系统实际上是：

```text
固定工作流
        +
动态 Branch 实例
```

而不是动态修改 LangGraph 的拓扑。

---

# 三、Knowledge Registry

整个系统维护一个统一的知识中心。

Registry 并不是简单的数据池，而是整个系统唯一可信的数据源。

Registry 至少维护四类对象。

## 1. Findings Registry

保存所有已经发现的信息。

例如：

* 输入边界
* 特殊结构
* 错误模型
* 最坏复杂度
* 数值风险
* 特殊构造
* 新发现的反例

每个 Finding 都具有唯一 ID。

重复 Finding 不重新创建，而是：

* 更新证据
* 更新置信度
* 建立新的关联

---

## 2. Branch Registry

保存所有 Branch。

Branch 的生命周期包括：

```text
Created

Ready

Running

Blocked

Completed

Discarded
```

Branch 保存：

* 来源 Finding
* 目标
* 当前状态
* 输出
* 覆盖情况

---

## 3. Generator Registry

保存所有生成能力。

注意：

Registry 保存的是生成能力，而不是完整生成器。

例如：

```text
gen_chain()

gen_star()

gen_random()

gen_overflow()

gen_duplicate()
```

每一个 Branch 贡献一个或多个模块。

最终统一组合。

---

## 4. Test Case Registry

保存所有已经通过验证的数据。

包括：

* 输入
* 答案
* 来源 Branch
* 覆盖哪些 Finding
* 覆盖哪些错误模型
* Generator 参数
* Seed

---

# 四、Finding 产生过程

Finding 不是由一个 Agent 一次性产生。

而是由多个 Principle Agent 并行产生。

每个 Principle 代表一种分析视角。

例如：

```text
Format

Constraint

Boundary

Structure

Distribution

Complexity

Algorithm

Numeric

Subtask

Adversarial
```

整个过程：

```text
题面+标程
      │
──────────────
│   │   │   │
多个 Principle Agent
│   │   │   │
Finding Finding Finding
```

每个 Principle Agent 一次产生多个 Finding。

每个 Finding 保持原子性。

例如：

```text
树可以退化成链

DFS 深度达到 N

存在 int 溢出风险

存在大量重复值
```

而不是组合多个信息。

---

# 五、Finding 生命周期

新的 Finding 并不会直接创建 Branch。

首先进入 Registry。

Registry 完成：

## 注册

赋予唯一 ID。

## 查重

判断是否已有类似 Finding。

如果已有：

更新

而不是重新创建。

## 合并

如果只是已有 Finding 的补充信息。

则更新已有 Finding。

## 建立关联

维护：

Finding

↓

Branch

↓

Generator

↓

Test Case

之间的关系。

因此 Registry 更像整个系统的知识图谱。

---

# 六、Branch Planner

Branch Planner 不负责发现信息。

而负责：

根据整个 Registry 推导：

哪些 Branch 值得创建。

输入不是：

新增 Finding。

而是：

整个 Registry。

Branch Planner 综合考虑：

* 全部 Findings
* 全部 Branch
* 全部 Generator
* 全部 Test Cases

进行统一规划。

主要职责包括：

## Finding 聚合

多个 Finding 可以共同形成一个 Branch。

例如：

```text
链

+

DFS

+

N 最大

↓

深链压力测试
```

---

## 查重

判断：

是否已有 Branch 正在解决。

若已有：

更新已有 Branch。

而不是重新创建。

---

## 覆盖判断

若已有测试集已经覆盖。

则不创建。

---

## 价值评估

综合：

收益

成本

重复程度

优先级

决定是否值得探索。

---

## BranchTask 创建

最终生成：

BranchTask

进入 Branch Registry。

状态：

Ready。

---

# 七、Branch 调度

Branch Scheduler 管理整个 Branch Pool。

负责：

* 并发控制
* 优先级
* 依赖管理
* 资源限制

例如：

```text
Ready

↓

Scheduler

↓

Running
```

运行完成：

```text
Completed
```

失败：

```text
Blocked

Retry

Discard
```

---

# 八、Branch 内部流程

所有 Branch 使用同一套固定流程。

```text
Branch Analysis

↓

Strategy Design

↓

Generator Implementation

↓

Candidate Generation

↓

Validation

↓

Evaluation

↓

Merge
```

各阶段职责：

Branch Analysis

分析当前 Branch 的测试目标。

Strategy Design

设计构造策略。

Generator Implementation

实现当前策略。

Candidate Generation

生成候选数据。

Validation

运行：

Validator

Std

Checker

错误解

对拍

Evaluation

评估：

是否真正贡献价值。

Merge

贡献：

Generator

Test Case

Finding

Coverage

---

# 九、Branch 的输出

Branch 不只是产生测试点。

还可能产生：

新的 Finding。

例如：

在验证过程中发现：

全部权值相同时还有新的错误算法。

则：

```text
Validation

↓

New Finding

↓

Registry

↓

Planner

↓

New Branch
```

整个系统因此形成：

```text
Finding

↓

Branch

↓

New Finding

↓

Branch
```

不断演化。

---

# 十、Generator 的组织方式

整个系统不会不断合并源代码。

而是：

不断合并生成能力。

每个 Branch 贡献：

一个 Generator Module。

例如：

```text
gen_chain

gen_star

gen_duplicate

gen_random

gen_overflow
```

统一维护：

Generator Registry。

最后由：

Generator Composer

自动生成：

统一入口。

例如：

```cpp
main()

↓

Dispatcher

↓

对应 Generator Module
```

因此：

Branch 之间互不影响。

最终 Generator 可以自动组合。

整个过程无需人工合并大量代码。

---

# 十一、最终汇总

系统最终沉淀四类成果。

## Knowledge

所有 Finding。

所有关联关系。

## Generator

完整 Generator Registry。

统一入口。

所有模块。

## Test Suite

最终测试点。

答案。

覆盖关系。

## Audit

完整验证报告。

覆盖率。

Branch 贡献。

Generator 来源。

所有数据均可追溯。

---

# 十二、整体数据流

整个系统最终可以抽象为：

```text
题面 + 标程
        │
        ▼
Principle Agents
        │
        ▼
Findings
        │
        ▼
Knowledge Registry
        │
        ▼
Branch Planner
        │
        ▼
Branch Pool
        │
        ▼
Scheduler
        │
        ▼
Branch Workers
        │
        ▼
Generator
Test Case
New Findings
        │
        ▼
Knowledge Registry
        │
        ▼
持续演化
        │
        ▼
Generator Composer
        │
        ▼
最终 Generator
        │
        ▼
最终 Test Suite
```

整个系统的核心思想可以概括为：

> **系统不断发现知识（Findings），知识驱动任务（Branches），任务产生新的知识，并持续沉淀为生成能力（Generators）与测试能力（Test Cases），最终形成一个能够持续演化的信息学竞赛测试数据生成系统，而不是一次性的生成器。**
