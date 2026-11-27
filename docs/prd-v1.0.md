# AgentKit PRD v1.0：可信执行与进化底座

> 状态：已批准的目标需求；P0/P1 控制面与 P2 代码智能已部分实现，尚未达到生产发布门禁。
>
> 日期：2026-11-26。
>
> 适用基线：`main` 提交 `e2600d4` 及其后续 C-15 至 C-20。
>
> 与旧文档关系：`docs/v1.0-baseline.md` 保留为 2026-11-23 冻结的实现基线；本文件定义下一阶段目标形态，不追溯修改旧 Must 的完成状态。`docs/prd-v0.3.md` 及更早版本作为历史存档。

---

# 0. 核心设计理念

AgentKit 是给 AI Agent 使用的编程执行与验收工具，不是给人使用的 IDE、聊天界面或任务看板。

未来产品定义：

> **AgentKit 是 AI 工人的可信执行、验收与进化底座。**

核心回路：

```text
主管 AI（Muse 或任意 Agent）
  │ 提交 Goal / Task / 验收契约
  ▼
Goal Controller / Task Scheduler
  │ 选择工人、模型、工具和预算
  ▼
Worker Backend
  │ builtin worker / 外部 CLI / MCP Agent
  ▼
Sandbox + Tool Plane
  │ 文件、命令、代码智能、记忆
  ▼
Independent Verifier
  │ fail-to-pass、冻结验收、功能验证、回归检查
  ▼
EvidenceReceipt + Retrospective
  │ 可复核证据、成本、失败原因、下一步
  ▼
Eval / Regression / Memory Flywheel
```

三条不可动摇的原则：

1. **证据先于声明**：工人可以说完成，但只有独立验收证据能决定完成。
2. **复杂度必须证明收益**：Goal、多 Agent、语义检索、额外评审层都必须由评测证明值得。
3. **本地优先与人类把关**：无遥测、不自动 push/部署、不绕过平台限额、公开操作前需要明确授权。

北极星指标：

> **每花费一美元，能获得多少个经过独立验证的成功任务。**

考核时必须同时计入主管、工人、验收器、重试和返工成本，不能只比较工人 token。

---

# 1. 背景与问题

## 1.1 行业变化

截至 2026-11，代码读写、搜索、仓库地图、检查点、worktree、子 Agent、MCP、headless JSON、模型切换等能力已经高度商品化。

AgentKit 不能依靠“功能更多”建立差异。当前更有价值的方向是：

- 上下文工程，而不是堆提示词；
- 少而正交的工具，而不是几百个重叠工具；
- 环境证据，而不是模型自述；
- 可替换工人后端，而不是绑定一个工人；
- 可复现评测，而不是自我感觉良好；
- 结果闭环，而不是失败后人工重新派活。

## 1.2 AgentKit 当前基础

已实现并值得保留：

- 统一紧凑 JSON 信封、输出预算和续读语义；
- read/search/ls/glob/repo_map/edit/patch/create/delete/rename/run；
- 精确编辑、语法拒写、stale-write guard、checkpoint；
- 内置工人、OpenAI 兼容模型客户端、任务队列和 worktree；
- 暂停、恢复、累计预算、稳定错误码和结构化日志；
- 独立验收、基础防作弊、密钥脱敏、项目记忆；
- 离线假模型评测、真实模型 dogfood、AI 反馈草稿；
- TaskSpec/GoalSpec/EvidenceReceipt/Retrospective 协议、Goal v1、MCP stdio；
- task handoff/diff/steer/answer/accept/reject、fail-to-pass 与冻结验收；
- `HandoffPacket` v1 多工具交接协议（不含凭证或私有会话）；
- bubblewrap 文件系统/进程隔离适配、Builtin/XERJ 代码智能与真实节点回归。

当前明确缺口：

- bubblewrap 已适配并支持 `network=false` 硬隔离与 `network=["api.example.com"]` 域名白名单；白名单模式使用独立网络命名空间、回环中继和宿主代理，按域名与 TLS SNI 放行，拒绝重复 Host、管道后续请求和非全局解析地址，任务报告/收据记录 shared/isolated/allowlist/host；默认仍共享网络以兼容现有模型调用；Qoder/OpenCode 复用登录所需认证目录仍由 CLI 工具规则保护，尚未实现独立凭证 broker，不可信仓库不得使用真实登录态。保存登录态与仓库外 Qoder `config_dir` 还必须获得用户级 `[security] allow_saved_credentials=true` 授权，项目配置不能自行开启；PAT/API Key 路径不受此开关影响；Windows 沙箱未完成，但 Windows 不是当前生产部署平台，仅保留 direct-mode 开发兼容；worktree 本身仍不是安全边界；
- Goal 级验收与 retrospective 已有 v1，但缺真实长任务回归与外部作弊留出集；
- MCP stdio 已实现 tools/resources/outputSchema、动态任务/Goal 资源与分页；完整支持 2025-06-18/2025-11-25 的资源订阅和状态变更通知；2026-07-28 的 `server/discover`、缓存与 `subscriptions/listen` 尚未实现，因此明确拒绝该版本；
- `HandoffPacket` 与 Codex/OpenCode/Qoder/Cursor 适配器已实现，支持 builtin↔Codex↔OpenCode↔Qoder↔Cursor 接力；OpenCode V1 与 V2 均已适配，V2 官方免费模型可免登录使用，真实 2.4.2 最小任务验证通过，免费额度 429 会暂停且不绕过；LSP 引用窄客户端与词法回退已实现，Pyright 1.1.414 与 typescript-language-server 6.1.0 真实引用验证通过，其余语言服务器矩阵待测；Cursor 支持官方 headless CLI 与可选 Python SDK，CLI 仅轮次/时间预算，SDK 可在运行时报告 token 与按运行成本，但真实 API Key 任务未验证且 USD 硬预算不支持；Antigravity consumer 不包装，自动接力仍默认关闭；
- XERJ 已接入，但固定 benchmark 中返回字符仍高于内置后端，未达到 token 门槛；
- 结构化 retrospective 与证据收据已有本地实现，缺 SWE-bench/Terminal-Bench 等外部 benchmark；
- 已有本地固定版本构建、wheel/sdist SHA256、版本化清单、artifact 校验、OpenSSL 分离式签名/公钥验证与显式回滚；远端签名发布与自动升级仍未实现；
- 生产目标平台为 Linux VPS；Windows 仅保留 direct-mode 开发兼容，派活沙箱不是当前生产门槛。OpenCode V2 与发布签名接入后 Windows 最终全量 **455 passed, 93 skipped**（含真实 Pyright），Linux Python 3.10/3.12 + bubblewrap 最终全量均为 **544 passed, 4 skipped**；DCO 提交 `ae85bf3` 已推送，Linux GitHub Actions 通过；正式签名与发布仍未执行。

