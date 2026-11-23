# AgentKit

> 给 AI Agent 用的轻量编程工具：省 token 的工具集 + 可派活的内置工人 + 独立验收。
> A lightweight coding toolkit built for AI agents, not humans.

**状态：M2 Python 原型——直接模式工具集 + 派活（内置工人、OpenAI 兼容接口、独立验收与防作弊）可用。**

## 它解决什么
- 现有编程工具为人设计，AI 用起来读得多、改容易失败、不知道改得对不对。
- 主管级大模型（贵）亲自读日志、跑测试，成本高；便宜模型又常「说做完了其实没做完」。

## 三种用法
| 模式 | 谁写代码 | 场景 |
|---|---|---|
| 直接模式 | 主管 AI 自己，用本工具的省 token 工具集 | 没配任何模型 |
| 派活模式 | 内置工人，接任意 OpenAI 兼容接口 | 主管只派活 + 看 ≤300 token 的验收报告 |
| 混合模式 | 难的主管写，简单的派出去 | 日常 |

工人只能「提交」，不能「宣布完成」——是否完成由验收器在干净环境里独立判定，并自动标记删测试、改验收脚本等可疑行为。

## 与现有项目的区别
派活 + 验收已有优秀项目（如 Grok Workhorse、worker-bridge、LooppooL），省 token 工具也有（如 XERJ、Aider、SWE-agent）。AgentKit 想做的是**组合**：
1. 自带一套省 token 的工具集，工人和主管共用；
2. 没有模型时主管也能直接用；
3. 单二进制、低内存（目标：空闲 < 50 MB），云端 Agent 一条命令装好；
4. 公开、可复现的评测。

## 安装与使用

```bash
pip install git+https://github.com/tangniutt/agentkit    # Python 3.10+，无需 root
# pip 不可用 / 提示 externally-managed-environment：pipx install ... 或 uv tool install ...（见 llms.txt）
agentkit doctor                                          # 自检：版本、git、写权限、磁盘、tree-sitter、模型配置
```

AI 读 [`llms.txt`](llms.txt) 即可上手。所有命令默认输出一行紧凑 JSON：

```json
{"ok":true,"data":{...},"error":null,"truncated":false,"next":null}
```

- 输出预算：默认 2000 token（≈4 字符/token），`--budget N` 可调；任何输入都不超限，截断时 `truncated:true` 且 `next` 给出续读命令。
- `--human` 输出人类可读文本。

| 命令 | 作用 |
|---|---|
| `agentkit repo_map` | 目录 + 主要符号（tree-sitter，支持 Python/JS/TS/Go/Rust/Java 等；无 tree-sitter 时退化为正则） |
| `agentkit search "re" [path] [-i] [--fixed] [-g GLOB]` | 内容搜索，遵守 .gitignore，结果 `path:line:text`（有 ripgrep 用 ripgrep） |
| `agentkit ls [dir] [-r]` / `agentkit glob '**/*.py'` | 列目录 / 按模式找文件 |
| `agentkit read f.py:10-40` / `read f.py#Class.method` | 按行段 / 按符号读；拒读二进制；大文件分段 |
| `agentkit edit f.py --old "..." --new "..."` | 唯一匹配替换；0 次 / 多次匹配返回最接近 3 处；语法错误拒写并给行号；读后文件被改则拒写 |
| `agentkit patch` (stdin JSON) | 多文件原子补丁（replace/create/delete/rename），任一失败全部回滚 |
| `agentkit create / delete / rename` | 新建 / 删除 / 重命名，同样自动快照 |
| `agentkit run "cmd" [--timeout 120]` | 超时杀进程组；返回 exit_code、耗时、stderr 尾部优先；完整日志落盘 |
| `agentkit verify --check "pytest -q"` | 在干净 worktree（非 git 则临时副本）跑验收；≤300 token 报告：pass/fail/invalid、改动文件、可疑标记 |
| `agentkit checkpoint list / diff ID / rollback ID` | 每次改动前自动快照，回滚后哈希校验；不依赖 git |
| `agentkit memory brief / add / list / search / show / forget` | 项目记忆（Markdown + front matter，SQLite FTS5 检索，代码锚定失效标记） |
| `agentkit doctor` | 自检，每项给修复建议 |
| `agentkit model add/list/test/remove` | 配置工人模型（任意 OpenAI 兼容接口）；配置里只存 Key 的**环境变量名** |
| `agentkit task create "目标" --check "pytest -q" [--allow DIR] [--budget-tokens/--budget-usd/--budget-minutes] [--tier]` | 派活：工人在独立 worktree/分支 `agentkit/<id>` 干活；无模型时返回「直接模式」提示 |
| `agentkit task status / wait / report / cancel / clean` | status <100 token；report ≤300 token（与 verify 同格式，verdict 多一个 `needs_review` = 测试过了但有作弊嫌疑）；`wait` 单个任务结束时直接带回报告（省一次调用） |
| `agentkit task resume ID [--budget-*]` | 续跑 paused（429/超时）/ interrupted / stopped 任务：沿用原 worktree 和分支，工人拿到压缩后的历史（做过的动作 + 当前 diff），token / 金额预算按整个任务累计 |
| `agentkit uninstall [--yes] [--this-repo] [--delete-branches]` | 卸载：不带 `--yes` 只列出将删除的内容；删除包、`~/.config/agentkit`、`~/.local/share/agentkit`、各仓库 `.agentkit/` 与 worktree；**不动你的代码和 git 历史**（任务分支默认保留，git 已跟踪的共享记忆保留） |
| `agentkit logs <id> [--errors]` / `agentkit debug-bundle <id>` | 定位失败：结构化事件（哪一步、哪个组件、错误码）；脱敏归档用于报 Bug（错误码见 [docs/errors.md](docs/errors.md)） |
| `agentkit feedback --bug <id> \| --idea "..."` | 生成脱敏的 Issue 草稿和预填链接，**从不自动提交** |

