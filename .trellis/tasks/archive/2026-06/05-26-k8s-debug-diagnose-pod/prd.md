# ship k8s-debug diagnose_pod v1.0

## Goal

把 `plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py`
（当前 v0.2 stub: 63 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/diagnose_pod.py:29-59`）推到 v1.0：对单个 Pod 并发拉 `kubectl
describe / logs(current + previous) / events / top`，把结果归一化为一组
**已分类的 phase**（Pending / ContainerCreating / Running-NotReady /
Running-Restarting / Running-Healthy / Terminating / CrashLoopBackOff /
ImagePullBackOff / OOMKilled —— 9 类，stub docstring 已锁定），每条 finding
附 `hint` 字段指向 `references/{common_issues,pod_lifecycle}.md` 的对应锚点
供 LLM 跟读。

本任务同时**首次**在 k8s-debug plugin 落 kubectl 包装 lib（`scripts/lib/
kubectl.py`），把 `local` 与 `ssh-core ssh_execute.py <alias>` 两种执行
模型抽到一个签名后，供 check_namespace / cluster_health / helm_status
后续 3 个 child 复用。

## User value

- 「这个 Pod 为什么起不来」→ 一行命令拿到 phase + 关键 Events + 上一轮死亡
  日志：
  ```bash
  diagnose_pod.py local production payment-svc-abc123 --previous
  ```
  返回结构化 `{phase: "CrashLoopBackOff", last_termination: "OOMKilled",
  hint: "references/common_issues.md#oomkilled"}`，Claude 不需要再 round-trip
  跑第二条命令。
- CrashLoopBackOff 自动跑 `kubectl logs --previous`，省掉「忘记加 --previous
  → 看了一堆新进程刚启动的日志没看到崩溃原因」的常见坑（见
  `references/common_issues.md:23` 显式提醒）。
- OOMKilled 自动从 `Last State` 提取 `Exit Code: 137` 和 `Reason:
  OOMKilled`，归一化成单一 `phase` 字符串，避免依赖人去解析 `describe` 文本。
- 远端跳板机场景：本机没有 `KUBECONFIG`，跳板机有 —— `--host bastion-prod`
  自动走 ssh-core，对调用方透明。

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/diagnose_pod.py:4-7` docstring + argparse
   `:32-42`）：
   ```
   diagnose_pod.py <host> <namespace> <pod> [--container NAME]
                   [--json] [--logs-tail 200] [--previous]
   ```
   v0.2 stub 已经把 7 个 flag 接上，本任务直接沿用，不重新设计 surface。

2. **9 个 phase 分类已锁定**（`scripts/diagnose_pod.py:14-17` docstring）：
   `Pending | ContainerCreating | Running-NotReady | Running-Restarting |
   Running-Healthy | Terminating | CrashLoopBackOff | ImagePullBackOff |
   OOMKilled`。所有 9 类都在 `references/pod_lifecycle.md`（阶段 1-4 +
   common_issues.md OOMKilled 节）有处理建议。本任务**不新增** phase，
   也**不重写** references。

3. **Host model 已锁定**（`SKILL.md:45-51`）：
   - `local` → `subprocess.run(["kubectl", ...])`，用本机 `KUBECONFIG`
   - 其他 alias → `subprocess.run([ssh-core/ssh_execute.py, alias,
     "kubectl ..."])`
   本任务把这条二分逻辑落进 `scripts/lib/kubectl.py`，签名约为
   `run_kubectl(host, namespace, args, json=False, timeout=30) -> CompletedProcess`。

4. **Output contract 已锁定**（`SKILL.md:71-94`）：
   ```json
   {"success": true, "exit_code": 0, "stdout": "...", "stderr": "",
    "data": {"host": "...", "namespace": "...", "pod": "...",
             "phase": "CrashLoopBackOff",
             "summary": {"pods": "warn", ...},
             "findings": [
               {"severity": "warn", "kind": "Pod", "name": "...",
                "phase": "CrashLoopBackOff", "restart_count": 12,
                "hint": "references/common_issues.md#oomkilled"}
             ],
             "raw": {"describe": "...", "logs": "...", "events": [...]}}}
   ```
   `data.raw` 保留原始 kubectl 输出（截断到 10 MB 上限，参 `SKILL.md` Safety
   baseline）供 LLM 自由解析；`data.findings` 是结构化诊断信号供调用方
   直接 branch。

5. **Parent PRD 已为 k8s-debug 4 个 child 预留 exit code 区间 80-119**
   （父 PRD Phase 2+ 表 + 已有 5 个先例：ssh_config_manager 4-8、
   deploy_pubkey 10-15、ssh_server_transfer 20-29、ssh_tunnel 30-39、
   log-aggregator 40-69）。本 child 占 **80-89** 共 10 个 code。
   `data.failure_class` 在所有 80-89 出口必填。

6. **Failure_class 命名空间（本 child 独占 80-89，最终在 design.md D-X 锁定）**：
   - `80 host_unreachable` —— ssh-core 远端不可达
   - `81 kubectl_missing` —— 本机或远端找不到 `kubectl`
   - `82 kubeconfig_missing` —— `KUBECONFIG` 未设 / 文件不在
   - `83 pod_not_found` —— `kubectl get pod` 返回 NotFound
   - `84 namespace_not_found` —— namespace 不存在
   - `85 forbidden` —— RBAC 拒绝（`kubectl auth can-i` 显式判断）
   - `86 api_timeout` —— `kubectl` 自身超时（区别于 124 Python-side timeout）
   - `87 metrics_unavailable` —— metrics-server 没装 → `top pod` 软失败
     （warning 级，不影响 phase 分类，exit 0 + warning finding）
   - `88-89` 预留扩展

