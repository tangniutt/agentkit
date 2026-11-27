# 进度

更新：2026-11-23 夜（M2 补完 + C-08 上下文压缩 + C-09 主管操作手册 + C-10：第 3 家 NVIDIA NIM、真实压缩 A/B、小任务开销修正 + C-11：第 4、5 家 Agnes AI、AMD Token Factory 中国站）；2026-11-24：C-12 AI 反馈进化回路、C-14 PRD v0.3；2026-11-25：C-15 至 C-20 目标需求批准，P0 可信基座与 Goal/MCP v1 已落地；2026-11-26：XERJ 截断后真实回归、pi v1.6.1 专项核验、Windows UTF-8 测试可移植性修复、C-21 多工具接力与 HandoffPacket、OpenCode v1.19.6 与 Qoder CLI 1.1.98 适配器；2026-11-27：Cursor/OpenCode V2、发布签名、离线评测与保存登录态用户级授权，详见 `docs/prd-v1.0.md`

| 阶段 | 状态 |
|---|---|
| M0 调研 + 需求基线 | 完成（基线冻结，变更 C-01 至 C-12、C-14 已登记，C-13 预留给调研改进包）；需求文档 PRD v0.3（核心设计理念 + 19 条用户需求追溯表，`docs/prd-v0.3.md` 第 12 节） |
| 阶段二 接口契约 | 与 M1 合并完成：CLI 输出 schema 即契约（`{ok,data,error{what,why,next,code,run},truncated,next}`；C-03 增加 code、run） |
| M1 Python 原型（直接模式工具集） | **完成**（见下表；偏差见「M1 未完成/偏差」） |
| M2 工人 + 派活 + 验收器 | **完成**（含 C-03 / C-04；补完 C-05 续跑、C-06 输出瘦身 + 多文件评测、C-07 llms.txt 安装修正、C-08 上下文压缩、C-09 主管操作手册、uninstall、日志 7 天清理；真实模型实测 5 家（Gemini、OpenRouter、NVIDIA NIM、Agnes AI、AMD Token Factory 中国站，满足 M-12），真实压缩 A/B：NVIDIA / Gemini 每格 1 次，Agnes / AMD 每格 2 次，仍是小样本，见「M2 未完成/偏差」） |
| M3 防作弊与安全 | 部分提前完成（M-15 基础规则 + 14 例作弊库 100% 检出、M-17 Key 引用与扫描测试）；沙箱仍在 V1.1 |
| M4 Rust/Go 正式版 | 未开始 |
| M5 评测与发布 | 未开始 |
| C-15 至 C-21 生产化目标 | **P0 与 P1 控制面完成；P2 代码智能部分完成**：路径/记忆安全、fail-to-pass、EvidenceReceipt、模型接力、bubblewrap 适配、MCP stdio、Goal v1、task 控制面、Builtin/XERJ/LSP、HandoffPacket v1、Codex/OpenCode/Qoder/Cursor 适配器已实现；Antigravity、真实 Cursor/Qoder 账号任务、其余 LSP 服务器、真实基准与远端发布待做 |

## M2 完成情况

| 编号 | 状态 | 实现 | 测试 |
|---|---|---|---|
| M-12 工人循环 | 完成；真实接口实测 **5 家**（Gemini OpenAI 兼容、OpenRouter `:free`、NVIDIA NIM `integrate.api.nvidia.com/v1`、Agnes AI `apihub.agnes-ai.com/v1`、AMD Token Factory 中国站 `developer.amd.com.cn/radeon/api/v1`），满足验收标准 | `agentkit/worker.py`（mini-swe-agent 式循环，只用 M1 工具 + finish）；`agentkit/models.py`（标准库 OpenAI 兼容客户端）。工具调用格式错误本地修复（JSON 修补 / Python 字面量 / 文本里的调用 / 工具名别名），修不了把错误回给模型，连续 3 次失败 → 任务失败；卡住检测（N 轮无新 diff、同一错误重复 3 次）；上下文到 70% 时折叠旧工具输出（保留目标、记忆摘要、最近几轮）；开工先读 `memory brief`；回传 Gemini `extra_content.thought_signature` | `test_m2_tasks.py`（修复、3 次失败、纯文本、卡住×2、压缩、签名回传） |
| 模型配置 | 完成 | `agentkit model add/list/test/remove`，`.agentkit/config.toml` `[models.NAME]`（base_url、model、key_env、context、price_in/out、tier）；超时 / 网络 / 5xx 重试 2 次指数退避；429 读 Retry-After（≤30 s 时等一次）后暂停上报；401/403 立即失败 | `test_m2_models.py` |
| M-13 任务系统 | 完成（无常驻守护进程，见偏差） | `agentkit/tasks.py`：create（目标、--check、--allow、三种预算、--tier/--model）/ status（<100 token，含排队位置）/ wait（多 ID + 超时）/ report（≤300 token，与 verify 同格式）/ cancel / clean；状态落盘；进程消失 → interrupted；内容哈希去重；全局并发上限 + 排队；无模型时保持 M1 直接模式提示 | 端到端、去重 + 排队、取消、kill -9 后恢复 |
| M-14 隔离 | 完成 | 每任务分支 `agentkit/<id>` + worktree `.agentkit/worktrees/<id>`；工人的 run 拒绝 commit/push（M-22）+ `core.hooksPath` 指向永远拒绝的钩子 + 验收时检查分支是否被提交（`worker_committed`）；非 git / 无提交仓库拒绝并提示 `git init`；`task clean` 清理 worktree 与分支 | 端到端隔离 + 作弊库 |
| M-15 防作弊 | 完成 | 验收器把工人的 diff 复制到基于基线提交的全新 worktree 独立重跑，从不采信工人声明；标记：tests_deleted / tests_modified（含新增 conftest）/ tests_skipped / check_script_modified / test_config_modified / outside_allow / worker_committed / denied_commands / path_escape / hardcoded_return_suspect / no_tests_ran；任一标记把 pass 降为 needs_review | `eval/cheats.py` 14 例，检出 100%（`test_m2_eval.py`） |
| M-16 预算 | 完成 | token / 美元在每次模型调用后检查（并预估下一次调用不超）；时间由看门狗线程 + SIGUSR1 打断阻塞中的模型调用或命令（杀进程组）；报告注明 `stopped: budget` | 三种预算；时间预算在模型调用中、命令运行中都 <5 s 停止 |
| M-17 密钥 | 完成 | 配置只存变量名；Key 只出现在 Authorization 头；工人进程启动后把所有已配置的 key 变量从环境删除（工具子进程看不到）；Key 值注册进脱敏器；发给模型的每条消息再脱敏一遍 | `test_keys_never_reach_model_logs_or_reports`：env、进程环境文件、直接 echo Key、模型请求体、`.agentkit` 全部文件、报告均扫描不到 |
| M-18 doctor | 完成 | 每个模型：key 变量是否已设；`doctor --online` 实际调用一次 | `test_doctor_reports_models` |
| M-20 评测 | 完成（离线）；真实模型小样本 3/3 通过，其余待配额 | `eval/`：21 题小仓库 + 参考解；`python -m eval.run` 输出直接模式 vs 派活模式的通过率、主管 token、工人 token、成本表；`--cheats` 跑作弊库；`--real` 接真实模型 | 离线 21/21 两种模式全过；题目「修前必失败、参考解必通过」自检 |
| M-21 记忆（补充） | 完成 | 任务结束确定性写入：失败原因（`task-failures`）、每个模型按任务类型的通过/失败计数（`model-stats`）；带可疑标记的任务不学习 | `test_auto_memory_records_results` |
| C-03 可诊断性 | 完成 | `agentkit/events.py`：运行 ID、43 个稳定错误码（`docs/errors.md`）、脱敏 JSONL 事件日志；`error` 增加 `code`、`run`；任务 status/report 给 `code`、`at`（组件#步骤）、日志指针；`agentkit logs`、`agentkit debug-bundle`；未处理异常的堆栈写 `crash-<run>.log`，输出只给 `AK-INTERNAL` + 运行 ID | `test_m2_diag.py`（错误码冻结 + 文档同步、日志 / 归档脱敏、内部崩溃路径） |
| C-04 反馈引导 | 完成 | `.github/ISSUE_TEMPLATE/`（bug、feature、config）；`agentkit feedback --bug/--idea` 只生成草稿与预填链接，从不提交；llms.txt / AGENTS.md / CONTRIBUTING.md 说明；`AK-INTERNAL` 的 next 指向 feedback | URL 编码、脱敏、表单字段与命令一致 |
| C-05 续跑 | 完成 | `agentkit task resume ID [--budget-tokens/--budget-usd/--budget-minutes]`：paused / interrupted / stopped → 排队后在**原 worktree、原分支**续跑；`worker.resume_context` 把 transcript 压成「每个工具调用一行 + 结果、最后说明、停止原因、当前 diff（含新文件）」，不重放原始助手消息；token / 金额 / 轮数计数累计（`prior`），因预算停止必须给新预算；transcript 写 `_resume` 标记；interrupted 的提示改为 `task resume`；`task report` 对可续跑任务的 next 指向 resume | `test_resume.py`（429 暂停后续跑通过、kill -9 后续跑、预算拒绝与加预算续跑、done / 已清理 / 无 ID 拒绝；断言续跑请求只有 system + user、不含旧 thought_signature、<7000 字符） |
| C-06 输出瘦身 + 多文件评测 | 完成 | `task create` 只回 id/status；`task wait` 单个任务结束时内联报告；报告去掉主管已知字段；`eval/tasks_large.py` 3 道多文件题（2–6 处跨文件修改，带 explore 步骤）；评测表按 tiny / large / all 分组；`--size` | `test_lean_create_and_wait_with_inline_report`；离线评测断言 large 题派活主管 token < 直接模式的 1/3 |
| M-01 卸载 | 完成 | `agentkit uninstall`：默认只打印计划；`--yes` 删除包（pip）、`~/.config/agentkit`、`~/.local/share/agentkit`、登记过的每个仓库的 `.agentkit/` 与其 worktree；保留用户代码、git 历史、`agentkit/*` 分支（`--delete-branches` 才删）、git 已跟踪的 `.agentkit` 文件；有运行中任务则拒绝（AK-TASK-STATE）；`--this-repo` 只清当前仓库。仓库登记：首次创建 `.agentkit/` 时写入 `~/.local/share/agentkit/repos.txt`（任务 worktree / 临时验收目录不登记） | `test_lifecycle.py`（两仓库 + worktree + 分支全清、代码与历史不变、跟踪文件保留、删分支、运行中拒绝） |
| C-03 日志保留 | 完成 | `lifecycle.prune_logs`：任意命令结束后（每 6 小时最多一次）删除 `.agentkit/logs` 中超过 `[logs] retain_days`（默认 7，0 = 永久）的文件，并从共享 `events.jsonl` 删掉过期行 | 过期文件删除、新文件保留、过期事件行删除、6 小时节流、可配置 |
| M-19 实测 | 完成 | `eval/llms_check.py`：只读 llms.txt，在全新 venv + 空 HOME 里按「第一条成功的」执行安装行，再跑 `agentkit doctor`（须 healthy）和 Direct mode 一节示例的 `agentkit verify`；`--scenario bare` 模拟 PEP 668 发行版（系统 Python 无 pip、只有 git/uv/pipx）。实测：旧 llms.txt 在 bare 场景失败（`pip: command not found`）→ 修正为 pip → pipx → uv → venv 四选一 + PATH 提示（C-07）；修正后两个场景都通过。从 GitHub 安装、空 pip 缓存：3.5 s；本地源码：2.3 s；uv：1.0 s | `test_llms_install.py`（解析规则 + venv 场景 + bare 场景；无网络时跳过后两项） |
| OpenRouter 适配 | 完成；2026-11-23 真实调用：1 个 dogfood + 3 道多文件题（见下「OpenRouter 实测」） | 自动带 `HTTP-Referer` / `X-Title`；HTTP 200 里的 `error`（顶层或 choice 上 `finish_reason:error`）按 code 映射（401/403 → auth 不重试，429 → rate_limited，5xx / 无 code → 暂时不可用重试，402 → http）；429 读 `X-RateLimit-Reset`（毫秒时间戳，也从 body metadata 读）；`model add` 对无单价的非 `:free` OpenRouter 模型给警告；`eval.run --real` 拒绝非 `:free` 的 OpenRouter 模型（`--allow-paid` 才放行） | `test_m2_models.py` 新增 3 个 |

