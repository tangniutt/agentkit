# 路由评分标准

## TaskRisk

### complexity：0–3

| 分数 | 判断 |
|---:|---|
| 0 | 单文件、机械修改、验收明确；无需跨模块推理 |
| 1 | 局部功能或 Bug，已有模式可复用；少量上下文 |
| 2 | 多文件、跨模块、需要理解调用关系或数据流 |
| 3 | 架构调整、并发/分布式、迁移、协议或安全关键逻辑 |

### blast_radius：0–3

| 分数 | 判断 |
|---:|---|
| 0 | 文案、样式、注释、低影响内部脚本；错误易回滚 |
| 1 | 普通业务逻辑、局部 UI、非关键工具；影响可隔离 |
| 2 | 公共 API、数据库结构、权限、支付、任务调度；错误影响用户或数据 |
| 3 | 认证、授权、密钥、资金、不可逆数据迁移、平台安全边界 |

风险不确定时取较高分。`blast_radius` 不应因模型便宜而降低。

### context_pressure：0–3

- 0：目标文件明确，上下文远低于模型窗口。
- 1：需要少量相关模块。
- 2：需要较大仓库地图、长文档或多文件历史。
- 3：接近上下文上限，必须先压缩或拆分任务。

### verification_strength

- `basic`：至少一条失败基线检查。
- `standard`：功能测试 + 类型/构建 + 关键回归。
- `strong`：标准检查 + 独立 Reviewer + 安全/迁移专项检查。

高 `blast_radius` 不得使用 `basic`。

## required_capabilities

使用能力名称，不使用模型品牌：

- `code_edit`：精确编辑与补丁；
- `tool_use`：稳定调用工具并处理结果；
- `repo_reasoning`：跨文件理解；
- `planning`：拆解阶段与依赖；
- `review`：发现实现盲点；
- `long_context`：处理大上下文；
- `debugging`：从失败证据定位；
- `migration`：数据库/协议/依赖迁移；
- `security`：威胁建模与安全边界；
- `recovery`：中断后续跑。

能力由 ModelProfile 的实测结果证明，不由厂商描述推断。

## 角色拓扑

| 风险 | 拓扑 |
|---|---|
| complexity ≤1 且 blast_radius ≤1 | Implementer |
| complexity =2 或 blast_radius =2 | Planner + Implementer；可选 Reviewer |
| blast_radius =3 | Planner + Implementer + 独立 Reviewer + 强验收 |

Planner 与 Reviewer 的能力不得低于 Implementer。多个 Agent 必须使用独立工作区或明确串行边界。

## 升级判据

### 触发升级

- 同一冻结验收连续失败，且失败属于实现能力；
- 工具调用格式错误重复出现；
- 多轮无新 diff 或重复同一错误；
- 上下文超限，当前模型无法处理；
- 任务复杂度超过当前 ModelProfile 的实测范围。

### 不触发升级

- 429、配额、网络、5xx、超时；
- 401/403、错误 Key、参数或模型 ID 配置错误；
- 验收脚本本身损坏；
- 缺少权限或凭证；
- 单次随机失败，尚无足够证据。

### 升级选择

1. 识别缺失能力。
2. 过滤满足该能力的 ModelProfile。
3. 排除已撤销、过期或样本不足的配置。
4. 选择保守期望成本最低且风险等级足够的候选。
5. 保留累计预算与 handoff 证据。

## 路由伪代码

```text
risk = score_task()
required = capabilities_for(risk)
candidates = active_profiles_matching(required)
for candidate in candidates:
    candidate.score = expected_cost_per_verified_success(candidate, risk)
selected = lowest_risk_adjusted_cost(candidate)
if no candidate:
    ask for model evaluation or narrower task; do not guess
```

## 停止条件

- 预算耗尽；
- 达到最大尝试或 handoff 次数；
- 验收脚本无效；
- 需要人工权限或业务决策；
- 发现密钥泄漏、测试篡改或不可接受风险。

停止时输出已有 diff、失败证据、已花费和下一步建议。
