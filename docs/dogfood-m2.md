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
