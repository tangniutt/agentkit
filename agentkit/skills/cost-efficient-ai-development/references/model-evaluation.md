# 模型能力与成本评测

## ModelProfile

每个模型版本使用独立档案：

```json
{
  "model_id": "provider/model",
  "model_version": "VERSION",
  "status": "provisional|active|deprecated|revoked",
  "capabilities": {
    "code_edit": 0.0,
    "tool_use": 0.0,
    "repo_reasoning": 0.0,
    "planning": 0.0,
    "review": 0.0,
    "long_context": 0.0,
    "debugging": 0.0,
    "migration": 0.0,
    "security": 0.0,
    "recovery": 0.0
  },
  "pricing": {"input_per_million": 0.0, "output_per_million": 0.0, "cached_input_per_million": 0.0},
  "limits": {"context": 0, "max_output": 0, "rpm": 0},
  "evidence": {"sample_count": 0, "verified_pass_rate": 0.0, "wilson_lower_bound": 0.0},
  "evaluated_at": "ISO-8601",
  "eval_config_sha256": ""
}
```

能力分只能来自固定评测或真实任务记录。禁止用品牌、榜单排名或单次演示替代证据。

## 固定评测协议

1. 选择覆盖不同复杂度与 blast radius 的固定任务集。
2. 冻结仓库快照、验收命令、工具权限和预算。
3. 记录模型 ID、版本、推理强度、采样参数与随机种子。
4. 每个任务至少运行 3 次；关键任务运行 5 次。
5. 先运行 gold/oracle，确认任务与验收本身有效。
6. 记录全部成功、失败、超时和基础设施失败，不挑题。
7. 输出逐题 JSONL、汇总 JSON 和可复现脚本。

## 必测指标

- `verified_pass@1`、`verified_pass@3`；
- 按 complexity / blast_radius 分项的成功率；
- 工具调用格式错误率、中断恢复率；
- 输入/输出/缓存 token；
- 尝试成本、验收成本、返工成本；
- p50/p95 延迟；
- 人工介入次数；
- 可疑修改与验收误报。

## 成本口径

```text
task_total_cost =
  attempt_cost
  + verification_cost
  + rework_cost
  + optional_risk_cost

conservative_success_probability =
  Wilson lower bound of verified_pass_rate

expected_cost_per_verified_success =
  task_total_cost / conservative_success_probability
```

基础设施失败单独统计，不静默算作模型能力失败，也不从分母中偷偷删除。所有结果必须保留原始记录。

## 动态分级

- `provisional`：样本不足，只允许低风险任务或人工监督。
- `active`：达到最小样本量，能力分与成功率稳定。
- `deprecated`：模型版本退役或长期无维护。
- `revoked`：授权撤销或安全策略禁止。

升级条件：

- 新版本在固定集上达到更高能力分；
- 成功率提升不是由降低验收标准造成；
- 期望成本或高风险任务表现有可解释收益；
- 样本量与置信度足够。

降级条件：

- 连续评测显示能力退化；
- 工具调用错误率或返工成本显著上升；
- 版本、价格或限制发生不利变化。

## 最小样本建议

- 低风险路由：至少 5 个独立任务。
- 中风险路由：至少 10 个独立任务。
- 高风险路由：至少 20 个独立任务，并包含强验收与 Reviewer 对照。

样本不足时使用 Wilson 下界，不做激进升级。

## 禁止事项

- 不按“免费=低能力”或“昂贵=高能力”预设。
- 不通过删除失败记录提升成功率。
- 不让模型给自己打分后直接更新路由。
- 不把供应商私有 thought signature 写入公共记录。
- 不在记录中保存密钥、Cookie 或私有代码片段。