## 1.3 目标用户

- 主管 AI：Muse、Claude Code、OpenCode、Cline、Codex CLI 或其他能调用 CLI/MCP 的 Agent。
- 工人：内置轻量工人、用户配置的 OpenAI 兼容模型、外部编码 CLI、MCP Agent；Antigravity 等工具仅在官方接口与安全边界允许时作为独立后端接入。
- 验收者：确定性测试、功能/E2E 检查、独立 reviewer/evaluator。
- 人类：定义边界、审批高风险动作、审阅证据，不实时盯每一个工具调用。

---

# 2. 产品目标与非目标

## 2.1 产品目标

1. 让任意主管 AI 能用统一契约派活、监督、纠偏和验收。
2. 让不可信工人无法越出文件、进程、网络和密钥边界。
3. 让“完成”由可复现证据定义，降低误报通过和谎报完成。
4. 让模型断流、429、超时、5xx 可被感知并安全接力。
5. 让复杂目标通过有界 Goal 闭环持续推进，但不无限烧钱。
6. 让代码搜索、记忆和上下文按需加载，控制总 token 与缓存成本。
7. 让外部项目能够复现 AgentKit 的成本、质量与安全结论。

## 2.2 非目标

- 不做人用 IDE、聊天界面、TUI、Web 看板。
- 不训练、托管或转售模型。
- 不提供账号轮换、代理池、IP 伪装等绕过平台限额能力。
- 不自动 commit、push、部署或发布。
- 不收集遥测，不上传用户代码；模型调用和用户显式分享除外。
- 不在本阶段重写为 Rust/Go；原生二进制在价值证明后评估。
- 不默认启用 Unlimited Goal、嵌套工人或复杂多 Agent 编排。

---

# 3. 成功标准

| 维度 | 目标 | 验证方式 |
|---|---|---|
| 安全边界 | Critical/High 路径穿越与权限绕过为 0 | 安全回归集、外部红队题库 |
| 验收可靠性 | 漏报通过率低于 2%，可疑误报率不高于 5% | 留出集、外部作弊库、人工标注 |
| 成本效率 | 同等通过率下，总成本至少降低 20% | 真实主管模型 A/B，含验收与返工 |
| 主管成本 | 派活任务主管平均低于 3,000 token | 真实主管模型轨迹 |
| Goal 可信度 | 100% 的 completed 都有 Goal 级验收证据 | 状态机不变量测试 |
| 恢复能力 | paused/interrupted/stopped 可恢复，换模型不丢 diff 与预算 | 故障注入测试 |
| MCP | 核心工具 100% 有 schema、outputSchema、错误码和副作用注解 | MCP 契约测试 |
| 代码智能 | XERJ 接入在召回、延迟、token 上至少不劣于内置后端 | 固定代码检索 benchmark |
| 可复现性 | 每次评测能固定任务、环境、模型、提交和结果哈希 | 发布物与 receipt 校验 |
| 安装 | 受支持平台一条命令安装，30 秒内完成 | 干净环境安装测试 |

说明：上述是目标门槛，不是当前已达成结果。未达到门槛时不得在 README 或宣传材料中写成已实现。

---

# 4. 使用流程

## 4.1 直接模式

```text
memory brief
→ repo_map / search / read
→ edit / patch
→ run
→ verify
→ checkpoint rollback 或保留结果
```

直接模式面向可信主管 AI，仍受路径、预算、权限和输出契约约束。

## 4.2 单任务派活

```text
task create --input task.json
→ task wait
→ task report / task diff
→ pass：主管决定是否合并
→ needs_review：检查证据和 suspicious
→ fail：查看失败检查
→ paused：resume 或 handoff
```

## 4.3 Goal 模式

```text
goal create --input goal.json
→ 可选 goal plan（Muse 提供或配置模型生成）
→ goal run
→ 每轮 task create / wait / verify
→ Goal 级验收
→ retrospective + regression candidate
→ completed / needs_review / paused / failed / cancelled
```

Goal 默认关闭。只有在多步骤、结果可验证、需要持续迭代时启用。

---

# 5. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│ Supervisor AI：Muse / OpenCode / Cline / Codex / 任意 Agent   │
└───────────────────────────┬──────────────────────────────────┘
                            │ CLI JSON / MCP stdio
┌───────────────────────────▼──────────────────────────────────┐
│ Interface Layer                                              │
│ tools / resources / notifications / capabilities / manual    │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Control Plane                                                │
│ Goal Controller / Task Scheduler / Model Router / Event Bus  │
└───────────────┬───────────────────────────────┬──────────────┘
                │                               │
┌───────────────▼──────────────┐   ┌────────────▼─────────────┐
│ Worker Backends              │   │ Trust Plane              │
│ builtin / external CLI / MCP │   │ sandbox / verifier /     │
└───────────────┬──────────────┘   │ evidence receipt         │
                │                   └────────────┬─────────────┘
┌───────────────▼──────────────┐                │
│ Tool Plane                   │                │
│ read/edit/run/checkpoint      │                │
│ code intelligence / memory   │                │
└───────────────┬──────────────┘                │
                │                               │
