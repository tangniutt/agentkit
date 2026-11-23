# M2 dogfood 记录（派活模式 + 验收器 + 可诊断性）

日期：2026-11-23 · 版本：agentkit 0.2.0（M2 Python 原型）

## 1. 离线（本地假模型，CI 同款路径）

目标仓库：AgentKit 自身工作区的副本（`git init` 后一次提交）。人工先写好规格测试 `tests/test_events_describe.py`，派工人实现 `agentkit/events.py` 里的 `describe(code)`。

```
agentkit model add fake --base-url http://127.0.0.1:<port>/v1 --model fake-scripted --key-env DOGFOOD_KEY --price-in 0.1 --price-out 0.4
agentkit task create "Add a function describe(code) to agentkit/events.py ..." \
    --check "python -m pytest -q -p no:cacheprovider tests/test_events_describe.py" \
    --allow agentkit/events.py --budget-minutes 1.5 --budget-tokens 200000
agentkit task wait t9ebfd8 --timeout 100
  {"done":true,"tasks":[{"id":"t9ebfd8","status":"done","verdict":"pass","rounds":5,"tok":13573,"usd":0.0015,"s":1}]}
agentkit task report t9ebfd8
  {"task":"t9ebfd8","status":"done","verdict":"pass","checks":[{"cmd":"python -m pytest ...","result":"pass","exit":0,"s":0.57}],
   "changed":["M agentkit/events.py"],"suspicious":[],"cost":{"s":1.3,"tokens":13573,"usd":0.0015},"env":"git-worktree",
   "claim_untrusted":"describe() added; tests pass","branch":"agentkit/t9ebfd8"}
agentkit logs t9ebfd8 --tail 12      # search -> read#hint -> edit -> run -> finish -> verifier pass
```

结果：通过；主分支未被改动，改动只在 `agentkit/t9ebfd8` 分支 / worktree；验收在全新 worktree 里独立重跑；报告 ≈ 560 字符（< 300 token），status < 100 token。

## 2. 真实模型（Gemini OpenAI 兼容接口，免费档）

配置：`--base-url https://generativelanguage.googleapis.com/v1beta/openai/ --key-env GEMINI_API_KEY`（配置里只有变量名；Key 由沙箱代理在网络层注入）。`agentkit model test` 通过（0.6–0.8 s）。

### 2.1 同一 dogfood 任务（gemini-3-flash-preview，mid 档）

```
agentkit task wait t95aeaf
  {"id":"t95aeaf","status":"paused","verdict":"fail","rounds":7,"tok":30745,"s":38,
   "code":"AK-MODEL-429","at":"model#7","log":"agentkit logs t95aeaf --errors","why":"rate limited (429) ..."}
agentkit logs t95aeaf --tail 12
  ... #2 tool:ls, #3-#6 tool:read (每轮模型 1.1–3.5 s) ...
  #7 model model_error 27.371s AK-MODEL-429 {"what":"HTTP 429: ... You exceeded your current quota ..."}
  #7 model worker_end AK-MODEL-429 {"status":"paused"}
  #- verifier verified AK-VERIFY-FAIL {"verdict":"fail"}
```

工人用了 6 轮探索仓库（ls / read），第 7 轮遇到免费档配额 429：客户端按 Retry-After 等了一次（≈27 s），再次 429 → 任务暂停并上报（没有反复轰炸）。C-03 的 `code / at / log` 让主管一眼看出「卡在模型第 7 轮的 429」，不需要翻日志文件。

### 2.2 小规模评测（`python -m eval.run --real ... --tasks <id> --no-direct`，逐题运行）

| 题目 | 模型 | 结果 | 轮数 | 工人 token | 主管 token |
|---|---|---|---|---|---|
| ev-add | gemini-3.5-flash-lite | pass | 8 | 12,368 | 285 |
| ev-max | gemini-3.5-flash-lite | pass | 7 | 11,719 | 290 |
| ev-pal | gemini-3-flash-preview | pass | 7 | 12,465 | 290 |
| ev-pal | gemini-3.5-flash-lite | 暂停：429（日配额，Retry-After ≈ 39,346 s） | 2 | 1,088 | 337 |
| ev-fizz / ev-bsearch / ev-merge | gemini-3-flash-preview | 暂停：429（每分钟限额，Retry-After 57–58 s > 30 s 上限，不等待） | 1 | 0 | ~335 |
| ev-fizz | gemini-3.8-flash | 失败：HTTP 503（发现的 bug，已修复，见下） | 1 | 0 | 361 |
| ev-bsearch | gemini-3.8-flash | 暂停：429（日配额） | 1 | 0 | 333 |

