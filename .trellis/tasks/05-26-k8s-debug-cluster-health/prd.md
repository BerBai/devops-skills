# ship k8s-debug cluster_health v1.0

## Goal

把 `plugins/k8s-debug/skills/k8s-debug/scripts/cluster_health.py`
（当前 v0.2 stub: 56 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/cluster_health.py:26-52`）推到 v1.0：对**整集群**拉一组健康快照
——nodes / node_conditions / control_plane / kube_system pods / kube-system
events / versions——综合评分 `ok | warn | crit` 给出 `top findings`。本脚本
定位为 SKILL.md 决策树（`SKILL.md:36-38`）「集群级问题」分支的**唯一入口**，
也是「钻 namespace 之前先看集群」的常用前置（脚本 docstring
`cluster_health.py:17` 明示「**first** check before drilling into a namespace」）。

复用 `k8s-debug-diagnose-pod` ship 时落地的 `scripts/lib/kubectl.py`。

## User value

- 「集群是不是出问题了」→ 一条命令同时看节点 + 控制面 + kube-system：
  ```bash
  cluster_health.py local --since 30 --json
  ```
  → `{summary: {nodes: warn, control_plane: ok, kube_system: ok,
  events: warn}, findings: [{kind: Node, name: worker-3, condition:
  DiskPressure, hint: "..."}, {kind: Event, reason: NodeNotReady, ...}]}`
- 上游路由信号：Claude 看到 `nodes=crit` → 提示用户先修节点；看到
  `kube_system=crit` 且 nodes=ok → 控制面 / addon 问题。
- 排除「这是集群问题还是应用问题」的常见判断：跑一次 cluster_health 全绿，
  再去 check_namespace 钻应用 —— 避免在应用层徒劳排查 1 小时然后发现是节点
  内存压力。

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/cluster_health.py:4` docstring + argparse
   `:29-35`）：
   ```
   cluster_health.py <host> [--json] [--since 30]
   ```
   只有 `--since` 与 `--json` 2 个 flag，本任务直接沿用。

2. **7 个采集字段已锁定**（`scripts/cluster_health.py:7-15` docstring）：
   `nodes / node_conditions / control_plane / kube_system / events /
   versions / summary`。本任务不新增字段。

3. **Control-plane 探测的双路径已锁定**（同 docstring 第 9-11 行）：
   - 自托管集群：`kubectl -n kube-system get pods -l component=kube-apiserver`
     等
   - 托管集群（EKS / GKE / AKS）：control-plane pod 不可见，退化到
     `kubectl get --raw=/healthz` + `/readyz`
   - 双路径都失败 → finding `severity: crit` + hint 指向「托管集群无法直接
     看 control-plane」references 锚点（v1.0 在 ship 时同步补一条到
     `references/common_issues.md`，新加节，**不**在 PRD 预占文案）

4. **节点 4 个 condition 已锁定**（业内标准 + docstring 第 8 行）：
   `Ready / DiskPressure / MemoryPressure / PIDPressure / NetworkUnavailable`。
   `Ready != True` → `crit`；任一 pressure `= True` → `warn`；
   `SchedulingDisabled` → `warn`（来自 `kubectl cordon`）。

5. **Versions 字段语义**：
   - `kubectl version --short` 取 client + server 版本
   - 每节点 kubelet 版本：`kubectl get nodes -o jsonpath='{...
     .status.nodeInfo.kubeletVersion}'`
   - 集群内多版本 skew > 1 minor → `warn`（K8s 官方支持 ±1 minor）

6. **`--since` 仅作用于 events 字段**（docstring `:13` 明示「kube-system
   Warning+ events in last <since> min」）。默认 30 min。

7. **Host model + Output contract** 同 diagnose_pod / check_namespace
   （`SKILL.md:45-94`）。

8. **Failure_class 命名空间（本 child 独占 100-109，最终在 design.md D-X
   锁定）**：
   - `100 cluster_unreachable` —— `kubectl get nodes` 直接连不上 API
     server（区别于 lib 的 80 host_unreachable —— 100 是 K8s API 层）
   - `101 healthz_unavailable` —— `/healthz` 与 `/readyz` 都拿不到，
     control_plane 字段空（warning 级，exit 0 + warning finding）
   - `102 metrics_partial` —— kubelet 版本采集失败但 nodes 列表 OK
     （warning，exit 0）
   - `103-109` 预留扩展
   - 80-86 透传同 check_namespace

9. **测试基线**：`tests/test_cluster_health_unit.py`（≥35 unit，覆盖 7 字段
   + 4 condition × 3 档 + 3 failure_class）+ `tests/
   test_cluster_health_integration.py -m live_k8s`（≥2 PASS：healthy
   集群 + 含 DiskPressure 节点的 fixture）。

## Out of scope

- **节点 OS 层诊断**（CPU/load/disk/network）—— 归 `remote-debug/diagnose_host`，
  本脚本只看 K8s 层 condition
- **集群 install / upgrade 建议** —— 不做
- **多集群对比** —— 本 child 只看一个集群（`<host>` 对应一个 KUBECONFIG
  context）；多集群对比走 `remote-debug/compare_across_hosts` 模式（v1.1
  扩展）
- **etcd 内部健康指标**（如 raft leader、storage size）—— 仅看 `etcd` pod
  Ready，深度指标走专用工具

## Constraints

- **托管集群 control-plane 退化路径必须实现**：很多用户跑在 EKS/GKE，
  pod-level 探测会全空 —— 必须自动 fallback 到 /healthz，不能直接报错
- **`--since` 仅作用 events**：其他字段（nodes / control_plane）都是
  current state，不受时间窗影响
- **`raw` 字段 10 MB 上限**（kube-system 大集群 events 可能很大）：超出
  截断 + marker
- **复用 `lib/kubectl.py`**：不引入 raw API 路径（`/healthz` 也走 `kubectl
  get --raw=/healthz`，不直接 HTTP）
- **不写本机 KUBECONFIG**：只读，永远不改 context

## Acceptance Criteria

- [ ] 7 字段全部采集，托管集群路径软失败 + finding 注明
- [ ] 节点 4 condition 评分规则在 design.md D-X 明文锁定
- [ ] `tests/test_cluster_health_unit.py` ≥ 35 PASS
- [ ] `tests/test_cluster_health_integration.py -m live_k8s` ≥ 2 PASS
- [ ] 托管集群 fixture（mock /healthz 返回 ok + pod 列表空）覆盖到
- [ ] `--since` 仅影响 events 字段（unit test 显式断言）
- [ ] `.trellis/spec/backend/error-handling.md` 追加 100-109 段
- [ ] SKILL.md decision tree 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_k8s` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`
- 同批 sibling：`k8s-debug-diagnose-pod`（foundation）、`k8s-debug-check-namespace`、
  `k8s-debug-helm-status`
- 推荐 ship 顺序：diagnose_pod → check_namespace → cluster_health → helm_status

## Notes

- 2026-05-26 PRD 初稿。托管集群路径是关键决策点 —— 不实现就会在 EKS/GKE
  用户那里直接 broken；实现的代价是引入第二条采集路径（`/healthz`），
  design.md 要 trade-off。
- 节点 OS 层诊断与本脚本边界：本脚本看「K8s 给节点打的标签」，OS 层
  「真实指标」走 remote-debug；二者互补，对应不同的诊断深度。
