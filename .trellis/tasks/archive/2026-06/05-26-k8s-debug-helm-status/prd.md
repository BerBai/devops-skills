# ship k8s-debug helm_status v1.0

## Goal

把 `plugins/k8s-debug/skills/k8s-debug/scripts/helm_status.py`
（当前 v0.2 stub: 61 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/helm_status.py:28-57`）推到 v1.0：两种调用形态——

1. **Per-release**: `<host> <namespace> <release>` → `status / history(last 5)
   / associated_jobs / stuck_resources / summary`
2. **Cluster-wide pending scan**: `<host> --list-pending` → 列出所有 namespace
   中处于 `pending-install / pending-upgrade / pending-rollback` 的 release

对应 `references/common_issues.md:170-191`「Helm release stuck pending-*」
小节描述的诊断需求。

依赖远端 host 已安装 `helm` CLI（无 Python helm SDK 可走，全部 shell out）；
复用 `k8s-debug-diagnose-pod` ship 时落地的 `scripts/lib/kubectl.py` 做
关联 K8s 资源查询（如 helm hook jobs）。

## User value

- 「production 的 stripe-checker release 是不是出问题了」→
  ```bash
  helm_status.py local production stripe-checker --json
  ```
  → 一次拿到 status + history + 卡住的 hook job + 推荐 next step
  （rollback 还是 uninstall）。
- 「整集群有几个 release 卡 pending」→
  ```bash
  helm_status.py bastion-prod --list-pending --json
  ```
  → 列出所有 pending-* release，常见于上一次 `helm upgrade` 被 CI runner 中断、
  网络抖动后没人清理。
- 上游路由：Claude 看到 `status=pending-upgrade` 且 `associated_jobs` 有 stuck
  → 直接走 `references/common_issues.md#helm-release-stuck-pending-upgrade--pending-rollback`
  指导用户 `kubectl delete job <stuck>` + `helm rollback`。

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/helm_status.py:4-8` docstring +
   argparse `:28-36`）：
   ```
   helm_status.py <host> <namespace> <release> [--json]
   helm_status.py <host> --list-pending [--json]
   ```
   v0.2 stub 已经实现了「`namespace` 与 `release` nargs='?'，与 `--list-pending`
   互斥」的 argparse 校验（`:42-44`）—— 本任务沿用这个 surface 与 mutual
   exclusion 逻辑。

2. **8 个 release 状态值已锁定**（`scripts/helm_status.py:11-12` docstring）：
   `deployed | failed | pending-install | pending-upgrade | pending-rollback |
   superseded | uninstalling | uninstalled`。本任务**不新增** status，
   也**不重写** common_issues.md 的 Helm 节。

3. **Per-release 4 个输出字段已锁定**（`scripts/helm_status.py:13-16`
   docstring）：
   - `status`: 8 值之一
   - `history`: 最近 5 个 revision + status + age
   - `associated_jobs`: helm hook jobs，卡住 / 失败的
   - `stuck_resources`: helm 认为自己拥有但被外部改过的资源

4. **数据采集命令明确**：
   - `helm -n <ns> status <release> -o json` —— 拿 status + first_deployed
     + last_deployed + revision
   - `helm -n <ns> history <release> -o json` —— 拿 revision history（取最近 5）
   - `kubectl -n <ns> get jobs -l app.kubernetes.io/instance=<release>` ——
     拿 hook jobs（参 common_issues.md 第 184 行）
   - `helm list --all-namespaces --pending -o json` —— 拿 cluster-wide
     pending（`--list-pending` 路径）

5. **Stuck resources 判定**（无 helm 直接 API；通过对比 release manifest
   与实际 cluster state 判定）：
   - v1.0 简化策略：仅检测 hook job 卡 Running >5min 或 Failed，作为
     `associated_jobs.stuck`
   - 真正的「外部 drift 检测」（CRD 字段对比）→ v1.1，归 iac-state 范畴

6. **Host model + Output contract** 同其他 3 个 sibling。`<host> == local`
   直接 shell out `helm`；其他 alias 经 `ssh-core/ssh_execute.py
   <alias> "helm ..."`。

7. **Failure_class 命名空间（本 child 独占 110-119，最终在 design.md D-X
   锁定）**：
   - `110 helm_missing` —— 本机或远端找不到 `helm` 二进制
   - `111 release_not_found` —— `helm status` 返回 NotFound
   - `112 helm_version_unsupported` —— helm 2.x 或 < 3.0（v1.0 只支持 helm 3+；
     2.x tiller 模型差太多）
   - `113 history_truncated` —— release 没有 history（exit 0 + warning）
   - `114 pending_scan_partial` —— `--list-pending` 时某些 namespace
     forbidden（warning，仍返回能拿到的 release）
   - `115-119` 预留扩展
   - 80-86（lib/kubectl 透传，用于 jobs 查询）共存

8. **测试基线**：`tests/test_helm_status_unit.py`（≥30 unit，覆盖 8 status
   + per-release/list-pending 双路径 + 4 failure_class）+ `tests/
   test_helm_status_integration.py -m live_k8s`（≥2 PASS：deployed
   release + pending-upgrade fixture release —— 后者用 `helm install` 后
   立即 `kill -9` helm 进程模拟中断，是父任务规划 fixture 决议
   `kind + helm CLI` 的具体实现路径）。

## Out of scope

- **Helm chart 静态分析**（template 解析 / values schema 校验） —— 归 iac-state
- **Helm 2.x 支持** —— v1.0 只支持 3+，2.x 报 112
- **Release 写操作**（install / upgrade / rollback / uninstall）—— 归
  `ssh-guarded`；本脚本只读
- **外部 drift 真检测**（diff release manifest vs cluster state）—— v1.1
- **`helm repo` 管理** —— 不在本脚本范围
- **OCI registry credential 配置** —— 用户自己 helm 侧配置

## Constraints

- **需 helm 3+**：远端没装直接 110；版本 < 3.0 报 112，提示用户升级
- **JSON 解析必须健壮**：helm 3 的 `-o json` 字段 schema 在不同 minor
  版本有微差（如 3.6 vs 3.13），design.md 要列实际遇到的字段差异
- **`--list-pending` 性能**：大集群（>1000 release）单次 `helm list
  --all-namespaces` 可能很慢；v1.0 默认串行，超时上限 60s（exit 124）；
  并发分 namespace 是 v1.1 优化
- **失败 class 110-119 独占**，与 lib/kubectl 80-86 共存（jobs 查询走 lib）
- **JSON envelope** 与其他 sibling 一致

## Acceptance Criteria

- [ ] 两种调用形态（per-release / `--list-pending`）全部实现
- [ ] 8 个 status 值全部能解析并归一化
- [ ] `history` 取最近 5 个 revision，少于 5 不报错
- [ ] `associated_jobs.stuck` 判定规则：Running >5min 或 status=Failed
- [ ] `tests/test_helm_status_unit.py` ≥ 30 PASS
- [ ] `tests/test_helm_status_integration.py -m live_k8s` ≥ 2 PASS
- [ ] pending-upgrade fixture 通过「helm install + kill helm」模拟（或等效
      方式）实现
- [ ] `.trellis/spec/backend/error-handling.md` 追加 110-119 段
- [ ] SKILL.md decision tree 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_k8s` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`
- 同批 sibling：`k8s-debug-diagnose-pod`（foundation）、`k8s-debug-check-namespace`、
  `k8s-debug-cluster-health`
- 推荐 ship 顺序：diagnose_pod → check_namespace → cluster_health →
  **helm_status** （helm CLI 依赖最重，留最后）

## Notes

- 2026-05-26 PRD 初稿。helm 3.x JSON schema 跨版本差异是已知坑（社区有
  多个 issue），design.md 阶段要在 kind+helm 3.13 实测后列出实际字段，
  避免 v1.0 在 3.6 用户那里 schema 不兼容。
- v1.0 stuck 检测降级到「仅 hook jobs」是有意为之 —— 真正的 drift 检测
  需要重做 helm 内部状态机，超出 v1.0 体量；用户跑完本脚本若 stuck=空但
  release 仍有问题，建议手动 `helm diff` 或走 iac-state（v1.1 联动）。
