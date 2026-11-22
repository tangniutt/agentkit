# 进度

更新：2026-11-23

| 阶段 | 状态 |
|---|---|
| M0 调研 + 需求基线 | 完成（基线冻结，变更 C-01 至 C-04 已登记） |
| 阶段二 接口契约 | 与 M1 合并完成：CLI 输出 schema 即契约（`{ok,data,error{what,why,next,code,run},truncated,next}`；C-03 增加 code、run） |
| M1 Python 原型（直接模式工具集） | **完成**（见下表；偏差见「M1 未完成/偏差」） |
| M2 工人 + 派活 + 验收器 | **完成**（含 C-03 可诊断性、C-04 反馈引导；真实模型评测部分待配额重置，见「M2 未完成/偏差」） |
| M3 防作弊与安全 | 部分提前完成（M-15 基础规则 + 14 例作弊库 100% 检出、M-17 Key 引用与扫描测试）；沙箱仍在 V1.1 |
| M4 Rust/Go 正式版 | 未开始 |
| M5 评测与发布 | 未开始 |

## M2 完成情况

| 编号 | 状态 | 实现 | 测试 |
|---|---|---|---|
| M-12 工人循环 | 完成（离线）；真实接口实测 1 家（Gemini OpenAI 兼容），另 2 家待测 | `agentkit/worker.py`（mini-swe-agent 式循环，只用 M1 工具 + finish）；`agentkit/models.py`（标准库 OpenAI 兼容客户端）。工具调用格式错误本地修复（JSON 修补 / Python 字面量 / 文本里的调用 / 工具名别名），修不了把错误回给模型，连续 3 次失败 → 任务失败；卡住检测（N 轮无新 diff、同一错误重复 3 次）；上下文到 70% 时折叠旧工具输出（保留目标、记忆摘要、最近几轮）；开工先读 `memory brief`；回传 Gemini `extra_content.thought_signature` | `test_m2_tasks.py`（修复、3 次失败、纯文本、卡住×2、压缩、签名回传） |
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

测试：`pytest -q` **156 个全绿**（M1 的 107 + M2 新增 49）；CI（Python 3.10 / 3.12）通过。Dogfood：`docs/dogfood-m2.md`（离线派活通过；真实 Gemini 任务暴露并修复了 503 处理 bug）。

## M2 未完成/偏差

| 项 | 偏差 | 原因 / 计划 |
|---|---|---|
| M-12 | 验收标准要求「至少跑通 3 家 OpenAI 兼容接口」：目前只实测 Gemini（另有本地假服务器）；OpenRouter / 魔搭 / Ollama 待用户提供 Key 或本地模型 | 待测 |
| M-12 | 卡住阈值、轮数上限是配置值（`[worker] stuck_rounds=10`、`max_rounds=40`、`same_error_limit=3`），未按评测调优 | M5 评测后调 |
| M-13 | 没有常驻守护进程：每个任务是独立的后台进程，状态全部落盘；任何 task 命令先「恢复」（进程已不在的 running → interrupted）再「调度」（按并发上限启动排队任务），工人结束时也会调度 | 满足「重启后状态不丢」且更轻 |
| M-13 | paused（429 / 超时）任务不能原地续跑，只能重新 create | 续跑（task answer/steer）属 Should |
| M-14 | 「只读 .git 引用」改为：命令拦截 + 拒绝型 git 钩子 + 验收时检测分支被提交（`worker_committed`）。混淆写法 + `--no-verify` 仍可能提交成功，但一定被标记为 needs_review | 真正只读需沙箱（V1.1） |
| M-15 | `hardcoded_return_suspect` 是启发式（新增 `if x == 字面量: return 字面量`），可能误报 | 误报只会降为 needs_review |
| M-16 | 若主线程 4 s 内没响应停止信号，看门狗直接记 stopped 并退出，报告在 `task report` 时补做验收 | 兜底路径 |
| M-17 | 发给模型的消息统一脱敏：源码里若有形似密钥的字面量，模型看到的是 `[REDACTED]` | 与 M1 输出脱敏一致的取舍 |
| M-20 | 真实模型评测只完成小样本（3 题通过、6 次被免费档 429/503 挡住）：**real-model eval pending quota reset**；直接模式基线是「参考解 + 工具」的脚本化主管，不是真实主管模型 | 配额重置后跑 `python -m eval.run --real ...` |
| M-20 | 离线小题上派活模式的主管 token 反而略多（固定开销约 280 token/题）；省 token 结论需更大的真实任务 | M5 |
| C-03 | 普通命令的事件写入共享的 `.agentkit/logs/events.jsonl`（按 run_id 区分，5 MB 轮转），每个任务一个独立文件，而不是每条命令一个文件 | 避免成千上万个小文件 |
| C-03 | JSON 契约 `error` 由 3 键变为 5 键（加 `code`、`run`），属于对 M-02 schema 的扩展（已登记 C-03） | — |
| 其他 | 仍未做：`agentkit uninstall`、日志 7 天保留清理、M-19「陌生 AI 只读 llms.txt 自装」实测 | M5 |

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
| M-19 llms.txt + README | 文档完成，「陌生 AI 只读 llms.txt 自装」实测待做 | `llms.txt`、README 使用一节 | |

测试：`pytest -q` 107 个全绿（本机 Python 3.12 与 3.10；CI 同样通过）。Dogfood：见 `docs/dogfood-m1.md`（发现并修复 1 个 verify 误报 bug）。

性能实测（本开发机，10 万个小文件，非 git）：`search` 0.6 s（ripgrep）/ 2.1 s（纯 Python 兜底）；`glob` 0.3 s；`repo_map` 0.6 s 且输出 ≤2000 token。

## M1 未完成/偏差

| 项 | 偏差 | 原因 / 计划 |
|---|---|---|
| M-01 | 仍是 Python 包，不是单二进制；本机（有 pip 缓存）安装约 3 秒，干净机器 ≤30 秒未实测 | 基线允许 M1 用 Python 包；二进制在 M4 |
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
1. 真实模型评测补完（配额重置后）：Gemini 剩余题目 + 第 2、3 家 OpenAI 兼容接口（OpenRouter / 魔搭 / 本地 Ollama），记录通过率 / token / 成本。
2. 补 M-19「陌生 AI 只读 llms.txt 自装」实测、日志保留清理、`uninstall`。
3. M4：Rust/Go 单二进制。
