# 任务记录（JSONL）

每行一个独立任务结果。不要记录密钥、Token、Cookie、密码、私有会话或未授权代码片段。

## 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 稳定任务 ID |
| `model_id` | string | 供应商模型 ID |
| `model_version` | string | 模型版本或快照 |
| `role` | string | planner / implementer / reviewer |
| `complexity` | int | 0–3 |
| `blast_radius` | int | 0–3 |
| `verified_pass` | bool | 独立验收是否通过 |
| `attempts` | int | 模型尝试次数 |
| `input_tokens` | int | 输入 token |
| `output_tokens` | int | 输出 token |
| `attempt_usd` | number | 模型调用成本 |
| `verification_usd` | number | 验收成本 |
| `rework_usd` | number | 返工成本 |
| `latency_s` | number | 总耗时 |
| `failure_kind` | string | none / infrastructure / config / capability / risk |
| `reasoning_level` | string | low / medium / high |
| `timestamp` | string | ISO-8601 |
| `eval_config_sha256` | string | 冻结评测配置哈希 |

## 示例

```json
{"task_id":"task-001","model_id":"provider/model-a","model_version":"2026-11","role":"implementer","complexity":1,"blast_radius":1,"verified_pass":true,"attempts":1,"input_tokens":12000,"output_tokens":1800,"attempt_usd":0.012,"verification_usd":0.001,"rework_usd":0.0,"latency_s":42.5,"failure_kind":"none","reasoning_level":"low","timestamp":"2026-11-28T00:00:00Z","eval_config_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

## 汇总

```bash
python scripts/score_records.py --input records.jsonl --min-samples 5
```

只汇总同一模型版本与同一评测配置；不要混合不同验收标准。