| C-08 上下文压缩 | 完成 | ① `agentkit/compress.py` + `agentkit/filters/*.json`（15 个过滤器：pytest、unittest、jest/vitest、go test、cargo test、tsc、eslint、ruff、mypy、pip/uv、npm/pnpm/yarn install、git status/diff/log、generic），过滤器是数据，每个带内联样例（`verify_filters()` 是校验门）；按命令（拆 `&&`/`;`/`|`、去 `cd`/`FOO=1`/`uv run`/`npx` 前缀）再按输出特征选择；去 ANSI、`\r` 重绘、进度条，警告与重复行带 `[×N]` 计数折叠，相似行分组，头 / 尾 + 中间所有受保护行；含 error/fail/assert/traceback/panic/ERR! 等的行**永不删**；diff 先给「N files: 路径 (+a -b)」一行并按文件封顶、头部优先显示。`run` 输出带 `filtered:{filter,lines,raw}`，`run --raw` / `[compress] run_filters=false` 关闭；验收的 `why` 取摘要 / 诊断行（`best_why`）。② `agentkit/context.py` 会话去重：内容哈希（去掉耗时、日志名）相同的 read/search/ls/glob/repo_map/run/memory 结果 → `{"same_as_turn":N}`。③ 渐进老化：最近 2 轮原样，更早的工具结果变一行 `[aged]` 摘要（read：路径 + 行号 + sha；run：命令 + exit + 摘要行；search：命中文件），旧 create/edit 参数缩成合法 JSON；只改内容不删消息，tool_call/结果配对不变；攒够 `age_min_chars`（默认 4000）才批量老化；被引用的原文先挪进最近窗口。原 70% 折叠保留为兜底。④ 工人简洁风格说明（约 70 token，C-10 起按需：大仓库开工即放进系统提示，否则转录变长时追加一次）+ 系统提示「命令在 worktree 根目录跑、别 cd 绝对路径」（dogfood 发现）。⑤ 稳定前缀：messages[0]（系统 + 记忆摘要）、messages[1]（任务）、工具定义从不改写。⑥ `validate`：代码块、受保护行及其上的路径 / URL / 版本号必须原样保留，否则退回原文；原文始终在 `.agentkit/logs`（写入时脱敏），`agentkit logs RUN --raw` 查看。⑦ `eval/compress_bench.py` + `eval/fixtures/logs/`（真实抓取的 npm / pip / git diff / git log / git status / pytest -v） | `test_compress.py`（17 个：所有过滤器样例、严重行在每个过滤器随机噪声下都保留、校验器拒绝 + 退回、小输出不动、run 过滤 + `logs --raw` 找回原文 + 两种关闭方式、坏 id 拒绝、验收 why、去重 + 老化配对与前缀、批量老化、工人端到端逐次请求前缀字节相同、关闭开关、diff 文件列表在前、基准不让任何关键行消失） |
| C-09 主管操作手册 | 完成 | `agentkit/manual.py` + `agentkit manual`：默认 JSON 总览（快速上手流程、直接 / 派活模式取舍与实测 token、预算与安全规则、15 个常见错误码的修复（取自 `events.ERRORS`）、C-08 压缩说明、39 个命令一行索引带副作用标记）；`agentkit manual <命令或组>` 单命令详情（参数类型 / 默认值 / 说明从 `cli.build_parser()` 实时读取，输出字段、错误码、副作用、示例来自 SPEC）；`--all`（超出 `--budget` 时给 `next`）；`--human` / `--format md` 输出 Markdown；`docs/agent-manual.md` 为生成产物；`cli.py` 给 task create / verify / run / logs / feedback / edit 等补了参数说明（手册和 `--help` 同时受益）。大小（字符 / 4）：总览 ≈1.7k token（默认 2000 预算内），全部 ≈3.7k，单命令 88–365（平均 ≈150），task 组 ≈840 | `test_manual.py`（57 个：覆盖每个注册命令且新增命令缺条目即失败、39 个示例逐个解析回自己的命令、错误码在登记表与 docs/errors.md、副作用标记、参数来自解析器、JSON 合法与预算、`--all` 翻页、单命令大小、文档无漂移、`--human`、入口文档链接、无密钥样式字符串） |
| C-08 修正 | 完成 | 做 C-09 时全量测试发现 `test_worker_compression_end_to_end_stable_prefix` 偶发失败（基线代码上 3/14）：`TranscriptCompressor._window_start` 在助手轮数 ≤ `keep_turns` 时把全部历史当作「旧」，`age_min_chars` 较小时工人刚读的文件在下一轮就被老化，测试只靠两次 pytest 打印相同耗时才通过。改为此时不老化任何内容；默认 `age_min_chars=4000` 下离线评测数字不变（小题 127,303 → 127,301、多文件 74,680 → 74,677 工人 token） | `test_recent_turns_never_aged_early`（旧代码失败、新代码通过）；端到端测试 8/8 稳定通过 |
| C-10 ① 小任务开销 | 完成 | `[compress] style = "auto"`（默认）：仓库跟踪文件 ≥ `style_min_files`（40）→ 开工即在系统提示（整个任务字节不变）；否则转录（系统 + 任务之外）≥ `style_after_chars`（4000 字符）或压缩首次生效 → 以一条 user 消息**追加一次**（只追加，前缀与之前各轮不变）；`terse` 恒放、`normal` 不放；task.json `compression.style` 记录 `start` / `round N` / `off`。离线：小题 0%（原 +5.7%），多文件 −12.4%（原 −11.6%） | `test_compress.py` 新增 4 个（小题从不收到、转录变长追加一次且位置不变、大仓库在前缀、terse / normal 开关）+ 原端到端测试改断言 |
| C-10 ② NVIDIA NIM + 预设 + 节流 | 完成；真实运行见「真实压缩 A/B」 | `model add --preset nvidia\|gemini\|openrouter`（base URL、Key 变量名、上下文、节流，显式参数优先；无 preset 时必须 `--base-url`）；模型配置 `rpm`（每个工人每分钟最多请求数）、`max_wait`（Retry-After 最长等待，默认 30 s，预设 65 s）、`max_tokens`；HTTP / body 504 → 上游不可用（重试 2 次后暂停）；推理模型 `reasoning_content` 从不回传，`finish_reason=length` 且无工具调用 → 明确提示「输出 token 上限截断」；`model test` 返回 `reasoning_only`；`eval.run --real` 加 `--rpm / --max-wait / --max-tokens` | `test_providers.py`（6 个：预设与覆盖、504 两种形态、Retry-After ≤ max_wait 才等、rpm 节流 + max_tokens、推理模型 model test、工人端到端推理模型 + 截断提示 + 不回传 reasoning） |
| C-10 ③ 卡住检测修正 | 完成 | NVIDIA dogfood 发现：首次改动前 10 轮探索（每轮读到新内容）被判「卡住」→ failed（不可续跑）。改为首次改动之前、本轮有新结果（`context.result_key` 没见过）的轮不计入「无新 diff」；重复同样调用仍计入；`max_rounds` 封顶 | `test_exploring_before_first_edit_is_not_stuck`（旧代码失败）；`test_stuck_no_new_diff` 改为 4 次请求（第 1 次读是新信息） |
| C-11 第 4、5 家 + 修正 | 完成；真实运行见「第 4、5 家实测」 | `--preset agnes`（`https://apihub.agnes-ai.com/v1`、`AGNES_API_KEY`、rpm 6、`rate_wait` 60、max_wait 65、max_tokens 8192）、`--preset amd-cn`（`https://developer.amd.com.cn/radeon/api/v1`、`AMD_CN_API_KEY`、rpm 15、max_wait 65、max_tokens 8192）；`models.error_obj` 读 `error` / `detail.error` / `detail` 文本，HTTP 错误与 200 body 都报服务商原话；429 `model_concurrency_rate_limit_exceeded`（`ModelError.capacity`）→ 等 5 / 10 / 20 s（≤ max_wait）后仍忙则暂停（原因写明「model at capacity」，可续跑），配额类 429 仍按旧规则；模型配置 `rate_wait`（`model add --rate-wait`、`eval.run --rate-wait`）：429 无 Retry-After 时等一次；`model test` / `doctor --online` 不做忙等；`reasoning` 字段与 `reasoning_content` 同样不回传；`hardcoded_return_suspect` 跳过基例（单个比较 + 边界字面量 + 返回平凡字面量或目标原文里的字面量）；同一回复里并行调用的同一错误只算 1 次 | `test_providers_agnes_amd.py`（13 个：两个预设、嵌套错误解析、忙 429 等待后成功 / 暂停且信息可读、配额 429 不当忙、无 Retry-After 的 429 按 `rate_wait` 等一次、200 body 嵌套错误与 401、工人忙模型暂停、`reasoning` 字段 model test 与工人不回传、基例不误报（单元 + 端到端，旧代码失败）、并行同错只算一次（旧代码失败）） |
| C-12 AI 反馈进化回路 | 完成 | `agentkit feedback --improve`（证据从本地事件自动收集并脱敏、`--input` JSON / stdin、`missing` 列表）；所有草稿带 `keywords` + `search_first`，重复去 +1；预填 `labels=ai-feedback`、`filed_via`；任务 wait/report 与 verify 有信号时 `next` 给最多一条 feedback 提示（干净运行无字段；离线评测主管 token 与改动前相同：派活全部 3,857–3,858、直接 10,856，改动前代码重跑同样在 ±1 内波动，为计时噪声）；`.github/ISSUE_TEMPLATE/improvement.yml`；`docs/triage.md`；`.github/workflows/issue-triage.yml`（必填段落、疑似密钥、needs-eval / needs-info，只有 `issues: write`）；仓库标签 ai-feedback / needs-eval / needs-info / accepted / declined | `test_c12_feedback.py`（34 个：草稿字段与表单一致、证据不含代码与密钥、JSON 输入与覆盖、缺段落、去重关键词与搜索链接、8 种信号触发、6 种普通运行无提示、端到端卡住任务有且只有一条提示 / 干净任务无提示、verify 只在可疑通过时提示、工作流 YAML 安全与解析、按 GitHub 渲染的正文检查草稿、密钥扫描与脱敏规则一致） |