┌───────────────▼───────────────────────────────▼──────────────┐
│ Learning Plane：Eval / Regression / Retrospective / Memory    │
└──────────────────────────────────────────────────────────────┘
```

---

# 6. 核心协议

所有协议必须有版本号、JSON Schema、稳定错误码和向后兼容策略。协议是 AgentKit 的核心资产。

## 6.1 ToolResult

保持现有五键信封，并增加可选的结构化恢复信息：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "what": "model timeout",
    "why": "no response after retries",
    "next": "agentkit task handoff t1a2b3c --model worker-b",
    "code": "AK-MODEL-TIMEOUT",
    "run": "t1a2b3c",
    "retryable": true,
    "recovery": ["resume", "handoff"]
  },
  "truncated": false,
  "next": null
}
```

要求：

- 任何输出都受预算约束，包括错误信封。
- `truncated=true` 必须提供可执行的续读方式。
- 错误必须区分可重试、可换模型、需人工决策、不可恢复。
- 大日志、diff、report、bundle 使用 artifact handle，不反复返回全文。

## 6.2 TaskSpec

```json
{
  "schema_version": "1.0",
  "goal": "修复库存计算错误",
  "context": ["src/stock.py", "tests/test_stock.py"],
  "allowed_paths": ["src/"],
  "forbidden_paths": ["tests/", ".github/"],
  "acceptance": {
    "checks": ["pytest -q tests/test_stock.py"],
    "baseline_must_fail": true,
    "frozen": true
  },
  "budget": {
    "tokens": 200000,
    "usd": 2.0,
    "minutes": 20,
    "rounds": 40
  },
  "worker": {
    "backend": "builtin",
    "model": "cheap",
    "failover": ["mid"]
  }
}
```

要求：

- 验收契约在工人开始前冻结并计算哈希。
- 工人不能修改验收命令、测试配置或冻结哈希。
- 允许路径与禁止路径同时存在；禁止路径优先。
- 未提供验收检查时只能创建 draft，不能直接运行。

## 6.3 GoalSpec

```json
{
  "schema_version": "1.0",
  "outcome": "将 API P95 延迟降至 200ms 以下",
  "non_goals": ["不改变公开 API", "不更换数据库"],
  "acceptance": {
    "checks": ["python bench.py --rounds 3", "pytest -q"],
    "manual_evidence": ["benchmark-report.json"],
    "frozen": true
  },
  "constraints": {
    "max_tasks": 8,
    "max_handoffs": 2,
    "allowed_paths": ["src/"]
  },
  "budget": {
    "tokens": 500000,
    "usd": 5.0,
    "minutes": 60
  },
  "planning": {
    "mode": "supervisor",
    "approval_required": false
  }
}
```

要求：

- outcome 不可在运行中偷改；变更 outcome 必须创建新 Goal。
- 约束可以版本化追加，但必须记录修改人、时间、原因。
- Goal 预算覆盖所有子任务、验收和返工。
- 默认不允许 Unlimited。

## 6.4 EvidenceReceipt

每次任务或 Goal 验收生成不可变收据：

```json
{
  "schema_version": "1.0",
  "receipt_id": "er_01J...",
  "subject": {"kind": "task", "id": "t1a2b3c"},
  "base_commit": "e2600d4...",
  "diff_sha256": "...",
  "acceptance_sha256": "...",
  "environment_sha256": "...",
  "checks": [
    {
      "cmd": "pytest -q",
      "result": "pass",
      "exit": 0,
      "log_sha256": "..."
    }
  ],
  "verdict": "pass",
  "suspicious": [],
  "verifier_version": "0.3.0",
  "worker": {"backend": "builtin", "model": "cheap"},
  "cost": {"tokens": 12345, "usd": 0.12, "seconds": 42},
  "created_at": "2026-11-25T00:00:00Z"
}
```

要求：

- receipt 内容寻址、不可变、可校验。
- `task report` 和 `goal report` 返回 receipt ID 与摘要，不返回大全文。
- `debug-bundle` 默认包含 receipt。
- 外部评测直接消费 receipt，避免重新解释日志。

## 6.5 Retrospective

每轮任务或 Goal 结束生成结构化复盘：

- 目标与最终结论；
- 修改文件与关键决策；
- 通过的验收与证据；
- 失败原因、已排除路径、未解决问题；
- 模型、token、费用、耗时；
- 是否产生 regression candidate；
- 下一步建议。

Retrospective 与稳定项目记忆分开存储，按需检索。

## 6.6 Event

状态变更事件使用追加式 JSONL，至少包含：

- event_id、schema_version、ts；
- goal_id/task_id/run_id；
- session_id/turn_id/item_id（存在原生 ID 时保留，否则使用稳定的本地关联 ID）；
- type、from、to；
- component、step、duration；
- error_code、retryable；
- artifact/receipt 指针。

MCP 只推送状态变更和需要决策的事件，不把全部工具日志推给主管。

---

# 7. 功能需求

## 7.1 直接模式工具包

### 核心工具包

工人默认只加载以下高影响工具：

1. read
2. search
3. edit
4. create
5. delete
6. rename
7. run
8. finish
9. task_status/task_report（仅主管）
10. verify（仅主管或验收者）

### 可选工具包

按任务动态加载：

- repo_map / symbols / references；
- patch；
- checkpoint；
- memory_search / memory_show / memory_add；
- web_fetch；
- Playwright/browser；
- XERJ/LSP；
- task_diff / task_answer / task_steer；
- Goal 控制工具。

要求：

- 工具名称、参数和边界不重叠。
- 高频多步操作可以合并为高层工具。
- 工具描述从同一注册表生成 CLI、MCP 和 manual，避免三份漂移。
- 每个工具声明 read-only、repo-write、state-write、exec、network、destructive、token 等副作用。

## 7.2 代码智能

### 默认策略

