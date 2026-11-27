# AgentKit

> 给 AI Agent 用的轻量编程工具：省 token 的工具集 + 可派活的内置工人 + 独立验收。
> A lightweight coding toolkit built for AI agents, not humans.
>
> **核心设计理念：AgentKit 是一个让 AI 自己提建议、持续改进、持续吸收先进工具设计理念的 AI 编程工具。** AI 用户带证据提 bug 与设计建议（`agentkit feedback --improve`）→ [评分](docs/triage.md) → 接受的成为基线变更 → 合并前用评测量化。

**状态：生产化进行中——M2 Python 原型 + P0 可信基座 + Goal/MCP 2025 + 任务控制面 + Builtin/XERJ/LSP 代码智能已落地；Linux VPS 最终全量门禁已通过，Cursor/Qoder 真实账号任务、强凭证隔离、其余 LSP 服务器与正式发布待完成。Windows 仅保留开发机 direct-mode 兼容，不作为生产部署平台或验收门槛。**

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

AI 读 [`llms.txt`](llms.txt) 即可上手；管理 AgentKit 的主管 AI 用 `agentkit manual`（总览）/ `agentkit manual <命令>`（详情），Markdown 版见 [`docs/agent-manual.md`](docs/agent-manual.md)。所有命令默认输出一行紧凑 JSON：

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
| `agentkit run "cmd" [--timeout 120] [--raw]` | 超时杀进程组；返回 exit_code、耗时、stderr 尾部优先；完整日志落盘。C-08：按命令过滤冗长输出（pytest / jest / go / cargo / tsc / eslint / ruff / mypy / pip / npm / git status·diff·log / 通用），去 ANSI 与进度条、重复行计数折叠，错误 / 失败行永不删；`filtered.raw` 给出 `agentkit logs RUN --raw` 找回原文；`--raw` 或 `[compress] run_filters = false` 关闭 |
| `agentkit verify --check "pytest -q"` | 在干净 worktree（非 git 则临时副本）跑验收；≤300 token 报告：pass/fail/invalid、改动文件、可疑标记 |
| `agentkit checkpoint list / diff ID / rollback ID` | 每次改动前自动快照，回滚后哈希校验；不依赖 git |
| `agentkit memory brief / add / list / search / show / forget` | 项目记忆（Markdown + front matter，SQLite FTS5 检索，代码锚定失效标记） |
| `agentkit doctor` | 自检，每项给修复建议 |
| `agentkit model add/list/test/remove` | 配置工人模型（任意 OpenAI 兼容接口）；配置里只存 Key 的**环境变量名** |
| `agentkit task create "目标" --check "pytest -q" [--backend builtin\|codex\|opencode\|qoder\|cursor\|cursor] [--model NAME] [--allow DIR] [--budget-tokens/--budget-usd/--budget-minutes] [--tier]` | 派活：builtin API Key 模型、Codex、OpenCode、Qoder 或 Cursor CLI 在独立 worktree/分支 `agentkit/<id>` 干活；默认要求验收在基线失败、修复后通过；无模型时返回「直接模式」提示 |
| `agentkit task status / wait / report / diff / steer / answer / accept / reject / cancel / clean` | status <100 token；report ≤300 token；支持预算内 diff、中途纠偏、工人提问回答和接受/拒绝记录 |
| `agentkit task resume ID [--model NAME] [--backend builtin\|codex\|opencode\|qoder\|cursor] [--budget-*]` | 续跑 paused（429/超时）/ interrupted / stopped 任务：沿用原 worktree 和分支；可换模型或后端，token / 金额预算按整个任务累计 |
| `agentkit task handoff ID [--model NAME] [--backend builtin\|codex\|opencode\|qoder\|cursor]` | 显式换工人接力，保留 worktree、diff、记忆和累计预算，记录接力历史 |
| `agentkit goal create/run/status/wait/report/steer/answer/cancel/resume` | 有界结果闭环：执行、独立验收、失败反馈、提问回答、续跑或换模型，只有验收通过才完成 |
| `agentkit uninstall [--yes] [--this-repo] [--delete-branches]` | 卸载：不带 `--yes` 只列出将删除的内容；删除包、`~/.config/agentkit`、`~/.local/share/agentkit`、各仓库 `.agentkit/` 与 worktree；**不动你的代码和 git 历史**（任务分支默认保留，git 已跟踪的共享记忆保留） |
| `agentkit logs <id> [--errors]` / `agentkit debug-bundle <id>` | 定位失败：结构化事件（哪一步、哪个组件、错误码）；脱敏归档用于报 Bug（错误码见 [docs/errors.md](docs/errors.md)） |
| `agentkit feedback --bug <id> \| --idea "..."` | 生成脱敏的 Issue 草稿和预填链接，**从不自动提交** |
| `agentkit manual [命令] [--all] [--format md]` | 主管 AI 操作手册（C-09）：总览约 1.9k token，全量约 4.8k token；由 CLI 定义自动生成，见 [docs/agent-manual.md](docs/agent-manual.md) |
| `agentkit mcp` | 启动 MCP stdio 服务端，向 Muse 等 Agent 暴露核心工具、动态任务/Goal 资源、资源分页；支持 2025-06-18/2025-11-25 的资源订阅与状态变更通知，2026-07-28 明确不支持 |
| `agentkit code search/defs/refs/index` | 可替换代码智能：内置词法/符号检索、LSP 语义引用、可选 XERJ 索引/代码检索/定义/记忆；XERJ 不可用时自动回退 |