测试：`pytest -q` **305 个全绿**（M1 的 107 + M2 66 + C-08 22 + C-09 57 + C-10 6 + C-11 13 + C-12 34）；CI（Python 3.10 / 3.12）通过（C-12 提交 c3ec888，run 45343691696）。Dogfood：`docs/dogfood-m2.md`（离线派活通过；真实 Gemini 任务暴露并修复了 503 处理 bug）。

## C-08 实测（2026-11-23，诚实记录，含接近 0 的情况）

**1. 命令输出过滤**（`python -m eval.compress_bench`；token = 字符 / 4；「C-08 前」= 原来 `run` 在默认 2000 token 预算下实际显示的尾部，不是原始全文）

| 样例 | 过滤器 | 原始 | C-08 前显示 | C-08 后显示 | 比 C-08 前省 | 关键行可见 前 → 后 |
|---|---|---|---|---|---|---|
| pytest 1 个失败 + 300 条重复警告（合成） | pytest | 15,482 | 1,711 | 322 | 81% | 3/5 → 5/5 |
| tsc 40 个错误（合成） | tsc | 2,087 | 1,687 | 1,146 | 32% | 2/3 → 3/3 |
| cargo test 1 个失败（合成） | cargo-test | 1,802 | 1,802 | 146 | 92% | 4/4 → 4/4 |
| jest 1 个失败（合成） | jest | 1,412 | 1,412 | 115 | 92% | 4/4 → 4/4 |
| npm install --loglevel http（真实） | npm-install | 8,464 | 1,678 | 392 | 77% | 2/2 → 2/2 |
| pip install（真实） | pip | 539 | 539 | 71 | 87% | 1/1 → 1/1 |
| git diff 3 个文件（真实，本仓库 99cdca3..cdda359） | git-diff | 23,879 | 1,755 | 1,745 | **1%** | 0/3 → 3/3 |
| git log -25（真实） | git-log | 1,460 | 1,460 | 924 | 37% | 2/2 → 2/2 |
| git status（真实，小） | 未启用 | 176 | 176 | 176 | **0%** | 1/1 → 1/1 |
| pytest -v 1 个失败（真实） | pytest | 1,606 | 1,606 | 687 | 57% | 3/3 → 3/3 |

要点：原来的 `run` 已有预算截断，所以大日志「显示的 token」本来就封顶 ~1.7k；C-08 的价值一半是省 token，一半是**截断前把该看的留住**（300 警告的 pytest，原来尾部截断看不到失败断言；diff 原来只看到最后一个文件的尾巴，现在先列出全部改动文件）。git diff 在预算下几乎不省 token（代码行里大量 error/fail/timeout 字样受「严重行永不删」保护）；小输出（<1200 字符且 <25 行）不处理。