1. 结构和词法优先：路径、文件名、符号、ripgrep、tree-sitter。
2. 语义检索按需：自然语言问题或跨模块定位。
3. LSP 定义/引用按需：重构、跳转、影响分析。
4. 仓库地图是导航入口，不是唯一真相源。

### 后端接口

```text
CodeIntelBackend
  search(query, filters)
  symbols(path)
  references(symbol)
  repo_map(path, budget)
  health()
```

实现优先级：

1. BuiltinBackend：当前 rg + tree-sitter，作为离线与小仓库 fallback。
2. XerjBackend：通过稳定 CLI JSON 或 MCP 调用 XERJ。
3. LspBackend：作为可选增强，不阻塞基础功能。

### XERJ 接入门槛

- 核验 canonical repository、版本、许可证和 NOTICE 要求。
- Query-1 许可证未明确前不依赖。
- 不 fork 整个 XERJ，不把 Python 核心绑定到其内部实现。
- 在固定数据集上比较 recall@k、首相关结果 token、延迟、索引更新时间、内存和断网 fallback。
- XERJ 不可用时自动退回 BuiltinBackend，并在结果中注明后端。

## 7.3 记忆系统

### 四层记忆

1. **静态规则**：根目录与嵌套 AGENTS.md/CLAUDE.md、路径规则。
2. **项目记忆**：架构、约束、命令、决策、踩坑，Markdown + front matter。
3. **任务进度**：当前 Goal/Task 的状态、已完成、剩余、阻塞。
4. **复盘记忆**：每轮 retrospective、失败路径、证据和回归候选。

### 要求

- 文件优先，可提交到 git；向量索引只作为可选检索加速。
- 每条记忆有 source、created、updated、scope、confidence、usefulness。
- 代码锚点变化时标记 stale；路径规则随文件位置作用域生效。
- 同主题冲突不静默覆盖，保留历史并要求显式解决。
- 自动记忆只写有证据的内容；模型猜测不能自动升级为规则。
- `memory brief` 保持有界，只加载核心规则和标题索引。
- 用户纠正、Goal 决策、已确认 bug 必须能进入长期记忆。

## 7.4 任务控制面

新增或补全：

- `task create --input task.json`
- `task status`
- `task wait`
- `task report`
- `task diff`
- `task answer`
- `task steer`
- `task resume`
- `task handoff`
- `task cancel`
- `task clean`
- `task accept`
- `task reject`

### 工人提问

工人工具集增加 `ask`：

```json
{
  "question": "是否需要兼容旧版 API？",
  "options": ["保持兼容", "只支持新版"],
  "recommendation": "保持兼容",
  "blocking": true
}
```

要求：

- 提问进入 task status/report，不依赖 UI。
- 主管通过 `task answer` 回答，答案写入任务上下文。
- 非阻塞问题不得无限等待。

### 中途纠偏

`task steer` 只能追加约束或澄清，不得偷换 outcome；所有变更留痕。

### 状态事件

新增 `task watch` 或 MCP notification：

- 只在 queued/running/verifying/paused/completed 等状态变化时通知；
- 不推送每个工具调用，避免主管 token 膨胀。

## 7.5 模型路由、健康与接力

### 模型能力档案

每个模型记录：

- context、max output、tool calling、reasoning 字段；
- 价格、缓存价格、rpm、并发限制；
- 最近成功率、延迟、429/5xx 次数；
- 支持的 WorkerBackend；
- 最后健康检查时间。

### 错误分类

| 类型 | 默认动作 |
|---|---|
| timeout/network/5xx | 有限重试，仍失败则 paused |
| 临时 429/capacity | 按 Retry-After 或配置等待，仍失败则 paused |
| 日配额耗尽 | paused，等待或换合规渠道 |
| 401/403 | failed，不盲目换模型 |
| 400/参数错误 | failed，修正配置 |
| 工具调用格式错误 | 本地修复或换模型 |
| bad response | 有限重试后 failed/paused |

### Handoff

`task handoff ID --model NAME`：

- 确认旧进程已停止；
- 保留 worktree、diff、检查点和累计预算；
- 使用结构化 handoff，不重放供应商私有 thought signature；
- 记录 from_model、to_model、reason、handoff_count；
- 默认最多 2 次，可配置；
- 不允许账号轮换或代理池。

### 自动接力

可选配置：

```toml
[worker]
failover = ["worker-b", "worker-c"]
max_handoffs = 2
auto_handoff = false
```

默认 `auto_handoff=false`，由主管决定；验证后再开放自动模式。

## 7.6 Worker 后端

```text
WorkerBackend
  run(TaskSpec, HandoffContext)
  cancel()
  status()
  collect_diff()
```

首批支持：

1. builtin：当前 Python 工人与 OpenAI 兼容 API Key 模型；
2. Codex CLI：官方 `codex exec --json`，已支持创建、预算停止、续跑和 builtin↔Codex 接力；
3. OpenCode v1：官方 `opencode run --format json`，已支持 usage、预算、会话续跑和接力；默认禁用 shell、网络工具、子 Agent 与项目配置；
4. Qoder CLI：官方 `qodercli --print --output-format stream-json`，复用账号登录或 PAT，已支持 Credits、预算、会话续跑和接力；默认禁用遥测、项目配置、MCP、shell、网络工具与子 Agent；
5. 其他 external CLI：Cursor 等按官方 headless 契约逐个适配；
6. MCP Agent：通过 MCP 调用外部 Agent。

要求：

- AgentKit 不绑定某一个最强工人。
- 所有后端都使用同一 TaskSpec、预算、diff 捕获和 EvidenceReceipt。
- 外部 CLI 默认不能直接操作主工作树；必须进入隔离 worktree/sandbox。
- 后端不可用时返回结构化错误，不静默降级成“成功”。

### 多编程工具接力（非代理、非账号轮换）

