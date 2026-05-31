# Design — k8s-debug diagnose_pod v1.0

> 配套 PRD: `prd.md`。
> 实施清单: `implement.md`。
> 本任务**首次**在 k8s-debug plugin 落 `scripts/lib/`，所有决策为后续 3 个
> sibling（check_namespace / cluster_health / helm_status）共用。

---

## 1. Architecture overview

```
diagnose_pod.py  (CLI, ~180 loc)
└── lib/kubectl.py          ← 本任务新增；后续 3 个 sibling 复用
│    ├── run_kubectl(host, namespace, args, *, json=False, timeout=30)
│    │     → KubectlResult{stdout, stderr, returncode, elapsed_s,
│    │                     failure_class, route: "local"|"remote"}
│    ├── class KubectlError(Exception)  # carries failure_class + exit_code
│    └── _classify_kubectl_error(returncode, stderr) -> str | None
└── lib/__init__.py        ← 本任务新增；复制 ssh-core lib 的 4 个核心 helper
     ├── json_result(...)          (copied from ssh-core lib)
     ├── emit(...)                  (copied from ssh-core lib)
     ├── msys_safe_env(...)         (copied from ssh-core lib)
     └── unimplemented(...)         (copied from ssh-core lib, retained for stubs)
```

数据流：

```
argv → argparse → run_kubectl(describe) ─┐
                  run_kubectl(logs)      ├─→ classify_phase()
                  run_kubectl(events)    │     ↓
                  run_kubectl(top)       │   9 phase enum
                                         │     ↓
                                         └─→ build findings + hint
                                              ↓
                                            json_result envelope
                                              ↓
                                            emit(as_json=args.json)
```

4 个 kubectl 子调用通过 `concurrent.futures.ThreadPoolExecutor(max_workers=4)`
并发执行（每个调用本身是阻塞 subprocess，线程化的目的是把
4×network-bound RTT 并行起来，wall-clock 从 ~4s 降到 ~1s）。

---

## 2. Design decisions

### D1. `scripts/lib/__init__.py` 复用策略

**决定**: k8s-debug plugin 维护**自己的** `scripts/lib/__init__.py`，从
`plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py` **复制粘贴**
4 个核心 helper（`json_result` / `emit` / `msys_safe_env` /
`unimplemented`），不做跨 plugin import。

**Rationale**:
- 现有先例：ssh-guarded、log-aggregator 都维护各自的 `scripts/lib/`，
  没有跨 plugin import。每个 plugin 的 `scripts/` 是独立可分发的单元
  （SKILL.md + scripts/ + references/），跨 plugin 的 sys.path 把戏
  在用户拷贝 plugin 到别处时会断。
- 4 个 helper 共计 ~50 LOC、零业务逻辑、API 稳定（自 ssh_execute v1.0
  shipping 至今未变），复制成本极低。
- 真要抽到仓库级共享 lib 是另一个 ADR 的事，超出本 child 范围。

**Alternatives**:
- (A) `sys.path.insert(0, "../../../ssh-core/skills/ssh-core/scripts")` →
  rejected：路径硬编码、违反 plugin 独立性、用户重排 plugin 目录就破。
- (B) `repo_root/lib/` 共享 → rejected：架构变动，需要单独 ADR；现在 4 个
  plugin × 5 个 helper = 20 处复制，量级未到需要抽象的临界。
- (C) `pip install devops-skills-shared` → rejected：本仓库刻意不发包，
  scripts/ 是源码 import-relative 直接跑。

**Source of truth**: `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:26-67`
（`json_result` + `emit` + `msys_safe_env`）。后续 4 个 helper 若在 ssh-core
有非破坏性改动，**不强制同步**到 k8s-debug；若有破坏性改动（语义/签名
改变），ship commit 必须同步所有 plugin 的副本。

### D2. 远端 kubectl 调用走 ssh_execute.py 子进程

**决定**: `run_kubectl(host, ...)` 在 `host != "local"` 时，**子进程调
`plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py --json`**，解析
返回的 JSON envelope 拼装 KubectlResult。

