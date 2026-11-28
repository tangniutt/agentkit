# Muse × AgentKit v0.2.3 测试任务书

> 版本：v0.2.3
> 发布页：https://github.com/tangniutt/agentkit/releases/tag/v0.2.3
> 执行者：Muse
> 目标：验证直接模式、MCP stdio、派活模式三条真实使用路径，并产出可复现的脱敏证据。
> 重要：本任务书只生成测试结论和 Issue 草稿；**未经人类用户确认，不得提交公开 Issue、推送代码、发布 Release 或部署服务。**

## 1. 交给 Muse 的开场提示词

```text
请在 Linux 测试环境中安装 AgentKit v0.2.3，严格按照 docs/muse-v0.2.3-test-plan.md 执行直接模式、MCP、派活模式测试。使用临时仓库和测试专用凭证；不得修改验收命令来制造通过，不得轮换账号或绕过限额。失败时保存精确命令、工作目录、退出码、error.code、error.run、task/goal ID、日志 ID 和最小复现步骤。所有输出先脱敏；最终把结果和 Issue 草稿交给我确认，不要自行提交 Issue。
```

## 2. 目标与完成标准

必须完成：

- [ ] 安装并验证 wheel/sdist 之一，记录版本、Python、平台、Git commit。
- [ ] 直接模式：读、搜、编辑、运行、独立验收、检查点、记忆、安全拒绝。
- [ ] MCP：初始化、工具清单、资源清单、至少两个只读工具调用。
- [ ] 派活模式：至少一个 builtin 或官方外部工人任务，独立验收通过并取得 EvidenceReceipt。
- [ ] Goal：至少一个有界 create/run/report 闭环。
- [ ] 失败分类与证据：每个失败项可复现、可脱敏、可定位。
- [ ] 输出测试报告和待确认 Issue 草稿；不自动提交。

可选（有测试账号或第二个模型时）：

- [ ] 外部工人：Codex / OpenCode / Qoder / Cursor 至少一个。
- [ ] paused/interrupted 后的 `task resume`。
- [ ] 两个模型之间的 `task handoff`。
- [ ] 测试专用凭证的 `credential show → revoke → cancel` 演练。

## 2.1 v0.2.1 回归重点（保留）

先完成 v0.2.3 的 2.3 节，再依次回归 2.1、2.2 和完整三阶段流程。

| 反馈 | 复测动作 | 预期 |
|---|---|---|
| #1 沙箱启动 | 运行 `agentkit doctor`；在支持 bubblewrap 的 Linux VM 创建派活任务；若环境不支持，检查失败与 cancel | sandbox 探针与真实启动一致；不支持时快速 `AK-SANDBOX-UNAVAILABLE`，不静默无沙箱运行 |
| #2 CLI 崩溃 | 对已完成任务运行 `task diff`、`task accept`；另测 steer/answer/reject | 不再出现 `Namespace.ids` / `AK-INTERNAL` |
| #3 验收语义 | 弱化测试断言但让错误实现通过，运行直接 `verify` | 顶层 `verdict=needs_review`，`code=AK-VERIFY-SUSPICIOUS` |
| #4 MCP 参数 | 传错误参数名/类型；使用 `path` 别名读相对文件；单任务工具传单元素 `ids` | 参数错误返回 `-32602 Invalid arguments` 和 issues；别名可用；相对路径按 `--root` 解析 |
| #5 任务书/噪声 | 按 D-01/D-04/D-05/D-06 原命令执行；检查 changed | search flag 顺序可执行；forget 用 id；`.env` 为 `AK-PATH`；日志字段正确；无 `__pycache__` 噪声 |
| #6.1 去重 | 相同目标/check 创建两次，再加 `--fresh` | 第二次显式 `deduped/reused/original_status/verdict`；fresh 生成新 task |
| #6.2 心跳 | 运行中重复 `task status` | 返回 `idle_s`，可区分慢任务与卡死 |
| #6.3 全局模型 | `model add --global`，换仓库 `model list`，再加项目同名覆盖 | 全局模型可见，项目覆盖生效，配置不含 Key 值 |
| #6.4 直接回执 | 修复后运行 `verify --receipt` | baseline fail、acceptance pass，生成带哈希的 EvidenceReceipt |
| #6.6 轮次明细 | 派活后运行 `task report --details` | 包含每轮 token、模型耗时和动作；默认紧凑 report 不展开 |