**未被限流的 3 题全部通过（3/3）**；共尝试 9 次，6 次被免费档限流 / 过载挡住。之后配额耗尽，按要求停止真实运行：**真实模型评测其余部分待配额重置（real-model eval pending quota reset）**。

## 3. dogfood 发现并修复的问题
1. **HTTP 503（模型过载）被当成永久错误直接失败**。修复：500/502/503 归为「暂时不可用」，与超时一样指数退避重试 2 次，仍失败则暂停并给 `AK-MODEL-UNAVAILABLE`（新增错误码，测试覆盖）。
2. **Retry-After 为 0 时显示 "retry after 0s"**。修复：只在 >0 时显示。
3. **同一秒内「改文件 → 重跑测试」可能命中旧的 .pyc**（文件大小不变、mtime 同秒）。评测自检时发现；工人进程现在设置 `PYTHONDONTWRITEBYTECODE=1`，验收器本来就用全新 worktree。
4. 真实模型在小任务上倾向多读几轮再改（6 轮探索）；记录为观察，V1.1 的「模型分档 / 卡住升档」再处理（不在 V1 范围）。

## 4. 主管 token 观察（诚实记录）
离线评测里，这些题都极小（单个 5 行文件），主管直接改反而更省（直接模式 21 题共约 5.3k token，派活模式约 5.8k token：create + wait + report 的固定开销约 280 token/题）。派活模式的优势要在需要多轮探索的真实任务上才体现（上面真实任务里工人花了 1.2 万 token，主管只花约 290 token）。M-20 的「省 token」结论需要更大的真实任务集（M5）。

## 5. M2 补完（2026-11-23 上午）

### 5.1 M-19 实测：机械 AI 只读 llms.txt 自装
`python -m eval.llms_check [--src .] [--scenario venv|bare]`：只执行 llms.txt「Install」一节的命令（按顺序取第一条成功的），再跑 `agentkit doctor` 与 Direct mode 一节的 `agentkit verify`。

| 场景 | 旧 llms.txt | 新 llms.txt |
|---|---|---|
| 全新 venv、空 HOME、空 pip 缓存、从 GitHub 安装 | 通过（安装 3.5 s） | 通过 |
| bare：PEP 668 发行版系统 Python（3.13，无 pip、无 ensurepip），PATH 上只有 git / uv | **失败**：`pip: command not found` | 通过：pip → pipx 失败后 `uv tool install` 成功（1.0 s），按提示加 `~/.local/bin` 到 PATH，doctor healthy，verify pass |

结论：单行 `pip install` 在越来越常见的「系统 Python 不让装包」环境里必然失败；改成四选一 + PATH 提示 + 「都不行就请人类装 pipx/uv」（C-07）。

### 5.2 真实模型：多文件题与续跑
- gemini-3-flash-preview × ev-shop（shop 包，库存价值忽略数量 + 折扣按小数算，两个文件要改）：工人 7 轮修好两处，在调用 finish 前被 429 暂停；验收器在全新 worktree 独立重跑 → **pass**。主管只花 228 token（create + wait，报告内联），同题直接模式 2,178 token。
- 下一题 ev-csv 第 1 轮就 429（日配额，Retry-After ≈ 20,000 s），按要求停止，不再重试。
- 这正是 C-05 续跑的场景：配额重置后 `agentkit task resume <id>` 会在原 worktree 继续，而不是从头再派。

### 5.3 主管 token（C-06 前后，离线 24 题）
| | 小题 21 道 | 多文件题 3 道 |
|---|---|---|
| 直接模式 | 5,253 | 5,442 |
| 派活模式（瘦身前） | ≈ 5,800 | — |
| 派活模式（瘦身后） | 3,226 | 553 |

单个小题：create 303 字符 + wait（内联报告）294 字符 ≈ 149 token，原来 create + wait + report ≈ 268 token。

### 5.4 发现并修复
1. llms.txt 在 PEP 668 / 无 pip 环境装不上（见 5.1）。
2. `uninstall --this-repo` 后，CLI 收尾的事件日志会把刚删掉的 `.agentkit/logs` 又建回来 → uninstall 不再写事件、不触发日志清理。
3. 主管侧冗余：create 回显 model / branch / null 的 queue_pos，report 回显主管自己给的检查命令、恒定的 env、通过时的工人声明 → C-06 删除。