```python
# Pseudo-code for the remote branch in lib/kubectl.py
ssh_argv = [
    sys.executable,
    str(SSH_EXECUTE_PATH),                 # resolved at import time
    host,
    " ".join(shlex.quote(a) for a in kubectl_argv),
    "--timeout", str(timeout),
    "--json",
]
proc = subprocess.run(ssh_argv, capture_output=True, text=True,
                      timeout=timeout + 5)   # +5 buffer for ssh_execute itself
envelope = json.loads(proc.stdout)
# envelope["data"]["failure_class"] is one of None/network/auth/remote_error/timeout
# We map it to our 80-86 namespace below.
```

**Rationale**:
- 复用 ssh_execute v1.0 已经付费的能力：BatchMode、ConnectTimeout、
  `filter_ssh_noise`、`classify_failure`（network/auth/remote_error）、
  124 timeout 语义。
- envelope 契约是稳定的跨 plugin 边界——不需要 import ssh-core 任何
  Python 符号；只依赖"`ssh_execute.py --json` 输出 5-key dict"这个
  contract（`error-handling.md` §Pillar 1 已固化）。
- failure_class 透传：ssh-core 报 `network` → 我们映射到 80
  `host_unreachable`；ssh-core 报 `auth` → 我们映射到 85
  `forbidden`（在 K8s 上下文 ssh auth fail 等同于"用户没权限到达 kubectl"，
  与 RBAC forbidden 同源——见 D-3 mapping table）。

**Alternatives**:
- (B) `subprocess.run(["ssh", host, "kubectl ..."])` 直接调原生 ssh →
  rejected：要在 k8s-debug lib 里重写 BatchMode/ConnectTimeout/noise-filter/
  网络-vs-auth-vs-timeout 分类，违反 DRY；且后续 ssh_execute 的修复
  （比如 v1.1 加 ControlMaster）不会自动惠及 k8s-debug。
- (C) `paramiko.SSHClient().exec_command()` 跨 plugin 共享 paramiko →
  rejected：paramiko 是 ssh-core 内部 SFTP 决策（ADR-003 D1），ssh_execute
  本身不用 paramiko；k8s-debug 仅做 exec，没必要把 cryptography 依赖拉进来。

**Cost**: 多一层 Python 启动（~50ms）。对 4 个并发 kubectl 调用（最长
~1s wall-clock）影响不大；若以后成为瓶颈，可在 ssh_execute v1.x 引入
ControlMaster，k8s-debug 自动受益。

**Resolving `SSH_EXECUTE_PATH`**: 启动时从本文件位置向上找
`plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py`；找不到
（用户只装了 k8s-debug 没装 ssh-core）→ `KubectlError(failure_class=
"host_unreachable", exit_code=80, stderr="ssh-core ssh_execute.py not
found at expected relative path; install ssh-core plugin or use host=local")`。

### D3. 9-phase 分类状态机

**输入**: `kubectl get pod -o json`（**不是** `describe`——JSON 比文本
解析稳）+ `kubectl get events -o json`（Event Reasons 用于
ImagePullBackOff 的早期识别）。`describe` 的文本输出仅作为 `data.raw.describe`
保留供 LLM 后续解析，**不参与** phase 判别。

**判别顺序**（从最特异到最一般，**首个匹配即返回**）：

```
1. .status.phase == "Pending"
   └─ if any container has waiting.reason == "ImagePullBackOff"|"ErrImagePull"
       → ImagePullBackOff                    [terminal]
   └─ if any container has waiting.reason == "ContainerCreating"
       → ContainerCreating                   [intermediate, may settle]
   └─ → Pending                              [scheduling stuck]

2. .status.phase == "Running"
   └─ if metadata.deletionTimestamp != null
       → Terminating
   └─ if any container has waiting.reason == "CrashLoopBackOff"
       → CrashLoopBackOff                    [auto-trigger --previous]
   └─ if any container has lastState.terminated.reason == "OOMKilled"
       → OOMKilled                           [exit 137 confirmed]
   └─ if any container.restartCount > 0 AND container.ready == true
       → Running-Restarting                  [healed but has history]
   └─ if any container.ready == false
       → Running-NotReady                    [probe failing]
   └─ → Running-Healthy                      [terminal, all ready]

3. .status.phase == "Succeeded" → Running-Healthy
   (Jobs 跑完进入 Succeeded；本脚本统一归入 Running-Healthy，hint 为空)

4. .status.phase == "Failed" → CrashLoopBackOff
   (Job 失败 / RestartPolicy=Never 容器死掉，与 CrashLoop 同根因路径)

5. fallthrough → Pending  (未知 phase 容错，warning finding)
```

