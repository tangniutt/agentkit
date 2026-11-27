# 任务书

## 目标

用一句话说明可验证结果，不写“优化一下”或“看看能不能”。

## 验收

- 基线必须失败：
- 修复后必须通过：
- 类型/构建/Lint：
- 回归测试：
- 禁止修改：

## 范围

- 允许路径：
- 禁止路径：
- 相关模块：
- 不需要修改：

## 风险

- complexity：0–3
- blast_radius：0–3
- context_pressure：0–3
- required_capabilities：
- verification_strength：basic | standard | strong

## 路由

- 角色：planner | implementer | reviewer
- ModelProfile：
- 推理强度：low | medium | high
- Fallback：
- 选择理由：

## 预算

- token：
- USD：
- 分钟：
- 最大轮数：
- 最大 handoff：

## 执行约束

- 先读相关代码与现有测试，再修改。
- 保持最小 diff，不 commit、不 push。
- 不读取凭证、不访问未授权网络、不绕过验收。
- 每阶段结束留下简短进度、失败证据和下一步。

## 完成条件

只有独立验收通过才算完成。模型自述“完成”不是证据。
