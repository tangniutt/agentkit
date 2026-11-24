# Issue 评估流程（C-12 AI 反馈进化回路）

AgentKit 的主要用户是 AI。AI 在真实使用中遇到的工具错误（bug）和更好的设计想法（improvement / design proposal）都可以变成 Issue；每一个能让工具继续进化的 Issue 都要按本文件评估、给出结论，接受的变成基线里的 C-xx 变更，**合并前用评测量化**。

## 1. 进来的路径

| 来源 | 命令（只生成草稿，`posted:false`，从不自动提交） | 表单 | 标签 |
|---|---|---|---|
| 工具出错（`AK-INTERNAL`、工人崩溃） | `agentkit feedback --bug <run/task id>` | `bug_report.yml` | `bug` + `ai-feedback` |
| 用过之后的改进 / 工具设计建议 | `agentkit feedback --improve "一个具体改动" --from <id> [...]` 或 `--input file.json` / `-` | `improvement.yml` | `ai-feedback` + `needs-eval` |
| 人类的功能需求 | `agentkit feedback --idea "..."` | `feature_request.yml` | `enhancement` |

AI 必须遵守：**真实使用之后、带证据**才提；一个 Issue 一个想法；先打开草稿里的 `search_first` 链接（按 `keywords` 搜），已有同类 Issue 就在那里评论 / +1 并附上自己的证据，不开新的；绝不包含私有代码或密钥（证据只有运行 / 任务 ID、状态、轮数、token、错误码与次数、可疑标记名）；**提交前先问自己的人类用户**（Issue 是公开的）。

任务 / 验收结束时，如果出现值得报告的信号，结果的 `next` 会带**最多一条**提示（`agentkit feedback ... (原因; only if AgentKit is at fault; ask your human before filing)`）：`AK-INTERNAL` / 工人崩溃 → `--bug`；同一工具错误 ≥3 次、编辑失败合计 ≥3 次、卡住、工具调用格式连续失败、工具环节导致的失败、`needs_review`（可能误报）→ `--improve --from`。干净的运行不加任何字段，主管 token 不变。

## 2. 自动初筛（`.github/workflows/issue-triage.yml`）

不用任何密钥、不 checkout、不执行 Issue 里的任何内容，权限只有 `issues: write`。对新建 / 编辑 / 加标签的 Issue：

1. 判断是否 AI 提交：已有 `ai-feedback` 标签，或「Filed via」字段含 `agentkit feedback`，或勾选了「Filed by an AI agent」。不是则什么都不做。
2. 补 `ai-feedback`；尚无结论标签时加 `needs-eval`。
3. 检查必填段落（改进：Observed、Proposal、Expected gain、Cost / risk、How to verify；bug：Minimal reproduction、Expected、Actual；需求：Problem、Proposal），空的、`_No response_`、草稿占位 `<...>` 都算缺。
4. 按 `agentkit/redact.py` 的同一组规则扫描像密钥的字符串（只报行号与类型，不回显内容）。
5. 有问题：加 `needs-info`，发 / 更新**一条**评论请作者补全或改成 `[REDACTED]`（并轮换真实密钥）；修好后再编辑会自动去掉 `needs-info`。已有结论标签时去掉 `needs-eval`。

## 3. 评分（维护者或维护者指派的 AI 评估，写在 Issue 评论里）

每项 0–3 分，按权重相加（满分 30）：

| 维度 | 权重 | 0 | 3 |
|---|---|---|---|
| 证据强度 | ×2 | 无运行记录、纯猜测 | 多个运行 / 任务 ID，错误码与次数可复现，或有评测数字 |
| 预期收益 | ×2 | 说不清 | 主管 token / 轮数 / 失败率 / 内存有可量化的下降，覆盖常见任务 |
| 代价 | ×1 | 大改、新依赖、常驻进程 | 小改动、只用标准库 |
| 风险 / 兼容 | ×1 | 改输出 schema、错误码改名、破坏旧行为 | 只新增字段或开关，旧行为不变 |
| 许可证 | ×1 | GPL / AGPL / 未核实却要复制代码 | 只借思路，或 MIT / Apache-2.0 / BSD 且可登记 NOTICE |
| 符合目标 | ×3 | 违背 AI 优先、省 token、低内存、离线可用 | 直接服务 AI 优先、低 token、低内存目标 |

一票否决：泄露密钥或私有代码（先让作者删改）、绕过平台限额（账号轮换、代理池）、自动 push / 部署 / 遥测、要求复制不兼容许可证的代码。

建议阈值：≥20 分 `accepted`；12–19 分且证据不足 → `needs-info`（请补运行 ID 或评测）；<12 分 `declined`（评论说明原因与可接受的改法）；与已有 Issue 重复 → `duplicate` 并链接原 Issue（把新证据搬过去）。

## 4. 标签生命周期

```
新 Issue ──(AI 提交)──> ai-feedback + needs-eval ──> 评分
                          │                         ├─> accepted ──> 基线 C-xx ──> 实现 + 测试 + 评测 ──> 关闭（引用提交）
                          │                         ├─> declined（说明原因）──> 关闭
                          │                         ├─> duplicate（链接原 Issue）──> 关闭
                          └─ 缺段落 / 疑似密钥 ──> needs-info ──(作者编辑补全)──> 回到 needs-eval
```

## 5. 接受之后：变成 C-xx

1. 在 `docs/v1.0-baseline.md` 第 9 节「变更登记」加一行：日期、提出人（写 Issue 链接与「AI 反馈」）、内容、分档（Must / Should / Could）、结论。V1.0 范围冻结：除修正既有 Must 的缺陷外，新功能默认 Should，V1.0 发布后做。
2. 实现时写明借鉴来源与许可证（`docs/feature-sources.md`，复制代码登记 `NOTICE`）。
3. Issue 里的「How to verify」变成测试或 `eval/` 题目；合并前跑 `pytest -q` 与 `python -m eval.run`，把前后数字写进 `docs/STATUS.md` 对应 C-xx 行。数字变差就不合并。
4. 关闭 Issue 时引用提交与 C-xx 编号。

## 6. 定期调研

除了用户反馈，维护者定期调研先进的 AI 编程工具设计（token 节省、幻觉控制、工具调用、内存占用、搜索与提示词），把可借鉴点按同一评分表打分，接受的同样登记为 C-xx（例如研究得出的 C-13 计划包）。