**字段提取**: 用 `pod_status_data = json.loads(kubectl get pod -o json)`
后按 `.status.containerStatuses[*]` 遍历；不要用 `dict.get` 链——所有
读取都走一个 `safe_dig(d, "status", "containerStatuses")` helper（返回
`None` 而非抛 KeyError），避免一个 schema 变更让脚本整体崩。

**测试 fixture**: 9 个 phase × 至少 1 个 `pod_status.json` fixture（放
`tests/fixtures/k8s/pod_status_*.json`），单测用 monkeypatch 把
`run_kubectl` 替换成 fixture-loader，断言 `classify_phase()` 返回值正确。
fixture 用真实 `kubectl get pod -o json` 输出做种子（kind 集群手动跑
出来，2KB 一份，9 份 ~18KB，checked-in）。

### D4. failure_class 80-89 namespace（**正式锁定**，覆盖 PRD §6 草案）

| Code | Failure class | 触发条件 | ssh_execute envelope 映射 |
|------|---------------|----------|-----------------------|
| 80 | `host_unreachable` | 远端 ssh 不通 / DNS / 拒连 / SSH timeout | envelope.failure_class == "network" OR "timeout" with route="remote" |
| 81 | `kubectl_missing` | `which kubectl` 失败 / `kubectl: command not found` | stderr 匹配 `command not found` 或 returncode == 127 |
| 82 | `kubeconfig_missing` | `KUBECONFIG` 未设或文件不存在 | stderr 匹配 `KUBECONFIG.*does not exist` 或 `no configuration has been provided` |
| 83 | `pod_not_found` | `kubectl get pod NAME` returns NotFound | stderr 匹配 `pods "<pod>" not found` |
| 84 | `namespace_not_found` | namespace 不存在 | stderr 匹配 `namespaces "<ns>" not found` |
| 85 | `forbidden` | RBAC 拒绝 OR ssh auth fail（K8s 上下文同义） | envelope.failure_class == "auth" OR stderr 匹配 `forbidden:` |
| 86 | `api_timeout` | `kubectl --request-timeout=30s` 自身超时 | stderr 匹配 `context deadline exceeded` 或 `Unable to connect to the server.*timeout` |
| 87 | `metrics_unavailable` | metrics-server 未装 → `top pod` 软失败 | stderr 匹配 `Metrics API not available` |
| 88 | (reserved) | — | — |
| 89 | (reserved) | — | — |

**与 ssh_execute 124 的边界**:
- `exit 124` = **Python-side** `subprocess.TimeoutExpired` 在我们这里
  触发（kubectl 子进程整体卡死，超过 `timeout + 5s` 缓冲）→ 直接返回
  `exit 124`，**不**进 80-89 namespace。
- `exit 86 api_timeout` = kubectl 自身的 `--request-timeout=30s` 触发
  （kubectl 进程正常退出，stderr 报超时）→ failure_class=`api_timeout`。

**softer policy for 87**: `metrics_unavailable` 是**唯一**的软失败——
我们 emit `exit 0` + 一条 `severity: warn` finding，**不阻塞** phase 判别。
理由：metrics-server 是可选组件，集群没装 ≠ 诊断失败。

**`data.failure_class` 必填规则**: 80-87 出口必填对应字符串；exit 0
出口若有 87 warn finding，`data.failure_class` 仍为 `None`（phase
判别成功），warn 通过 `data.findings[*].severity` 表达。