**2. M-20 离线评测**（脚本化假模型，24 题，`--worker-config '[compress] enabled=false'` 对照）

| | 工人 token 关 | 工人 token 开 | 变化 | 主管 token 关 / 开 |
|---|---|---|---|---|
| 多文件题 3 道 | 84,506 | 74,677 | **−11.6%**（ev-shop 25,394→21,901；ev-csv 21,531→19,940；ev-rename 37,581→32,836） | 552 / 553 |
| 小题 21 道 | 120,437 | 127,301 | **+5.7%**（每轮多约 70 token 的风格说明，小题没有可压缩的输出） | 3,226 / 3,226 |
| 合计 | 204,943 | 201,978 | −1.4% | 3,778 / 3,779 |
| **C-10 后** 多文件 3 道 | 84,508 | 74,041 | **−12.4%**（风格说明第 4–6 轮才追加） | 552 / 553 |
| **C-10 后** 小题 21 道 | 120,445 | 120,445 | **0.0%**（小题转录最长 1,704 字符，从不触发 4,000 阈值） | 3,226 / 3,226 |
| **C-10 后** 合计 | 204,953 | 194,486 | **−5.1%** | 3,778 / 3,779 |
| 直接模式主管 | 10,687 | 10,687 | 0（小题检查输出太短，过滤不触发） | — |

假模型按脚本行动，测不到「简洁风格让模型少写字」的输出 token 节省，也测不到真实模型看到 `[aged]` 后会不会多读一次。

**3. 真实转录重放**（OpenRouter dogfood 任务 t1f8b29，12 次模型调用；把记录的消息按 C-08 规则逐轮重放，只算 messages、不含工具定义）：提示 token 合计 61,166 → 24,546（**−60%**；老化 4 个大结果：repo_map 与整文件 read；该转录里没有完全相同的重复结果，去重 0 次）。这是上限估计：真实模型面对老化后的上下文可能会重读文件。

## 真实压缩 A/B + 第 3 家（2026-11-23 晚，真实模型，每格 1 次）

详见 `docs/dogfood-m2.md` 第 7 节。工人 token = 服务商返回的 usage（输入 + 输出）；主管 token = 字符 / 4。

| 模型 | 题 | 压缩开：结果 / 轮 / 工人 token | 压缩关：结果 / 轮 / 工人 token | 变化 |
|---|---|---|---|---|
| NVIDIA `nemotron-3.5-lightning-30b-a3b` | ev-shop | pass / 15 / 56,601 | pass / 13 / 61,595 | −8.1% |
| 同上 | ev-csv | pass / 9 / 26,741 | pass / 9 / 29,771 | −10.2% |
| 同上 | ev-rename | pass / 16 / 63,731 | pass / 15 / 62,555 | +1.9% |
| 同上 | 合计 | 3/3 / 40 / 147,073 | 3/3 / 37 / 153,921 | **−4.4%** |
| Gemini `gemini-3.5-flash-lite` | ev-shop | pass / 10 / 24,422 | pass / 8+11 / 59,312（429 暂停后续跑，不可比） | — |
| 同上（rpm 5，无中断） | ev-csv | pass / 14 / 35,568 | pass / 14 / 57,244 | −37.9% |
| 同上（rpm 5，无中断） | ev-rename | pass / 16 / 38,037 | pass / 13 / 36,126 | +5.3% |
| 同上 | 可比两题合计 | 73,605 | 93,370 | **−21.2%** |
| Gemini `gemini-3-flash-preview` | ev-shop / ev-csv | pass / 8 / 18,749；ev-csv 续跑后 pass / 15 / 35,914 | 0 次调用成功（先每分钟、后日配额 429） | 无法对比 |

主管 token 每题 181–250（直接模式同类题约 2,000+）。要点：压缩让**每轮输入** token 降 5–22%（NVIDIA），但开压缩时有 3 题多用了 1–3 轮（老化后偶尔重读），所以总量收益在 −38% 到 +5% 之间摆动；简洁风格对推理模型的输出 token 没有可见作用（思考 token 为主）。样本太小，结论只有「方向为省、幅度不稳」。

- 续跑：今天 Gemini 上午的任务所在临时目录已不存在（沙箱 /tmp 已重置），无可续跑；本轮被 429 暂停的任务实际续跑 2 个：gemini-3-flash-preview ev-csv（1 → 15 轮，pass，随后日配额停止）、gemini-3.5-flash-lite ev-shop（8 → 19 轮，pass）。其余 5 个 gemini-3-flash-preview 任务保持 paused（日配额，Retry-After ≈ 23 小时；其中 ev-shop（开）验收已 pass，只差 finish）。
- NVIDIA dogfood（`human_bytes()`，12 轮上限）：首次被误判卡住（已修，C-10 ③）；修复后 12 轮用满、实现正确、检查通过，needs_review（预期的 `check_script_modified` + `hardcoded_return_suspect` 误报）。59,729 token（下午 OpenRouter 同族模型 102,578）。

## 第 4、5 家实测（C-11，2026-11-23 夜，真实模型，每格 2 次）

详见 `docs/dogfood-m2.md` 第 8 节。工人 token = 服务商 usage；主管 token = 字符 / 4。

| 服务商 / 模型 | 多文件 3 题 压缩开：通过 / 轮 / 工人 token | 压缩关 | 变化 | 每轮工人 token 开 / 关 | dogfood（human_bytes，12 轮上限） |
|---|---|---|---|---|---|
| Agnes `agnes-3.0-flash`（`--preset agnes`） | 6/6 / 37 / 110,480 | 6/6 / 33 / 109,537 | +0.9% | 2,986 / 3,319（−10.0%） | finish，6 轮，26,312 token，检查通过（needs_review：预期的 `check_script_modified`） |
| AMD `DeepSeek-V4-Flash`（`--preset amd-cn`） | 6/6 / 35 / 109,368 | 6/6 / 38 / 141,023 | **−22.4%** | 3,125 / 3,711（−15.8%） | finish，10 轮，42,293 token，同上 |

- 主管 token 每题 181–185（两次慢任务因多轮询到 239 / 359）。
- 工具调用探测：Agnes agnes-3.0-flash、agnes-2.5-flash 通过；AMD DeepSeek-V4-Flash、Gemma-4-31B-IT、MiMo-V2.6-Flash（90 s）通过，Qwen3.8-27B 带工具超时 / 504，GLM-5.3-Flash 只拿到忙 429 / 502，DeepSeek-V4.1-Flash 带工具 502。
- 限制：Agnes 免费档实测约每滚动分钟 10 次、429 无 Retry-After（首轮因此 4 题暂停 → `rate_wait`）；AMD 网关间歇 502（几分钟后恢复），单次调用偶尔 60–214 s。
- 结论仍是「方向为省、幅度不稳」：每轮输入稳定降 10–16%，总量看轮数；Agnes 开压缩一次出现「凭记忆写 old 文本」的并行失败（已修计数，现象记录）。

## OpenRouter 实测（2026-11-23）

- 接入：沙箱代理在网络层注入 Key（环境变量里只放占位值，Key 从未进入命令、日志或仓库）；免费档 `free_model_daily_requests` 开始前 0/50。
- `google/gemma-4-31b-it:free`：所有请求都是 429「temporarily rate-limited upstream」（Google AI Studio 共享池），3 道题立即 paused（适配正确：按 rate_limited 暂停、可续跑），不计入额度。改用同样支持 tools 的 `nvidia/nemotron-3.5-lightning:free`。
- dogfood（本仓库副本，给 `fsutil.py` 加 `human_bytes()` + 测试，`max_rounds=12`）：12 轮用完被停，7/8 测试通过；102,578 token（输入 100,010）。工人 4 轮浪费在 `cd /workspace`（编造的绝对路径）上 → 系统提示加了「命令在 worktree 根目录跑，别 cd 绝对路径」；它自己写的边界测试有一个期望错误（1023.99 MB 断言为 GB）；验收器正确给出 fail + `check_script_modified`（目标本身要求新建测试文件，属预期标记）。
- 多文件题（`--size large`，`max_rounds=14`）：**3/3 pass**——ev-shop 14 轮 69,712 token、ev-csv 11 轮 42,130、ev-rename 14 轮 50,047；主管 token 235 / 181 / 235。ev-shop、ev-rename 用满 14 轮被停但改动已正确，验收器独立重跑判 pass（再次说明「不信工人声明、只信独立验收」有用）。
- 额度：结束后 49/50 已用（共约 51 次调用；还剩 1 次，按要求停止，没有跑第 5–8 道题）。
- 这些真实运行发生在 C-08 之前（压缩关闭的代码）；C-08 对同类转录的离线重放见上。