### 5.5 OpenRouter
（上午）沙箱里 `OPENROUTER_API_KEY` 不存在，未发出请求。下午见第 6 节。

## 6. OpenRouter 真实运行 + C-08（2026-11-23 下午）

### 6.1 真实运行
- Key 由沙箱代理在网络层注入；命令里只用占位值，Key 不进日志 / 仓库。`GET /api/v1/key`：开始 0/50，结束 49/50。
- `google/gemma-4-31b-it:free`：上游（Google AI Studio 共享池）全部 429，3 道题即时 paused（按设计暂停、可 `task resume`，不耗额度）。换 `nvidia/nemotron-3.5-lightning:free`（支持 tools）。
- dogfood：在本仓库副本里让工人给 `agentkit/fsutil.py` 加 `human_bytes()` 并写测试（`max_rounds=12`）。结果：12 轮停止，7/8 测试通过，102,578 token。验收器判 fail，并标出 `check_script_modified`（目标要求新建测试文件，预期内）。
- 3 道多文件评测题：**3/3 pass**（ev-shop 14 轮 69,712 token；ev-csv 11 轮 42,130；ev-rename 14 轮 50,047；主管 181–235 token / 题）。其中两题用满轮数被停，但改动已正确，独立验收判 pass。

### 6.2 发现并修复
1. **工人编造工作目录**：dogfood 里模型 4 轮在 `cd /workspace && pytest …`（路径不存在），再 `pwd`、`ls` 才找回。修复：系统提示写明「命令在 worktree 根目录运行，用相对路径，别 cd 绝对路径」。
2. **输入 token 堆积**：12 轮 10 万输入 token，主要是早期 `repo_map` 和整文件 `read` 每轮重复发送（上下文 100 万，70% 折叠永远不触发）。→ C-08 渐进老化；同一转录离线重放 −60%。
3. **`npm ERR!` 不算严重行**（C-08 自测发现）：`\berr!\b` 在「!」后没有词边界，`npm ERR! code ERESOLVE` 会被 git-diff 的按文件封顶删掉，而校验器用同一个正则也没拦住。修复正则；随机噪声测试覆盖每个过滤器 × 9 种严重行。
4. **git diff 被校验器整体退回**：源码里的 ``` 被当成「代码块」，按文件封顶后校验失败 → git-diff 过滤器关闭代码块检查（diff 本身就是代码；文件头 / hunk 头仍受保护）。
5. **diff 在预算下只看到最后一个文件**：原 `run` 尾部优先截断。→ diff 先输出「N files: 路径 (+a -b)」一行，并改为头部优先显示（3/3 改动文件可见，原来 0/3）。

### 6.3 C-08 数字
见 `docs/STATUS.md`「C-08 实测」：冗长日志 C-08 后显示 token 省 32–92%（pytest 300 警告 81%、npm 77%、cargo / jest 92%），git diff 1%、git status 0%；离线评测工人 token 多文件题 −11.6%、小题 +5.7%、合计 −1.4%；主管 token 不变。

## 7. 第 3 家（NVIDIA NIM）+ 真实压缩 A/B + 续跑（2026-11-23 晚）

所有数字都是**真实模型**（标注「离线」的除外）；每格只跑 1 次，轮数差 1–3 轮就能左右 token，噪声大。Key 全部由沙箱代理在网络层注入，命令里只有占位值。

### 7.1 C-08 跟进：小任务的风格说明开销（离线，24 题脚本化假模型）

| | 压缩关 | 改前（风格说明恒在） | 改后（`style = "auto"`） |
|---|---|---|---|
| 小题 21 道 工人 token | 120,445 | 127,301（+5.7%） | **120,445（0.0%）** |
| 多文件 3 道 工人 token | 84,508 | 74,677（−11.6%） | **74,041（−12.4%）** |
| 合计 | 204,953 | 201,978（−1.4%） | **194,486（−5.1%）** |
| 主管 token 小题 / 多文件 | 3,226 / 552 | 3,226 / 553 | 3,226 / 553 |

小题转录最长 1,704 字符，从不触发（阈值 4,000）；多文件题在第 4–6 轮追加说明。

### 7.2 NVIDIA NIM（`nvidia/nemotron-3.5-lightning-30b-a3b`，`--preset nvidia`：rpm 30、max_wait 65、max_tokens 8192，`max_rounds=20`）

`/v1/models` 33 个模型；带 tools 的探测：nemotron-3.5-lightning（0.5 s）、minimax-m3.1-flash（21 s）、kimi-k3.1-pro（48 s）都返回正确的 `tool_calls`（content 为空、思考在 `reasoning_content`）；glm-5.3-flash 90 s 超时。`model test` 对推理模型返回 `reasoning_only: true, finish_reason: length`（16 token 预算全用在思考上，接口正常）。

| 题 | 压缩 | 结果 | 轮数 | 工人 token（入 / 出） | 主管 token | 用时 |
|---|---|---|---|---|---|---|
| ev-shop | 开 | pass | 15 | 56,601（53,285 / 3,316） | 185 | 49 s |
| ev-shop | 关 | pass | 13 | 61,595（59,200 / 2,395） | 185 | 38 s |
| ev-csv | 开 | pass | 9 | 26,741（24,925 / 1,816） | 181 | 21 s |
| ev-csv | 关 | pass | 9 | 29,771（28,381 / 1,390） | 181 | 23 s |
| ev-rename | 开 | pass | 16 | 63,731（60,958 / 2,773） | 185 | 44 s |
| ev-rename | 关 | pass | 15 | 62,555（60,224 / 2,331） | 185 | 36 s |
| **合计** | 开 / 关 | 6/6 | 40 / 37 | **147,073 / 153,921（−4.4%）** | 551 / 551 | |

每轮输入 token：开 3,552 / 2,769 / 3,810，关 4,554 / 3,153 / 4,015（−22% / −12% / −5%）；但开压缩多用了 3 轮（老化后模型偶尔重读），输出 token 反而多（推理 token 为主，简洁风格管不到思考）。

### 7.3 Gemini（免费档）

- 16:4x 调用 1 次成功（配额已重置）。`gemini-3-flash-preview` A/B：3 题 × 2 档同时开跑，**每分钟限额**立即 429（Retry-After 50–56 s > 原 30 s 上限）→ 5 个任务第 1 轮暂停，只有 ev-shop（开）跑了 8 轮（验收 pass，finish 前被 429 暂停）。→ 发现：免费档每分钟限额的 Retry-After 稍超 30 s，原实现只能暂停；新增 `max_wait`（预设 65 s）与 `rpm` 节流。
- **续跑**：给暂停的任务配 `max_wait = 65` 后逐个 `task resume`：ev-csv（开）续跑 14 轮 → **pass**（15 轮、35,914 token，压缩 aged 3、风格说明第 6 轮），随后 `gemini-3-flash-preview` **日配额** 429（Retry-After 83,458 s）→ 按要求停止，其余 5 个保持 paused（可续跑；ev-shop（开）验收已 pass）。
- 改用 `gemini-3.5-flash-lite`（`max_wait 65`；第二批再加 `rpm 5`）：

| 题 | 压缩 | 结果 | 轮数 | 工人 token | 备注 |
|---|---|---|---|---|---|
| ev-shop | 开 | pass | 10 | 24,422 | 一次跑完 |
| ev-shop | 关 | pass | 8 + 11 | 17,874 + 41,438 = 59,312 | 第 8 轮 429 暂停 → `task resume` 后 pass（续跑重新探索，不可直接对比） |
| ev-csv | 开 | pass | 14 | 35,568 | rpm 5，无 429 |
| ev-csv | 关 | pass | 14 | 57,244 | 同上 |
| ev-rename | 开 | pass | 16 | 38,037 | 同上 |
| ev-rename | 关 | pass | 13 | 36,126 | 同上 |

可直接对比的两题（ev-csv + ev-rename，同轮数上限、都无中断）：开 73,605 vs 关 93,370（**−21.2%**）；ev-rename 开压缩多 3 轮，反而 +5.3%。

### 7.4 dogfood（NVIDIA，本仓库副本，给 `fsutil.py` 加 `human_bytes()` + 测试，`max_rounds=12`）

1. 第一次：10 轮全在探索（repo_map、分段 read 200 行的 fsutil.py、glob、search），**被判「卡住：10 轮无新 diff」→ failed（不可续跑）**。→ 修复：首次改动之前，本轮拿到新结果（之前没见过的读取范围 / 搜索）的探索轮不计入卡住；重复同样的调用仍计入；`max_rounds` 仍封顶。回归测试 `test_exploring_before_first_edit_is_not_stuck`（旧代码失败）。
2. 修复后第二次：12 轮用满 → `stopped: rounds`（可续跑），实现正确、检查通过（59,729 token，31 s）；验收器给 needs_review：`check_script_modified`（目标要求新建测试文件，预期）+ `hardcoded_return_suspect`（`if n == 0: return "0 B"`，启发式**误报**，已知取舍：只降级不判失败）。对比下午 OpenRouter 同族模型：12 轮 7/8 测试、102,578 token。

### 7.5 本轮发现并修复
1. 小任务付风格说明开销（C-08 跟进，见 7.1）。
2. 免费档每分钟限额 Retry-After 50–56 s 超过固定 30 s 上限 → 任务总在暂停：`max_wait` / `rpm`（7.3）。
3. 推理模型：`finish_reason=length` 且只有 `reasoning_content` 时原来报「no tool call」→ 现在明确说「输出 token 上限截断」；`reasoning_content` 从不回传（测试断言）；`model test` 显示 `reasoning_only`。
4. HTTP 504 原归为本地超时 → 归为上游不可用（重试 2 次后暂停，可续跑），状态码与 200 body 两种都覆盖。
5. 卡住检测误判探索（7.4）。


## 8. 第 4、5 家：Agnes AI + AMD Token Factory 中国站（C-11，2026-11-23 夜）

所有数字都是**真实模型**（标注「离线」的除外）。Key 由沙箱代理在网络层注入，命令里只有占位值。工人 token = 服务商返回的 usage（输入 + 输出）；主管 token = 字符 / 4。多文件题 `max_rounds=20`，dogfood `max_rounds=12`；按服务商顺序跑（同一服务商同一时间只有 1 个任务）。

### 8.1 工具调用探测（`/v1/models` + 带 `read` / `finish` 两个工具的两轮请求）

| 服务商 | 模型 | 结果 |
|---|---|---|
| Agnes | agnes-3.0-flash | 200，0.6 s，`tool_calls` 正确（content 空，无推理字段）；响应 model 名为 `agnes-3.0-flash-angel` → **选用** |
| Agnes | agnes-2.5-flash | 200，1.5 s，`tool_calls` 正确，带 `reasoning_content` |
| Agnes | agnes-2.5-pro-alpha / -theta、agnes-3.0-pro | 未测（可能收费） |
| AMD | DeepSeek-V4-Flash | 200，6 s，`tool_calls` 正确（带 `reasoning_content`）；`/models` 标 tools + reasoning、上下文 1M → **选用** |
| AMD | Gemma-4-31B-IT | 200，3–4.5 s，`tool_calls` 正确 |
| AMD | MiMo-V2.6-Flash | 200 但 90 s；`tool_calls` 正确，思考在 `reasoning` 字段 |
| AMD | Qwen3.8-27B | 无工具时文本在 `reasoning`、content 空（`model test` → `reasoning_only`）；带工具：一次 >110 s 客户端超时、一次 504 |
| AMD | GLM-5.3-Flash | 多次 429 `model_concurrency_rate_limit_exceeded`（`detail.error`，Retry-After 1）或 502，未拿到工具调用结果 |
| AMD | DeepSeek-V4.1-Flash | 纯文本 200（17 s）；带工具时 502 |

观察：AMD 网关这段时间对多个模型间歇返回 502 `upstream request failed`（约 10 s，纯文本 body），几分钟后恢复；MiniCPM5-2B 同时 200。

### 8.2 Agnes 限速实测
- 首轮（`rpm 10`，无 `rate_wait`）：ev-shop、ev-csv 各 5 轮 pass（14,652 / 14,231 token），第 11 次请求起 HTTP 429 `{"error":{"code":"","message":"You’ve reached the API rate limit for free users. …"}}`，**不带 Retry-After** → 旧逻辑立即暂停，ev-rename 与关压缩的 3 题都在第 1 轮暂停。
- 每 6 s 一次请求：第 1–10 次 200，第 11 次（60.2 s）429，之后恢复 → 约「每滚动 60 s 10 次」。
- 修复：模型配置 `rate_wait`（429 无 Retry-After 时等一次），Agnes 预设 `rpm 6` + `rate_wait 60`。之后 4 组共 12 题无一暂停。

### 8.3 多文件题（压缩开 / 关，每格 2 次）

| 服务商 / 模型 | 题 | 开：结果 / 轮 / 工人 token（第 1 次；第 2 次） | 关：结果 / 轮 / 工人 token（第 1 次；第 2 次） |
|---|---|---|---|
| Agnes `agnes-3.0-flash` | ev-shop | pass / 6 / 17,223；pass / 6 / 18,581 | pass / 6 / 21,857；pass / 6 / 22,774 |
| | ev-csv | pass / 5 / 13,550；pass / 5 / 13,955 | pass / 5 / 14,612；pass / 5 / 14,680 |
| | ev-rename | pass / 9 / 30,231；pass\* / 6 / 16,940 | pass / 6 / 19,964；pass / 5 / 15,650 |
| | **合计** | **6/6，37 轮，110,480** | **6/6，33 轮，109,537（开 +0.9%）** |
| AMD `DeepSeek-V4-Flash` | ev-shop | pass / 5 / 16,508；pass / 5 / 17,442 | pass / 6 / 23,777；pass / 7 / 31,530 |
| | ev-csv | pass / 7 / 19,350；pass / 6 / 16,655 | pass / 5 / 14,733；pass / 5 / 14,395 |
| | ev-rename | pass / 5 / 15,410；pass / 7 / 24,003 | pass / 7 / 23,880；pass / 8 / 32,708 |
| | **合计** | **6/6，35 轮，109,368** | **6/6，38 轮，141,023（开 −22.4%）** |

- 主管 token 每题 181–185（12 组里 2 组更高：Agnes ev-rename\* 239，AMD 关压缩 ev-shop 359——任务慢，`task wait` 多轮询了几次）。
- 每轮工人 token：Agnes 开 2,986 vs 关 3,319（−10.0%），AMD 3,125 vs 3,711（−15.8%）。总量差别主要来自轮数：Agnes 开压缩时 ev-rename 一次用了 9 轮（一次只改一个文件，不是老化后重读），把总量拉平；AMD 关压缩时多了 3 轮。
- \* Agnes 第 2 次开压缩 ev-rename：工人在一次回复里并行发了 3 个 `edit`，改的是它**记错内容**的测试文件（测试本来就用新名字；该任务老化了 6 个旧结果，第 3 轮读到的测试文件很可能已只剩一行摘要——推断），3 个都「old text not found」→ 被判「卡住：同一错误 3 次」→ failed（不可续跑），但改动其实已完成，独立验收判 pass。→ 修复：同一回复里的并行调用报同一错误只算 1 次（回归测试旧代码失败）。模型「凭记忆」写 old 文本（疑与老化有关），这一现象记录在案，暂不改老化策略（12 组里只出现 1 次）。
- AMD：工人里有几次单次模型调用 60–214 s（502 / 超时后重试成功），无任务暂停；没有遇到忙模型 429（DeepSeek-V4-Flash）。

### 8.4 dogfood（`fsutil.human_bytes()` + 测试，本仓库克隆，`max_rounds=12`）

| 服务商 | 结果 | 轮 | 工人 token（入 / 出） | 用时 | 验收 |
|---|---|---|---|---|---|
| Agnes `agnes-3.0-flash` | worker finish | 6 | 26,312（25,686 / 626） | 51 s | 检查 pass；needs_review：`check_script_modified`（目标要求新建测试文件，预期） |
| AMD `DeepSeek-V4-Flash` | worker finish | 10 | 42,293（41,130 / 1,163） | 244 s | 同上 |

两家都一次写对、测试 5 / 5 覆盖目标里的值；对比 NVIDIA（12 轮未 finish，59,729）和 OpenRouter（12 轮 7/8，102,578）。两份实现都用 `if n < 1024: return f"{n} B"`，没有触发 `hardcoded_return_suspect`；该误报（NVIDIA 的 `if n == 0: return "0 B"`）由单元 + 端到端回归测试覆盖（旧代码失败）。

### 8.5 本轮发现并修复
1. Agnes 429 不带 Retry-After → 旧逻辑立即暂停：新增 `rate_wait`，预设 rpm 6 + 60 s（8.2）。
2. AMD 错误嵌套在 `detail.error`，429 / 401 原来只给原始 JSON；忙模型 429（Retry-After 1）原来只等 1 s 一次就暂停：解析嵌套错误、忙 429 等 5 / 10 / 20 s 再暂停，与配额 429 区分。
3. 推理文本在 `reasoning` 字段（AMD Qwen3.8 / MiMo）：确认从不回传（测试）。
4. 并行调用同一错误被算作 3 次「卡住」（8.3 \*）。
5. `hardcoded_return_suspect` 基例误报（C-10 dogfood 记录的问题）。