**`error-handling.md` 同步追加**: ship commit 必须把上表压缩成一行
`80-87` 段加进 `error-handling.md` exit code 表（格式比照 `20-29` /
`30-39` 现有先例）。

### D5. hint 锚点格式

**决定**: hint 字符串格式为 `references/<file>.md#<english-slug>`，
其中 `<english-slug>` 是 references 文件中**英文小标题**的 GitHub-style
slug（小写、空格转 `-`、特殊字符去掉）。

**当前 references 锚点清单**（实现时**先验证存在**，不存在则 ship 阶段
同步加锚）：

| Phase | hint |
|-------|------|
| Pending | `references/pod_lifecycle.md#阶段-1-pending` + `references/common_issues.md#pending--never-schedules` |
| ContainerCreating | `references/pod_lifecycle.md#阶段-2-containercreating` |
| Running-NotReady | `references/pod_lifecycle.md#3a-running-但-readyfalse` |
| Running-Restarting | `references/pod_lifecycle.md#3b-running-但有-restart` |
| Running-Healthy | (no hint — healthy state) |
| Terminating | `references/pod_lifecycle.md#阶段-4-terminating` |
| CrashLoopBackOff | `references/common_issues.md#crashloopbackoff` |
| ImagePullBackOff | `references/common_issues.md#imagepullbackoff--errimagepull` |
| OOMKilled | `references/common_issues.md#oomkilled` |

**Rationale**:
- references 现有小标题中英混排（有些纯英文 "## CrashLoopBackOff"、
  有些含中文 "## 阶段 1: Pending"）。GitHub-style slug 对中文友好
  （保留中文字符 + 空格转 `-`），所以 hint 既能贴在 LLM prompt 里
  让 Claude 直接 `cat references/x.md` 后 grep 找到，也能在未来转
  HTML 时 anchor 正确。
- **不**用 `file.md:line` 行号——行号会随 references 修订漂移，hint
  立刻失效；slug 与小标题强绑定，改标题才需要更新 hint。

**Acceptance gate**: 实现完后跑一遍
```bash
for hint in $(grep -oE 'references/[^"]+' diagnose_pod.py); do
  file=${hint%%#*}; anchor=${hint##*#}
  grep -q "^## .*$anchor" "plugins/k8s-debug/skills/k8s-debug/$file" \
    || echo "MISSING: $hint"
done
```
列出 MISSING 的 hint → ship 阶段补 references 小节。

### D6. live_k8s kind fixture 策略

**决定**: live test 走 **session-scoped kind cluster**，由 conftest.py
spin-up，CI 与本地都跑（不跳过）。

```python
# tests/conftest.py (additions)
@pytest.fixture(scope="session")
def kind_cluster():
    """Session-scoped kind cluster for live_k8s tests."""
    cluster_name = "k8s-debug-test"
    # Reuse existing cluster if present (developer workflow speed-up).
    existing = subprocess.run(["kind", "get", "clusters"],
                              capture_output=True, text=True)
    if cluster_name not in existing.stdout.splitlines():
        subprocess.run(["kind", "create", "cluster",
                        "--name", cluster_name,
                        "--wait", "60s"], check=True)
    kubeconfig = tempfile.mkdtemp(prefix="kind-kubeconfig-")
    cfg_path = Path(kubeconfig) / "config"
    subprocess.run(["kind", "export", "kubeconfig",
                    "--name", cluster_name,
                    "--kubeconfig", str(cfg_path)], check=True)
    os.environ["KUBECONFIG"] = str(cfg_path)
    yield {"name": cluster_name, "kubeconfig": cfg_path}
    # No teardown — explicit `kind delete cluster --name k8s-debug-test`
    # when developer wants to reset. CI runs in ephemeral runner so
    # tear-down is implicit.

@pytest.fixture
def fixture_pods(kind_cluster):
    """Spin up 3 fixture Pods (CrashLoop / Healthy / Pending) in `live-k8s-debug` ns."""
    subprocess.run(["kubectl", "create", "namespace", "live-k8s-debug",
                    "--dry-run=client", "-o", "yaml"], ...)
    subprocess.run(["kubectl", "apply", "-f",
                    "tests/fixtures/k8s/live_pods.yaml",
                    "-n", "live-k8s-debug"], ...)
    # Wait until each Pod reaches its target phase (poll up to 60s)
    ...
    yield ["crashloop-pod", "healthy-pod", "pending-pod"]
    # ns gets cleaned via finalizer at session end
```

