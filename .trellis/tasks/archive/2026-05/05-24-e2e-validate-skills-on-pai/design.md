# Design — E2E validate v1.0 skills on pai

## Methodology

**验证活动，不是产品开发**。核心：在受控、可重复的步骤下，对 9 个 v1.0 完成的 CLI 脚本做最小可信演练。

### 分层验证

每个脚本三层验证，任一层达标即"覆盖"：

1. **L1 — 静态契约**：`--help` 跑得通、参数表与 SKILL.md/references 描述一致。已被 `tests/test_manifests.py` 覆盖。
2. **L2 — 自动化集成**：已有 `pytest -m live_ssh` 的 9 个测试。本任务负责跑通 + 归档结果。
3. **L3 — 手动 happy-path**：从 SKILL.md "When to use" 触发场景里挑 1 个最具代表性的，跑一次取 stdout 写报告。L3 即便 L2 已 PASS 也要做——它产出的是给人看的「实际效果证据」。

### 验证不等于穷举

每个脚本只跑 1 个最有信息量的场景；不追求 branch coverage。例如 `inspect_container` 跑一个 healthy 本地容器即可，不需要逐个容器状态都跑（unit test 已覆盖）。

### 走 pai vs 走本地

- **走 pai（6 个测试）**：ssh-core 的 ssh_execute/ssh_cluster + remote-debug 全 4 个。SSH 路径已通，无需 fixture 准备。
- **走本地 Docker（3 个测试）**：docker-quick 全 3 个，用 throwaway container 自动起停。本地 Docker 27.5.1 已就绪，无需 fixture。

⇒ **零 fixture 准备成本**。这是本次"只跑已完成"决策的最大红利。

## Phase 编排

3 个 Phase，串行依赖。

### Phase A — Pre-flight (~5 min)

无副作用检查 + 报告骨架。

- A1：`ssh-add -l` 应列出 ed25519 key（否则 live_ssh 测试全 skip）
- A2：`ssh pai 'echo ok && uname -a'` 通
- A3：`docker ps` 本地可用
- A4：`pytest tests/` 默认套件（无 -m）全绿——基线保护
- A5：创建 `validation-report.md` 骨架（per-skill 章节占位）

**Phase A 出口**：基线绿 + 报告骨架就位。

### Phase B — Automated test suite (~10–20 min)

- B1：`pytest tests/ -m live_ssh -v -s 2>&1 | tee /tmp/devops-skills-phase-b.log`，**不**因失败而停（要看全部）
- B2：把 9 个测试结果分类整理到 report：测试名 → verdict → 关键 log 摘录（FAIL 优先抓 traceback 关键行）

**Phase B 出口**：9 个 live_ssh 测试每个都有 verdict 行。

### Phase C — Manual happy-path sweep (~15–30 min)

对 9 个脚本各跑 1 个 happy-path。每脚本写报告条目：

```
#### <script>
- Command: `python3 plugins/.../<script>.py <args>`
- Stdout (truncated, ~20 行):
   ...
- Verdict: PASS / FAIL / PARTIAL — <一句话>
```

顺序按依赖（前置 skill 先验）：

1. **ssh-core**（基础）
   - `ssh_execute pai 'uptime'`
   - `ssh_cluster pai,local 'hostname'`（2 节点广播）
2. **remote-debug**（依赖 ssh-core）
   - `diagnose_host pai`
   - `compare_across_hosts pai local`
   - `port_check pai --port 22 --port 443`
   - `tail_log pai /var/log/syslog --lines 5`
3. **docker-quick**（本地 Docker，独立）
   - `inspect_container --container <一个本地容器名>`（直接挑一个现有的 kline-redis 之类的，read-only 安全）
   - `compose_status --project-dir <一个有 compose.yml 的本地目录>`（如果有）；否则起一个 throwaway compose
   - `image_audit --image nginx:alpine` 或 `--image redis:alpine`（取已存在的本地 image）

**Phase C 出口**：report 9 节都有 stdout + verdict。

### Phase D — Findings + close (~5 min)

- D1：在 report 末尾写 Findings 一节：bug / 测试缺口 / SKILL.md drift；如全部 PASS，明确写 "No findings."
- D2：本地 `docker ps --filter 'name=^smoke-' --filter 'name=^poc-'` 应 0；pai `ssh pai 'ls /tmp/poc-* 2>/dev/null'` 应空（如非空，手动清理）
- D3：（可选）把 Findings 里每个 bug spin out 成子任务

**Phase D 出口**：报告完整，环境无残留。

## Risks & Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| ssh-agent 没 key → live_ssh 全 skip | Phase B 空跑 | A1 预检；缺则提示 user `ssh-add ~/.ssh/id_ed25519` |
| 本地 Docker 容器名冲突（用户已有 kline-*） | docker-quick 测试失败 | 测试用唯一名（`smoke-` 或 `poc-` 前缀）；C 阶段挑 read-only 命令对真实存在的容器跑（inspect 不改状态） |
| pai 网络抖动 | live_ssh 间歇失败 | B1 跑完即归档，FAIL 行附 stderr 关键句；不重试 |
| pytest live_ssh 卡死 | 阻塞 | 加 `--timeout=120`（pytest-timeout 若未装则手动 Ctrl-C 终止单测） |
| 手动跑发现 v1.0 脚本真有 bug | 影响验收 | 列入 Findings，不在本任务修；若 bug 阻塞验证本身（脚本崩溃），FAIL 标记后跳过该脚本 |

## Compatibility / Rollback

- 验证不动仓库代码 → 「回滚」= 删 report + 清理 throwaway 容器
- Phase 任一中断都安全：report 已写部分仍有价值；implement.md 的 checkbox 是续跑指针