安全默认：`run`/`verify` 拒绝 `git push`、`git commit`、`sudo`、`rm -rf /`/`~`、`curl | sh`、读取 `~/.ssh` 和 `.env`；可在 `.agentkit/config.toml` 的 `[run] allow/deny` 调整。Linux bubblewrap 支持 `[sandbox] network=false` 硬隔离，也支持 `network = ["api.openai.com"]` 域名白名单：沙箱内只能访问回环中继，宿主代理按域名与 TLS SNI 放行，拒绝重复 Host、管道后续请求和非全局解析地址，任务报告会记录 shared/isolated/allowlist/host。所有输出与日志自动脱敏（`sk-...`、`ghp_...`、`AKIA...` 等）。

运行状态写在仓库内 `.agentkit/`（快照、日志、记忆、任务、worktree；其中 `.gitignore` 只放行 `config.toml` 与记忆文件），除了调用你配置的模型接口外不上传任何数据。

### 多工人后端

- 接入边界：只接可脚本化的官方 CLI/API/RPC/MCP 编程工具；不接 GUI 自动化、电脑操作工具，也不为需要模拟 Windows 沙箱的工具做兼容层。生产运行平台为 Linux VPS。
- API Key 模型：`agentkit model add ... --key-env VAR`，使用 builtin OpenAI 兼容工人。
- 保存登录态授权：Qoder 保存账号登录、OpenCode `allow_account_login=true`，以及仓库外的 Qoder `config_dir`，都要求先在用户级 `~/.config/agentkit/config.toml`（或 `$XDG_CONFIG_HOME/agentkit/config.toml`）设置 `[security] allow_saved_credentials=true`；项目配置不能自行开启。API Key/PAT 路径不需要此开关。该授权不是强凭证隔离，不可信仓库仍不得使用真实登录态。
- Codex CLI：配置 `[workers.codex]` 的 `command`、`model`、可选 `key_env`、`sandbox` 和 `price_in/price_out`，然后 `task create ... --backend codex`。bubblewrap 默认掩蔽宿主 HOME/XDG 与常见云凭证文件/agent socket，仅重新挂载显式授权的后端状态目录；Codex 保存登录不会隐式可用，请使用专用 API Key 环境变量。
- OpenCode V1：配置 `[workers.opencode]` 的 `command` 与 `model=provider/model`，使用官方 `opencode run --format json`；默认禁用 shell、网络工具、子 Agent 和项目配置，验收由 AgentKit 执行。
- OpenCode V2：设置 `version = "v2"`，可指定 `model = "opencode/beam-free"` 等官方免费模型，无需登录；AgentKit 使用 `--standalone run --format json`、V2 `permissions/policies` 和独立数据目录。免费额度用尽返回 429 并暂停续跑，不绕过限额；需要保存账号登录时必须显式 `allow_account_login = true`，并获得上述用户级授权。真实 V2 2.4.2 最小任务已验证 token/session 解析。
- Qoder CLI 账号：安装官方 Qoder CLI。AgentKit 默认使用专用 `XDG_CONFIG_HOME/agentkit/qoder` 目录，不复用 `~/.qoder`；保存账号登录需要上述用户级授权，先设置 `QODER_CONFIG_DIR=<该目录>` 再运行 `qodercli login`，或显式配置 `[workers.qoder].config_dir` 后使用 `--backend qoder`。也可用 `QODER_PERSONAL_ACCESS_TOKEN`，无需授权开关。适配器只调用 CLI，不读取登录文件；默认关闭遥测、项目配置、MCP、shell、网络工具和子 Agent，并记录请求 Credits。Credits 不等同于 USD；`--budget-usd` 需配置价格或确认零成本。
- Cursor CLI：安装官方 `agent` CLI，配置 `[workers.cursor]` 并设置 `CURSOR_API_KEY`（或专用 `key_env`），使用 `--backend cursor --budget-minutes N`。适配器使用官方 headless/stream-json、独立配置目录和 session resume，默认拒绝 shell、网络、MCP 与密钥路径；CLI 模式不报告 token/成本，因此拒绝 token/USD 预算。可选 `transport = "sdk"`（需 `pip install -e '.[cursor]'` 和显式 `model`）使用官方 Python SDK，支持工具白名单、session resume、运行时 token 用量与按运行成本；SDK 未报告用量时仍拒绝 token 预算，USD 硬预算仍不支持。真实 Cursor API Key 任务尚未验证。
- 所有外部后端默认要求 bubblewrap；`allow_unsandboxed` 仅供测试。接力可在 builtin/Codex/OpenCode/Qoder/Cursor 间进行，保留同一 worktree、diff、AgentKit 记忆和累计预算；Cursor 仅支持轮次/时间预算。
- 域名白名单要求模型 CLI/工具遵守 `HTTP_PROXY`/`HTTPS_PROXY`；不遵守代理的客户端会在独立网络命名空间内直连失败，不会自动回退到共享网络。`network=false` 或白名单在 bubblewrap 不可用时也会拒绝启动。
- Antigravity 只接官方条款明确允许的企业/API 方案；consumer 登录态不包装。所有外部工具均不做反代、不共享登录态、不轮换账号。