**`tests/fixtures/k8s/live_pods.yaml`** 含 3 个 Pod manifest:
- `crashloop-pod`: image `busybox`, command `["sh","-c","exit 1"]` → CrashLoopBackOff
- `healthy-pod`: image `busybox`, command `["sh","-c","sleep 3600"]` → Running-Healthy
- `pending-pod`: nodeSelector `nonexistent=true` → Pending (FailedScheduling)

**CI 兼容性**:
- GitHub Actions 已知 kind action: `helm/kind-action@v1`，~30s 启动。
- 本机：检查 `which kind` 失败 → `pytest.skip("kind binary not installed")`
  （**skip** 而非 fail——`-m live_k8s` 主动开启时跑，默认套件不跑就不影响）。
- pytest marker 定义在 `pyproject.toml`：
  ```toml
  [tool.pytest.ini_options]
  markers = [
      "live_ssh: requires reachable SSH host (existing)",
      "live_k8s: requires kind cluster + kubectl + helm",
  ]
  ```

**Rationale**:
- kind 比 minikube 快、CI 友好（Docker-in-Docker 一份镜像）、与生产
  K8s API 兼容。
- session scope 避免每个测试都 60s spin-up——3 fixture Pod
  apply 进同一个 cluster ~10s，整套 `-m live_k8s` ≤ 90s。
- 父任务规划决议（PRD §7）已经选 kind；本 design 只补 fixture 形态。

**Out of scope for this child**:
- `kind` 镜像缓存策略（`--image=kindest/node:v1.28.x` 固定版本）→
  在 helm_status sibling 任务一起加（那个任务也需要 kind+helm）。
- 多版本 K8s API 兼容性测试 → v1.1。

---

## 3. CLI entry point (`diagnose_pod.py`)

薄壳（沿用 v0.2 argparse + 内部新增逻辑），骨架：

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _diagnose(args)              # all subprocess calls inside
    except KubectlError as e:
        result = json_result(
            success=False, exit_code=e.exit_code,
            stderr=e.stderr,
            data={"host": args.host, "namespace": args.namespace,
                  "pod": args.pod,
                  "failure_class": e.failure_class},
        )
    emit(result, as_json=args.json)
    return result["exit_code"]


def _diagnose(args) -> dict:
    """Returns the json_result envelope.

    1. Probe namespace exists (`kubectl get ns <ns>`).
       → exit 84 namespace_not_found if NotFound.
    2. Probe pod exists (`kubectl get pod <pod> -n <ns> -o json`).
       → exit 83 pod_not_found if NotFound.
    3. Classify phase from pod_status_data.
    4. Plan kubectl calls based on phase:
       - Always: describe, events
       - Always: logs --tail=200 (current)
       - If CrashLoopBackOff OR args.previous: logs --previous
       - Always (soft): top pod
    5. Run planned calls in ThreadPoolExecutor(max_workers=4).
    6. Build findings:
       - One Pod-level finding with severity by phase
         (ok | warn | crit) + hint from D5 table.
       - For each Event with type=Warning in last 30min,
         one Event finding with severity=warn.
       - If top failed: one finding severity=warn,
         hint=references/common_issues.md#... (metrics-server section).
    7. Assemble data.raw = {describe, current_logs, previous_logs,
                            events, top}; truncate each >10MB with marker.
    8. Return envelope with phase, summary, findings, raw.
    """
```

**Phase → severity table**:

| Phase | summary.pods | finding severity |
|-------|--------------|------------------|
| Pending | warn | warn |
| ContainerCreating | warn | warn |
| Running-NotReady | warn | warn |
| Running-Restarting | warn | warn |
| Running-Healthy | ok | ok |
| Terminating | warn | warn |
| CrashLoopBackOff | crit | crit |
| ImagePullBackOff | crit | crit |
| OOMKilled | crit | crit |

---

## 4. `lib/kubectl.py` 详细签名

```python
@dataclass
class KubectlResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_s: float
    failure_class: str | None    # one of 80-87 strings or None
    route: str                   # "local" | "remote"
    argv: list[str]              # the kubectl argv that ran (for debugging)