安全默认：`run`/`verify` 拒绝 `git push`、`git commit`、`sudo`、`rm -rf /`/`~`、`curl | sh`、读取 `~/.ssh` 和 `.env`；可在 `.agentkit/config.toml` 的 `[run] allow/deny` 调整。所有输出与日志自动脱敏（`sk-...`、`ghp_...`、`AKIA...` 等）。

运行状态写在仓库内 `.agentkit/`（快照、日志、记忆、任务、worktree；其中 `.gitignore` 只放行 `config.toml` 与记忆文件），除了调用你配置的模型接口外不上传任何数据。

### 派活模式要点
- 工人只能用上面的工具（外加 `finish` 提交），不能 commit / push；工人说「完成」不算数：验收器把它的改动复制到全新 worktree 独立重跑检查，并标记删测试、跳过测试、改验收脚本、改白名单外文件、偷偷提交等行为。
- 预算超限 5 秒内停止；429 按 Retry-After 等一次后暂停上报（不轮换账号）；Key 无效立即失败。
- OpenRouter：`--base-url https://openrouter.ai/api/v1`，免费模型 id 以 `:free` 结尾（`model add` 对无单价的非 `:free` 模型给出警告）；自动带 `HTTP-Referer` / `X-Title`；`X-RateLimit-Reset` 与 HTTP 200 里的 provider 错误都按对应状态码处理。
- 日志：`.agentkit/logs/` 默认保留 7 天，`[logs] retain_days` 可调（0 = 永久）。
- 评测：`python -m eval.run [--cheats] [--size tiny|large]`（离线假模型，21 道小题 + 3 道多文件题 + 14 例作弊库）；`python -m eval.run --real --base-url URL --model-id ID --key-env VAR` 接真实模型；`python -m eval.llms_check [--scenario bare]` 模拟「只读 llms.txt 的陌生 AI」在全新环境安装并跑 doctor + verify（M-19）。

### 报告问题
AI 代用户报 Bug / 提需求：先搜已有 Issue，**先问人类用户**（Issue 公开），用 `agentkit feedback` 生成草稿，不含密钥或私有代码，一个问题一个 Issue。

开发：`pip install -e .[dev] && pytest -q`。

## 文档
- [PRD v0.2](docs/prd-v0.2.md)
- [V1.0 需求基线（冻结）](docs/v1.0-baseline.md)
- [功能清单与借鉴来源](docs/feature-sources.md)
- [进度](docs/STATUS.md) · [错误码](docs/errors.md) · dogfood 记录：[M1](docs/dogfood-m1.md) · [M2](docs/dogfood-m2.md)

## 明确不做
不做人用 IDE / 聊天界面；不托管或转售模型；**不提供多账号轮换、代理池等绕过平台限额的功能**；不自动 push / 部署；无遥测。

## 参与
见 [CONTRIBUTING.md](CONTRIBUTING.md)。V1.0 范围已冻结，新功能建议请开 Issue 登记，V1.0 发布后统一评估。

## 许可证
Apache-2.0
