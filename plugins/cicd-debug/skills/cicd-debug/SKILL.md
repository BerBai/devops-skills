---
name: cicd-debug
description: CI/CD pipeline diagnostic playbook for GitHub Actions and GitLab CI. Use when the user reports pipeline failures — failing jobs, stuck queues, missing secrets, runner registration issues, cache misses, matrix explosion, flaky tests, slow builds. Read-only: analyzes pipeline runs, runner status, secret scopes; does NOT trigger reruns or modify workflows.
---

# cicd-debug

CI/CD 流水线**只读**诊断。覆盖 GitHub Actions 与 GitLab CI 两大主流。不替你 rerun / cancel / push 修复 —— 那些是写操作，可用 `gh` / `glab` 人工执行。

## When to use

触发场景：
- "CI 挂了" / "pipeline failed" / "job 卡 queue 不动"
- "GitHub Actions runner offline" / "self-hosted runner 没注册上"
- "缓存没命中，构建慢死" / "matrix 跑了 200 个 job"
- "secret 在 PR fork 拉不到" / "环境变量没传给 reusable workflow"
- "上周还好好的，今天突然 fail" → bisect 哪个 commit / 哪个 action 升级搞的鬼

Skip when:
- 用户要 rerun / cancel / 触发新 pipeline → 用 `gh` / `glab` 人工或走 `ssh-guarded`
- 问题是应用本身（test 失败、build error） → 把 log 给 Claude 直接分析就行
- 想做 CI 性能优化 / 重新设计 pipeline → 这是设计任务，超出诊断 plugin

## Decision tree

```
CI/CD 症状？
│
├─ 某次 run 失败 / 行为意外 ── pipeline_analyzer.py <provider> <run-id>
│                              → 全 job 状态 + 关键错误行
│                              → 与上一次成功 run 的 diff
│                              → 标记可疑 step（action 升级 / cache miss / env 变化）
│
├─ Runner 不工作 ─────────── runner_check.py <provider>
│                              → 在线 runner 数、忙闲、label 覆盖
│                              → self-hosted runner 最近 heartbeat
│                              → 队列长度（GitLab）/ pending workflow 数（GH）
│
└─ Secret / 环境作用域困惑 ── secret_scope_audit.py <provider> <repo>
                                → 每个 secret 的可见性（repo / env / org）
                                → 哪些 workflow 引用了它
                                → PR-from-fork 场景的 secret 暴露面
```

## Providers

第一参 `<provider>` 取值：
- `gh` —— GitHub Actions（走 `gh api` 命令，要 `gh auth login` 完成）
- `glab` —— GitLab CI（走 `glab api` 命令，要 `glab auth login`）

我们用官方 CLI 而非裸 REST，少写认证代码。

## Quick reference

```bash
# 分析单次 GitHub Actions run
python scripts/pipeline_analyzer.py gh 12345678 --json
python scripts/pipeline_analyzer.py gh 12345678 --diff-against last-success

# 分析 GitLab pipeline
python scripts/pipeline_analyzer.py glab 567890 --project mygroup/myrepo

# Runner 健康
python scripts/runner_check.py gh --org myorg --json
python scripts/runner_check.py glab --group mygroup

# Secret 审计
python scripts/secret_scope_audit.py gh --repo owner/name
python scripts/secret_scope_audit.py glab --project mygroup/myrepo
```

## Output contract

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "<human-readable>",
  "stderr": "",
  "data": {
    "provider": "gh",
    "summary": {"jobs": "warn", "runners": "ok", "diff_vs_last_success": "crit"},
    "findings": [
      {"severity": "crit", "kind": "action_version_drift",
       "job": "test", "step": "actions/checkout",
       "from": "v3", "to": "v4",
       "hint": "see references/pipeline_failures.md#action-version-drift"}
    ],
    "raw": {"run_id": 12345678, "workflow": ".github/workflows/test.yml"}
  }
}
```

## References

- `references/pipeline_failures.md` —— 常见失败模式：cache miss / env 漂移 / action 升级 / matrix 爆炸 / 超时
- `references/runner_debug.md` —— self-hosted runner 注册、label 选择、并发与 ephemeral runner
- `references/secret_scopes.md` —— GH 的 repo/env/org/dependabot 四层 + PR-fork 安全模型；GitLab 的 protected 变量

## What this skill is *not*

- 不触发 / cancel / approve / rerun —— 写操作
- 不改 workflow YAML —— 设计任务
- 不替代 `act` 本地跑 workflow（v0.3 候选）
- 不监控（订阅事件触发动作）—— 一次性诊断快照
