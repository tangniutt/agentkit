# 进度

更新：2026-11-23（M2 补完 + C-08 上下文压缩 + OpenRouter 实测）

| 阶段 | 状态 |
|---|---|
| M0 调研 + 需求基线 | 完成（基线冻结，变更 C-01 至 C-08 已登记） |
| 阶段二 接口契约 | 与 M1 合并完成：CLI 输出 schema 即契约（`{ok,data,error{what,why,next,code,run},truncated,next}`；C-03 增加 code、run） |
| M1 Python 原型（直接模式工具集） | **完成**（见下表；偏差见「M1 未完成/偏差」） |
| M2 工人 + 派活 + 验收器 | **完成**（含 C-03 / C-04；补完 C-05 续跑、C-06 输出瘦身 + 多文件评测、C-07 llms.txt 安装修正、C-08 上下文压缩、uninstall、日志 7 天清理；真实模型实测 2 家（Gemini、OpenRouter），评测仍是小样本，见「M2 未完成/偏差」） |
| M3 防作弊与安全 | 部分提前完成（M-15 基础规则 + 14 例作弊库 100% 检出、M-17 Key 引用与扫描测试）；沙箱仍在 V1.1 |
| M4 Rust/Go 正式版 | 未开始 |
| M5 评测与发布 | 未开始 |

## M2 完成情况

| 编号 | 状态 | 实现 | 测试 |
|---|---|---|---|
| M-12 工人循环 | 完成（离线）；真实接口实测 2 家（Gemini OpenAI 兼容、OpenRouter `:free`）；魔搭 / Ollama 待测 | `agentkit/worker.py`（mini-swe-agent 式循环，只用 M1 工具 + finish）；`agentkit/models.py`（标准库 OpenAI 兼容客户端）。工具调用格式错误本地修复（JSON 修补 / Python 字面量 / 文本里的调用 / 工具名别名），修不了把错误回给模型，连续 3 次失败 → 任务失败；卡住检测（N 轮无新 diff、同一错误重复 3 次）；上下文到 70% 时折叠旧工具输出（保留目标、记忆摘要、最近几轮）；开工先读 `memory brief`；回传 Gemini `extra_content.thought_signature` | `test_m2_tasks.py`（修复、3 次失败、纯文本、卡住×2、压缩、签名回传） |
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

| C-08 上下文压缩 | 完成 | ① `agentkit/compress.py` + `agentkit/filters/*.json`（15 个过滤器：pytest、unittest、jest/vitest、go test、cargo test、tsc、eslint、ruff、mypy、pip/uv、npm/pnpm/yarn install、git status/diff/log、generic），过滤器是数据，每个带内联样例（`verify_filters()` 是校验门）；按命令（拆 `&&`/`;`/`|`、去 `cd`/`FOO=1`/`uv run`/`npx` 前缀）再按输出特征选择；去 ANSI、`\r` 重绘、进度条，警告与重复行带 `[×N]` 计数折叠，相似行分组，头 / 尾 + 中间所有受保护行；含 error/fail/assert/traceback/panic/ERR! 等的行**永不删**；diff 先给「N files: 路径 (+a -b)」一行并按文件封顶、头部优先显示。`run` 输出带 `filtered:{filter,lines,raw}`，`run --raw` / `[compress] run_filters=false` 关闭；验收的 `why` 取摘要 / 诊断行（`best_why`）。② `agentkit/context.py` 会话去重：内容哈希（去掉耗时、日志名）相同的 read/search/ls/glob/repo_map/run/memory 结果 → `{"same_as_turn":N}`。③ 渐进老化：最近 2 轮原样，更早的工具结果变一行 `[aged]` 摘要（read：路径 + 行号 + sha；run：命令 + exit + 摘要行；search：命中文件），旧 create/edit 参数缩成合法 JSON；只改内容不删消息，tool_call/结果配对不变；攒够 `age_min_chars`（默认 4000）才批量老化；被引用的原文先挪进最近窗口。原 70% 折叠保留为兜底。④ 工人系统提示加简洁风格（约 70 token）+ 「命令在 worktree 根目录跑、别 cd 绝对路径」（dogfood 发现）。⑤ 稳定前缀：messages[0]（系统 + 记忆摘要）、messages[1]（任务）、工具定义从不改写。⑥ `validate`：代码块、受保护行及其上的路径 / URL / 版本号必须原样保留，否则退回原文；原文始终在 `.agentkit/logs`（写入时脱敏），`agentkit logs RUN --raw` 查看。⑦ `eval/compress_bench.py` + `eval/fixtures/logs/`（真实抓取的 npm / pip / git diff / git log / git status / pytest -v） | `test_compress.py`（17 个：所有过滤器样例、严重行在每个过滤器随机噪声下都保留、校验器拒绝 + 退回、小输出不动、run 过滤 + `logs --raw` 找回原文 + 两种关闭方式、坏 id 拒绝、验收 why、去重 + 老化配对与前缀、批量老化、工人端到端逐次请求前缀字节相同、关闭开关、diff 文件列表在前、基准不让任何关键行消失） |
测试：`pytest -q` **189 个全绿**（M1 的 107 + M2 65 + C-08 17）；CI 见文末。Dogfood：`docs/dogfood-m2.md`（离线派活通过；真实 Gemini 任务暴露并修复了 503 处理 bug）。

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
| 直接模式主管 | 10,687 | 10,687 | 0（小题检查输出太短，过滤不触发） | — |

