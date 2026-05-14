# Pipeline failure patterns

按业内观察频率排序。每条 5 段：Symptoms / Causes / Diagnostic / Remedy / Prevention。

---

## Action / image 版本漂移

> **Symptoms**: "上周还能跑，今天就 fail"。没改代码，CI 就坏。
>
> **Common causes**:
> 1. workflow 引用 `actions/checkout@v3`，maintainer 修了 `v3` 标签
> 2. docker image 引用 `:latest`，仓库 push 了新版本
> 3. base image `ubuntu-latest` 被 GH 升级（22.04 → 24.04）
> 4. provider 拉取的 default branch 跟上次不一样
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --log | grep -E "actions/|Run image"
> # 拉两次成功/失败 run 的 setup-* step diff
> ```
>
> **Remedy**: 把 `@v3` 改成 `@<commit-sha>`，把 `:latest` 改成具体 tag，固定 `runs-on: ubuntu-22.04` 而非 `ubuntu-latest`。
>
> **Prevention**: 用 Dependabot 自动跟踪 action 版本；team 约定**禁用** `:latest` 与 mutable tag。

---

## Cache miss 导致构建变慢

> **Symptoms**: build time 翻倍，依赖每次都重装。
>
> **Common causes**:
> 1. cache key 含动态字段（时间戳 / 随机串）
> 2. cache scope 是 branch 级，第一次跑新分支必然 miss
> 3. lock 文件改动（package-lock.json / Gemfile.lock / Cargo.lock）但 hash 函数没刷新
> 4. cache 超过容量（GH 每 repo 10 GB，按 LRU 驱逐）
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --log | grep -E "Cache (restored|saved|miss)"
> gh cache list --repo <owner>/<repo>      # 看现存 cache 列表
> ```
>
> **Remedy**: cache key 用 `hashFiles('**/package-lock.json')` 这类内容寻址；fallback key (`restore-keys:`) 用 prefix 命中近似缓存。
>
> **Prevention**: 单测 cache key 设计是否合理（first PR 应 miss，第二次 push 应 hit）。

---

## Matrix 爆炸

> **Symptoms**: 一个 push 触发 100+ 个 job，队列堵塞。
>
> **Common causes**:
> 1. 多维 matrix 全笛卡尔积（OS × Node × DB × region = 几十）
> 2. include / exclude 没用，全部组合跑
> 3. matrix 由 dynamic generation 给出（脚本错给了大集合）
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --json jobs | jq '.jobs | length'
> # 看 workflow yaml
> gh workflow view <name> --yaml
> ```
>
> **Remedy**: `include:`/`exclude:` 精确收敛；用 `max-parallel` 限制并发；非关键组合移到 nightly。
>
> **Prevention**: matrix 维度 ≤ 3；笛卡尔积 ≤ 10。

---

## Secret 在 PR-from-fork 拉不到

> **Symptoms**: 同样 workflow，主仓 PR 能拉 secret，fork PR 拉到空字符串 / undefined。
>
> **Common causes**: GitHub 的安全模型 —— `pull_request` 事件来自 fork 时**不会**注入 secret，防止恶意 PR 偷密钥。
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --log | grep -E "(secret|env).*\\bempty\\b"
> gh api repos/<owner>/<repo>/actions/runs/<id> --jq .event
> # 看 event 是 pull_request 还是 pull_request_target
> ```
>
> **Remedy**: 用 `pull_request_target` 触发**特定**需 secret 的 job（注意：这会用 base branch 的 workflow 文件，需要 reviewer gate）。或者把需 secret 的 job 拆到 `workflow_run` 后续触发，让维护者审过再跑。
>
> **Prevention**: 安全审计：所有 `pull_request_target` workflow 都不应 checkout PR 的代码并直接执行。

---

## Self-hosted runner 不接活

> **Symptoms**: queued job 卡 N 分钟没人接。
>
> **Common causes**:
> 1. Runner 离线（机器关了 / 网断 / 服务挂）
> 2. label 不匹配（workflow 要 `[self-hosted, gpu]`，没机器同时挂这两个 label）
> 3. Runner 在跑别的 job 占满（concurrent ≥ 配置上限）
> 4. ephemeral runner 用完没自动起新的
>
> **Diagnostic**:
> ```bash
> gh api orgs/<org>/actions/runners --jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
> gh api repos/<owner>/<repo>/actions/runners
> # 在 runner 机器上：
> ssh <runner-host> "systemctl status actions.runner.* "
> ssh <runner-host> "tail -50 ~/actions-runner/_diag/Runner_*.log"
> ```
>
> **Remedy**: 重启 runner service；扩容；调整 label 匹配；走 ARC（Actions Runner Controller）做 K8s 弹性。
>
> **Prevention**: ephemeral runner + autoscaler；监控 queued > 5min 告警。

---

## 超时

> **Symptoms**: job 跑到 6h 被 GH 自动 cancel。`The job was canceled because it exceeded the maximum execution time`.
>
> **Common causes**:
> 1. `timeout-minutes` 没设，吃 default 360
> 2. 单测无限 retry / hang 在 IO
> 3. 等外部资源（DB 启动、镜像拉取）
> 4. 死锁（端口竞争 / DB 互斥）
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --log | tail -100
> # 看最后一段在做什么；通常一段时间没新行 = hang
> ```
>
> **Remedy**: 每 job 必加 `timeout-minutes`；测试加 timeout；hanging step 单独 wrap 在 `timeout` shell 命令里。
>
> **Prevention**: org / repo 默认 `timeout-minutes: 30`，特殊需求显式调高。

---

## Reusable workflow / composite action 的 input 传递错

> **Symptoms**: caller 传了 input，callee 拿不到 / 拿到空 / 类型错。
>
> **Common causes**:
> 1. caller 用 `with:`，callee 用 `${{ inputs.X }}`，但 X 名字拼错
> 2. caller 用 `env:`，callee 不在同进程，拿不到
> 3. secret 没用 `secrets: inherit` 传过去
> 4. boolean / number 当 string 处理（YAML 类型推断）
>
> **Diagnostic**:
> ```bash
> gh run view <run-id> --log | grep "::set-output\|::error"
> # 在 callee step 加 `echo '${{ toJson(inputs) }}'` 临时调试
> ```
>
> **Remedy**: 输入用 `inputs.X` + 输出用 `outputs.Y`；secret 用 `secrets:` block；类型在 caller 处 `${{ true }}` 显式声明。
>
> **Prevention**: reusable workflow 写 schema 注释；CI 跑 actionlint。

---

## GitLab CI 特有：rules vs only/except 共存

> **Symptoms**: job 在该跑的时候不跑 / 不该跑的时候跑了。
>
> **Common causes**:
> 1. 同 job 既写 `rules:` 又写 `only:` → 行为不定义（实际：rules 胜出）
> 2. `rules.if` 表达式语法错（`==` 写成 `=`）
> 3. `changes:` 引用了不存在的 path
> 4. workflow rules 与 job rules 冲突
>
> **Diagnostic**:
> ```bash
> glab ci view --pipeline-id <id>
> # 跳到 GitLab UI 的 "CI Lint" 校验工具
> ```
>
> **Remedy**: 项目 wide 选定 `rules` 风格，删干净 `only/except`。
>
> **Prevention**: pre-commit 跑 `gitlab-ci-lint`；新 repo 模板里就用 rules。