class KubectlError(Exception):
    def __init__(self, failure_class: str, exit_code: int, stderr: str):
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"kubectl failed [{failure_class}]: {stderr}")


def run_kubectl(
    host: str,
    namespace: str | None,        # None for cluster-wide calls (cluster_health uses this)
    args: list[str],              # e.g. ["get", "pod", "foo", "-o", "json"]
    *,
    use_json: bool = False,       # adds -o json to argv if True and not already present
    timeout: int = 30,            # kubectl --request-timeout=<>s
    expect_notfound_ok: bool = False,  # if True, NotFound returns result with rc=0
) -> KubectlResult:
    """Run kubectl on host (local or alias-routed via ssh_execute).

    On host=="local": subprocess.run(["kubectl", ...]) with KUBECONFIG.
    Otherwise: subprocess.run([sys.executable, ssh_execute.py, host, "kubectl ...", --json]).

    Raises KubectlError for 80-86 failure classes (caller catches to envelope).
    Returns KubectlResult with failure_class set to 87 for soft failures
    (top/metrics not available) so caller can decide policy.
    """
```

**`run_kubectl` 内部分支**:

```python
def run_kubectl(host, namespace, args, *, use_json=False, timeout=30, expect_notfound_ok=False):
    kubectl_argv = _build_kubectl_argv(args, namespace, use_json, timeout)
    if host == "local":
        return _run_local(kubectl_argv, timeout)
    else:
        return _run_remote(host, kubectl_argv, timeout)


def _build_kubectl_argv(args, namespace, use_json, timeout):
    argv = ["kubectl", f"--request-timeout={timeout}s"]
    if namespace is not None:
        argv += ["-n", namespace]
    argv += args
    if use_json and "-o" not in args:
        argv += ["-o", "json"]
    return argv


def _run_local(kubectl_argv, timeout):
    try:
        proc = subprocess.run(
            kubectl_argv, capture_output=True, text=True,
            timeout=timeout + 5,                # buffer for kubectl internal handling
            env=msys_safe_env(), check=False,
        )
    except subprocess.TimeoutExpired:
        raise KubectlError("api_timeout", 86, f"kubectl wall-clock timeout after {timeout+5}s")
    except FileNotFoundError:
        raise KubectlError("kubectl_missing", 81, "kubectl binary not found in PATH")
    fc = _classify_local_failure(proc.returncode, proc.stderr)
    if fc and fc not in ("metrics_unavailable",):
        # 80-86 → hard error; 87 is soft, returned in result for caller policy
        code = _FAILURE_CLASS_TO_EXIT[fc]
        raise KubectlError(fc, code, proc.stderr)
    return KubectlResult(
        stdout=proc.stdout, stderr=proc.stderr,
        returncode=proc.returncode, elapsed_s=...,
        failure_class=fc, route="local",
        argv=kubectl_argv,
    )