假模型按脚本行动，测不到「简洁风格让模型少写字」的输出 token 节省，也测不到真实模型看到 `[aged]` 后会不会多读一次。

**3. 真实转录重放**（OpenRouter dogfood 任务 t1f8b29，12 次模型调用；把记录的消息按 C-08 规则逐轮重放，只算 messages、不含工具定义）：提示 token 合计 61,166 → 24,546（**−60%**；老化 4 个大结果：repo_map 与整文件 read；该转录里没有完全相同的重复结果，去重 0 次）。这是上限估计：真实模型面对老化后的上下文可能会重读文件。

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
| M-12 | 验收标准要求「至少跑通 3 家 OpenAI 兼容接口」：已实测 2 家（Gemini、OpenRouter `:free`），第 3 家（魔搭 / Ollama）待测 | 下次会话 |
| M-12 | 卡住阈值、轮数上限是配置值（`[worker] stuck_rounds=10`、`max_rounds=40`、`same_error_limit=3`），未按评测调优 | M5 评测后调 |
| M-13 | 没有常驻守护进程：每个任务是独立的后台进程，状态全部落盘；任何 task 命令先「恢复」（进程已不在的 running → interrupted）再「调度」（按并发上限启动排队任务），工人结束时也会调度 | 满足「重启后状态不丢」且更轻 |
| M-13 / C-05 | 续跑只用「压缩历史 + 当前 diff」重开对话，不还原完整对话；时间预算按每次续跑单独计算（token / 金额累计）；共享 `events.jsonl` 过期行清理与并发追加之间无锁，极端情况下可能丢一行日志 | 取舍：省 token、跨服务商安全 |
| M-14 | 「只读 .git 引用」改为：命令拦截 + 拒绝型 git 钩子 + 验收时检测分支被提交（`worker_committed`）。混淆写法 + `--no-verify` 仍可能提交成功，但一定被标记为 needs_review | 真正只读需沙箱（V1.1） |
| M-15 | `hardcoded_return_suspect` 是启发式（新增 `if x == 字面量: return 字面量`），可能误报 | 误报只会降为 needs_review |
| M-16 | 若主线程 4 s 内没响应停止信号，看门狗直接记 stopped 并退出，报告在 `task report` 时补做验收 | 兜底路径 |
| M-17 | 发给模型的消息统一脱敏：源码里若有形似密钥的字面量，模型看到的是 `[REDACTED]` | 与 M1 输出脱敏一致的取舍 |
| M-20 | 真实模型评测仍是小样本。本轮（2026-11-23 上午）：gemini-3.5-flash-lite 1 次连通调用成功后，第一道评测题即 429（日配额，Retry-After ≈ 20,000 s）；gemini-3-flash-preview 跑通多文件题 ev-shop（verdict pass，7 轮，工人 17,385 token，主管 228 token vs 直接模式 2,178；工人在调用 finish 前被 429 暂停，验收器独立重跑通过），下一题 ev-csv 即 429 日配额，按要求停止。累计真实结果：4 题通过（ev-add、ev-max、ev-pal、ev-shop），其余都被免费档配额挡住，没有一题是模型做错。下午 OpenRouter（nemotron-3.5-lightning:free）：3 道多文件题 3/3 通过（ev-shop、ev-csv、ev-rename），dogfood 1 个 12 轮未完成（7/8）；累计真实评测题 7 道全过 | 配额重置后（约北京时间 16:00）用 `task resume` 续跑被暂停的任务，或 `python -m eval.run --real ... --size large` |
| M-20 | 省 token：离线对照（24 题）——小题：直接模式 5,253 vs 派活 3,226 主管 token（C-06 瘦身前派活约 5.8k，反而更多）；多文件题：直接 5,442 vs 派活 553（约 1/10）。直接模式基线是「参考解 + 工具」的脚本化主管，不是真实主管模型 | M5 用真实主管模型再测 |
| C-08 | 小题工人 token +5.7%（风格说明的固定开销），只有多文件 / 冗长输出时才净省；「严重行永不删」让含大量 error 字样的代码 diff 几乎不省；过滤器只有内置的（不读项目自定义过滤器，避免工人改过滤器藏错误）；老化摘要是规则生成的，不用模型；token 用 字符/4 估算 | 后续：按任务规模决定是否加风格说明；真实模型对照评测（压缩开 / 关）留到配额允许时 |
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
1. 真实模型评测补完：OpenRouter 额度重置后（每天 50 次）用 C-08 开 / 关各跑同一组多文件题，测真实工人 token 与轮数（离线只测到 −11.6% / +5.7%）；Gemini 续跑被暂停的任务；第 3 家（魔搭 / 本地 Ollama）。
2. C-08 调整：小任务不加风格说明（或更短），diff 过滤考虑「代码行里的 error 字样」不算诊断行（需先变更登记，因与「严重行永不删」冲突）。
3. M4：Rust/Go 单二进制（安装、卸载随之简化）。
