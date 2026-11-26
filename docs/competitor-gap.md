# 与主流编程工具的功能对比与补漏

更新：2026-11-26。对比对象：Claude Code、Codex CLI、OpenCode、Aider、jcode、pi（pi 已核验 canonical 仓库、v1.6.1 真实 JSONL 事件与公开 benchmark；其余功能来自公开文档和仓库，未逐项实测）。

| 能力 | Claude Code | Codex CLI | OpenCode | Aider | jcode | AgentKit V1.0 | 处理 |
|---|---|---|---|---|---|---|---|
| 读文件 | 有 | 有 | 有 | 有 | 有 | M-04 | — |
| 按模式列文件（glob / ls） | 有 | 经 shell | 有 | — | 有 | **缺** | C-02 并入 M-05 |
| 内容搜索 | 有 | 经 shell | 有 | — | 有 | M-05 | — |
| 仓库地图 | — | — | — | 有 | — | M-06 | 我们的优势 |
| 编辑 / 补丁 | 有 | apply_patch | 有 | 多种格式 | 有 | M-07 | — |
| 新建 / 删除 / 重命名文件 | 有 | 有 | 有 | 有 | 有 | **未写明** | C-02 并入 M-07 |
| 编辑后语法检查 | — | — | LSP 诊断 | lint | — | M-07 | V1.1 加 LSP 诊断 |
| 执行命令 / 后台任务 | 有 | 有 | 有 | 有 | 有 | M-08 | — |
| 命令权限（允许 / 禁止列表、危险命令拦截） | 有 | 审批策略 + 沙箱 | 有 | — | 风险分级 | **只禁了 git push** | C-02 新增 M-22 |
| 沙箱 | — | 有 | — | — | — | Should | 保持 |
| 检查点 / 撤销 | 有 | — | 有 | git 自动提交 | 有 | M-09 | — |
| 子 Agent | 有 | — | 有 | 架构师 / 编辑器双模型 | swarm | M-12/M-13 | — |
| 上下文压缩（长任务） | 有 | 有 | 有 | — | 有 | **缺** | C-02 并入 M-12 |
| 任务计划 / 待办 | 有 | 有 | 有 | — | 有 | 未写 | Should |
| 记忆 / AGENTS.md | 有 | 有 | 有 | 约定文件 | 有 | M-21 | — |
| 网页读取 / 查文档 | 有 | 有 | 有 | 有 | 有 | 未写 | Should（工人查 API 文档用） |
| MCP 客户端（工人调外部工具） | 有 | 有 | 有 | — | 有 | 未写 | Should |
| MCP 服务端（给主管用） | 有 | 有 | — | — | 有 | Should | 保持 |
| 无界面 / 脚本化运行 | 有 | exec | 有 | 有 | 有 | M-02 | — |
| 会话恢复 | 有 | 有 | 有 | — | 有 | M-13 | — |
| 提示词缓存 | 有 | 有 | 有 | 有 | 有 | 未写 | Should |
| Hooks | 有 | — | 插件 | — | 有 | 未写 | Could |
| 技能 / 斜杠命令 | 有 | 有 | 有 | — | 有 | — | Could |
| 图片输入 | 有 | 有 | 有 | 有 | 有 | — | Won't（V1） |
| Notebook 编辑 | 有 | — | — | — | — | — | Could |
| 独立验收 + 防作弊 | — | — | — | — | — | M-10/M-15 | 我们的优势 |
| 统一输出预算 | — | — | — | — | — | M-03 | 我们的优势 |
| 直接模式 / 主管派活接口 | — | — | — | — | — | M-11/M-13 | 我们的优势 |

## 变更 C-02（已登记到基线）
- M-05 增加：`ls` / `glob` 按模式列文件（遵守 .gitignore、受输出预算约束）。
- M-07 增加：新建、删除、重命名文件，同样走检查点。
- M-12 增加：工人上下文压缩（接近模型上限的 70% 时，把早期工具输出折叠为摘要，保留任务目标、记忆、最近 N 轮）。
- 新增 M-22 命令权限：默认禁止列表（`git push`、`git commit`、`rm -rf /`、`sudo`、`curl | sh`、读取 `~/.ssh` 等）、可配置允许列表、违规时拒绝执行并写入报告的可疑标记。
- Should 增加：任务计划工具、网页读取、MCP 客户端、提示词缓存、LSP 诊断。

## pi 专项核验（2026-11-26）

### 一手事实

