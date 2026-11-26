# 功能清单与借鉴来源

更新：2026-11-23 · 对应 V1.0 基线 + 变更 C-01（项目记忆）

开工规则：写任何模块前，先打开下表「参考代码位置」里的实现读一遍；能复用的直接复用（保留版权声明并登记到 `NOTICE`），不能复用的只借思路。

## 一、参考项目总表（许可证已通过 GitHub API 核实，2026-11-23）

| 项目 | 许可证 | 规模 / 活跃度 | 语言 | 用途 | 能否复制代码 |
|---|---|---|---|---|---|
| [jcode](https://github.com/1jehuang/jcode) | MIT | ~2.2 万星，持续更新 | Rust | 轻量单二进制、低内存多会话、swarm 子 Agent、工具核心 | 可以 |
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | MIT | ~1 万星 | Python | 极简工人循环（M1 原型骨架） | 可以 |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | MIT | ~2.1 万星 | Python | 窗口式读文件、编辑后 lint 拒写、filemap | 可以 |
| [Aider](https://github.com/Aider-AI/aider) | Apache-2.0 | ~5 万星 | Python | tree-sitter 仓库地图、编辑格式、lint | 可以（保留 NOTICE） |
| [XERJ](https://github.com/xerj-org/xerj) | Apache-2.0 | ~1.9 万星 | Rust | 代码搜索 / 定义 / 引用、记忆、输出预算 | 可以；Query-1 模型闭源，只能作为可选组件 |
| [pi](https://github.com/earendil-works/pi) | MIT | v1.6.1（2026-11-25） | TypeScript | 极简 agent harness、四工具默认集、树状会话、结构化压缩、JSONL/RPC | 可以；本轮只借鉴设计，不复制代码 |
| [Grok Workhorse](https://github.com/mrchatam/Grok-workhorse) | MIT | 5 星，较新 | JavaScript | 主管派活 MCP 接口、守护进程跑测试、工人不能 push | 可以 |
| [worker-bridge](https://github.com/trollbot2012/worker-bridge) | MIT | 0 星，较新 | Python | 独立 worktree、独立验证 diff、并发限制、日志脱敏 | 可以 |
| [LooppooL](https://github.com/Penty-d/LooppooL) | MIT | 0 星，较新 | TypeScript | 「验证者假设工人会撒谎」、模型分档、预算上限 | 可以 |
| [Spewer](https://github.com/modiqo/spewer) | Apache-2.0 | 1 星 | Rust | 派活后返回「带证据的收据」 | 可以（保留 NOTICE） |
| [Serena](https://github.com/oraios/serena) | GitHub 显示 NOASSERTION（主体 GPL-3.0，SolidLSP 部分 MIT，待逐文件核实） | ~3.2 万星 | Python | LSP 符号级查找 / 编辑 | 只借思路，核实前不复制 |
| [OmniRoute](https://github.com/diegosouzapw/OmniRoute)（镜像 cheaper-inference/OmniRoute） | MIT | 活跃 | TypeScript | 上下文压缩：RTK 命令输出过滤、会话去重、渐进老化、校验器、缓存友好前缀、简洁输出风格（C-08） | 可以（已复用严重词表与部分过滤正则，见 NOTICE 第 3 条） |
| [RTK](https://github.com/rtk-ai/rtk) | Apache-2.0（2026-11-23 GitHub API 核实） | 活跃 | Rust | 命令感知输出过滤、过滤器即数据 + 内联测试、原始输出可找回（C-08） | 可以；本轮只借思路，未复制代码 |
| Qoder | 闭源（只有公开文档和技术博客） | — | — | 记忆系统设计 | 不能，只借思路 |
| Claude Code | 闭源 | — | — | 工具设计、Agent 体验 | 不能，只借思路 |
| Nib、Choragos | 未核实 | — | — | 编辑格式按模型家族、单 Go 二进制派活 | 核实前不复制 |

小星项目（Grok Workhorse、worker-bridge、LooppooL）代码可复用，但成熟度低，借鉴时必须自己补测试。

## 二、功能清单与来源

### A. 安装与运行（Must）
| 编号 | 功能 | 借鉴 | 参考代码位置 |
|---|---|---|---|
| M-01 | 一条命令安装、单二进制 | jcode 安装脚本与发布流程；XERJ 单二进制 | `jcode/scripts`、`jcode/RELEASING.md`、`jcode/packaging` |
| M-18 | doctor 自检 | jcode provider doctor；XERJ `doctor` | `jcode/crates/jcode-provider-doctor` |
| M-19 | llms.txt | XERJ 文档做法 | `xerj/` 根目录文档 |
| — | 低内存多任务 | jcode 每多一个会话约 3.3 MB 的架构（自报数据） | `jcode/docs/SERVER_ARCHITECTURE.md`、`jcode/docs/MEMORY_BUDGET.md` |

### B. 省 token 工具集（Must）
| 编号 | 功能 | 借鉴 | 参考代码位置 |
|---|---|---|---|
| M-02 | CLI 紧凑 JSON | Grok Workhorse 结构化结果 | `Grok-workhorse/bin`、`lib` |
| M-03 | 输出预算、截断续读 | XERJ 输出预算；SWE-agent 窗口式输出 | `xerj/`；`SWE-agent/tools/windowed` |
| M-04 | read 按行段 / 符号 | SWE-agent windowed；Serena 思路 | `SWE-agent/tools/windowed` |
| M-05 | search | XERJ 搜索；SWE-agent search | `SWE-agent/tools/search` |
| M-06 | repo_map | Aider 仓库地图（tree-sitter + 排名） | `aider/aider/repomap.py` |
| M-07 | edit + 语法检查拒写 | SWE-agent 编辑后 lint；Aider 编辑格式与 linter | `SWE-agent/tools/windowed_edit_linting`、`windowed_edit_replace`；`aider/aider/coders`、`aider/aider/linter.py` |
| M-08 | run | mini-swe-agent 环境执行；jcode 后台任务 | `mini-swe-agent/src/minisweagent/environments` |
| M-09 | checkpoint | jcode 原生版本控制思路；Grok Workhorse worktree | `jcode/docs/AGENT_NATIVE_VCS_CORE_BEHAVIOR.md` |

### C. 派活、子 Agent 与验收
| 编号 | 功能 | 借鉴 | 参考代码位置 |
|---|---|---|---|
| M-10 | verify 独立验收、≤300 token 报告 | LooppooL 验证任务；worker-bridge 独立验证；Spewer 证据收据 | `LooppooL/src`、`worker-bridge/src` |
| M-11 | 直接模式 | 自研（无现成项目） | — |
| M-12 | 内置工人循环 | mini-swe-agent（约百行的 Agent 循环） | `mini-swe-agent/src/minisweagent/agents` |
| M-13 | task create / status / wait / report / cancel | Grok Workhorse 的 delegate_task / wait_task / task_result | `Grok-workhorse/bin/workhorse-mcp`、`lib` |
| M-14 | 独立 worktree、工人不能 push | Grok Workhorse；worker-bridge | `Grok-workhorse/lib`、`worker-bridge/src` |
| M-15 | 防作弊检查 | LooppooL「工人可能撒谎」；Grok Workhorse 守卫插件 | `Grok-workhorse/adapters/kilo/config/plugin/workhorse-guard.js`、`adapters/claude-code/pretooluse-guard.mjs` |
| M-16 | 预算硬上限 | LooppooL 预算；jcode 账号路由不静默扣费原则 | `LooppooL/src`；`jcode/docs/ACCOUNT_ROUTING.md` |
| M-17 | 密钥不进上下文、日志脱敏 | worker-bridge 日志脱敏；jcode 会话环境隔离 | `worker-bridge/src`；`jcode/docs/HARNESS.md` |
| Should | 并行工人互相通知「你读过的文件被改了」 | jcode swarm | `jcode/crates/jcode-swarm-core` |
| Should | 模型分档路由 | LooppooL；routr 思路 | `LooppooL/src` |
| Could | 工人再派子工人（嵌套） | jcode swarm 自主派队友 | `jcode/crates/jcode-swarm-core` |

**子 Agent 设计结论**：V1 中「工人」就是子 Agent，由主管派出，可并行、各自独立 worktree。V1 不允许嵌套（工人再派工人），避免成本失控和难以验收；V1.1 加入 jcode 式「文件被改通知」。

### D. 项目记忆（变更 C-01，新增 Must M-21）

Qoder 记忆系统闭源，但其文档和技术博客公开了设计；jcode 记忆依赖付费的 Jev 决策接口，不符合我们「零依赖、免费可用」的目标。以下是为 AI 使用者重新设计的方案。

| 功能 | 借鉴 | 为什么对 AI 好用 |
|---|---|---|
| 两层记忆：静态（AGENTS.md / 规则，人或主管写）+ 自动（工具写） | Qoder CLI 的 Static memory + Auto-memory | 稳定规矩和经验分开，不互相污染 |
| 本地 Markdown 存储，可提交到 git 与团队共享 | Qoder CLI auto-memory 为本地 Markdown | AI 和人都能直接读改，零依赖 |
| 分类：项目信息、开发约定、踩坑经验、工具与模型经验、主管偏好 | Qoder 的 5 大类（用户偏好、项目信息、开发约定、经验教训、工具与技能） | 按类检索，结果更准 |
| 固定加载：最核心 ≤300 token（测试命令、构建命令、禁区）每次开工自动给 | Qoder「固定查询」：必须一开始就知道的记忆不等检索 | 工人不用每次重新摸索怎么跑测试 |
| 记忆目录：只给标题列表，正文按需取 | Qoder 经验「模型不回忆，是因为不知道你记得什么」 | 用几十 token 让模型知道有哪些记忆 |
| 关联：每条可写 `related`，检索时顺带给出相关条目标题 | Qoder 记忆网络（解决「重构→要跑测试」这类多跳） | 低成本解决语义上不相关的关联 |
| 检索：关键词全文检索（SQLite FTS5），V1 不用向量 | Qoder 混合检索的简化版 | 零模型依赖、毫秒级 |
| 自动写入（确定性，无需模型）：验收通过的测试命令、失败原因、哪个模型在哪类任务上通过 / 失败 | 自研；Qoder Agent 执行日志提取的思路 | 不花 token 就能积累经验 |
| **代码锚定失效**：记忆可绑定文件或符号，对应代码改动后自动标记「可能过期」 | 自研（未在参考项目中见到） | 防止 AI 照着过期记忆犯错，这是 AI 专用的关键点 |
| 反思：任务结束后记录用过哪些记忆、任务是否通过，给记忆打有用分，低分降权 | Qoder Reflection Agent 的效果评估 | 记忆越用越准；V1 只做计数，V1.1 再用模型精炼 |
| 冲突处理：同主题新记忆覆盖旧记忆并保留历史 | Qoder 冲突检测与合并 | 不会出现两条互相矛盾的规矩 |
| 输出同样受预算约束 | 本项目 M-03 | 记忆不会反过来撑爆上下文 |

**M-21 的 V1 范围**：读取 AGENTS.md；`memory add / list / search / show / forget`；固定加载核心记忆；记忆目录；关键词检索；验收后的确定性自动写入；代码锚定失效标记。
**放到 Should**：模型驱动的经验提取与精炼、有用分降权、关联多跳自动扩展、跨项目全局记忆、可选接入 XERJ 记忆。

**M-21 验收标准**：
- 新工人开工时，核心记忆 + 记忆目录合计 ≤500 token；
- 同一仓库第二次派同类任务，工人用于「摸索怎么跑测试」的工具调用次数下降 50% 以上（评测集上对比）；
- 绑定的代码改动后，相关记忆 100% 被标记「可能过期」；
- 记忆中扫描不到任何密钥明文。

### E. 上下文压缩（变更 C-08）

| 功能 | 借鉴 | 参考代码位置 | 实现 |
|---|---|---|---|
| 命令感知输出过滤（过滤器是 JSON 数据 + 内联样例，选择顺序：命令 → 输出特征 → generic） | OmniRoute RTK 引擎；RTK | `OmniRoute/open-sse/services/compression/engines/rtk/{lineFilter,commandDetector,filterLoader}.ts`、`filters/*.json`；`docs/compression/RTK_COMPRESSION.md` | `agentkit/compress.py`、`agentkit/filters/*.json`（15 个） |
| 严重行永不丢（error/fail/assert/traceback…） | OmniRoute `severityVocabulary.ts`（MIT，词表已复用） | 同上 | `compress.SEVERITY` |
| 重复行计数折叠、相似行分组 | RTK `deduplicator.ts` / `grouper.ts` 思路 | `engines/rtk/deduplicator.ts`、`grouper.ts` | `apply_filter` 第 2–4 步 |
| 安全校验：代码块、路径、URL、版本号、错误行不变，否则退回原文 | OmniRoute `validation.ts` | `open-sse/services/compression/validation.ts` | `compress.validate` |
| 原始输出可找回 | RTK raw output recovery | `engines/rtk/rawOutput.ts` | `agentkit logs RUN --raw`、`run --raw` |
| 会话去重（内容哈希 → same_as_turn） | OmniRoute `engines/session-dedup` | `engines/session-dedup/index.ts` | `agentkit/context.py` |
| 渐进老化（最近 2 轮原样，更早的工具结果一行摘要，批量进行） | OmniRoute `progressiveAging.ts`、`cacheAwareConfig.ts` | 同名文件 | `agentkit/context.py`、`worker.py` |
| 简洁输出风格 | OmniRoute `outputMode.ts` / `outputStyles`（caveman） | `open-sse/services/compression/outputMode.ts` | `worker.STYLE_TERSE` |
| 缓存友好的稳定前缀（system + 工具定义 + 记忆摘要字节不变） | OmniRoute `prefixFreeze.ts` | 同名文件 | 老化从第 3 条消息开始；测试逐次请求比对前缀 |

### F. 主管 AI 操作手册（变更 C-09）

只借思路，没有复制任何第三方代码（NOTICE 不变）。

| 功能 | 借鉴 | 说明 | 实现 |
|---|---|---|---|
| 给 AI 读的入口文档 | llms.txt 约定（llmstxt.org，Answer.AI 提出） | 一个短小、Markdown、给大模型读的入口文件，链接到更详细的文档；本项目 llms.txt 已沿用，C-09 在其中链接手册 | `llms.txt`「Operating manual」一节 |
| 机器可读的命令自描述、从真实命令定义生成 | GitHub CLI `gh`（MIT）：`gh help reference` 由命令树生成完整参考；`--json` 不带字段时列出可用字段 | 文档从代码生成而不是手抄，所以不会漂移；输出字段可被发现 | `manual.walk()` 遍历 `cli.build_parser()`；`agentkit manual --format md` |
| 按需取单个对象的说明，避免一次读全部 | Kubernetes `kubectl explain <resource>[.field]`（Apache-2.0） | 主管只取它要用的命令详情，省 token | `agentkit manual <命令>`（单命令约 90–370 token） |
| 错误码 → 修复动作 | 本项目 C-03 错误码登记表 | 手册的「错误恢复」直接读 `events.ERRORS`，不另写一份 | `manual.recover()` |

### G. 可替换代码智能（变更 C-18）

| 功能 | 借鉴 | 许可证 | 实现 |
|---|---|---|---|
| 文件夹索引、代码检索、定义查询 | XERJ `autoindex` / `search` / `def` | 引擎 Apache-2.0 | `agentkit/codeintel.py` 的 `XerjBackend` |
| 记忆存取 | XERJ `/_memory` Elasticsearch-compatible API | 引擎 Apache-2.0 | `memory_store` / `memory_recall` |
| 无 XERJ 时回退 | 本项目 rg + tree-sitter | Apache-2.0 | `BuiltinBackend` |
| 统一后端选择 | 自研窄接口 | — | `backend=auto\|builtin\|xerj` |

来源：<https://github.com/xerj-org/xerj>、<https://xerj.org/llms.txt>。只调用公开 CLI/HTTP，不复制代码。Query-1 是独立专有模型，本项目不依赖；默认 XERJ embedding 是词法特征，不是神经语义。验证使用 SHA-256 核验的 rc.105 Windows 二进制、临时节点和样例仓库，真实执行 search/def/memory store/recall。`eval.codeintel_bench` 在同一 AgentKit 仓库上对比内置与 XERJ 的 recall@5、MRR、返回字符和延迟；XERJ 原始 JSON 的大响应由适配层按 `--full` 截断。静态复核确认上游 MCP 已有词法 `xerj_references`，但不解析 alias/同名符号/调用边，注释与字符串也计入；AgentKit 尚未暴露或实测该接口。语义引用仍应走 LSP，结构匹配走 ast-grep。

### H. pi 极简 harness 设计（2026-11-26 核验）

| 功能 | 借鉴 | 许可证 | 实现 / 结论 |
|---|---|---|---|
| 默认四工具与按需工具 | pi `createCodingTools`、`--tools` | MIT | AgentKit 保留现有工具与验收契约；先测前缀和轮次，不按工具数量盲目删功能 |
| 稳定分层系统提示 | pi `system-prompt.ts` | MIT | 已有稳定前缀；后续增加前缀体积与缓存命中评测 |
| 树状会话与活动分支重放 | pi `session-manager.ts` | MIT | Goal/task 已有 worktree 与结构化 resume；V1.1 评估分支会话，不重放供应商 thought signature |
| 结构化压缩 | pi `compaction.ts` / `utils.ts` | MIT | 已有规则老化与 70% 兜底；借鉴 Goal/Constraints/Progress/Decisions/Next Steps 摘要格式 |
| 工具结果上限 | pi read/bash 50 KB、2,000 行；摘要序列化 2,000 字符 | MIT | 已有输出预算与过滤器；补真实任务的 token/轮次 A/B |
| 无内置沙箱 | pi `security.md` | MIT | **不借鉴**；外部工人必须经过 AgentKit 权限、沙箱、独立验收 |

来源：<https://github.com/earendil-works/pi>、<https://github.com/earendil-works/pi/blob/v1.6.1/packages/coding-agent/docs/how-pi-works.md>、<https://github.com/earendil-works/pi/blob/v1.6.1/packages/coding-agent/docs/compaction.md>、<https://github.com/earendil-works/pi/blob/v1.6.1/packages/coding-agent/docs/security.md>。本轮临时安装 v1.6.1，用本地假 OpenAI 兼容服务器真实执行 JSON mode，核验 session/message/tool/agent_settled 事件；未复制代码，NOTICE 不变。

### I. 外部编程工具接力接口核验（2026-11-26）

| 工具 | 官方接口 | 可借鉴 / 结论 |
|---|---|---|
| Antigravity CLI | `agy -p`、`--output-format json\|stream-json`、`agy acp` | headless 技术可行，JSON 有 status/response/usage，stream-json 有工具事件；但官方 FAQ 明确 consumer 计划不支持第三方包装 `agy`，AgentKit 不接 consumer 登录态 |
| Codex CLI | `codex exec --json`、`codex exec resume`、官方 Python SDK 控制 `codex app-server` | v0.172.0 已核验 JSONL、thread resume、output schema、read-only/workspace-write 沙箱与 usage；`agentkit/backends.py` 已实现 exec JSONL 适配、usage/预算、取消和 builtin↔Codex 接力。无内置总超时，旧 MCP server 已移除；使用工具自身认证，不提取登录态，不反代后端 |
| OpenCode | `opencode run --format json --dir DIR --model provider/model [--session ID]` | v1.19.6（commit `f4e363c8295fa90a03dddc451882fffe8fbce357`，MIT）已核验 JSONL、`step_finish.part.tokens/cost`、V1 `permission` 与最后匹配优先规则，并用真实 `debug config` 确认注入策略原样解析；适配器禁用项目配置、插件、网络工具、子 Agent，默认禁用 shell，支持预算/续跑/接力。当前 dev/V2 的 `--standalone/--archived` 不用于稳定版。2026-11-26 官方 providers 文档确认：当前版本凭证存于本地 SQLite（通常 `~/.local/share/opencode/opencode.db`），`XDG_DATA_HOME` 与 `OPENCODE_DB` 可改变路径；旧版使用数据目录中的 `auth.json`。因此默认必须隔离数据目录，环境变量 API Key 不写入凭证库。2026-11-26 官方 V2 2.4.2 二进制进一步核验：`opencode run` 仅支持 `--format default|json`（JSON 仍为逐行事件），凭 `cwd` 定位仓库，支持 `--standalone/--session/--model`；隔离环境无需登录即可列出 `opencode/beam-free`、`opencode/hy4-flash-free` 等免费模型，但免费额度用完会返回 `provider.free-limit`/429，官方建议连接 provider、订阅 Go 或切换模型，不允许绕过限额。V2 权限字段改为 `permissions`/`shell`/`subagent`，并新增 `policies` 硬拒绝；现有 V1 适配器不能直接用于 V2。 |
| Qoder CLI | `qodercli --print --output-format stream-json --permission-mode dont_ask --setting-sources user --strict-mcp-config` | 1.1.98（npm 官方包，Apache-2.0）已核验帮助与公开 SDK 契约：`system/init`、`assistant.message.usage`、`result.subtype/error_code/permission_denials`、session resume；适配器复用官方账号登录或 PAT，不读取凭证，PAT 注册进日志脱敏，禁用遥测/项目配置/MCP/shell/网络工具/子 Agent，记录 token 与请求 Credits。真实账号任务尚未执行 |
| Cursor CLI / SDK | `agent -p/--print`、`--output-format stream-json`、`--resume`、`CURSOR_API_KEY`；可选 `cursor-sdk` 的 `Agent.create/resume/send/get_usage` | 2026-11-26 官方 headless/output/auth/parameters 与 Python SDK 1.5.2 文档及安装包签名已核验：CLI 适配器使用独立配置、默认权限和 Shell/WebFetch/MCP/密钥 deny，不读取登录态、不用 `--force`；SDK 为可选专有依赖（不 vendored），支持本地工具白名单、setting_sources、session resume、TokenUsage 与 UsageCost。CLI 与 SDK 均未完成真实 API Key 任务；CLI 无 token/成本字段，SDK 未报告用量时拒绝 token 预算，USD 硬预算均不支持 |

来源：<https://antigravity.google/docs/cli/headless/>、<https://antigravity.google/docs/faq/>、<https://github.com/openai/codex/blob/main/docs/exec.md>、<https://github.com/anomalyco/opencode/tree/v1.19.6>、<https://opencode.ai/docs/cli/providers/>、<https://opencode.ai/docs/>、<https://opencode.ai/docs/cli/>、<https://opencode.ai/docs/permissions/>、<https://opencode.ai/docs/policies/>、<https://opencode.ai/docs/console/go/>、<https://docs.qoder.com/cli/run-in-scripts>、<https://docs.qoder.com/cli/authentication>、<https://docs.qoder.com/cli/sdk/references-typescript>、<https://cursor.com/docs/cli/headless>、<https://cursor.com/docs/cli/reference/output-format>、<https://cursor.com/docs/cli/reference/permissions>、<https://cursor.com/docs/cli/reference/authentication>、<https://cursor.com/docs/cli/using>、<https://cursor.com/docs/sdk/python>。本轮未复制代码，NOTICE 不变；真实接入须逐账号/计划核实服务条款。

### J. LSP 窄客户端（2026-11-26 核验）

| 功能 | 借鉴 | 许可证 | 实现 / 结论 |
|---|---|---|---|
| stdio JSON-RPC 生命周期 | Serena SolidLSP `ls_request.py` / `ls.py` / `initialize_params.py` | 文件头 MIT；Serena 主体 GitHub NOASSERTION | 只读参考接口边界，未复制代码；自研 `agentkit/lsp.py`，支持 Content-Length 帧、initialize/didOpen/definition/references/shutdown、UTF-16 坐标、超时和进程清理 |
| 语义引用与回退 | LSP 3.17 `textDocument/references` | 协议规范 | `agentkit code refs` 优先使用配置的 stdio LSP，否则返回 `semantic:false` 的词法引用；真实 pylsp/多语言服务器矩阵待测 |

来源：<https://github.com/oraios/serena/tree/main/src/solidlsp>、<https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/>。NOTICE 不变。

## 三、开工克隆清单（已克隆到开发环境）

jcode、mini-swe-agent、SWE-agent、Aider、XERJ、Grok Workhorse、worker-bridge、LooppooL 、OmniRoute（C-08）已浅克隆（只取最新版本），仅供阅读参考，不提交到本仓库。pi v1.6.1、OpenCode v1.19.6 与 Qoder CLI 1.1.98 临时安装在开发机用户临时目录（非全局、非仓库内），仅用于接口核验；Qoder 包已检查 postinstall 后未执行。M1 原型以 mini-swe-agent 的循环 + SWE-agent 的工具 + Aider 的仓库地图为骨架；M4 正式版参考 jcode 的 Rust 架构。