目标：Antigravity、Codex CLI 等工具可以作为独立工人，在 token/额度耗尽、上下文上限、超时或工具故障后，由另一个工人接着写；所有工人共享 AgentKit 的任务状态、项目记忆与验收证据。

**边界**：

- AgentKit 不转发这些工具的 API 流量，不嵌入其登录 Cookie/OAuth token，不做代理池、账号轮换或限额绕过。
- 每个工具只通过其官方支持的 CLI/API/RPC/MCP 接口调用，并使用用户为该工具单独配置的凭证；服务条款不允许第三方编排时不得接入。
- 只接可脚本化输入/输出的编程工具；不接 GUI 自动化、电脑操作工具，也不为需要模拟 Windows 沙箱的工具做兼容层。生产运行平台为 Linux VPS，Windows 仅 direct-mode 开发兼容。
- Antigravity 官方 FAQ 明确：consumer 计划不支持把 `agy` 包在第三方编程 Agent/应用里，也不支持自动账号轮换；因此 AgentKit 不包装 consumer Antigravity 登录态，只保留 HandoffPacket，未来仅评估明确允许的企业/API 方案。
- 不共享工具私有会话与 thought signature。交接使用 AgentKit 自己的结构化摘要、diff 和验收契约。
- 外部工具是需隔离的不可信工人，必须经过 sandbox、网络白名单、路径/命令权限和独立验收后才能启用。