- canonical 仓库是 [earendil-works/pi](https://github.com/earendil-works/pi)，旧地址 `badlogic/pi-mono` 会重定向；当前核验版本 v1.6.1，MIT 许可。
- 默认模型工具只有 `read / bash / edit / write`；`grep / find / ls / powershell / codemode / tool_search` 按需启用。系统提示、工具声明、技能描述、项目上下文和 cwd 分层组装，技能正文按需读取。
- 会话是 JSONL 树；只有活动分支进入下一次模型请求。压缩默认在 `contextTokens > contextWindow - 16384` 时触发，保留最近约 20,000 token；摘要记录 Goal、Constraints、Progress、Decisions、Next Steps、Critical Context，以及累计读/改文件列表。用于摘要的工具结果每条最多 2,000 字符。
- 默认 read/bash 结果截断到 2,000 行或 50 KB；完整输出可落到临时文件。JSON mode 输出严格 JSONL，`agent_settled` 才表示自动重试、压缩恢复和排队工作全部结束。
- pi **没有内置文件/进程/网络/密钥权限系统或沙箱**；项目 trust 只控制项目资源加载，不限制工具行动。生产使用必须另加容器、microVM 或 OS 沙箱。
- 体积不是“极小”：npm v1.6.1 包解压约 24.9 MB；临时 npm 安装含依赖约 152 MB；Windows x64 独立压缩包 62,017,479 字节。独立包包含运行时，体积换安装便利，不等于运行内存低。作为参照，AgentKit 当前 wheel 为 184,591 字节、含开发依赖的虚拟环境约 35 MB，但 Python 运行时不计入 wheel，二者不能直接等价。

### “同模型更省 token、效果更好”的证据边界

- [Composio 的 30 任务实测](https://composio.dev/content/pi-agent-vs-claude-code)使用同一 DeepSeek V4 Flash：Claude Code 16/30、平均 741,659 token/任务、总成本 $3.12；pi 20/30、558,885 token/任务、$0.56。pi token 约少 24.6%，成功数更多。但 pi 用 high 而非 max reasoning，24/30 走官方 DeepSeek API、其余走 OpenRouter，其他 harness 走 OpenRouter，因此不能把差距全部归因于 harness。
- [预注册双 harness 论文](https://arxiv.org/html/2608.01347v1)覆盖 4,643 次有效运行：在匹配的 model-task-prompt 单元上，pi 0.82.1 的固定前缀为 1,147–1,642 token，Claude Code 2.1.220 为 15,983–20,330 token；Claude Code 每成功任务成本高 5–30×，轮数多 2–7×。该研究任务较小（≤4 文件），主模型组经过协议网关，且版本不是当前 pi v1.6.1，只能作为机制证据，不能当作当前版本 universal benchmark。
- 结论：**小前缀 + 少轮次 + 结果截断 + 结构化压缩**是真实机制；省 token 不等于完成质量更高，必须按“每个独立验证成功任务的成本”比较。缓存折扣不能计入效率收益。

### AgentKit 的实测与决策

本仓库当前工人系统提示 912 字符；默认 14 个工具 schema 共 3,497 字符，合计约 1,102 token（字符/4 估算）。pi v1.6.1 在同一台机器、自定义短系统提示和默认四工具下，系统提示 80 字符、工具 schema 2,956 字符，合计约 760 token。AgentKit 多出的工具承载了输出预算、精确编辑、记忆、提问和独立验收契约，不能仅按 token 数删除。候选核心包（read/search/repo_map/edit/create/delete/rename/run/ask/finish）schema 约 682 token，极简六工具约 444 token，但省 schema 可能增加 shell 轮次，必须先做同任务 A/B，不能直接改默认。

借鉴顺序：固定稳定前缀 → 按需暴露工具 → 收紧工具结果 → 树状会话/结构化压缩 → 以 verified success/USD 评测。暂不接入 pi 外部工人：其原生 bash 会绕过 AgentKit 命令权限，当前 bubblewrap 仍共享网络且不是保密边界；外部 WorkerBackend 必须等网络白名单与权限平面通过生产门禁后再开放。

## 核心资产复核（2026-11-26）

- 已商品化：headless/MCP、worktree、steps/timeout/金额预算、repo map、检查点、Goal 循环、第二模型评审。保留为兼容能力，不再作为独立卖点。
- 可验证差异一：**证据链**。基线 fail-to-pass、干净环境、不可变 EvidenceReceipt、环境指纹、防作弊；用留出作弊集量化误放行与误报。
- 可验证差异二：**连续接力**。Muse、内置工人、外部 CLI 共用 Goal/worktree/累计预算/HandoffPacket；验证接力后收据连续、预算不重置、无凭证泄漏。
- 可验证差异三：**成本结果**。只比较每个独立验证成功任务的总成本（含验收与返工，不计缓存折扣）；记忆、压缩、XERJ 只有改善该指标才算资产。
- 最高风险：网络白名单与凭证隔离、真实外部适配器、真实长任务/公开 benchmark 与留出作弊集。核验范围与来源见 [PRD C-21](file:///U:/Agentkit/docs/prd-v1.0.md#L613) 及 [功能来源](file:///U:/Agentkit/docs/feature-sources.md#L146)。