## M2 未完成/偏差

| 项 | 偏差 | 原因 / 计划 |
|---|---|---|
| M-12 | 卡住阈值、轮数上限是配置值（`[worker] stuck_rounds=10`、`max_rounds=40`、`same_error_limit=3`），未按评测调优 | M5 评测后调 |
| M-13 | 没有常驻守护进程：每个任务是独立的后台进程，状态全部落盘；任何 task 命令先「恢复」（进程已不在的 running → interrupted）再「调度」（按并发上限启动排队任务），工人结束时也会调度 | 满足「重启后状态不丢」且更轻 |
| M-13 / C-05 | 续跑只用「压缩历史 + 当前 diff」重开对话，不还原完整对话；时间预算按每次续跑单独计算（token / 金额累计）；共享 `events.jsonl` 过期行清理与并发追加之间无锁，极端情况下可能丢一行日志 | 取舍：省 token、跨服务商安全 |
| M-14 | 「只读 .git 引用」改为：命令拦截 + 拒绝型 git 钩子 + 验收时检测分支被提交（`worker_committed`）。混淆写法 + `--no-verify` 仍可能提交成功，但一定被标记为 needs_review | 真正只读需沙箱（V1.1） |
| M-15 | `hardcoded_return_suspect` 是启发式（新增 `if x == 字面量: return 字面量`），可能误报；C-11 起跳过基例（单个比较 + 0 / 1 / -1 / 空串，返回平凡字面量或目标原文里的字面量），代价：只特判边界值的作弊（如测试只测 0）不再标记 | 误报只会降为 needs_review |
| M-16 | 若主线程 4 s 内没响应停止信号，看门狗直接记 stopped 并退出，报告在 `task report` 时补做验收 | 兜底路径 |
| M-17 | 发给模型的消息统一脱敏：源码里若有形似密钥的字面量，模型看到的是 `[REDACTED]` | 与 M1 输出脱敏一致的取舍 |
| M-20 | 真实模型评测仍是小样本。本轮（2026-11-23 上午）：gemini-3.5-flash-lite 1 次连通调用成功后，第一道评测题即 429（日配额，Retry-After ≈ 20,000 s）；gemini-3-flash-preview 跑通多文件题 ev-shop（verdict pass，7 轮，工人 17,385 token，主管 228 token vs 直接模式 2,178；工人在调用 finish 前被 429 暂停，验收器独立重跑通过），下一题 ev-csv 即 429 日配额，按要求停止。累计真实结果：4 题通过（ev-add、ev-max、ev-pal、ev-shop），其余都被免费档配额挡住，没有一题是模型做错。下午 OpenRouter（nemotron-3.5-lightning:free）：3 道多文件题 3/3 通过（ev-shop、ev-csv、ev-rename），dogfood 1 个 12 轮未完成（7/8）；累计真实评测题 7 道全过 | 配额重置后（约北京时间 16:00）用 `task resume` 续跑被暂停的任务，或 `python -m eval.run --real ... --size large` |
| M-20 | 省 token：离线对照（24 题）——小题：直接模式 5,253 vs 派活 3,226 主管 token（C-06 瘦身前派活约 5.8k，反而更多）；多文件题：直接 5,442 vs 派活 553（约 1/10）。直接模式基线是「参考解 + 工具」的脚本化主管，不是真实主管模型 | M5 用真实主管模型再测 |
| C-08 | 小题开销已由 C-10 修正（0%）；真实 A/B 每格 1 次、收益不稳（NVIDIA −4.4%，Gemini 可比两题 −21.2%，单题 −38% 到 +5%）；`style_min_files` / `style_after_chars` 阈值按离线题与常识定，未用真实评测调优；「严重行永不删」让含大量 error 字样的代码 diff 几乎不省；过滤器只有内置的（不读项目自定义过滤器，避免工人改过滤器藏错误）；老化摘要是规则生成的，不用模型；token 用 字符/4 估算 | 后续：按任务规模决定是否加风格说明；真实模型对照评测（压缩开 / 关）留到配额允许时 |
| C-03 | 普通命令的事件写入共享的 `.agentkit/logs/events.jsonl`（按 run_id 区分，5 MB 轮转），每个任务一个独立文件，而不是每条命令一个文件 | 避免成千上万个小文件 |
| C-03 | JSON 契约 `error` 由 3 键变为 5 键（加 `code`、`run`），属于对 M-02 schema 的扩展（已登记 C-03） | — |
| M-01 | `uninstall` 只能找到登记过的仓库（本版本之前创建的 `.agentkit/` 不在登记表里，需在该仓库里运行 `agentkit uninstall --yes --this-repo`）；pip 以外的安装方式（pipx / uv / venv）不自动卸载包，输出会提示 | M4 单二进制后简化 |
| M-19 | 实测是脚本化的「机械 AI」（只执行 llms.txt 里的命令），不是另一个真实大模型；bare 场景依赖机器上有 uv 或 pipx | 足以发现文档缺口（已发现 1 个并修正） |

## M1 完成情况

| 编号 | 状态 | 实现 | 测试 |
|---|---|---|---|
| M-01 安装 | 完成（Python 包形态） | `pip install git+https://github.com/tangniutt/agentkit`，console script `agentkit` | 本地 venv 安装验证 |
| M-02 CLI JSON | 完成 | `agentkit/cli.py`，固定 5 键 envelope，`--human` | 每次测试调用都校验 schema（`tests/conftest.py`）；GitHub Actions（Python 3.10 / 3.12）运行 |
| M-03 输出预算 | 完成 | `core.finalize/fit` 兜底 + 各命令按预算填充、`next` 续读 | `test_cli_budget.py`（大文件、超长单行、随机预算模糊测试） |
| M-04 read | 完成 | `file:a-b`、`file#Symbol`、拒读二进制、>1MB 分段、拒读 `.env`/`~/.ssh` | `test_read_search.py` |
| M-05 search/ls/glob | 完成 | ripgrep 优先，Python 兜底；遵守 .gitignore（git 仓库用 `git ls-files`，否则内置解析） | 两种引擎 × git/非 git |
| M-06 repo_map | 完成 | tree-sitter 节点遍历（Py/JS/TS/Go/Rust/Java/C/C++/Ruby），跨文件引用计数排序，正则兜底 | 6 语言 + 1 万文件预算测试 |
| M-07 edit | 完成 | 唯一匹配、0/多次返回 3 个候选、多文件原子补丁、语法检查拒写（Python `ast`，其他 tree-sitter ERROR/MISSING 节点）、create/delete/rename、读后哈希校验 | `test_edit_checkpoint.py` |
| M-08 run | 完成 | 进程组超时击杀、exit_code/耗时、stderr 尾部优先、完整日志 `.agentkit/logs/` | 超时后台子进程被杀测试 |
| M-09 checkpoint | 完成 | 内容寻址快照 `.agentkit/objects` + manifest，list/diff/rollback/create，回滚哈希校验，回滚本身可撤销 | 回滚哈希一致、非 git 可用 |
| M-10 verify | 完成 | git worktree（非 git 用临时副本）；pass/fail/invalid；改动文件；可疑标记 tests_deleted/tests_modified/check_script_modified/test_config_modified；≤300 token，完整报告落盘 | `test_verify_memory.py` |
| M-11 直接模式 | 完成 | 无模型时全部工具可用；`task create` 返回结构化「直接模式」提示 | 全工具无模型冒烟测试 |
| M-18 doctor | 完成（模型连通性待 M2） | 版本、python、git、写权限、磁盘、tree-sitter、ripgrep、模型配置；每项 fix 提示 | |
| M-21 记忆（基础） | 完成 | `.agentkit/memory/*.md` + front matter；add/list/search/show/forget/brief；FTS5（grep 兜底）；锚点哈希 → maybe_stale；verify 通过（且无可疑标记）时确定性写入「通过的检查命令」 | brief ≤500 token、锚点失效 |
| M-22 命令权限 | 完成 | 默认拒绝 git push/commit、sudo、rm -rf / 或 ~、curl\|sh、读 ~/.ssh / .env；`.agentkit/config.toml` `[run] allow/deny` | 18 条拒绝 + 10 条放行用例 |
| 密钥脱敏（M-17 基础） | 完成（模式匹配） | 所有输出、run 日志、记忆写入均脱敏 | `test_run_security.py` |
| M-19 llms.txt + README | 完成（实测见 M2 表「M-19 实测」） | `llms.txt`、README 使用一节 | `test_llms_install.py` |

