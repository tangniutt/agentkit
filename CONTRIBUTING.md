# 参与贡献

1. **先看范围**：V1.0 只做 `docs/v1.0-baseline.md` 第 1.1 节的 Must 项。新功能请开 Issue，标注 Should / Could，V1.0 发布后评估。
2. **先找参考**：实现较大功能前，先在 Issue 里列出参考过的开源项目及其许可证。
3. **复用代码**：仅限 MIT / Apache-2.0 / BSD，保留原版权声明并登记到 `NOTICE`；GPL / AGPL 只借鉴思路，不复制代码。
4. **DCO 签名**：每个提交用 `git commit -s`，表示你有权按 Apache-2.0 贡献这段代码。
5. **验收标准**：PR 需对应基线中的验收标准，并附测试或评测结果。
6. **不接受**：绕过平台限额 / 封禁的功能（账号轮换、代理池等）。
7. **报告问题 / 提需求（含 AI 代用户提交）**：用 Issue 表单（Bug / Feature）。AI Agent 可运行 `agentkit feedback --bug <run_id>` 或 `agentkit feedback --idea "..."` 生成已脱敏的标题、正文和预填链接——该命令**从不自动提交**。提交前先搜索已有 Issue、征得人类用户同意（Issue 公开）、不含密钥或私有代码、一个问题一个 Issue。需要附件时用 `agentkit debug-bundle <id>`（脱敏归档）。错误码见 `docs/errors.md`。安全漏洞请走 SECURITY.md 的私下渠道。
8. **AI 改进建议（C-12）**：AI 用过 AgentKit 后有更好的改进或工具设计想法，用 `agentkit feedback --improve "一个具体改动" --from <任务/运行ID>` 生成「AI improvement / design proposal」表单草稿（观察、自动收集的脱敏证据、提案、预期收益、代价 / 风险、参考与许可证、验证方法）。先按草稿的 `search_first` 查重，重复就去原 Issue 评论 / +1；一个 Issue 一个想法；先问人类；不含私有代码或密钥。所有 `ai-feedback` Issue 由 `.github/workflows/issue-triage.yml` 初筛（必填段落、疑似密钥、`needs-eval`），再按 `docs/triage.md` 评分，接受的登记为基线 C-xx 并在合并前用评测量化。
