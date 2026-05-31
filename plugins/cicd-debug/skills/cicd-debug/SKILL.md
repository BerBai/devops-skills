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
│                              → 最近 heartbeat（GitLab 支持；GitHub API v1.0 返回 null）
│                              → 队列长度；running_job 仅 repo/project scope 可用
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
python scripts/runner_check.py gh --repo owner/name --json
python scripts/runner_check.py glab --group mygroup
python scripts/runner_check.py glab --project mygroup/myrepo --json

# Secret 审计
python scripts/secret_scope_audit.py gh --repo owner/name
python scripts/secret_scope_audit.py gh --repo owner/name --include-codespaces --json
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
    "run_ref": "12345678",
    "run_meta": {
      "workflow_name": "test.yml",
      "branch": "main",
      "head_sha": "abc123",
      "event": "push",
      "status": "completed",
      "conclusion": "failure",
      "created_at": "2026-05-26T10:00:00Z",
      "url": "https://github.com/owner/repo/actions/runs/12345678"
    },
    "job_states": [
      {
        "name": "test",
        "status": "completed",
        "conclusion": "failure",
        "runner_name": "my-runner",
        "runner_labels": ["self-hosted", "linux"],
        "duration_sec": 240.0,
        "retry_count": 0,
        "steps": [
          {"name": "checkout", "conclusion": "success", "number": 1},
          {"name": "run tests", "conclusion": "failure", "number": 2}
        ]
      }
    ],
    "failed_steps": [
      {
        "job": "test",
        "name": "run tests",
        "exit_code": 1,
        "log_excerpt": "Error: Test failed\n    at test.js:42"
      }
    ],
    "actions_used": [
      {"action": "actions/checkout", "ref": "v4", "used_in": ["test", "lint"]},
      {"action": "actions/setup-python", "ref": "v5", "used_in": ["test"]}
    ],
    "cache_summary": {
      "hits": 2,
      "misses": 1,
      "saves": 0,
      "events": [
        {"job": "test", "key_prefix": "node-cache", "outcome": "hit"},
        {"job": "lint", "key_prefix": "node-cache", "outcome": "hit"},
        {"job": "build", "key_prefix": "build-cache", "outcome": "miss"}
      ]
    },
    "runner_used": [
      {"name": "my-runner", "labels": ["self-hosted", "linux"], "runs_on": ["ubuntu-latest"]}
    ],
    "diff_summary": {
      "reference_run_ref": "12345677",
      "action_version_drift": [
        {"action": "actions/checkout", "from_ref": "v3", "to_ref": "v4"}
      ],
      "env_drift": [],
      "runner_label_drift": [],
      "cache_drift": []
    },
    "summary": "crit",
    "findings": [
      {
        "severity": "crit",
        "kind": "failed_step",
        "job": "test",
        "step": "run tests",
        "exit_code": 1,
        "log_excerpt": "Error: Test failed\n    at test.js:42",
        "hint": "see references/pipeline_failures.md#failed-steps"
      },
      {
        "severity": "warn",
        "kind": "action_version_drift",
        "action": "actions/checkout",
        "from_ref": "v3",
        "to_ref": "v4",
        "hint": "see references/pipeline_failures.md#action-version-drift"
      }
    ],
    "failure_class": null,
    "elapsed_s": 5.2
  }
}
```

`runner_check.py` 的 `data` 负载使用同一 envelope，核心字段是：

```json
{
  "provider": "gh",
  "scope": {"kind": "repo", "value": "owner/name"},
  "runners": [
    {
      "id": "123",
      "name": "linux-runner-1",
      "os": "linux",
      "status": "online",
      "busy": false,
      "ephemeral": false,
      "labels": [{"name": "self-hosted", "type": "read-only"}],
      "last_heartbeat": null,
      "running_job": null,
      "machine_info": {"os": "linux"}
    }
  ],
  "aggregate": {
    "online_count": 1,
    "offline_count": 0,
    "busy_count": 0,
    "total_count": 1,
    "label_coverage": {"self-hosted": 1},
    "queue_depth": 0,
    "queue_p95_age_sec": null,
    "queue_max_age_sec": null
  },
  "summary": "ok",
  "findings": [
    {
      "severity": "info",
      "kind": "last_heartbeat_unavailable",
      "hint": "see references/runner_debug.md#last-heartbeat-unavailable"
    }
  ],
  "failure_class": null,
  "elapsed_s": 0.7
}
```

`secret_scope_audit.py` 的 `data` 负载使用同一 envelope，核心字段是：

```json
{
  "provider": "gh",
  "scope": {"kind": "repo", "value": "owner/repo"},
  "secrets": [
    {
      "name": "AWS_DEPLOY_KEY",
      "scope_kind": "repo",
      "scope_value": "owner/repo",
      "kind": "actions",
      "visibility": null,
      "num_selected_repos": null,
      "masked": null,
      "protected": null,
      "environment_scope": null,
      "updated_at": null,
      "referenced_in_workflows": [
        {"path": ".github/workflows/deploy.yml", "jobs": ["deploy"]}
      ],
      "risk_flags": ["long_lived_cloud_credential", "exposed_on_fork_pr"]
    }
  ],
  "environments": [],
  "workflow_files": [],
  "summary": "warn",
  "findings": [],
  "failure_class": null,
  "elapsed_s": 1.2
}
```

v1.0 risk flags: `long_lived_cloud_credential`, `exposed_on_fork_pr`,
`pull_request_target_unsafe_checkout`, `unused`. v1.1 will add deeper
environment/org-scope cross-checks.

## References

- `references/pipeline_failures.md` —— 常见失败模式：cache miss / env 漂移 / action 升级 / matrix 爆炸 / 超时
- `references/runner_debug.md` —— self-hosted runner 注册、label 选择、并发与 ephemeral runner
- `references/secret_scopes.md` —— GH 的 repo/env/org/dependabot 四层 + PR-fork 安全模型；GitLab 的 protected 变量

## What this skill is *not*

- 不触发 / cancel / approve / rerun —— 写操作
- 不改 workflow YAML —— 设计任务
- 不替代 `act` 本地跑 workflow（v0.3 候选）
- 不监控（订阅事件触发动作）—— 一次性诊断快照
