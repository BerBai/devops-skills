---
name: iac-state
description: Terraform / OpenTofu state diagnostic playbook. Use when the user reports IaC issues — locked state, state drift, missing resources, plan diverges from reality, module validation failures, or unexpected destroy operations. Read-only: inspects state, detects drift, validates modules.
---

# iac-state

Terraform / OpenTofu 的**只读**诊断。不跑 `apply / destroy / import`；那些是写操作。

## When to use

触发场景：
- `state is locked` / `Error acquiring the state lock`
- `terraform plan` 输出意外（无故要 destroy / 大量 in-place update）
- `terraform validate` / module 校验失败
- 怀疑 state 与真实云资源**漂移**（drift）
- module 升级前需要审查
- 多人/多 CI 同时跑 Terraform，state 历史混乱

Skip when:
- 用户要 `terraform apply` / `destroy` / `import` —— 这些是写操作，本 plugin 不做
- 跟云账单、成本相关 —— 不在 v0.2 范围（aws-cost v0.3）
- 跟 Helm / k8s manifest 相关 —— 用 `k8s-debug`

## Decision tree

```
IaC 症状？
│
├─ state 本身的问题（lock / size / 资源缺失）
│   └─ state_inspect.py <host> <project-dir>
│        → 列资源 + size + 是否 locked + remote backend 信息
│
├─ 怀疑漂移（真实云 vs state）
│   └─ drift_check.py <host> <project-dir>
│        → 跑 `terraform plan -detailed-exitcode` 解读差异
│        → 按 resource 类型分组：unmanaged / drifted / orphaned
│
└─ module 本身的健康
    └─ module_validate.py <host> <module-path>
         → fmt + validate + 输入/输出 schema 检查
         → version pin / source 类型审查
         → 已弃用 provider/语法标记
```

## Host model

第一参 `<host>`：
- `local` → `subprocess.run(["terraform", ...])` / `["tofu", ...]`
- 其他 alias → 经 `ssh-core/ssh_execute.py`

CI 场景（在 GH Actions runner 上）通常想本地跑：直接传 `local` 即可。

## Tool autodetect

每个脚本先 `command -v tofu` → 若有用 OpenTofu，否则 fallback 到 `terraform`。可强制 `--tool tofu|terraform`。

## Quick reference

```bash
# state 概览
python scripts/state_inspect.py local ~/infra/prod --json
python scripts/state_inspect.py bastion /srv/infra/staging

# 漂移检测
python scripts/drift_check.py local ~/infra/prod
python scripts/drift_check.py local ~/infra/prod --target aws_instance.web --json

# module 校验
python scripts/module_validate.py local ~/modules/eks-cluster --json
```

## Output contract

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "data": {
    "host": "local",
    "project": "/srv/infra/prod",
    "tool": "terraform",
    "summary": {"state": "ok", "drift": "warn", "modules": "ok"},
    "findings": [
      {"severity": "warn", "kind": "drift",
       "resource": "aws_security_group.web",
       "diff_summary": "ingress rule 0.0.0.0/0:443 missing in state",
       "hint": "see references/drift_patterns.md#unmanaged-rule-changes"}
    ],
    "raw": {"plan_exit_code": 2, "plan_json": "..."}
  }
}
```

## References

- `references/state_health.md` —— state 文件结构、lock 机制、backend 类型对比
- `references/drift_patterns.md` —— 典型漂移模式（手动改云控制台、其他 IaC 工具、生命周期 ignore_changes）
- `references/module_audit.md` —— module 升级前 checklist + 常见反模式

## What this skill is *not*

- 不替你 apply / destroy / import / state rm —— 写操作走 ssh-guarded 或人
- 不做云资源成本估算（用 infracost / terraform-cost-estimation）
- 不做策略合规扫描（用 OPA / Sentinel / Checkov / tfsec）
- 不替你管理 backend（migrate state 是高风险操作）
