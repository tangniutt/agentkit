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

## 三、开工克隆清单（已克隆到开发环境）

jcode、mini-swe-agent、SWE-agent、Aider、XERJ、Grok Workhorse、worker-bridge、LooppooL 、OmniRoute（C-08）已浅克隆（只取最新版本），仅供阅读参考，不提交到本仓库。M1 原型以 mini-swe-agent 的循环 + SWE-agent 的工具 + Aider 的仓库地图为骨架；M4 正式版参考 jcode 的 Rust 架构。