测试：`pytest -q` 107 个全绿（本机 Python 3.12 与 3.10；CI 同样通过）。Dogfood：见 `docs/dogfood-m1.md`（发现并修复 1 个 verify 误报 bug）。

性能实测（本开发机，10 万个小文件，非 git）：`search` 0.6 s（ripgrep）/ 2.1 s（纯 Python 兜底）；`glob` 0.3 s；`repo_map` 0.6 s 且输出 ≤2000 token。

## M1 未完成/偏差

| 项 | 偏差 | 原因 / 计划 |
|---|---|---|
| M-01 | 仍是 Python 包，不是单二进制；全新 venv + 空 HOME + 空 pip 缓存从 GitHub 安装 3.5 秒（`eval/llms_check.py`，本开发机网络）；真正的干净虚拟机未测 | 基线允许 M1 用 Python 包；二进制在 M4 |
| M-04 | >1MB 文件按「每页最多 2000 行 + 预算」分段，但文件仍整体读入内存（未流式） | 原型足够；M4 改流式 |
| M-05 | 10 万文件 <2 秒只在有 ripgrep 时达到；纯 Python 兜底约 2.1 秒 | doctor 提示安装 ripgrep |
| M-06 | 排序用跨文件标识符计数近似 Aider 的 PageRank；单次最多解析 3000 个代码文件；tree-sitter 语法包首次使用需联网下载（离线时自动退化为正则） | 足够产出概览；精排留给 V1.1 |
| M-07 | 「评测集一次成功率 ≥99%」无法在 M1 度量；没有语法包的语言不做语法检查；原本就有语法错误的文件不因已有错误拒写（只拒绝错误数增加） | 依赖 M-20 评测集 |
| M-08 | 命令正常结束后也会清理其进程组中的后台进程（不允许残留守护进程）；Windows 无进程组，只杀主进程 | 符合「子进程残留按进程组清理」；Windows 非 V1 目标 |
| M-09 | 保留最近 200 个快照，旧快照的对象文件暂不回收 | 后续加 `checkpoint gc` |
| M-10 | 干净副本不含 .gitignore 忽略的文件（如仓库内 `.venv`、`node_modules`），依赖需在仓库外可用；非 git 仓库的「改动文件」只能根据快照推断；花费字段在直接模式恒为 0 | 干净环境的代价；派活模式（M2）填真实花费 |
| M-17 | 只做模式匹配脱敏；「已配置 Key 明文零出现」需 M2 的 Key 引用机制 | M2 |
| M-18 | 模型连通性检查只检查「是否配置」 | 模型接入在 M2 |
| M-21 | 未做：任务失败原因 / 模型表现的自动写入（依赖 M2 任务）、有用分、冲突历史（同 id 新记忆直接覆盖旧记忆）、「探索次数下降 50%」评测 | M2 / M-20 |
| M-22 | 基于正则与分词的护栏，不是沙箱；刻意混淆（如 `$(echo git) push`）可绕过 | 沙箱为 Should（V1.1） |
| `.agentkit/` | 目录会以未跟踪项出现在 `git status`（其 `.gitignore` 只放行 config 与记忆，便于团队共享） | 设计如此；不想提交可在仓库 .gitignore 加 `.agentkit/` |
| M-12–M-16、M-20 | 不在 M1 范围 | M2 / M3 / M5 |

## 下一步
1. **Linux VPS 门禁**：Linux Python 3.10/3.12 + bubblewrap 全量已在 GitHub Actions 通过；Windows direct-mode 仅作非阻塞开发兼容回归，不是生产门槛。
2. **P1 收尾**：Goal 状态事件流、核心/可选工具包；MCP 2026 继续保持明确不支持，除非完整实现 `server/discover`、缓存与 `subscriptions/listen` 后再开放。
3. **P2 智能与生态**：补齐真实 LSP 服务器矩阵、外部 WorkerBackend、模型能力与健康档案、缓存感知成本；Builtin/XERJ/LSP 适配与 Pyright 真实引用已落地，待补公开 benchmark。
4. **P3 外部验证与发布**：SWE-bench Verified / Terminal-Bench / 内部长任务、外部作弊留出集、公开成本-质量报告、固定版本与回滚。
5. 原生二进制在 P3 证明价值后重新评估。

## 2026-11-25 生产化验证

