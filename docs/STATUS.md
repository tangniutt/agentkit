# 进度

更新：2026-11-23

| 阶段 | 状态 |
|---|---|
| M0 调研 + 需求基线 | 完成（基线冻结，变更 C-01、C-02 已登记） |
| 阶段二 接口契约 | 与 M1 合并完成：CLI 输出 schema 即契约（`{ok,data,error{what,why,next},truncated,next}`） |
| M1 Python 原型（直接模式工具集） | **完成**（见下表；偏差见「M1 未完成/偏差」） |
| M2 工人 + 派活 + 验收器 | 未开始 |
| M3 防作弊与安全 | 未开始（M1 已有基础可疑标记与命令拦截） |
| M4 Rust/Go 正式版 | 未开始 |
| M5 评测与发布 | 未开始 |

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
M2：工人循环（mini-swe-agent 骨架）+ OpenAI 兼容接口 + task create/status/wait/report/cancel + 每任务 worktree。