### 派活模式要点
- 工人只能用上面的工具（外加 `finish` 提交），不能 commit / push；工人说「完成」不算数：验收器把它的改动复制到全新 worktree 独立重跑检查，并标记删测试、跳过测试、改验收脚本、改白名单外文件、偷偷提交等行为。
- 预算超限 5 秒内停止；429 按 Retry-After 等一次（≤ `max_wait`，默认 30 s）后暂停上报（不轮换账号）；Key 无效立即失败；504 视为上游暂时不可用（重试后暂停，可续跑）。
- 预设：`agentkit model add NAME --preset nvidia|gemini|openrouter|agnes|amd-cn --model ID`（填好 base URL、Key 变量名、上下文、免费档节流 `--rpm` / `--max-wait` / `--max-tokens` / `--rate-wait`，显式参数优先）。已实测 5 家（推理模型的 `reasoning_content` / `reasoning` 不回传，被 token 上限截断时会告诉模型）：

  | 预设 | 服务商 | Key 变量 | 实测可用工具调用的模型 | 免费方式 / 限制（2026-11-23） |
  |---|---|---|---|---|
  | `gemini` | Google Gemini（OpenAI 兼容端点） | `GEMINI_API_KEY` | gemini-3-flash-preview、gemini-3.5-flash-lite | 免费档，每分钟 / 每天限额低 |
  | `openrouter` | OpenRouter | `OPENROUTER_API_KEY` | `nvidia/nemotron-3.5-lightning:free` | 只用 `:free` 模型，每天 50 次 |
  | `nvidia` | NVIDIA NIM | `NVIDIA_API_KEY` | nemotron-3.5-lightning-30b-a3b | 免费试用约 40 RPM |
  | `agnes` | Agnes AI（国际） | `AGNES_API_KEY` | **agnes-3.0-flash**（也通：agnes-2.5-flash） | flash 模型目前促销零价（厂商说明）；实测约每分钟 10 次，429 不带 Retry-After → 预设 rpm 6 + `rate_wait` 60 s。pro 模型可能收费，别用 |
  | `amd-cn` | AMD Radeon Cloud Token Factory（中国站） | `AMD_CN_API_KEY` | **DeepSeek-V4-Flash**（也通：Gemma-4-31B-IT、MiMo-V2.6-Flash） | 每日积分，北京时间 00:00 重置；约 20 RPM、每 Key 8 并发；忙时 429 `model_concurrency_rate_limit_exceeded`（等 5 / 10 / 20 s 后暂停）；偶发 502 |