- Windows 直接模式与跨平台模块：`pytest -q` = **344 passed, 62 skipped**（Python 3.13.7）；已修复 UTF-8 文档在 GBK locale 下的测试读取，并新增 Windows CI job。最终 wheel 构建成功（184,591 字节，SHA-256 `60812aea…`），在全新虚拟环境安装后 `doctor` 健康通过、MCP 列出 31 个工具且后端 schema 正确、HandoffPacket 与 CodexBackend 导入校验通过。
- 53 个跳过项均为 Linux 派活专属（worktree 工人、续跑、模型接力、压缩端到端、作弊库）。
- Linux CI 等价测试：普通容器 Python 3.10 / 3.12 均为 **386 passed, 2 skipped**；最新 Python 3.12 + bubblewrap + Codex 全量为 **404 passed, 2 skipped**。首轮暴露并修复：StopWorker 后 `outcome` 未绑定、acceptance 夹具基线错误、junction 测试跨平台、容器缺 `pgrep`；沙箱专项又修复源码/仓库被 `/tmp` 遮蔽、父进程绑定、状态路径只读、PID namespace、连接重置和命令替换绕过。
- 已修复并回归：目录联接/符号链接越界、记忆 ID 穿越、Unicode 验收漏检、权限包装绕过、检查点/状态并发、CRLF 编辑、Windows run 进程树、输出预算硬上限、长行续读；GitHub Actions Windows 的 `charmap` locale 暴露了验收报告 `write_text` 未指定 UTF-8 的问题，已显式使用 UTF-8 并新增非 UTF-8 locale 回归测试。
- bubblewrap 新增 `[sandbox] network=false` 硬隔离，任务报告与 EvidenceReceipt 记录 `sandbox_network`；一次性特权 Linux 容器实测共享/隔离网络、完整派活与禁网暂停。修复 editable 源码被 `/tmp` 遮蔽、短命父进程绑定、任务锁/证据路径只读、PID namespace 覆盖宿主 PID、ConnectionResetError 未归类等真实问题。工人不可写记忆/state，自动记忆改由主管侧幂等落盘；收据与复盘由验收器强制重写，命令位置替换默认拒绝。后续又新增域名白名单：独立网络命名空间、回环中继、宿主代理与 TLS SNI 校验。
- 中断的子智能体调研已缩窄重派并收到 Codex headless、竞品核心资产、一线实践、评测基准、XERJ 缺口最终报告；结论已并入竞品/PRD/来源文档。Antigravity 补核长时间未返回，已停止并改用官方 FAQ/headless 文档：consumer 不支持第三方包装，不接其登录态。
- 停用的“路径与记忆安全”“核心协议”代码子智能体没有最终报告，但主线按原始清单审计实际产物并回归 73 项安全/协议测试；未发现缺项后继续使用，不把停止状态当完成证据。
- 新增：`TaskSpec / GoalSpec / EvidenceReceipt / Retrospective / Event / HandoffPacket` 协议、`task handoff`、fail-to-pass、bubblewrap 适配、MCP stdio、Goal v1、task diff/steer/answer/accept/reject、Builtin/XERJ、Codex/OpenCode/Qoder/Cursor 适配器。支持 `--backend builtin|codex|opencode|qoder|cursor` 与跨后端接力，保留 worktree/diff/记忆/累计预算；API Key 模型继续走 builtin OpenAI 兼容层。OpenCode 使用稳定 v1.19.6 JSONL；Qoder 使用官方 CLI 账号登录或 PAT、记录请求 Credits，不读取凭证，并将 PAT 注册进日志脱敏。Cursor 使用官方 headless/stream-json、API Key、隔离配置与 session resume，拒绝 token/USD 预算。Antigravity consumer 不接登录态；Cursor/Qoder 真实账号任务尚未执行。
- XERJ 使用 SHA-256 核验的 rc.105 二进制与临时真实节点验证 search/def/memory；Query-1 未使用，默认词法 embedding 未冒充神经语义。
- `eval.codeintel_bench` 同条件 6 查询：内置后端 recall@5 从 1/6 提升到 5/6、MRR 0.275、3,204 字符、0.885 s；XERJ 5/6、MRR 0.264、17,208 字符、0.583 s。XERJ 原始返回 51,521 字符，适配层按 `--full` 截断后下降 66.6%，但仍约为内置的 5.4 倍；结论是延迟更低、召回同级、token 不占优，不宣称全面更优。真实 memory store/recall 已通过。
- pi v1.6.1 已核验 canonical 仓库、MIT 许可、默认四工具、树状会话、压缩阈值与真实 JSONL 事件；Composio 30 任务和预注册双 harness 研究支持“小前缀 + 少轮次 + 结果截断”的机制，但都有版本、reasoning、provider 或任务规模局限。AgentKit 当前固定前缀约 1.1k token，不盲目删除验收与权限工具；pi 外部工人因权限绕过与网络边界未完成而暂缓。
- **尚未达到生产标准**：凭证隔离、Antigravity 官方可用方案、Cursor/Qoder 真实账号任务、Cursor SDK 实际用量/成本、其余真实 LSP 服务器、真实基准、远端发布与签名仍未完成；Codex/OpenCode/Qoder/Cursor、Linux 域名网络白名单、本地发布回滚和 LSP 窄客户端已接入。生产目标平台为 Linux VPS；Windows 仅支持 direct mode，作为开发机兼容性验证，不承担派活沙箱或生产验收。Linux GitHub Actions 已通过，工作树与 `origin/main` 同步。
- wheel/sdist 干净安装冒烟：最终版本 `0.2.0` 的 wheel 与 sdist 均在全新虚拟环境安装成功；wheel 的 `doctor`、真实 Python/TypeScript LSP、MCP 初始化与 32 工具清单、关键模块隔离导入通过，sdist 关键模块导入通过。非 root Linux 3.12 容器再次从最终 wheel 安装，`doctor healthy`、MCP 32 工具与 release verify 通过。最终大小与 SHA-256 以 `dist/release-manifest.json` 与 `SHA256SUMS` 为准；`trusted:false` 仍表示只证明完整性，不证明来源。
- 凭证隔离仍有已知高风险：Qoder 默认改用 `XDG_CONFIG_HOME/agentkit/qoder` 专用登录目录，不再默认绑定 `~/.qoder`；OpenCode 默认把数据/缓存/状态目录隔离到任务目录并清除继承的 `OPENCODE_DB`，不再绑定真实凭证库。bubblewrap 默认用任务内 HOME/XDG（含 runtime）并掩蔽宿主对应目录，同时清除常见云凭证文件路径与 Git/SSH/GPagent socket，仅重新挂载显式授权的后端状态目录；用户模型与安全配置通过不可变环境快照传入，不再写入 worker 可写目录。宿主到 worker 的基础环境改为白名单，仅显式重新注入当前后端选中的密钥，其他 GITHUB_TOKEN/AWS/OPENAI 等凭证不会继承；Codex 保存登录不再隐式可用，需使用专用 API Key。复用其他登录态必须显式设置 `QODER_CONFIG_DIR`/`config_dir` 或 OpenCode `allow_account_login=true`，并在用户级 `~/.config/agentkit/config.toml`（或 `$XDG_CONFIG_HOME/agentkit/config.toml`）设置 `[security] allow_saved_credentials=true`；项目配置不能伪造该授权。Qoder 专用 `key_env` 缺失时不会静默回退到保存登录态，PAT/API Key 路径不需要此开关。当前依靠 CLI 工具规则拒绝读写认证路径，不是强隔离；未完成独立凭证 broker/代理前，不应在不可信仓库中使用真实登录态。
- LSP 代码智能：新增 `agentkit/lsp.py` 自研 stdio 窄客户端（initialize/didOpen/definition/references/shutdown、Content-Length 帧、UTF-16 坐标、超时与进程清理）和 `agentkit code refs`；未配置 LSP 时回退到明确标注的词法引用。帧解析拒绝重复头、Transfer-Encoding 与超限头块；位置归一化拒绝远程 file URI；LSP 子进程默认不继承凭证类环境变量，Windows 用 Job Object 清理整个进程树。协议/生命周期/路径越界/代码智能/MCP/手册相关回归 **113 passed**（含真实服务器）；Serena 主体 NOASSERTION，只读参考 SolidLSP，未复制代码，NOTICE 不变。2026-11-26 使用官方 `pyright@1.1.414` 与临时仓库真实验证：`add` 定义处（第 1 行第 5 列）默认返回第 4 行调用，`--include-declaration` 同时返回定义和调用，CLI 与 MCP 结果均标记 `backend:lsp, semantic:true`；可通过 `AGENTKIT_LSP_TEST_COMMAND`（JSON argv 数组）启用 `tests/test_lsp_real.py`。最终 wheel 另以 `typescript-language-server@6.1.0` + `typescript@5.9.3` 验证 TypeScript 定义/调用；其余语言服务器矩阵尚未执行。
- MCP 资源与通知：新增动态 task/Goal 资源、`resources/list` 快照绑定游标分页、2025 `resources/subscribe/unsubscribe` 与后台 `resources/updated/list_changed` 通知；2026 明确返回 `-32022`，不伪装支持。契约与真实 stdio 通知测试 **13 passed**；审查后补齐异常 params、损坏状态文件、游标过期、监视异常与通知合并限流。
- 本地发布回滚：新增 `agentkit release plan|build|verify|sign|rollback`，构建 wheel/sdist、`SHA256SUMS` 与 `release-manifest.json`，校验哈希/大小/METADATA/全部源码模块/过滤器/许可文件/sdist 安全路径；路径同时按 POSIX 与 Windows 规则检查，拒绝穿越、重复 ZIP 项、加密项、符号链接与特殊文件。回滚默认 dry-run，拒绝重定向 pip 环境，Windows 控制台采用退出后 helper 安装。发布自动化 **28 passed**；真实构建与 verify 通过（`trusted:false` 表示只证明完整性，不证明来源），并新增 OpenSSL `SHA256SUMS` 分离式签名/公钥验证，签名测试使用临时密钥且不写入仓库，真实 `release sign`/`verify --signature --public-key` CLI 冒烟通过；临时 Windows 环境 helper 回滚结果 `installed:true, exit_code:0`。回滚安装私有暂存副本并使用 `python -I -m pip --isolated`，Windows helper 用 OpenProcess/WaitForSingleObject 等待退出；覆盖失败恢复旧 artifact。CLI 输出统一 UTF-8，修复 GBK 环境非 ASCII 崩溃。
- 2026-11-26 网络白名单：`agentkit/netproxy.py` 实现宿主 Unix socket 代理与沙箱内回环中继；中继只接收最小环境，不继承模型 Key/PAT。`network=false` 保持完全断网，`network=["api.example.com"]` 使用独立网络命名空间，HTTP 校验 URL/Host 且每连接只处理一个请求，HTTPS CONNECT 仅允许 443 并校验 TLS ClientHello SNI；域名解析拒绝非全局地址，限制模式在 bubblewrap 不可用时拒绝启动。Linux 代理/沙箱集成 **22 passed**，覆盖放行域名、拒绝域名、POST 请求体原样转发、CONNECT 同包 ClientHello、SNI 不匹配拒绝、管道后续请求不转发、DNS 重绑定拒绝、中继环境不继承密钥、白名单任务成功、非白名单模型端点阻断、限制网络不可降级、doctor 配置校验与收据记录；Windows 可运行单元测试通过。
- 2026-11-26 外部后端门禁：Windows 全量 **380 passed, 84 skipped**；Linux Python 3.12 + bubblewrap 全量 **462 passed, 2 skipped**；Qoder/OpenCode/Codex 单测与 MCP 契约 **32 passed**；外部后端/沙箱端到端 **30 passed**，覆盖无 AgentKit 模型创建、doctor 外部后端模式、PAT 日志脱敏、预算停止后续跑、builtin→Qoder/OpenCode、Qoder→OpenCode、OpenCode→Codex 和沙箱内执行。Qoder CLI 1.1.98 已核验官方帮助、公开 stream-json 契约与适配器真实参数解析（仅被登录拦截）；当前开发机 CLI 只读检查返回 `Not logged in`，需用户运行 `qodercli login`，Desktop 登录态未自动复用；真实账号任务尚未执行。OpenCode 已核验稳定版源码、CLI 帮助与真实 `debug config` 解析（权限策略原样生效），端到端使用假可执行文件。
- 2026-11-26 Cursor 接入前最终本地门禁：Windows 全量 **430 passed, 84 skipped**（启用真实 Pyright）；Linux Python 3.10 与 3.12 + bubblewrap 均为 **510 passed, 4 skipped**；发布自动化 **26 passed**。wheel/sdist 全新安装、`doctor`、MCP 32 工具、真实 Python/TypeScript LSP、关键模块导入、`twine check` 均通过；依赖审计在升级临时环境 pip 后未发现已知漏洞（未发布的 AgentKit 自身不在 PyPI 漏洞库范围）。构建时工作树为 `git_dirty:true`；后续已创建 DCO 提交 `ae85bf3` 并推送，GitHub Actions 通过。签名与正式发布仍未执行。
- 2026-11-27 Cursor 工人：新增官方 `agent --print --output-format stream-json` 适配、API Key 专用环境映射、独立配置目录、session resume、轮次/时间预算和 Shell/WebFetch/MCP/密钥 deny 策略；不读取登录态、不用 `--force`。另增加显式可选 `transport="sdk"`（`.[cursor]`，官方专有 `cursor-sdk` 1.5.2，不 vendored），使用本地工具白名单、隔离用户配置、session resume、TokenUsage 与按运行 UsageCost，并要求显式模型；SDK 未报告用量时拒绝 token 预算，USD 硬预算仍不支持。Qoder 专用目录改动后 Windows 最终全量 **449 passed, 92 skipped**，Linux Python 3.10 与 3.12 最终全量均为 **537 passed, 4 skipped**；OpenCode V2 与发布签名接入后 Windows 最终全量 **455 passed, 93 skipped**（含真实 Pyright），Linux Python 3.10 与 3.12 最终全量均为 **544 passed, 4 skipped**。中继端口文件启动竞态已修复，Windows 代理回归 **12 passed, 2 skipped** 且竞态用例连续 **20 passed**。SDK/CLI/代理/发布/MCP/手册 Windows 专项 **148 passed, 10 skipped**，Linux SDK 任务与代理专项 **37 passed**，可选 extra 安装导入通过；官方 SDK 1.5.2 在 Linux 以假 Key 创建 agent 时工具白名单被接受并到达 `Invalid User API Key`，未执行真实模型调用。可选 extra 依赖审计在升级临时 pip 后无已知漏洞（AgentKit 自身未发布故不在 PyPI 漏洞库范围）。最终 wheel/sdist 已重建并通过 verify、`twine check`、基础干净安装、doctor、双 LSP、MCP 32 工具/Cursor schema 与 `.[cursor]` 安装导入；最终哈希见 `dist/release-manifest.json`。真实 Cursor API Key 任务尚未执行。
- 2026-11-27 OpenCode V2：新增显式 `version="v2"`，使用官方 `--standalone run --format json`、V2 `permissions/policies`、session resume 与独立数据/缓存/状态目录；默认清除 `OPENCODE_DB`，真实账号登录需显式 `allow_account_login=true`。官方 V2 2.4.2 免登录列出 `opencode/beam-free` 等免费模型；真实最小任务返回 `submitted`，解析 **5,688 输入 / 38 输出 token** 与 session，证明事件契约可用。免费额度错误 `provider.free-limit`/429 映射为 `AK-MODEL-429` 暂停，不绕过限额。Windows OpenCode/MCP/手册专项 **105 passed, 9 skipped**，Linux V2 任务/沙箱/代理专项 **35 passed**；V2 策略经官方二进制验证且不妨碍免费模型列表。
- 2026-11-27 离线评测：Windows direct 模式 **24/24 通过**（3 large + 21 tiny）；worker 24 项与作弊库 14 项均因 `AK-TASK-STATE`（dispatch 仅 Linux）未执行，不能计为失败或通过。Linux 3.12 + bubblewrap 完整离线评测：direct **24/24**、worker **24/24**、作弊库 **14/14 detected**；worker 共 203,508 token、报告成本 0.0221 USD（假模型计价，仅回归口径）。该结果只作固定版本回归证据，不替代真实模型/外部基准。另用真实 `opencode/beam-free` 执行 `ev-add`：官方免费额度返回 429，AgentKit 映射为 `AK-MODEL-429` 暂停，代码未改动且独立测试仍失败；未切换账号/模型绕过限额，真实外部基准仍受额度阻塞。
- 2026-11-27 接入边界：生产平台为 Linux VPS；只接可脚本化的官方 CLI/API/RPC/MCP 编程工具，不接 GUI 自动化、电脑操作工具，也不为需要模拟 Windows 沙箱的工具做兼容层。Windows 仅保留 direct-mode 开发兼容，不再是生产验收门槛。
- 2026-11-27 外部工人评测：`python -m eval.run` 新增 `--backend codex|opencode|qoder|cursor`，外部工人复用同一 TaskSpec、token/USD/时间预算和独立验收；JSON 报告记录 schema、提交/工作树指纹、Python/平台、后端/模型、任务集合、配置哈希、预算与结果哈希，空任务集或任一任务失败返回非零。OpenCode 假工人端到端验证通过；外部评测强制 `[sandbox] mode=required`，报告记录实际沙箱，超时会取消任务并返回失败。真实 Cursor/Qoder/Codex 账号不可用，尚未完成真实外部基准。
- 2026-11-27 保存登录态授权：新增用户级 `[security] allow_saved_credentials=true`，项目配置不能伪造 `_user_security` 或自行开启；Qoder 保存登录、OpenCode `allow_account_login=true` 与仓库外 Qoder `config_dir` 默认拒绝，PAT/API Key 路径保持可用。Qoder 专用 `key_env` 显式映射到官方 PAT 变量，缺失时允许标准 PAT 回退但不会回退到保存登录态；`doctor` 显示授权与认证准备状态。Windows 全量 **463 passed, 94 skipped**；Linux Python 3.10/3.12 分组全量均为 **544 passed, 13 skipped**；该改动收紧默认值，但不替代独立凭证 broker。
- 2026-11-27 凭证与评测边界加固：提交 `79f65d1` 将用户配置改为环境快照，worker 基础环境改为白名单且只注入当前后端密钥；Qoder/Cursor 后端状态目录拒绝整仓、仓库祖先、`.git`、`.agentkit` 和受版本控制目录，相对路径统一按仓库根解析。外部评测强制 bubblewrap、记录实际沙箱并取消超时任务；提交 `640a166` 补齐“等待期间越过截止时间后完成”仍必须判失败的竞态回归。GitHub Actions Run `46043378003`（`79f65d1`）：Linux Python 3.10/3.12 各 **568 passed, 3 skipped**，Windows direct **473 passed, 98 skipped**；最终 Run `46044774691`（`640a166`）：Linux Python 3.10/3.12 各 **569 passed, 3 skipped**，Windows direct **474 passed, 98 skipped**。本机完整 Linux 3.12 + bubblewrap **567 passed, 4 skipped**，特权容器端到端 **19 passed**，发布自动化 **28 passed**。最终 `640a166` wheel/sdist 全新安装、doctor、release verify、MCP 32 工具与 `twine check` 均通过；wheel SHA-256 `675ec462674514103ad431bc8499a21c8679adb2aa17b8cef63a53e84fc38939`（223,348 字节），sdist SHA-256 `b036d1a1712c794330b4fdf8e96f0c5efd4586764a61c79f01a70784b6077001`（296,961 字节）。这不替代独立凭证 broker，真实外部账号基准仍未完成。
- 2026-11-27 CI 发布门禁：提交 `1e62f8e` 新增独立 Linux release job，从干净 checkout 构建 wheel/sdist、执行 `agentkit release verify` 与 `twine check`，并上传保留 14 天的 Actions artifact，不发布、不部署。Run `46045646069` 的 release job 已成功；下载工件再次校验通过，wheel SHA-256 `1e49849c9046a4de40c5ee2bbf1b08a0b2a6267d4522a1a830bbeb60605420dd`（222,416 字节），sdist SHA-256 `8b60a57388c386ddb41544a92c1281dc4572cb39baeceacb0604766ad3716061`（293,132 字节）。