def _run_remote(host, kubectl_argv, timeout):
    """Delegates to ssh_execute.py, maps its failure_class to ours."""
    command_str = " ".join(shlex.quote(a) for a in kubectl_argv)
    ssh_argv = [
        sys.executable, str(SSH_EXECUTE_PATH),
        host, command_str,
        "--timeout", str(timeout + 10),         # ssh_execute timeout > kubectl timeout
        "--json",
    ]
    try:
        proc = subprocess.run(ssh_argv, capture_output=True, text=True,
                              timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired:
        raise KubectlError("host_unreachable", 80,
                           f"ssh_execute timeout after {timeout+15}s")
    envelope = json.loads(proc.stdout)
    ssh_fc = envelope.get("data", {}).get("failure_class")
    # Map ssh-core's classes to k8s-debug's classes
    if ssh_fc == "network" or ssh_fc == "timeout":
        raise KubectlError("host_unreachable", 80, envelope["stderr"])
    if ssh_fc == "auth":
        raise KubectlError("forbidden", 85, envelope["stderr"])
    # ssh succeeded → inspect remote kubectl's exit code from envelope
    remote_rc = envelope["exit_code"]
    remote_stderr = envelope["stderr"]
    fc = _classify_local_failure(remote_rc, remote_stderr)  # reuse classifier
    if fc and fc not in ("metrics_unavailable",):
        code = _FAILURE_CLASS_TO_EXIT[fc]
        raise KubectlError(fc, code, remote_stderr)
    return KubectlResult(
        stdout=envelope["stdout"], stderr=remote_stderr,
        returncode=remote_rc, elapsed_s=envelope["data"]["elapsed_s"],
        failure_class=fc, route="remote",
        argv=kubectl_argv,
    )
```

---

## 5. Test plan summary（detail in `implement.md`）

- **Unit (`tests/test_diagnose_pod_unit.py`, ≥40)**:
  - 9 × phase classifier (fixture-driven, monkeypatch `run_kubectl`)
  - 8 × failure_class (80-87) injection paths
  - 1 × `--previous` auto-trigger on CrashLoop
  - 1 × `--previous` manual flag on non-CrashLoop
  - 1 × `--container` filter (multi-container Pod fixture)
  - 1 × 10MB truncation marker presence
  - 4 × ThreadPoolExecutor concurrency (mock 4 calls, assert wall-clock < sum)
  - 1 × JSON envelope schema validation (via `tests/util/json_envelope.py`)
  - 1 × hint anchor existence (grep references files for each hint)
  - 1 × SSH_EXECUTE_PATH not found → 80 host_unreachable
  - 5+ × edge cases (empty namespace, empty pod name, unknown phase fallthrough...)

- **Unit (`tests/test_kubectl_lib_unit.py`, ≥15)**:
  - `_classify_local_failure` over 8 stderr fixture strings
  - `_run_local` happy path + 6 failure paths
  - `_run_remote` envelope mapping (network/auth/timeout/remote_error → 80/85/80/forwarded)
  - `KubectlError` carries failure_class + exit_code
  - `SSH_EXECUTE_PATH` resolver finds the file relative to this lib

- **Integration (`tests/test_diagnose_pod_integration.py -m live_k8s`, ≥3)**:
  - CrashLoopBackOff fixture Pod → assert phase, `data.raw.previous_logs` non-empty
  - Running-Healthy fixture Pod → assert phase, summary=ok
  - Pending fixture Pod → assert phase, finding has FailedScheduling event

---

## 6. Cross-references

- `prd.md` — requirements + AC
- `implement.md` — phased build-out checklist
- `plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py` — v0.2 stub
- `plugins/k8s-debug/skills/k8s-debug/SKILL.md` — decision tree + output contract
- `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py` — remote route
  delegate
- `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:26-67` — helper
  source-of-truth (D1)
- `.trellis/spec/backend/error-handling.md` — JSON envelope + exit code
  table (will receive 80-87 append in ship commit)
- `.trellis/spec/backend/adr-001-ssh-execute.md` — ssh-core contract that
  D2 piggybacks on
- `.trellis/spec/backend/adr-003-sftp-transport.md` — lib-shared pattern
  precedent (form, not content)

---

## 7. Open questions deferred to v1.1

- **`kubectl exec` for application-level intel** (e.g. `jcmd` / `pprof`)
  → out of scope, application-layer probing belongs to user
- **Streaming `kubectl logs -f` for live debugging** → out of scope,
  log-aggregator/tail_multi covers it with `kube://` source
- **Multi-pod batch mode** (`diagnose_pod.py pod-a pod-b pod-c`) → out of
  scope, sibling `check_namespace` covers per-pod fanout
- **Helm-aware hints** (if Pod is owned by a Helm release, link to
  `helm_status`) → defer; needs `helm_status` shipped first to avoid
  circular dep
- **Custom phase definitions via plugin config** → YAGNI until a user
  asks
