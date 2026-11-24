# AGENTS.md — 给接手本项目的 AI Agent

先读这个文件，再动手。

## 项目是什么
AgentKit：给 AI Agent 用的轻量编程工具。三种模式：直接模式（主管 AI 自己用工具）、派活模式（内置工人接免费/付费模型）、混合模式。核心卖点：统一输出预算省 token、编辑校验、独立验收与防作弊、项目记忆。

## 必读文档（按顺序）
1. `docs/STATUS.md` — 当前进度、下一步
2. `docs/v1.0-baseline.md` — 冻结的 V1.0 范围（M-01 至 M-22）与验收标准
3. `docs/feature-sources.md` — 每个功能借鉴哪个开源项目、参考代码位置
4. `docs/competitor-gap.md` — 与 Claude Code / Codex / OpenCode / Aider / jcode 的对比
5. `docs/process.md` — 开发流程规矩
6. `docs/prd-v0.2.md` — 完整需求（v0.1 仅存档）

## 主管 AI 操作手册（C-09）
- `agentkit manual`（总览 ≈1.7k token）/ `agentkit manual <命令>`（单个命令详情）/ `agentkit manual --all --budget 4500`（全部）；Markdown 版 `docs/agent-manual.md`。
- 手册由 `agentkit/cli.py` 的真实参数定义 + `agentkit/manual.py` 的 SPEC + 错误码登记表生成。**新增或修改命令时必须同步 SPEC 并重新生成**：`agentkit manual --format md > docs/agent-manual.md`（`tests/test_manual.py` 检查覆盖、示例可解析、错误码存在、文档无漂移、大小）。

## 硬规矩
- **范围冻结**：只做基线里的 Must。新想法写进基线第 9 节「变更登记」，不要直接实现。
- **先读参考再写**：模块开工前读 feature-sources.md 指定的参考实现（clone 到仓库外，不要提交第三方仓库）。
- **许可证**：只复制 MIT / Apache-2.0 / BSD 代码，保留原版权声明并登记到 `NOTICE`；GPL/AGPL/未核实的只借思路。
- **验收后才合并**：每个 Must 有自动测试，`pytest` 全绿才能推到 main。
- **提交签名**：`git commit -s`（DCO）。
- **不做**：账号轮换、代理池等绕过平台限额的功能；自动 push/部署；遥测。
- **密钥**：绝不写进代码、测试、日志、文档。

## 目录（M2 Python 原型阶段）
- `agentkit/` — 源码；`tests/` — 测试；`docs/` — 文档
- `eval/` — M-20 评测题、作弊库、离线假模型服务器（`python -m eval.run`）
- 运行测试：`pip install -e .[dev] && pytest -q`；M-19 安装实测：`python -m eval.llms_check --src . [--scenario bare]`
- 卸载：`agentkit uninstall`（先看计划）→ `--yes`

## 出错时怎么定位（C-03）
- 每条失败命令的 JSON 都有 `error.code`（稳定错误码，见 `docs/errors.md`）和 `error.run`（运行 ID）。
- `agentkit logs <run或任务ID> --errors` 看结构化事件（哪一步、哪个组件、耗时、错误码）；任务的 `status`/`report` 里有 `code`、`at`（组件#步骤）和日志指针。
- 任务 paused（429 / 超时）、interrupted、stopped：`agentkit task resume ID` 在原 worktree 续跑（预算停的要加 `--budget-*`）。
- `run` / 验收输出被命令过滤器压缩过（`data.filtered`，C-08）：`agentkit logs <run-日志ID> --raw` 看原文（已脱敏）；`run --raw` 不过滤；过滤器在 `agentkit/filters/*.json`，改动后 `python -c "from agentkit.compress import verify_filters; print(verify_filters())"` 必须为空。
- `AK-INTERNAL` = AgentKit 自己的 bug：堆栈在 `.agentkit/logs/crash-<run>.log`，不会打进你的上下文。

## Reporting problems（C-04）
- 先搜已有 Issue：https://github.com/tangniutt/agentkit/issues
- `agentkit feedback --bug <run_id>` / `agentkit feedback --idea "..."` 只生成草稿（标题、正文、预填链接），**从不自动提交**。
- 提交前**先问你的人类用户**（Issue 是公开的）；绝不包含密钥或私有代码；一个问题一个 Issue；需要附件用 `agentkit debug-bundle <id>`。

## AI 反馈进化回路（C-12）
- 真实使用之后有更好的改进或工具设计想法：`agentkit feedback --improve "一个具体改动" --from <任务/运行ID>`（可加 `--observed/--gain/--cost/--prior-art/--license/--verify-by`，或 `--input file.json` / `-`）。证据（状态、轮数、token、错误码与次数、可疑标记）自动从本地事件收集并脱敏，不含代码；`missing` 列出还没填的段落。
- 任务 / 验收结果的 `next` 出现 `agentkit feedback ...` 时，说明这次运行有值得报告的信号（AK-INTERNAL、同一工具错误重复、卡住、可能误报）；只有 AgentKit 本身有问题时才提。干净的运行不会出现。
- 先打开草稿的 `search_first`（按 `keywords` 搜），已有同类就去评论 / +1 附上证据，不开新的；一个 Issue 一个想法；**提交前先问人类**；从不自动提交。
- 维护者按 `docs/triage.md` 的评分表评估（标签 needs-eval → accepted / declined / duplicate / needs-info），接受的登记为基线 C-xx，合并前用测试与评测量化。