- OpenRouter：`--base-url https://openrouter.ai/api/v1`，免费模型 id 以 `:free` 结尾（`model add` 对无单价的非 `:free` 模型给出警告）；自动带 `HTTP-Referer` / `X-Title`；`X-RateLimit-Reset` 与 HTTP 200 里的 provider 错误都按对应状态码处理。
- 上下文压缩（C-08）：工人的重复工具结果变成 `same_as_turn` 引用；最近 2 轮之外的旧工具结果批量老化为一行摘要；系统提示 + 工具定义 + 记忆摘要作为字节不变的前缀（利于服务商提示词缓存）；简洁输出风格（`style = "auto"`：大仓库从第 1 轮放进系统提示，否则转录变长时追加一次，小任务不付这笔开销）。`[compress] enabled / run_filters / dedup / aging / age_min_chars / style` 可调。实测数字见 [docs/STATUS.md](docs/STATUS.md)（离线：多文件题工人 token −12.4%，小题 0%；真实 NVIDIA NIM 3 道多文件题 −4.4%（每题 1 次，噪声大）；冗长日志 57–92%；真实转录重放 −60%）。
- 日志：`.agentkit/logs/` 默认保留 7 天，`[logs] retain_days` 可调（0 = 永久）。
- 评测：`python -m eval.run [--cheats] [--size tiny|large]`（离线假模型，21 道小题 + 3 道多文件题 + 14 例作弊库）；`python -m eval.run --real --base-url URL --model-id ID --key-env VAR` 接真实模型；`python -m eval.run --backend codex|opencode|qoder|cursor --model-id ID --worker-config '<后端 TOML>' --no-direct` 用同一 TaskSpec/预算/独立验收评测外部工人；外部评测强制要求 bubblewrap，超时会取消任务，JSON 报告记录提交、运行时、沙箱、任务集合与结果哈希，任一任务失败即返回非零；`python -m eval.llms_check [--scenario bare]` 模拟「只读 llms.txt 的陌生 AI」在全新环境安装并跑 doctor + verify（M-19）。

### 报告问题
AI 代用户报 Bug / 提需求：先搜已有 Issue，**先问人类用户**（Issue 公开），用 `agentkit feedback` 生成草稿，不含密钥或私有代码，一个问题一个 Issue。

开发：`pip install -e .[dev] && pytest -q`。

### 本地发布与回滚

- `agentkit release plan`：只读显示版本、git 提交、产物目录和构建计划。
- `agentkit release build`：要求干净 git 工作树，构建 wheel/sdist、`SHA256SUMS` 和 `release-manifest.json`；`--allow-dirty` 仅用于测试构建，`--overwrite` 需显式确认。
- `agentkit release verify dist/release-manifest.json`：校验文件哈希、大小、wheel METADATA/必需模块和 sdist 安全路径，不导入或执行 artifact；`trusted:false` 表示哈希不证明发布来源。可加 `--signature SHA256SUMS.sig --public-key public.pem` 校验分离式签名。
- `agentkit release sign dist/SHA256SUMS --key private.pem`：用系统 OpenSSL 对校验和文件做 SHA256 分离式签名，默认输出 `SHA256SUMS.sig`；私钥路径由用户提供，不写入仓库或日志。
- `agentkit release rollback dist/release-manifest.json`：默认只显示安装计划；`--yes` 才安装私有暂存副本，使用隔离 pip 环境。Windows 控制台运行时采用退出后 helper 安装，并写入 `rollback-VERSION-result.json`。
- 发布命令不自动签名、不推送、不发布，也不会自动升级用户环境；回滚保留 `.agentkit/` 状态。

## 文档
- [PRD v1.0：可信执行与进化底座](docs/prd-v1.0.md)（目标需求，C-15 至 C-20）
- [PRD v0.3](docs/prd-v0.3.md)（[v0.2 存档](docs/prd-v0.2.md)）
- [V1.0 需求基线（冻结）](docs/v1.0-baseline.md)
- [功能清单与借鉴来源](docs/feature-sources.md)
- [进度](docs/STATUS.md) · [错误码](docs/errors.md) · dogfood 记录：[M1](docs/dogfood-m1.md) · [M2](docs/dogfood-m2.md)

## 明确不做
不做人用 IDE / 聊天界面；不托管或转售模型；**不提供多账号轮换、代理池等绕过平台限额的功能**；不自动 push / 部署；无遥测。

## 参与
见 [CONTRIBUTING.md](CONTRIBUTING.md)。V1.0 范围已冻结，新功能建议请开 Issue 登记，V1.0 发布后统一评估。

## 许可证
Apache-2.0
