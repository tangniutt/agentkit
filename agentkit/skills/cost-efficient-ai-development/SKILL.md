---
name: cost-efficient-ai-development
description: 跨厂商的 AI 编程成本优化与智能模型调度工作流。用于任务难度与风险分级、模型能力评估、先低成本执行再按证据升级、Planner/Implementer/Reviewer 分层、Token/预算控制、验证成功率与每成功任务成本统计，以及建立可复现的 Skill eval。适用于用户要求省钱、选模型、动态升级、比较模型或优化 AI 编程质量与成本时。
---

# AI 编程成本优化与智能模型调度专家

## 目标

在**不降低验收标准**的前提下，最小化“每个可验证成功任务的总成本”。只依据模型能力、任务风险、实测效果和成本路由，不依据品牌、宣传或“免费/付费”标签。

## 不可变约束

- 不写死任何厂商、模型名称或固定角色。
- 不通过弱化测试、删除检查、跳过审查来省钱。
- 不轮换账号、不绕过限额、不共享凭证、不把代码发往未授权服务。
- 不把模型自述完成当作成功；只有独立验收通过才算 `verified_pass=true`。
- 不因单次失败就永久降级模型；样本不足时保持保守判断。
- 任务记录不得包含 API Key、Token、Cookie、密码或私有会话内容。

## 所需输入

开始前收集：

1. 任务目标、验收命令和允许修改路径。
2. 仓库语言、关键模块、依赖与现有测试。
3. 可用模型目录及版本；未知时先建立 `ModelProfile`。
4. 总预算、时间上限和可接受的人工介入次数。
5. 历史任务记录或固定评测集。

输入缺失时，使用保守默认：中低复杂度从最低合格能力开始，高影响任务必须配置独立 Reviewer。

## 工作流

### 1. 冻结验收与风险

先写清：

- 必须通过的命令、构建、类型检查和回归测试；
- 禁止修改的验收脚本、测试和配置；
- 允许路径与回滚方式。

按 `references/routing-rubric.md` 评分：

- `complexity`：0–3；
- `blast_radius`：0–3；
- `required_capabilities`；
- `context_pressure`；
- `verification_strength`。

### 2. 建立或读取 ModelProfile

每个模型版本记录：

- 能力分：编码、工具调用、规划、审查、长上下文、恢复；
- 实测 `verified_pass_rate` 与分风险成绩；
- 输入/输出价格、缓存价格、典型尝试成本；
- p50/p95 延迟、错误率、样本量与置信度。

按 `references/model-evaluation.md` 评估。没有足够样本时标记 `provisional`，不得直接升至最高级。

### 3. 计算候选路由

对每个候选模型计算：

```text
expected_cost_per_verified_success =
  (attempt_cost + verification_cost + expected_rework_cost)
  / conservative_success_probability
```

只考虑满足 `required_capabilities` 的候选。高 `blast_radius` 任务额外加入失败风险成本。

输出路由决策：

```json
{
  "task_risk": {"complexity": 2, "blast_radius": 1},
  "required_capabilities": ["code_edit", "tool_use"],
  "selected_profile": "MODEL_VERSION",
  "role": "implementer",
  "reasoning_level": "low",
  "expected_cost_per_verified_success": 0.0,
  "fallback_profiles": [],
  "budget": {"tokens": 0, "usd": 0.0, "minutes": 0}
}
```

### 4. 选择执行拓扑

- 低风险、小范围任务：单 Implementer。
- 中风险、多文件任务：可选 Planner + Implementer。
- 高风险任务：Planner + Implementer + 独立 Reviewer。
- Reviewer 能力不得低于 Implementer；最终仍以确定性验收为准。

先使用最低合格推理强度。只有失败证据证明需要更强推理时才升级。

### 5. 执行与预算

- 使用 `templates/task-brief.md` 生成任务书。
- 限制输出、工具结果和上下文；优先读取相关文件而不是整仓。
- 持续记录 token、费用、轮数、工具错误和耗时。
- 预算不足时暂停，不静默扩大预算。

### 6. 独立验证与失败分类

在干净环境运行冻结验收，并按 `references/routing-rubric.md` 分类失败：

- 基础设施：429、网络、5xx、超时；
- 配置：401/403、参数错误、能力不支持；
- 能力：验收持续失败、工具调用格式错误、卡住、上下文超限；
- 风险：可疑修改、测试篡改、越权路径。

只有能力失败才升级模型。认证或参数错误先修配置；基础设施失败按合规策略等待或暂停。

### 7. 升级、记录与学习

- 从当前 ModelProfile 的 fallback 列表选择“满足缺失能力且期望成本最低”的模型。
- 保留原 worktree、diff、验收结果和累计预算；不重放私有思维过程。
- 使用 `templates/task-record.md` 记录每次尝试。
- 用 `scripts/score_records.py` 汇总真实记录。
- 达到最小样本量后才更新能力等级；保留模型版本与评测配置。

## 成本评分脚本

```bash
python scripts/score_records.py --input records.jsonl --min-samples 5
python scripts/score_records.py --input records.jsonl --min-samples 5 --complexity 2 --blast-radius 1
```

脚本输出 JSON：每模型成功率、Wilson 下界、平均成本、保守期望成本和候选建议。脚本不访问网络，不执行模型，不读取密钥。

## 资源

- `references/routing-rubric.md`：任务风险、角色拓扑与升级判据。
- `references/model-evaluation.md`：ModelProfile、固定评测与动态分级。
- `templates/task-brief.md`：给执行模型的任务书。
- `templates/task-record.md`：JSONL 任务记录字段与示例。
- `scripts/score_records.py`：确定性成本与成功率汇总。

## 兼容性

遵循通用 Agent Skills 的 `SKILL.md` 结构。Codex 及其他支持该格式的工具需按实际版本验证；Qoder 中先运行 `/skills list` 确认发现，再用 `/cost-efficient-ai-development` 调用。未验证前不要宣称完整兼容。