接口与政策依据：[Antigravity headless](https://antigravity.google/docs/cli/headless/)、[Antigravity FAQ](https://antigravity.google/docs/faq/)、[Codex exec 文档入口](https://github.com/openai/codex/blob/main/docs/exec.md)。技术上有 headless 接口不等于所有账号/计划都允许第三方编排，接入前须逐计划核实。

Codex v0.172.0 已核验：`codex exec -C <repo> -m <model> -s read-only --json --output-schema schema.json -o answer.json -` 可输出 JSONL；`codex exec resume <thread_id>` 保留 rollout；沙箱为 read-only/workspace-write/danger-full-access，workspace-write 默认禁网；无内置总超时，需要父进程或 SDK `stream/interrupt`；usage 暴露 input/cached/cache-write/output/reasoning token。官方 Python SDK 可控制本地 `codex app-server` stdio JSON-RPC，但默认审批处理器会接受请求，AgentKit 必须显式配置审批策略；旧 MCP server 已移除。

OpenCode v1.19.6 已核验稳定 CLI 与 V1 权限 schema：`opencode run --format json --dir <worktree> --model provider/model [--session ID]`，`step_finish.part.tokens/cost` 提供用量；非交互权限请求默认拒绝。V1 适配器注入 `permission` 与 `OPENCODE_PERMISSION`，禁用项目配置、插件、网络工具和子 Agent，默认禁用 shell。V2 2.4.2 已单独核验并实现 `version="v2"`：使用 `--standalone run --format json`、V2 `permissions/policies`、cwd 工作树与独立数据目录；官方免费模型可免登录使用，真实最小任务成功，免费额度 429 暂停且不绕过。

Qoder CLI 1.1.98 已核验官方帮助与文档：`qodercli --print --output-format stream-json --input-format text --permission-mode dont_ask --setting-sources user --strict-mcp-config`，`system/init` 提供 session/tools/permissionMode，`assistant.message.usage` 提供 token 与请求 Credits，`result` 提供 subtype/error_code/permission_denials。账号登录由官方 `qodercli login` 或 `QODER_PERSONAL_ACCESS_TOKEN` 完成；AgentKit 不读取、导出或共享凭证，默认使用 `XDG_CONFIG_HOME/agentkit/qoder` 专用目录。保存登录态与仓库外 `config_dir` 必须由用户级配置显式授权，项目配置不能伪造该授权；专用 `key_env` 缺失时不会静默回退到保存登录态。适配器在 init 后复核工具与权限策略，失败即关闭。当前开发机 CLI 只读检查尚未登录，Desktop 登录态未自动复用；适配器的真实 CLI 参数已解析通过，仅被登录拦截；真实账号任务尚未执行，当前证据为官方 CLI 帮助、公开契约与假可执行文件端到端测试。

**HandoffPacket v1**：

```json
{
  "schema_version": "1.0",
  "task_id": "t1a2b3c",
  "goal_version": 3,
  "from_backend": "antigravity",
  "to_backend": "codex",
  "reason": "quota_exhausted|context_limit|timeout|tool_failure|supervisor_switch",
  "base_commit": "…",
  "acceptance_sha256": "…",
  "worktree": ".agentkit/worktrees/t1a2b3c",
  "diff_sha256": "…",
  "changed_files": ["path"],
  "checks": [{"cmd": "…", "result": "fail", "why": "…"}],
  "memory_brief_sha256": "…",
  "decisions": ["…"],
  "unknowns": ["…"],
  "remaining_budget": {"tokens": 0, "usd": 0, "minutes": 0}
}
```

交接包不得包含密钥、完整私有会话、用户未授权的代码外发地址或供应商认证头。接手工人在同一 worktree 上继续，AgentKit 保留累计预算与 `model_history/backend_history`，最终仍由 EvidenceReceipt 和独立验收决定完成。

**状态机**：

```text
running(A) → paused(quota/context/timeout) → handoff_ready
          → running(B, same worktree) → verifying → done | needs_review | failed
```

V1 先实现顺序接力。并行协作只用于可拆分的独立子任务和独立 worktree；不让多个工具同时改同一文件，避免冲突与重复成本。

**适配器能力**：

- `capabilities()` 返回版本化 `BackendCapabilities`：headless、streaming、session resume/fork、approval/diff 事件、native MCP、OAuth/API Key、usage/cost/Credits、取消；未声明能力保守为 false。
- 能力快照写入任务证据，并通过 `agentkit://capabilities` MCP 资源公开；Cursor CLI 与 SDK 的 usage/cost 能力分别声明。
- `run(HandoffPacket)` / `cancel()` / `status()` / `collect_diff()`。
- 工具无 headless 接口时，只生成可移植交接包，不伪装成已自动接管。
- usage 不可见时记录 `unknown`，不得估成 0；跨工具比较统一使用 verified success/USD。

落地顺序：先实现 HandoffPacket 与后端协议及假适配器测试 → 再实现网络白名单/凭证隔离 → 最后按官方文档逐个接入真实工具。未满足安全门禁前，外部工具接力默认关闭。

## 7.7 Goal Controller

### 组成

- GoalStore：持久化 GoalSpec、plan、状态、预算、任务列表。
- Planner：可选；默认使用主管提供的 plan。
- Scheduler：按依赖和预算启动任务。
- GoalVerifier：执行 Goal 级验收。
- Replanner：仅在失败或约束变化时生成新 plan 版本。
- RetrospectiveWriter：每轮生成复盘。

### 状态机

```text
draft → planned → running → verifying → completed
                         ↘ needs_review
                         ↘ paused
                         ↘ failed
                         ↘ cancelled
```

### 不变量

- 没有 acceptance 的 Goal 不能进入 planned。
- 子任务全部完成不等于 Goal 完成。
- 只有 GoalVerifier pass 才能 completed。
- 预算耗尽转 needs_review。
- Goal outcome 不可修改；约束变更产生新版本。
- paused 后恢复必须保留 plan、任务状态、预算和 retrospective。
- 默认最大任务数、轮数、时间、token、金额都有上限。

## 7.8 独立验收与证据

### 必须实现

1. **Baseline fail-to-pass**：同一验收在 base commit 上必须失败，修复后必须通过。
2. **Frozen acceptance**：验收命令、测试路径、配置和哈希在工人开始前冻结。
3. **Clean environment**：全新 worktree/容器，不采信工人环境。
4. **Layered verification**：
   - 单元/静态检查；
   - 功能或 E2E；
   - 回归评测；
   - 可选独立 reviewer。
5. **Environment fingerprint**：OS、运行时、依赖锁、环境变量名称（不含值）。
6. **EvidenceReceipt**：结果、日志哈希、diff 哈希、版本、成本。
7. **False positive/negative evaluation**：公开误报与漏报数据。

### 防作弊规则定位

现有 tests_deleted、tests_modified、check_script_modified 等规则保留为辅助信号，但不再是安全核心。安全核心是：

- 冻结验收；
- 基线失败；
- 干净环境；
- 不可篡改证据；
- 沙箱。

## 7.9 沙箱与安全

### Linux

优先使用 bubblewrap 或等效方案：

- 仅允许 worktree 和必要临时目录可写；
- `.git`、`.agentkit`、用户 home、密钥文件受保护；
- 目标默认拒绝网络，只允许模型接口和显式包管理源；当前提供 `[sandbox] network=false` 硬隔离与域名数组白名单，默认 `true` 仅兼容现有模型调用；白名单要求客户端遵守代理环境变量，直连出站被网络命名空间阻断；
- 进程组可杀，资源限制生效；
- 无法沙箱时拒绝派活或明确降级为 direct-only。
- `SandboxCapabilities` 显式声明 provider、pause/resume/fork、快照类型与网络模式；bubblewrap 当前只声明 Git/文件快照，不假装支持完整磁盘或内存快照。
- `SandboxStatus` 记录 pending/prepared/running/paused/verifying/completed/failed/cancelled/destroyed 生命周期，任务证据保留 provider 与能力快照。

### macOS

使用 Seatbelt 或等效方案；未验证前标记 experimental。

### Windows

- 直接模式可使用；
- 派活模式在 Job Object、文件 ACL 和网络限制验证前标记 unsupported；
- 不得用 worktree 冒充沙箱。

### 权限模型

- 能力式权限：fs_read/fs_write/exec/network/model/secret；
- allow/ask/deny；
- 高风险动作默认 ask；
- 正则命令拦截只作为体验护栏，不作为安全边界。

### 密钥与数据

- 配置只存环境变量名；
- 密钥注入到模型客户端，不进入工人工具环境；
- 日志、report、receipt、debug-bundle 全部脱敏；
- 模型调用前明确代码出境策略；
- 外部 benchmark 只能使用公开或用户明确授权的仓库。

## 7.10 MCP 接口

### stdio 第一阶段

提供：

- tools：核心工具和高层 task/goal 工具；
- resources：report、receipt、diff、log、bundle；
- notifications：状态变更、等待回答、需要决策；
- capabilities：版本、平台、工具包、后端健康度；
- outputSchema：所有结构化输出；
- annotations：read-only、destructive、network 等副作用。

### HTTP 后续

- 默认只读；
- Token 认证；
- 速率限制；
- 明确禁止未授权执行写操作。

### 要求

- CLI 与 MCP 共用同一命令/工具注册表。
- manual 从注册表生成，不能手写漂移。
- MCP 工具数量按核心/可选包渐进披露。
- Code Mode 作为实验，不默认启用。

## 7.11 评测与回归

### 外部基准

首批：

- Terminal-Bench 4.1 子集：终端工具、沙箱、防作弊与轨迹；
- Aider polyglot 子集：编辑合法率与 token 成本；
- SWE-Lancer 离线 IC 子集：全栈任务与独立验收；
- 内部长任务：多文件、迁移、Goal 闭环；
- 安全与作弊留出集：路径穿越、测试篡改、提示注入、密钥读取。

SWE-bench Verified 只作历史对照：OpenAI 2026 抽审指出测试/题面缺陷与训练污染，已停止将其作为主报指标。CodeEditorBench 是函数级编辑题，不测 Agent 闭环，只作辅助。

最小矩阵：每个外部基准先取 30 题、低/中/高 3 档固定模型与采样参数、AgentKit direct/builtin/external/Goal 对官方最小 harness、同预算 3 次；先验 gold/oracle 5 次。指标包含 pass@1/pass@3、编辑合法率、作弊/基础设施失败、token、美元、时延与 95% CI。公开逐题 JSONL、轨迹、补丁、配置、seed、全部失败/超时与汇总脚本，不挑题。

### 对照

1. 主管直接用 Claude Code/OpenCode 等原生工具；
2. AgentKit direct；
3. AgentKit builtin worker；
4. AgentKit external worker backend；
5. AgentKit Goal mode；
6. Grok Workhorse / Agentic Harness 等直接竞品。

### 指标

- pass@1、verified pass rate；
- 主管 token、工人 token、验收成本、总成本；
- 缓存命中与缓存感知成本；
- 轮数、工具调用、耗时；
- 人工介入次数；
- 误报通过率、可疑误报率；
- 每美元可验证成功任务数。

### 回归飞轮

- 每个已确认 bug 生成 regression candidate；
- 人工批准后进入冻结回归集；
- 每次发布跑留出集；
- 公开 JSON/Markdown 报告和复现脚本，不做 UI 看板。

## 7.12 发布与运维

- 语义化版本；
- `agentkit release plan/build/verify/rollback` 提供本地固定版本构建、wheel/sdist SHA256、版本化清单、artifact 元数据校验与显式回滚；
- 构建默认要求干净 git 工作树；`--allow-dirty` 仅用于测试，`--overwrite` 必须显式指定；
- 回滚默认 dry-run，`--yes` 才安装；Windows 控制台进程退出后由独立 helper 安装并写结果文件；
- 发布命令不 push、不发布、不签名、不自动升级；原生二进制签名在后续阶段实现；
- 固定版本安装命令由发布产物清单提供；
- 安装回滚保留 `.agentkit/` 状态，依赖与 Python 环境兼容性仍需发布矩阵验证；
- 支持平台矩阵；
- 日志保留、磁盘阈值、worktree 清理；
- 不自动升级用户环境。

---

# 8. 状态机

## 8.1 Task

| 当前状态 | 允许迁移 | 条件 |
|---|---|---|
| draft | queued/cancelled | acceptance 完整 |
| queued | starting/cancelled | 有可用工人与预算 |
| starting | running/interrupted | 进程启动成功 |
| running | verifying/paused/failed/stopped/cancelled | 工人结束或故障 |
| verifying | done/failed/invalid/needs_review | 独立验收完成 |
| paused | queued/failed/cancelled | resume 或 handoff |
| interrupted | queued/failed/cancelled | resume |
| stopped | queued/failed/cancelled | resume 或新预算 |
| done | cleaned | 不再需要 worktree |
| failed | cleaned | 不再需要 worktree |
| cancelled | cleaned | 不再需要 worktree |
| needs_review | cleaned | 人工审阅后决定 |

## 8.2 Goal

| 当前状态 | 允许迁移 | 条件 |
|---|---|---|
| draft | planned/cancelled | outcome 与 acceptance 完整 |
| planned | running/paused/cancelled | plan 与预算有效 |
| running | verifying/paused/failed/cancelled | 子任务推进结束 |
| verifying | completed/needs_review/failed | GoalVerifier 出结论 |
| paused | running/cancelled | 预算或约束更新 |
| needs_review | running/completed/cancelled | 人工决定继续或接受 |
| completed | archived | 证据已保存 |
| failed | archived | 证据已保存 |
| cancelled | archived | 证据已保存 |

---

# 9. 数据与存储

```text
.agentkit/
├── config.toml
├── goals/
│   └── g.../
│       ├── goal.json
│       ├── plan.json
│       ├── events.jsonl
│       └── retrospective.md
├── tasks/
│   └── t.../
│       ├── task.json
│       ├── report.json
│       ├── receipt.json
│       └── transcript.jsonl
├── worktrees/
├── logs/
├── memory/
├── retrospectives/
├── regression/
├── evidence/
└── bundles/
```

要求：

- 所有 JSON 状态写入使用原子替换。
- 跨进程状态使用文件锁或 SQLite 事务，禁止并发覆盖。
- receipt 和 evidence 内容寻址、不可变。
- 日志默认保留 7 天，可配置。
- 清理任务不删除 receipt、regression 和用户选择保留的记忆。

---

# 10. 非功能需求

## 10.1 性能

- 单次 CLI 启动与工具调用保持低延迟。
- `run` 输出流式过滤，禁止把无限日志整体读入内存。
- 大仓库索引按需构建、增量更新。
- 上下文压缩优先高保真结构化 handoff，不做无差别截断。
- 记录缓存 token，按缓存感知成本评估。

## 10.2 可靠性

- 所有操作幂等或可去重。
- 任务和 Goal 状态落盘，进程崩溃可恢复。
- 看门狗、取消和预算停止不留下孤儿进程。
- 换模型不重置累计预算。
- 外部后端失败不影响本地状态完整性。

## 10.3 可维护性

- 拆分 tasks.py、worker.py 等大模块为 store/scheduler/router/backend/verifier。
- 协议、后端、工具使用窄接口。
- 第三方代码只复制 MIT/Apache-2.0/BSD 并登记 NOTICE。
- 不维护大型本地 fork；适配器优先。
- CI 覆盖 Python 3.10/3.12、Linux，以及可用的 Windows direct 模式。

## 10.4 隐私

- 本地优先，无遥测。
- 用户代码只发送到明确配置的模型端点。
- debug-bundle 默认不含完整 transcript。
- 外部评测只使用公开或授权数据。
- 密钥永不进入 receipt、report、事件或记忆。

---

# 11. 分阶段实施计划

## P0：可信基座，约 1 周

### 范围

- 修复路径穿越、记忆 ID、Unicode 验收、权限绕过。
- 明确 Linux/Windows 支持边界。
- TaskSpec、EvidenceReceipt、Retrospective v1 schema。
- fail-to-pass 与冻结验收。
- `task resume --model` / `task handoff`。
- 输出预算硬上限与长行续读。
- 跨进程锁与唯一临时文件。

### 验收

- 已知 Critical/High 问题全部有失败测试和修复测试。
- 同一任务在 base 失败、修复后通过。
- 换模型保留 diff、轮数和累计预算。
- receipt 可由第三方重新校验哈希。

## P1：AI 控制面，约 2–3 周

### 范围

- MCP stdio、tools/resources/notifications/outputSchema。
- Goal Controller v1。
- task diff/answer/steer/accept/reject。
- 工具核心包/可选包。
- 状态变更事件流。
- 结构化 retrospective。

### 验收

- Muse 可通过 MCP 完成 create→wait→report→steer→answer。
- Goal 只有 GoalVerifier pass 才 completed。
- 预算耗尽只进入 needs_review。
- 新增命令自动进入 manual 和 schema 测试。

## P2：智能与生态，约 3–4 周

### 范围

- XerjBackend 与 BuiltinBackend fallback。
- LSP/ast-grep 可选适配。
- 外部 CLI/MCP WorkerBackend。
- 模型能力档案、健康状态和自动接力开关。
- 缓存感知成本。
- 作用域记忆、路径规则、回归候选。

### 验收

- XERJ benchmark 达到召回、延迟和 token 门槛。
- 外部工人产生同一 TaskSpec 和 EvidenceReceipt。
- 模型断流可由 Muse 或策略安全接力。
- 每个确认 bug 可生成 regression candidate。

## P3：外部验证与发布，约 2–4 周

### 范围

- Terminal-Bench 4.1、Aider polyglot、SWE-Lancer 离线 IC、内部长任务评测；SWE-bench Verified 仅历史对照。
- 安全与作弊外部留出集。
- 公开成本-质量报告。
- 固定版本、校验和、回滚文档。
- 再评估 zipapp/PyInstaller/原生二进制。

### 验收

- 至少 50 个真实任务、3 档模型、多轮次。
- 同等通过率下总成本改善达到 20% 或明确记录未达到。
- 误报通过率和可疑误报率公开。
- 干净环境一条命令安装指定版本并回滚。

---

# 12. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| Goal/多 Agent 变成 token 焚烧炉 | 高 | 默认关闭、有界预算、评测证明收益 |
| 工人绕过沙箱或验收 | 高 | 能力权限、冻结验收、留出作弊集 |
| XERJ API 变化或停止维护 | 中 | 适配器、固定版本、Builtin fallback |
| Query-1 许可证不明确 | 中 | 不依赖，只使用已核验引擎 |
| 模型免费档不稳定 | 高 | 健康状态、暂停恢复、模型接力 |
| 外部工人质量不可控 | 高 | 统一 TaskSpec、diff 捕获、EvidenceReceipt |
| 自评偏差 | 高 | 留出集、外部任务、第三方复现 |
| Windows 支持拖慢主线 | 中 | Linux-first，Windows direct-only 直至沙箱验证 |
| 功能范围再次膨胀 | 高 | P0-P3 门禁、C-xx 变更登记 |
| 没有外部需求 | 高 | 发布 benchmark，邀请真实 Agent 接入 |

---

# 13. 变更登记映射

| 编号 | 内容 | 本文件章节 |
|---|---|---|
| C-15 | 可信执行协议、Goal Controller 与任务控制面 | 5、6、7.4、7.7、8 |
| C-16 | 沙箱、独立验收与 EvidenceReceipt | 6.4、7.8、7.9、10 |
| C-17 | MCP、工具包与可替换 WorkerBackend | 5、7.1、7.6、7.10 |
| C-18 | 代码智能、记忆与上下文升级 | 7.2、7.3、10.1 |
| C-19 | 模型路由、健康状态与接力 | 7.5 |
| C-20 | 评测、回归、复盘与发布工程 | 6.5、7.11、7.12、11、12 |

---

# 14. 调研依据

以下资料用于形成本需求，不代表其中所有宣传数字都经过 AgentKit 本地复现。

- Qoder Goal-driven：<https://docs.qoder.com/qoder/goal-driven>
- Qoder Plan-driven：<https://docs.qoder.com/qoder/plan-driven>
- Qoder 自迭代实践：<https://blog.qoder.com/auto-iterate>
- Anthropic 上下文工程：<https://www.anthropic.com/research/effective-context-engineering-for-ai-agents>
- Anthropic 工具设计：<https://www.anthropic.com/research/writing-tools-for-agents>
- Anthropic 有效 Agent：<https://www.anthropic.com/research/building-effective-agents>
- Anthropic 长任务 Harness：<https://www.anthropic.com/research/harness-design-long-running-apps>
- XERJ：<https://github.com/xerj-org/xerj>、<https://xerj.org/>
- mini-swe-agent：<https://github.com/SWE-agent/mini-swe-agent>
- OpenHands SDK：<https://docs.openhands.dev/sdk/arch/overview>
- Cline Plan/Act：<https://docs.cline.bot/core-workflows/plan-and-act>
- Aider Repo Map：<https://aider.chat/docs/repomap.html>
- OpenCode Agents：<https://opencode.ai/docs/agents/>
- OpenCode Permissions：<https://opencode.ai/docs/permissions/>
- Agentic Harness：<https://github.com/moortekweb-art/agentic-harness>
- Grok Workhorse：<https://github.com/mrchatam/Grok-workhorse>
- SWE-bench：<https://www.swebench.com/>
- Terminal-Bench：<https://www.terminal-bench.com/>

---

# 15. 完成定义

AgentKit v1.0 目标形态完成，必须同时满足：

- P0 安全与验收问题关闭；
- TaskSpec/GoalSpec/EvidenceReceipt 可被第三方消费；
- MCP 可被真实主管 Agent 调用；
- Goal 闭环有界、可恢复、可审计；
- XERJ 与内置后端可切换且有 benchmark；
- 内置与外部工人都能产生统一证据；
- 模型断流可安全接力；
- 外部评测公开且可复现；
- 固定版本可安装、校验和回滚；
- 未达到的指标如实标注，不宣传为已完成。
