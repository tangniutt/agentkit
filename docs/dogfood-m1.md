# M1 Dogfood 记录

日期：2026-11-23 · 执行者：Hark（直接模式，无模型配置）· 工具：已安装的 `agentkit` 命令（`pip install -e .`）

## 样例仓库
`textstats`（仓库外的临时 git 仓库，不提交）：`textstats/core.py` 有 `tokenize / word_count / top_words`，`tests/test_core.py` 3 个测试。
预埋 bug：`tokenize` 用 `text.split(" ")`，连续空格和换行会产生空词 / 粘连词。

## 过程摘要（每步都是一行 JSON，以下为要点）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `agentkit doctor --human` | healthy，mode=direct（无模型），git / tree-sitter / ripgrep 均可用 |
| 2 | `agentkit repo_map` | 4 个文件，tree-sitter 解析出 6 个符号，约 120 token |
| 3 | `agentkit memory brief` | 空（新仓库） |
| 4 | `agentkit run "python -m pytest -q -p no:cacheprovider"` | exit_code=1，`test_word_count_whitespace` 失败：`assert 7 == 3` |
| 5 | `agentkit search "def tokenize"` | `textstats/core.py:4:def tokenize(text):` |
| 6 | `agentkit read 'textstats/core.py#tokenize'` | 只返回第 4–5 行（按符号读） |
| 7 | `agentkit edit ... --old 'text.split(" ") ]'`（故意写错空白） | 拒绝：0 次匹配，返回 3 个最接近候选，第 1 个就是第 5 行（相似度 0.42） |
| 8 | `agentkit edit ... --new 'for w in text.split(]'`（故意写出语法错误） | 拒写：`syntax error ... line 5`，附上下文，文件未改 |
| 9 | `agentkit edit ... --old 'for w in text.split(" ")]' --new 'for w in text.split() if w.strip(".,!?;:")]'` | 成功，自动快照 `cp-00001`，`@@ -5 +5 @@` |
| 10 | `agentkit run "python -m pytest ..." --budget 150` | 3 passed |
| 11 | `agentkit verify --check "python -m pytest ..." --check "python -m flake8_missing textstats"` | verdict=**invalid**（第二个检查命令本身不存在），正确区分「验收无效」与「不通过」 |
| 12 | 同上 | **发现 AgentKit 自身 bug**：`suspicious` 里出现 `check_script_modified: textstats/core.py`——误把检查命令的「被检查目录参数」当成了检查脚本 |
| 13 | 修复 `verify._check_paths`（只把被执行的脚本和测试路径算作检查依赖）+ 回归测试 `test_verify_source_dir_arg_is_not_check_script` | 重跑：invalid 且无误报；只用 pytest 时 verdict=pass，suspicious=[] |
| 14 | 模拟作弊：`agentkit edit tests/test_core.py` 把断言改成 `assert True`，再 `verify` | verdict=pass 但标记 `tests_modified` + `check_script_modified` |
| 15 | `agentkit checkpoint rollback cp-00002` | 测试文件恢复，hash_match=true，并给出可撤销回滚的 `undo` |
| 16 | `agentkit memory add "Run tests" ... --pin --anchor 'textstats/core.py#tokenize'`；`memory brief` | 核心记忆出现在 brief 中 |
| 17 | `agentkit run "git commit -am fix"` | 被 M-22 拒绝（rule git-commit），附原因与放行方法 |

## 发现的问题与处理
1. **verify 误报 check_script_modified**（步骤 12）：已修复并加回归测试。
2. **自动记忆会记住「作弊」那次通过的检查命令**（步骤 16 的 brief 里出现了步骤 14 的命令）：已改为仅在 verdict=pass 且无可疑标记时写入，并合并而非覆盖已有命令；加测试 `test_verify_auto_memory_skips_suspicious_runs`。
3. `--human` 对「字典列表」渲染太散：改为每项一行 `key=value`。
4. 观察：`.agentkit/` 会以未跟踪目录出现在 `git status`（记忆文件可共享的设计结果），已写入 STATUS 偏差表。

## 结论
直接模式下「定位 → 读 → 改（含失败重试） → 跑测试 → 独立验收 → 回滚」全流程只用 AgentKit 完成；每次输出都是单行 JSON 且在预算内；verify 的 invalid / 可疑标记在真实使用中起了作用，并帮助发现了 AgentKit 自己的 1 个误报 bug。
