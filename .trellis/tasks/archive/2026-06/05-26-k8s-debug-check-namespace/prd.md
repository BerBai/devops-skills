# ship k8s-debug check_namespace v1.0

## Goal

把 `plugins/k8s-debug/skills/k8s-debug/scripts/check_namespace.py`
（当前 v0.2 stub: 57 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/check_namespace.py:26-53`）推到 v1.0：对单个 namespace 并发拉一组
**域级快照**（pods / events / deployments / services / pvcs），每域评分
`ok | warn | crit`，汇总给出 `summary` 与 `top-3 findings`。本脚本是
SKILL.md 决策树（`SKILL.md:28-30`）的**默认入口**——「命名空间整体不健康」
分支的第一步。

复用 `k8s-debug-diagnose-pod` ship 时落地的 `scripts/lib/kubectl.py`，
不重复实现 host 二分执行 + 超时 + JSON 解析。

## User value

- 「production 命名空间是不是 OK」→ 一行命令拿到 5 个域的健康评分 + 前 3
  条最严重 finding：
  ```bash
  check_namespace.py local production --since 30 --json
  ```
  → `{summary: {pods: warn, events: ok, deployments: ok, services: ok,
  pvcs: ok}, findings: [{kind: Pod, name: payment-svc-abc, phase:
  CrashLoopBackOff, hint: "..."}]}`
- 用作上游路由信号：Claude 看到 `pods=crit` → 调 `diagnose_pod` 钻取；
  看到 `deployments=crit` 且 `pods=ok` → 大概率是 rollout 卡住 / replicas
  不齐，提示用户检查 HPA / 镜像 tag。
- 远端跳板机场景同 diagnose_pod，对调用方透明。

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/check_namespace.py:4-7` docstring +
   argparse `:29-36`）：
   ```
   check_namespace.py <host> <namespace> [--since 60] [--json]
   ```
   3 个 flag，本任务直接沿用。

2. **5 个采集域已锁定**（`scripts/check_namespace.py:8-13` docstring）：
   `pods / events / deployments / services / pvcs`。本任务不新增域；
   每域用 1-2 个 `kubectl` 命令拿到，串行或并发都可（design.md 决策）。

3. **Host model + Output contract 与 diagnose_pod 同**（`SKILL.md:45-94`）。
   `data.summary` 是 `{域: ok|warn|crit}` map，`data.findings[]` 是结构化
   信号列表，截到 top-3。`data.raw` 保留每域 kubectl 原始输出。

4. **依赖 `scripts/lib/kubectl.py`**（由 `k8s-debug-diagnose-pod` ship）：
   - 若 diagnose_pod 未先 ship，本 child 实现需要把 lib 抽出作为同 commit
     的一部分（实测顺序由 implement.md 协调，不在 PRD 强制）
   - 本 child 不重复测试 lib 内部

5. **每域评分规则**（待 design.md D-X 锁定，PRD 仅给出方向）：
   - `pods`: `crit` 若 ≥1 CrashLoopBackOff 或 ≥3 Pending/Failed；`warn`
     若有 Pending；否则 `ok`
   - `events`: `crit` 若 last `--since` min 内 ≥1 `Error` reason；`warn`
     若 ≥3 `Warning`；否则 `ok`
   - `deployments`: `crit` 若 ≥1 desired-vs-ready 不齐 ≥10min；`warn` 若
     正在 rollout；否则 `ok`
   - `services`: `crit` 若 ≥1 svc 的 endpoints 为空；否则 `ok`
   - `pvcs`: `crit` 若 ≥1 Pending；否则 `ok`

6. **Failure_class 命名空间（本 child 独占 90-99，最终在 design.md D-X 锁定）**：
   - `90 namespace_not_found` —— `kubectl get ns <ns>` NotFound
   - `91 partial_collection` —— 5 域里 ≥1 域采集失败（warning 级，exit 0 +
     `data.partial_failures: [域]`；超过 3 域失败升 exit 91）
   - `92-99` 预留扩展（host_unreachable / kubectl_missing / forbidden 等
     直接复用 `lib/kubectl.py` 抛出的 80-86 code，不在 90-99 重复占用）

7. **测试基线**：`tests/test_check_namespace_unit.py`（≥30 unit，覆盖 5 域
   评分规则 × 3 档 + 3 个 failure_class）+ `tests/
   test_check_namespace_integration.py -m live_k8s`（≥2 PASS：healthy ns +
   degraded ns 同集群 fixture）。

## Out of scope

- 写操作（patch / scale / delete）—— 归 `ssh-guarded`
- **跨 namespace 关联**（A 命名空间影响 B）—— 归 `cluster_health`
- 单 Pod 钻取 —— 归 `diagnose_pod`
- ConfigMap / Secret / RBAC 配置审计 —— v1.1（独立 child task）
- 长时间窗（`--since` > 1 day）—— v1.0 上限默认 60 min，超过加 warning

## Constraints

- **依赖 `lib/kubectl.py`**：若 diagnose_pod ship 在前，直接 import；
  否则与 diagnose_pod child 协调 lib 抽取时机（不重复实现）
- **失败 class 80-86 透传**：`lib/kubectl.py` 抛出的 host_unreachable /
  kubectl_missing / kubeconfig_missing / pod_not_found / namespace_not_found /
  forbidden / api_timeout 直接以原 exit code 退出，本脚本只新增 91
  `partial_collection`（90 与 lib 的 84 `namespace_not_found` 等价 —— 实际
  本 child 不需要 90，直接复用 84 即可，最终 design 再定）
- **`top-3 findings` 截断规则**：严重度排序 `crit > warn > ok`，同级按
  `kind` 字典序，避免随机性
- **JSON envelope** 与 diagnose_pod 一致

## Acceptance Criteria

- [ ] 5 域采集全部实现，每域评分规则在 design.md D-X 明文锁定
- [ ] `tests/test_check_namespace_unit.py` ≥ 30 PASS
- [ ] `tests/test_check_namespace_integration.py -m live_k8s` ≥ 2 PASS（healthy
      ns + degraded ns）
- [ ] `top-3 findings` 排序稳定（同输入产同输出）
- [ ] `--since` 默认 60 min，上限提示在 SKILL.md Quick reference 注明
- [ ] 复用 `lib/kubectl.py`，不引入第二份 host 二分逻辑
- [ ] `.trellis/spec/backend/error-handling.md` 追加 90-99 段（若实际只用 91
      则记录「90/92-99 预留未使用」）
- [ ] SKILL.md decision tree 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_k8s` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`
- 同批 sibling：`k8s-debug-diagnose-pod`（foundation，建议先 ship 给本 child
  提供 lib）、`k8s-debug-cluster-health`、`k8s-debug-helm-status`
- 推荐 ship 顺序：diagnose_pod → check_namespace → cluster_health →
  helm_status（按 SKILL.md decision tree 自下而上 + lib 依赖方向）

## Notes

- 2026-05-26 PRD 初稿。5 域评分阈值方向性给出，最终数值在 design.md D-X
  锁定，避免 PRD 与实现双写漂移。
- 90 与 lib 抛出的 84 `namespace_not_found` 语义重叠 —— PRD 阶段先两都登记，
  design 阶段二选一，error-handling.md 同步记录决议。