7. **测试基线**：与 ssh-core 5 个已 ship 脚本一致 ——
   `tests/test_diagnose_pod_unit.py`（≥40 unit，覆盖 9 个 phase 分类 + 80-87
   每个 failure_class）+ `tests/test_diagnose_pod_integration.py`
   （`-m live_k8s` 新 marker，至少 3 个：CrashLoop / Healthy / Pending）。
   live_k8s fixture 走 **kind 本地集群 + helm CLI**（父任务规划决议）。

8. **kubectl wrapper lib（`scripts/lib/kubectl.py`）是本 child 的副产物**：
   - 签名约定见 R3
   - 抛 `KubectlError(failure_class, exit_code, stderr)`，对应 80-86
   - 覆盖 `host=local` 与 `host=<alias>` 两条路径
   - 本任务自带的 unit test 覆盖到 lib；后续 3 个 child 复用时**不重复测试
     lib 内部**，只测自己脚本的归一化逻辑

## Out of scope

明确 v1.1+（写进 PRD 而非埋雷）：

- **写操作**：restart Pod / delete Pod / scale Deployment —— 一律不做，
  归 `ssh-guarded` 提交 request 流程
- **持续 watch / 事件流订阅**：本脚本是 one-shot snapshot；live 流走
  `log-aggregator/tail_multi.py` 的 `kube://` source
- **跨 Pod 关联诊断**（"为什么 web-1 死了 web-2 没死"）：属于
  `check_namespace` 视角
- **metrics-server 不可用时的 top 替代**（如 `cAdvisor` 直接 query）：v1.1
  考虑，v1.0 软失败（finding warning + 不阻塞）
- **应用层根因**（"为什么 Java 进程 segfault"）：本脚本只给 K8s 层信号，
  应用层留给用户用 `kubectl exec` + 业务 log

## Constraints

- **ADR-001 D1**：subprocess + 原生 ssh，**不** `import paramiko`；远端
  调用一律经 `ssh-core/ssh_execute.py`（lib 内部封装）
- **JSON envelope** 沿用全仓契约 `{success, exit_code, stdout, stderr, data,
  error?}`（`.trellis/spec/backend/error-handling.md:8-29`）
- **每条 finding 必带 `hint`**：string 形如 `references/common_issues.md#
  <anchor>`，LLM 跟读机制依赖此字段。若 phase 在 references 中没有对应小节
  → ship 阶段同步追加（不在本 PRD 中提前列出，避免双写漂移）
- **不重写 references/**：发现实现行为与 references 描述偏差，在 design.md
  D-X 节记录 + ship commit 同步修订
- **kubectl 子进程 10 MB 输出上限**（参 ssh-core Safety baseline），超出
  截断并加 marker `... <truncated 10MB cap>`
- **超时双轨**：`kubectl` 自身超时 (`--request-timeout=30s`) → exit 86
  `api_timeout`；Python `subprocess.run(timeout=)` 超时 → exit 124（GNU
  timeout 惯例，对齐 ssh_execute）
- **failure_class 80-89 namespace 独占本 child**；ship commit 同步追加到
  `.trellis/spec/backend/error-handling.md`

## Acceptance Criteria

- [ ] `diagnose_pod.py` 实现完整 9 phase 分类，unit test 每类至少 1 fixture
- [ ] `scripts/lib/kubectl.py` 落地，签名 `run_kubectl(host, namespace, args,
      json=False, timeout=30)`，覆盖 local + alias 两条路径
- [ ] `tests/test_diagnose_pod_unit.py` ≥ 40 PASS，覆盖 9 phase + 8 failure_class
- [ ] `tests/test_diagnose_pod_integration.py -m live_k8s` ≥ 3 PASS（kind
      集群 + CrashLoop / Healthy / Pending 三种 fixture Pod）
- [ ] `--previous` 在 CrashLoop 场景被自动调用（实测验证：fixture Pod 重启
      ≥1 次时，`data.raw.previous_logs` 非空）
- [ ] JSON envelope 通过 `tests/util/json_envelope.py` schema 校验
- [ ] `.trellis/spec/backend/error-handling.md` 追加 80-89 段（含 8 个
      failure_class 字符串 + 含义 + 引用本 task PRD R6）
- [ ] SKILL.md decision tree（`SKILL.md:23-43`）与实现保持一致，发现偏差
      在 ship commit 同步修订
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_k8s` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`（Phase 3 提前规划，phase 排期
  保持不变 —— 见父 PRD Notes）
- 同批 sibling：`k8s-debug-helm-status`、`k8s-debug-cluster-health`、
  `k8s-debug-check-namespace`
- 本 child 是 k8s-debug Phase 3 的 **foundation**（产出 kubectl lib），
  建议**先于其他 3 个 sibling ship**

## Notes

- 2026-05-26 PRD 初稿。kubectl lib 抽取是新增决策（stub docstring 未明示），
  动机：4 个 k8s-debug 脚本都要 local/alias 二分执行 + JSON 解析 + 超时
  双轨；不抽 lib 会出现 4 份近似但微妙不同的复制粘贴。复用先例：
  ssh-core `lib/sftp_transport.py`（ssh_upload/ssh_download/ssh_server_transfer
  三脚本共享）。
- live_k8s fixture 走 kind 是父任务规划决议；具体 fixture 实现（kind 集群
  spin-up 速度、缓存策略、CI 兼容性）留到 design.md / implement.md 决定。