每个复测项都记录 command、exit/code、run/task/log ID 和最小复现；未触发环境写 `skipped + 原因`，不要写 pass。

## 2.2 v0.2.2 回归重点（保留）

| 反馈 | 复测动作 | 预期 |
|---|---|---|
| #7 paused cancel | 让任务进入 paused，再 `task cancel` | 状态立即变 cancelled，不再可 resume |
| #8.1 上下文 | 不传 context 添加模型；再显式传 `--context-limit/--context-budget`；查看 model list/doctor | 未知上限不再写 32k 假值；显示 limit=unknown、budget=128000 与默认提示；预算控制压缩 |
| #8.2 paused 老化/批量 | 查看 paused status；运行 `task resume --all-paused` | status 有 `paused_s`；仅显式批量恢复，默认不自动重试 |
| #8.3 doctor | 同仓库分别在有/无可用沙箱时运行 doctor | 返回 `direct_ready`、`dispatch_ready`、`healthy_for`，不再用一个 healthy 混淆两种模式 |
| #8.4 policy 档位 | 在无 bubblewrap 的嵌套容器设 `[sandbox] mode="policy"` 后派活 | 不需 test-only `allow_unsandboxed`；task/report/receipt 标 policy；文档明确无进程 jail，且只支持共享网络 |
| #8.5 search | `search "pattern" --fixed file.py` | 正常返回结果，不再 AK-USAGE |
| CLI 缺失提示 | 未安装 Qoder/Codex 时分别选择后端 | 只提示缺少被选中的 CLI；不自动下载、不安装、不联网 |

本节保留 v0.2.2 回归；完成 2.3 后复测本节，再继续完整三阶段流程。

## 2.3 v0.2.3 图片能力定向复测

先完成本节，再回归 v0.2.1/v0.2.2 检查表和完整三阶段流程。

| 用例 | 操作 | 预期 |
|---|---|---|
| 视觉模型附件 | `model add ... --vision` 后 `task create ... --attach ref.png` | 请求含 `image_url`，task/round_details 记录附件与 image token 估算；transcript/log 不含 base64 |
| Goal 附件 | `goal create ... --attach ref.png` 后运行有界 Goal | TaskSpec 保留附件与能力门禁；Goal 子任务使用同一视觉模型，证据收据包含附件元数据 |
| 接力能力 | 对带附件任务执行 `task resume`/`task handoff` 到纯文本模型；再换视觉模型 | 纯文本目标返回 `AK-MODEL-CAPABILITY`；合法接力保留原 worktree、预算与接力历史 |
| 暂存图篡改 | 创建任务后修改 `.agentkit/tasks/<id>/attachments/` 中的暂存图，再触发请求 | 哈希/大小不一致时返回 `AK-CONFIG`，不发送被篡改图片 |
| 非视觉模型 | 对未开 vision 的 builtin 模型传 `--attach` | `AK-MODEL-CAPABILITY`，不创建任务、不静默丢图 |
| 外部后端 | 对 Codex/OpenCode/Qoder/Cursor 传 `--attach` | 明确 `AK-MODEL-CAPABILITY`，不伪装支持 |
| 证据图 | 工人在 `artifacts/` 产出 png/jpeg/webp | report/receipt 含路径与 SHA-256；MCP `include_images=true` 返回 image block |
| 护栏 | 传坏格式、超过 5 MiB 或总量超过 10 MiB | 明确拒绝；附件发送前有敏感信息警告 |

## 3. 安全与停止条件

- 只使用临时仓库和专用测试 Key；Key 只放环境变量，不写命令行、配置值、日志、截图或 Issue。
- 不提供多账号轮换、代理池、限额绕过、封禁规避或官方 CLI 反代。
- 不执行 `git push`、`git commit`、部署、自动升级或公开 Issue 提交。
- 不删除或弱化测试，不修改冻结的 `--check` 命令来让任务通过。
- 单个任务默认预算：`--budget-tokens 20000 --budget-minutes 10`；付费模型必须先取得人类用户确认。
- 遇到 401/403、429、5xx、超时或可疑权限行为立即停止该用例，分类记录，不反复重试。
- 真实保存登录态默认关闭；只有人类用户明确授权后才在测试环境开启。
- Windows 只测直接模式；派活生产路径使用 Linux + bubblewrap。

