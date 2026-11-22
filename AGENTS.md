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

## 硬规矩
- **范围冻结**：只做基线里的 Must。新想法写进基线第 9 节「变更登记」，不要直接实现。
- **先读参考再写**：模块开工前读 feature-sources.md 指定的参考实现（clone 到仓库外，不要提交第三方仓库）。
- **许可证**：只复制 MIT / Apache-2.0 / BSD 代码，保留原版权声明并登记到 `NOTICE`；GPL/AGPL/未核实的只借思路。
- **验收后才合并**：每个 Must 有自动测试，`pytest` 全绿才能推到 main。
- **提交签名**：`git commit -s`（DCO）。
- **不做**：账号轮换、代理池等绕过平台限额的功能；自动 push/部署；遥测。
- **密钥**：绝不写进代码、测试、日志、文档。

## 目录（M1 Python 原型阶段）
- `agentkit/` — 源码；`tests/` — 测试；`docs/` — 文档
- 运行测试：`pip install -e .[dev] && pytest -q`