## 4. 环境准备

### 4.1 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  https://github.com/tangniutt/agentkit/releases/download/v0.2.3/agentkit-0.2.3-py3-none-any.whl

agentkit doctor
agentkit manual
```

记录：

```text
AgentKit version:
Python:
OS / architecture:
git version:
bubblewrap:
ripgrep:
install artifact + SHA-256:
doctor healthy:
```

Linux 派活建议安装：

```bash
sudo apt-get install -y git ripgrep bubblewrap
```

### 4.2 创建可复现测试夹具

```bash
TEST_ROOT=$(mktemp -d /tmp/agentkit-muse-v023.XXXXXX)
cd "$TEST_ROOT"
git init -q -b main

cat > calc.py <<'PY'
def add(a, b):
    return a - b
PY

cat > test_calc.py <<'PY'
import unittest
from calc import add


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
PY

python -m unittest -v
# 预期：基线失败，add(2, 3) 得到 -1。

git add calc.py test_calc.py
git commit -qm "baseline: add is broken"
git rev-parse HEAD
```

后续所有直接模式命令都在 `TEST_ROOT` 中执行。记录 `TEST_ROOT` 和基线 commit，方便复现。

## 5. 直接模式测试

### D-01 自检与只读工具

```bash
agentkit doctor
agentkit read calc.py
agentkit search --fixed "return a - b" calc.py
agentkit ls
agentkit glob "**/*.py"
agentkit repo_map
```

预期：

- 每条命令输出一行合法 JSON，固定键为 `ok/data/error/truncated/next`。
- `read` 返回 `calc.py` 内容与 SHA。
- `search` 精确定位错误实现。
- `repo_map` 在预算内返回目录和符号。

证据：保存每条原始 JSON 的 `run` ID；不要只保存屏幕摘要。

### D-02 编辑、运行与独立验收

```bash
agentkit edit calc.py --old "return a - b" --new "return a + b"
agentkit run "python -m unittest -v"
agentkit verify --check "python -m unittest -v"
agentkit verify --check "python -m unittest -v" --receipt
```

预期：

- `edit` 成功且自动创建检查点。
- `run` 退出码为 0。
- `verify` 在干净 worktree/临时副本中返回 `verdict=pass`，checks 全部通过，并给出 changed/suspicious/cost/env。
- 普通直接模式 verify 保持轻量；加 `--receipt` 时会在基线提交上重跑检查，返回 `baseline` 并生成 EvidenceReceipt。
- `--receipt` 模式下 baseline 必须 fail、acceptance 必须 pass；baseline 已通过时 verdict 为 invalid。
- `changed` 不包含 `__pycache__/*.pyc` 等生成噪声。

证据：记录 edit/run/verify 的 `run` ID、verify report 路径；使用 `--receipt` 时再记录 receipt ID、路径与哈希。

### D-03 检查点回滚

```bash
agentkit checkpoint list
# 选择编辑前创建的检查点 ID。
agentkit checkpoint rollback CHECKPOINT_ID
agentkit verify --check "python -m unittest -v"
```

预期：

- 回滚后 `calc.py` 恢复为 `return a - b`。
- 验收重新变为 fail，证明回滚真实生效。
- 再次执行 D-02 的 edit，恢复正确实现，供后续测试使用。

### D-04 输出预算、过滤与原始日志

```bash
agentkit --budget 60 read calc.py
agentkit run "python -c 'print(\"line\"); [print(f\"row-{i}\") for i in range(200)]'"
```

预期：

- 小预算读取返回 `truncated=true` 和可执行的 `next`。
- 长输出可能被过滤；若有 `data.filtered.raw`，其中包含 `agentkit logs LOG_ID --raw` 命令。
- 无论是否过滤，都可从 `data.logs.stdout/stderr` 路径取得日志 ID，再用 `agentkit logs LOG_ID --raw` 找回脱敏后的原文。

证据：记录预算值、截断状态、filter 名、raw 日志 ID。

### D-05 记忆

```bash
agentkit memory add muse-test "add must return the sum" --id muse-v023
agentkit memory search "add sum"
agentkit memory brief
agentkit memory forget muse-v023
```

预期：搜索命中、brief 在预算内、forget 后不再命中。记忆不得包含 Key 或私有会话内容。

### D-06 安全负例

```bash
printf 'FAKE_TOKEN=not-a-real-secret\n' > .env
agentkit read .env
agentkit run "git push"
```

预期：

- 读取 `.env` 返回 `AK-PATH`（v0.2.3 的受保护文件拒绝行为）。
- `git push` 返回 `AK-RUN-DENIED`。
- 两个失败都不得泄露文件内容或凭证。

删除测试假文件：`rm .env`。

### D-07 可疑修改检测（可选但推荐）

复制测试仓库，修改 `test_calc.py` 弱化断言但让错误实现通过，然后运行：

```bash
agentkit verify --check "python -m unittest -v"
```

预期：`verdict=needs_review`，并出现 `tests_modified` 或相关可疑标记，不能误报为 pass。

## 6. MCP stdio 测试

### 6.1 Muse MCP 配置

在 Muse 的 MCP 配置中加入以下服务器；按 Muse 实际配置格式调整，命令和参数保持不变：

```json
{
  "mcpServers": {
    "agentkit": {
      "command": "agentkit",
      "args": ["--root", "/absolute/path/to/TEST_ROOT", "mcp"],
      "env": {}
    }
  }
}
```

把 `/absolute/path/to/TEST_ROOT` 替换为真实绝对路径。不要用 `python -m agentkit` 作为不可信仓库中的启动方式。

### 6.2 协议 smoke

依次执行：

1. `initialize`，`protocolVersion=2025-11-25`。
2. `tools/list`。
3. `resources/list`。
4. `tools/call agentkit_read`，读取 `calc.py`；`spec` 与 `path` 均可使用，相对路径按 `--root` 解析。
5. `tools/call agentkit_search`，搜索 `return a + b`。
6. `tools/call agentkit_doctor`。
7. 参数错误应返回 `-32602 Invalid arguments` 并列出缺失/未知字段；单任务工具接受 `id` 或单元素 `ids`，多任务工具接受 `ids` 或单个 `id`。
8. `agentkit_task_create` 支持 `fresh=true`；`agentkit_task_status` 活跃任务返回 `idle_s` 心跳；`agentkit_verify` 支持 `receipt=true`。

预期：

- 初始化成功，返回 server capabilities。
- 工具清单共 35 个，至少包含：
  - `agentkit_read`、`agentkit_search`、`agentkit_run`、`agentkit_verify`
  - `agentkit_task_create`、`agentkit_task_wait`、`agentkit_task_report`
  - `agentkit_goal_create`、`agentkit_goal_run`、`agentkit_goal_report`
  - `agentkit_credential_show`、`agentkit_credential_revoke`、`agentkit_route`
- 资源清单包含动态 task/Goal 资源能力。
- 两个工具调用结果与 CLI 直接调用一致。
- 2026-07-28 协议若被请求，应明确拒绝，不得伪装支持。

证据：保存初始化请求/响应、工具数量、调用请求/响应和 MCP stderr。不得保存凭证值。

### 6.3 MCP 写操作（派活阶段执行）

在派活任务创建后，用 MCP 再调用：

- `agentkit_task_status`
- `agentkit_task_wait`
- `agentkit_task_report`

确认 MCP 与 CLI 看到同一 task ID、状态、验收结论和 receipt。

## 7. 派活模式测试

### 7.1 前置条件

- Linux + git + bubblewrap 可用；`agentkit doctor` 的 sandbox 检查必须为 ok，dispatch 模式才允许继续。
- 若 sandbox 不可用，记录 `AK-SANDBOX-UNAVAILABLE` 或 doctor 的修复建议，不要用 `mode=off` 冒充生产派活。
- 测试仓库至少有一个 baseline commit。
- 基线验收失败：`python -m unittest -v`。
- builtin 模型或一个官方外部 CLI 已由人类用户授权。

### 7.2 配置 builtin 测试模型

Key 值只通过本机环境变量提供：

```bash
export BASE_URL="USER_PROVIDED_BASE_URL"
export MODEL_ID="USER_PROVIDED_MODEL_ID"
export KEY_ENV="USER_PROVIDED_KEY_ENV"
# 在人类用户提供的终端会话中设置 KEY_ENV 的值；不要写入本文件或日志。

agentkit model add muse-test \
  --base-url "$BASE_URL" \
  --model "$MODEL_ID" \
  --key-env "$KEY_ENV" \
  --tier low
# 可选：加 --global 写入用户级配置；项目级同名模型会覆盖它。

agentkit model test muse-test
```

预期：模型配置只保存变量名；`model test` 成功。若返回 401/403/429，停止并记录，不换账号或绕过限额。

### 7.3 创建、等待和验收任务

```bash
agentkit task create \
  "Fix add() in calc.py so add(2, 3) returns 5. Do not modify tests." \
  --check "python -m unittest -v" \
  --model muse-test \
  --budget-tokens 20000 \
  --budget-minutes 10
# 可选：加 --fresh 强制新跑，不复用相同内容的历史任务。
```

记录返回的 task ID；`deduped=true` 表示复用旧任务，必须同时检查 `reused`、`original_status` 和 `verdict`。然后：

```bash
agentkit task status TASK_ID
agentkit task wait TASK_ID --timeout 600
agentkit task report TASK_ID
agentkit task diff TASK_ID
agentkit credential show TASK_ID
```

预期：

- 任务在独立 worktree/分支运行。
- 工人不能 commit/push。
- 最终 `verdict=pass`，`baseline.result=fail`，`acceptance.result=pass`。
- report 包含成本、轮数、sandbox、receipt 和 changed files；`task report --details` 与完整 `report.json` 还包含 `round_details`（每轮 token、模型耗时、动作），默认紧凑报告不展开它。
- 活跃任务 status 包含 `idle_s`，可用于区分慢任务与卡死任务。
- `credential show` 只显示 Grant/Binding 元数据和环境变量名，不显示 Key。

证据：task ID、worker log、report、receipt、diff、credential audit、sandbox provider/network 模式。

### 7.4 Goal 有界闭环

使用新的 baseline 仓库或恢复 D-03 前的错误实现，然后：

```bash
agentkit goal create \
  "Fix add() and pass the frozen unittest check without modifying tests." \
  --check "python -m unittest -v" \
  --model muse-test \
  --max-iterations 2 \
  --budget-tokens 20000 \
  --budget-minutes 10

agentkit goal run GOAL_ID --timeout 180
agentkit goal status GOAL_ID
agentkit goal report GOAL_ID
```

预期：

- Goal 只调用一个底层 task。
- 只有独立验收 pass 才进入 `completed`。
- 失败会在有界 iteration 内反馈、重试或进入 `needs_review`。
- history 记录每轮 task 状态、verdict、成本和时间。

### 7.5 续跑与接力（条件满足时）

- 若任务自然进入 `paused` 或 `interrupted`：

```bash
agentkit task report TASK_ID
agentkit task resume TASK_ID
agentkit task wait TASK_ID --timeout 600
```

- 若有人类用户授权的第二个模型/后端：

```bash
agentkit task handoff TASK_ID --model SECOND_MODEL
agentkit task wait TASK_ID --timeout 600
```

预期：沿用同一 worktree、diff、记忆和累计预算；不得重放供应商私有 thought signature。没有触发条件时标记“未触发”，不要人为制造高成本故障。

### 7.6 测试凭证撤销/取消（可选，仅专用测试 Key）

```bash
agentkit credential show TASK_ID
agentkit task cancel TASK_ID
agentkit credential revoke BINDING_ID
agentkit credential show TASK_ID
agentkit logs TASK_ID --errors
```

预期：任务先停止，Binding 随后 inactive/revoked，审计记录 cancelled/revoked，worker 日志不含 Key 值。撤销无法收回已进入 CLI 内存的在途请求，因此需要立即停止时先 cancel，再 revoke。

### 7.7 外部工人矩阵（可选）

只有已安装官方 CLI 且人类用户提供授权时执行。每个后端至少一个最小任务，使用相同 `--check`：

| 后端 | 要求 | 记录 |
|---|---|---|
| Codex | 官方 CLI + 专用 API Key | session、usage、sandbox、receipt |
| OpenCode | V1/V2 官方 CLI；V2 免费模型 429 时停止 | session、token/cost、暂停错误码 |
| Qoder | 官方 CLI；保存登录需用户级 opt-in | session、Credits、credential metadata |
| Cursor | 官方 CLI 或 SDK；CLI 模式无 token 成本字段 | session、transport、预算行为 |

不接 consumer Antigravity 登录态，不反代，不共享账号，不轮换凭证。

## 8. 失败处理与证据模板

### 8.1 失败时立即收集

```bash
agentkit logs RUN_OR_TASK_ID --errors
agentkit logs RUN_OR_TASK_ID --raw
agentkit debug-bundle RUN_OR_TASK_ID
```

每个失败项必须记录：

```text
用例 ID：
版本 / git commit：
OS / Python / bubblewrap：
模式：direct / MCP / dispatch / Goal
后端 / 模型 / transport：
精确命令：
工作目录：
退出码：
stdout / stderr（脱敏）：
error.code：
error.run：
task ID / goal ID：
worker log ID：
check log ID：
receipt / diff / sandbox 信息：
最小复现步骤：
预期结果：
实际结果：
是否可稳定复现：
是否疑似安全问题：
```

### 8.2 错误分类

| 类型 | 常见错误码 | 处理 |
|---|---|---|
| 基础设施 | 429、5xx、超时、`AK-MODEL-429` | 停止或等待官方 Retry-After；不绕过 |
| 配置/认证 | `AK-CONFIG`、`AK-MODEL-KEYENV`、401/403 | 修配置；不记录 Key |
| 能力/验收 | `AK-VERIFY-FAIL`、`AK-WORKER-STUCK`、`AK-WORKER-MALFORMED` | 保留 diff 与验收日志 |
| 状态/恢复 | `AK-TASK-STATE`、`AK-TASK-INTERRUPTED`、`AK-BUDGET` | 记录状态迁移与 resume 结果 |
| 沙箱启动 | `AK-SANDBOX-UNAVAILABLE` | 停止派活，保存 doctor 与 worker.log；确认任务快速 failed 且 cancel 可收尾 |
| 安全/权限 | `AK-PATH`、`AK-RUN-DENIED`、可疑标记 | 停止相关用例，优先报告 |

### 8.3 Issue 草稿

可使用：

```bash
agentkit feedback --bug RUN_ID
```

或手工按以下结构生成草稿：

```text
Title: [v0.2.3][MODE][BACKEND] concise failure

Summary:
- 一句话说明用户影响

Environment:
- AgentKit v0.2.3, commit, OS, Python, backend/model

Steps to reproduce:
1. ...
2. ...
3. ...

Expected:
...

Actual:
...

Evidence:
- error.code / error.run
- task/goal/log IDs
- receipt/diff hashes
- debug-bundle path (脱敏)

Security / secrets:
- 已脱敏；不包含 Key、Cookie、Token、私有代码
```

**只能生成草稿。先把完整草稿交给人类用户确认，再决定是否公开提交。**

## 9. 最终报告模板

```text
# Muse × AgentKit v0.2.3 测试报告

## 环境
- 版本 / commit：
- OS / Python / bubblewrap：
- 后端 / 模型：

## 结果汇总
- D-01 ~ D-07：pass / fail / skipped + 原因
- MCP：pass / fail + 工具数
- Dispatch：pass / fail + task ID / verdict / receipt
- Goal：pass / fail + Goal ID / iterations
- 可选外部工人 / resume / handoff / revoke：

## 失败清单
1. 用例 ID + error.code + run/task/log ID + 最小复现

## 安全结论
- 是否发现 Key 泄露：
- 是否发现越权/沙箱绕过：
- 是否发现验收误报/漏报：

## 待确认 Issue 草稿
- 草稿标题：
- 证据文件：
- 建议优先级：

## 结论
- 可作为生产候选继续试用 / 需要修复后再测 / 存在阻断问题
- 明确列出未执行项，不把 skipped 当 pass
```

## 10. 当前已知边界

- 这是生产候选版，不代表真实账号基准已经通过。
- 官方 CLI 自身必须持有 provider 凭证；AgentKit 不宣称提供反代 broker。
- Windows 只支持直接模式；派活生产路径是 Linux VPS。
- 未执行的用例必须写 `skipped + 原因`，不得填写为通过。
