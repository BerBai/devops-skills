# Implementation — k8s-debug diagnose_pod v1.0

> 配套 PRD: `prd.md`；技术设计: `design.md`。
> 本任务**首次**在 k8s-debug plugin 落 `scripts/lib/`，所有产物（lib + fixtures）
> 后续 3 个 sibling 复用。

## Pre-flight 检查（task.py start 之前）

- [ ] `plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py` 仍为 v0.2 stub
      （`grep "UNIMPLEMENTED_NOTE" plugins/k8s-debug/.../diagnose_pod.py` 命中）
- [ ] `plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py` 存在且 `--help` exit 0
      （D2 的远端通路依赖此文件）
- [ ] `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py` 存在且
      暴露 `json_result` / `emit` / `msys_safe_env`
      （D1 的复制源头）
- [ ] 本机 `kubectl version --client` 退 0（live_k8s 测试依赖）
- [ ] 本机 `kind version` 退 0；若无 → 装：`brew install kind` (macOS)
      或 `go install sigs.k8s.io/kind@latest`
- [ ] `pytest tests/` 默认套件全绿（基线）

任何一项不满足 → STOP，处理后再 `task.py start`。

---

## Phase L — Library（lib/__init__.py + lib/kubectl.py）

### L1：建 `plugins/k8s-debug/skills/k8s-debug/scripts/lib/__init__.py`

- [ ] 创建 `plugins/k8s-debug/skills/k8s-debug/scripts/lib/__init__.py`
- [ ] 从 `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py` 复制：
  - 文件头 docstring（改成 "Shared helpers for k8s-debug scripts."）
  - `json_result` (lines 26-39)
  - `emit` (lines 42-55)
  - `msys_safe_env` (lines 58-66)
  - `unimplemented` (lines 151-160)，但 plugin 名串改成 `"k8s-debug"`
- [ ] **不复制** `classify_failure` / `filter_ssh_noise` / `alias_state_path` /
      `SSH_NOISE_PATTERNS` / `SSH_CLIENT_FAILURE_EXIT_CODE` /
      `NETWORK_PATTERN` / `AUTH_PATTERN`（这些 ssh-core 专属，本任务通过
      D2 把 ssh-core 的 envelope `failure_class` 当输入，不重新实现分类）
- [ ] 公开 surface 在文件头 docstring 显式列出（4 个 helper）

**Validation L1**:
```bash
python3 -c "
import sys
sys.path.insert(0, 'plugins/k8s-debug/skills/k8s-debug/scripts')
from lib import json_result, emit, msys_safe_env, unimplemented
print('imports ok')
result = json_result(True, 0, stdout='hi')
assert result['success'] is True and result['exit_code'] == 0
print('json_result ok')
"
```
退 0 + 打印 "imports ok" + "json_result ok"。

### L2：建 `lib/kubectl.py`

- [ ] 创建 `plugins/k8s-debug/skills/k8s-debug/scripts/lib/kubectl.py`，
      按 design.md §4 的 dataclass + 函数签名：
  - `KubectlResult` dataclass（7 字段）
  - `KubectlError` exception（3 参数）
  - `_FAILURE_CLASS_TO_EXIT = {"host_unreachable": 80, ...}` 8-entry dict
  - `SSH_EXECUTE_PATH` module-level constant，启动时解析路径，找不到
    抛 `ImportError` with clear message（**不**在 import 时抛 KubectlError，
    那只在调用时抛）
  - `run_kubectl(host, namespace, args, *, use_json=False, timeout=30,
    expect_notfound_ok=False) -> KubectlResult`
  - `_build_kubectl_argv(args, namespace, use_json, timeout)`
  - `_run_local(kubectl_argv, timeout)`
  - `_run_remote(host, kubectl_argv, timeout)`
  - `_classify_local_failure(returncode, stderr) -> str | None` （8 个分类，
    返回 80-87 之一字符串或 None）
- [ ] `_classify_local_failure` 用 **substring match**（不是正则），按
      design.md D4 表里的 stderr 关键字判别；顺序从最特异到最一般。
- [ ] `SSH_EXECUTE_PATH` 解析逻辑：
  ```python
  _LIB_DIR = Path(__file__).resolve().parent
  # lib/ → scripts/ → k8s-debug/ → skills/ → k8s-debug/ → plugins/ → repo
  _REPO_ROOT = _LIB_DIR.parents[5]
  SSH_EXECUTE_PATH = (_REPO_ROOT
      / "plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py")
  ```

**Validation L2**:
```bash
python3 -c "
import sys
sys.path.insert(0, 'plugins/k8s-debug/skills/k8s-debug/scripts')
from lib.kubectl import (run_kubectl, KubectlResult, KubectlError,
                         SSH_EXECUTE_PATH, _classify_local_failure)
assert SSH_EXECUTE_PATH.exists(), f'ssh_execute.py not at {SSH_EXECUTE_PATH}'
# Test classifier with fixture stderrs
assert _classify_local_failure(1, 'pods \"foo\" not found') == 'pod_not_found'
assert _classify_local_failure(1, 'namespaces \"bar\" not found') == 'namespace_not_found'
assert _classify_local_failure(0, '') is None
print('kubectl lib smoke ok')
"
```
退 0 + 打印 "kubectl lib smoke ok"。

### L3：手动 smoke run_kubectl(host="local")

- [ ] 用本机 kind cluster 或现成集群：
  ```bash
  python3 -c "
  import sys
  sys.path.insert(0, 'plugins/k8s-debug/skills/k8s-debug/scripts')
  from lib.kubectl import run_kubectl
  r = run_kubectl('local', None, ['get', 'ns'], use_json=True)
  print('rc=', r.returncode, 'fc=', r.failure_class)
  import json
  data = json.loads(r.stdout)
  print('namespaces:', [item['metadata']['name'] for item in data['items']])
  "
  ```
  期望：rc=0、fc=None、打印 ns 名字列表。

**Validation L3**：手动 smoke 通过；删除 throwaway 命令。

---

## Phase F — Fixtures（unit fixtures + live_k8s fixtures）

### F1：unit fixture JSON 种子

- [ ] 创建 `tests/fixtures/k8s/` 目录
- [ ] 用现有 kind cluster 或 minikube，跑出 9 份 `kubectl get pod -o json` 输出：
  - `pod_status_pending.json`（用 nodeSelector 不存在的 Pod）
  - `pod_status_container_creating.json`（PVC 卡住的 Pod，模拟中）
  - `pod_status_running_notready.json`（readinessProbe failing）
  - `pod_status_running_restarting.json`（restartCount>0 但当前 ready）
  - `pod_status_running_healthy.json`
  - `pod_status_terminating.json`（带 deletionTimestamp）
  - `pod_status_crashloop.json`（busybox exit 1）
  - `pod_status_imagepullback.json`（image 写错）
  - `pod_status_oomkilled.json`（memory limit 极低的 Pod）
- [ ] 每份文件最小化：删 `managedFields` / 删 `annotations` 里的
      `kubectl.kubernetes.io/last-applied-configuration`，保留
      `metadata.{name,namespace,deletionTimestamp}` + `status.*`
- [ ] 同步造一份 `events_*.json` 给关键 phase（至少 crashloop / pending）

**Validation F1**：
```bash
ls tests/fixtures/k8s/pod_status_*.json | wc -l   # 9
python3 -c "
import json, pathlib
for f in pathlib.Path('tests/fixtures/k8s').glob('pod_status_*.json'):
    d = json.loads(f.read_text())
    assert 'status' in d, f'{f.name} missing status'
    assert 'metadata' in d, f'{f.name} missing metadata'
print('all 9 fixtures valid JSON')
"
```

### F2：live_k8s fixture

- [ ] 编辑 `tests/conftest.py`，加 `kind_cluster` session-scoped fixture
      （按 design.md §D6 骨架）
- [ ] 创建 `tests/fixtures/k8s/live_pods.yaml`：3 个 Pod（crashloop /
      healthy / pending），统一 namespace `live-k8s-debug`
- [ ] 编辑 `pyproject.toml`，加 `live_k8s` marker：
  ```toml
  [tool.pytest.ini_options]
  markers = [
      "live_ssh: requires reachable SSH host",
      "live_k8s: requires kind cluster + kubectl",
  ]
  ```

**Validation F2**：
```bash
pytest --collect-only -m live_k8s tests/ 2>&1 | head -5
# 期望：no errors，列出会跑的 test（即便此时还没写测试也不报 marker 未知）
```

---

## Phase P — Phase classifier

### P1：实现 `classify_phase()` 与 `safe_dig()`

- [ ] 在 `diagnose_pod.py` 顶部加：
  ```python
  PhaseLiteral = Literal["Pending", "ContainerCreating",
                          "Running-NotReady", "Running-Restarting",
                          "Running-Healthy", "Terminating",
                          "CrashLoopBackOff", "ImagePullBackOff",
                          "OOMKilled"]
  ```
- [ ] 实现 `safe_dig(d, *keys, default=None)` helper
- [ ] 实现 `classify_phase(pod_status_data: dict) -> PhaseLiteral`
      按 design.md §D3 完整判别顺序，首匹配返回
- [ ] 实现 `_phase_severity(phase: PhaseLiteral) -> Literal["ok","warn","crit"]`
      按 design.md §3 的 phase→severity 表

**Validation P1**:
```bash
python3 -c "
import sys, json, pathlib
sys.path.insert(0, 'plugins/k8s-debug/skills/k8s-debug/scripts')
from diagnose_pod import classify_phase
for f in pathlib.Path('tests/fixtures/k8s').glob('pod_status_*.json'):
    expected = f.stem.replace('pod_status_', '')
    actual = classify_phase(json.loads(f.read_text()))
    print(f'{expected:25s} -> {actual}')
"
```
期望：9 行输出，每行的 expected 与 actual 一一对应（注意命名映射：
`crashloop` ↔ `CrashLoopBackOff` 等，可能需要手动核对一遍）。

---

## Phase D — `_diagnose()` 主流程

### D1：实现 `_diagnose(args) -> dict`

- [ ] 按 design.md §3 的 8 步骨架在 `diagnose_pod.py` 中实现 `_diagnose()`
- [ ] step 1: probe namespace via `run_kubectl('local', None, ['get', 'ns', args.namespace])`
      → 捕获 `KubectlError(namespace_not_found)` 重新抛
- [ ] step 2: probe pod via `run_kubectl(host, ns, ['get', 'pod', pod], use_json=True)`
      → 捕获 `KubectlError(pod_not_found)` 重新抛；存 `pod_status_data`
- [ ] step 3: `phase = classify_phase(pod_status_data)`
- [ ] step 4: 规划 kubectl call set：
  - always: describe / events / logs current
  - if phase == "CrashLoopBackOff" or args.previous: logs --previous
  - always (soft): top pod
- [ ] step 5: ThreadPoolExecutor(max_workers=4)，每个 future 包 try/except
      KubectlError（top 的 87 软失败不抛，转 warn finding；其他 80-86 仍抛）
- [ ] step 6: 拼 findings：
  - 1 个 Pod-level finding，severity=`_phase_severity(phase)`，hint 按
    design.md §D5 表查
  - 0-N 个 Event findings：遍历 events，type=="Warning" AND lastTimestamp 在
    30min 内的，每个一条 finding severity=warn
  - 如 top 软失败：加一条 severity=warn, hint=
    `references/common_issues.md#oomkilled`（top 的目标读者通常关心 OOM）
- [ ] step 7: `data.raw` 拼 + 每字段 10MB 截断（reuse 一个 `_truncate_10mb()`
      helper，在 `lib/__init__.py` 加是过度抽象——本任务里直接放
      `diagnose_pod.py` private function）
- [ ] step 8: 返回 envelope，`data` 含 host/namespace/pod/phase/summary/
      findings/raw/failure_class（成功时 None）

### D2：把 `main()` 改成调用 `_diagnose()` + KubectlError 兜底

- [ ] 改写 `main()`：删 v0.2 UNIMPLEMENTED 分支，按 design.md §3 的 main
      骨架重写
- [ ] try/except KubectlError → 包成 envelope，exit_code=e.exit_code
- [ ] 删除 `UNIMPLEMENTED_NOTE` 常量

**Validation D2**:
```bash
# 本机 happy path
python3 plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py \
  local default kube-dns --json 2>&1 | python3 -m json.tool | head -30
# 期望：success=true, exit_code=0, data.phase 是 9 个之一

# pod_not_found
python3 plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py \
  local default no-such-pod --json 2>&1 | python3 -m json.tool | head -10
# 期望：exit_code=83, data.failure_class="pod_not_found"

# namespace_not_found
python3 plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py \
  local no-such-ns whatever --json
# 期望：exit_code=84, data.failure_class="namespace_not_found"
```

---

## Phase T — Tests

### T1：`tests/test_kubectl_lib_unit.py`

- [ ] ≥15 用例，按 design.md §5 第二组：
  - 8 × `_classify_local_failure` stderr fixture
  - 1 × `_run_local` happy path（monkeypatch subprocess.run）
  - 6 × `_run_local` failure paths（FileNotFoundError, TimeoutExpired,
    NotFound, namespace 404, forbidden, metrics）
  - 1 × `_run_remote` envelope mapping（4 ssh-core failure_class →
    我们的映射）
  - 1 × `KubectlError` 字段
  - 1 × `SSH_EXECUTE_PATH` resolver（assert .exists()）

**Validation T1**:
```bash
pytest tests/test_kubectl_lib_unit.py -v 2>&1 | tail -5
# 期望：15+ passed
```

### T2：`tests/test_diagnose_pod_unit.py`

- [ ] ≥40 用例，按 design.md §5 第一组：
  - 9 × phase classifier（drive from `tests/fixtures/k8s/pod_status_*.json`）
  - 8 × failure_class 80-87 injection（monkeypatch run_kubectl 抛对应
    KubectlError；assert envelope.exit_code 与 data.failure_class）
  - 1 × CrashLoop 自动触发 `--previous`（monkeypatch + 断言
    "logs --previous" 在某次 run_kubectl args 里）
  - 1 × `--previous` flag 强制（非 CrashLoop pod 也带）
  - 1 × `--container` filter（multi-container fixture，断言 argv 里有
    `-c <container>`）
  - 1 × 10MB truncation marker（造一个 10.5MB string，断言截断后含
    `<truncated 10MB cap>`）
  - 4 × concurrency（mock 4 calls 每个 sleep 0.5s，assert wall-clock <
    3 × 0.5s 而不是 4 × 0.5s）
  - 1 × JSON envelope schema（`from tests.util.json_envelope import validate`）
  - 1 × hint anchor 存在性（grep references 文件，每个 hint 必须命中）
  - 1 × SSH_EXECUTE_PATH 缺失 → exit 80
  - 5+ × edge cases（unknown phase fallthrough, empty containerStatuses,
    Job Succeeded → Running-Healthy, Job Failed → CrashLoopBackOff,
    deletionTimestamp + restartCount>0 优先 Terminating 而非 Running-*）

**Validation T2**:
```bash
pytest tests/test_diagnose_pod_unit.py -v 2>&1 | tail -10
# 期望：40+ passed
```

### T3：`tests/test_diagnose_pod_integration.py`

- [ ] ≥3 用例，按 design.md §5 第三组：
  - `test_crashloop_pod_real_kind(fixture_pods, kind_cluster)`:
    diagnose `crashloop-pod` → assert phase, `data.raw.previous_logs`
    非空（CrashLoop 自动触发的产物）
  - `test_healthy_pod_real_kind(...)`:
    diagnose `healthy-pod` → assert phase="Running-Healthy",
    `summary.pods=="ok"`, findings 仅有 1 条 Pod-level + 可能的 0 个
    Event-level（healthy 时 Warning events 应该没有）
  - `test_pending_pod_real_kind(...)`:
    diagnose `pending-pod` → assert phase="Pending",
    findings 至少 1 条 Event finding 含 `FailedScheduling` reason

- [ ] 所有用例加 `@pytest.mark.live_k8s`

**Validation T3**:
```bash
pytest -m live_k8s tests/test_diagnose_pod_integration.py -v 2>&1 | tail -10
# 期望：3 passed
```

---

## Phase R — References 锚点验证

### R1：跑 design.md §D5 的 hint anchor 验证脚本

- [ ] 写一个一次性 shell 检查（**不**提交，本步只用于人工验证）：
  ```bash
  cd plugins/k8s-debug/skills/k8s-debug
  for hint in $(grep -oE 'references/[a-z_.]+#[^"]+' scripts/diagnose_pod.py | sort -u); do
    file=${hint%%#*}; anchor=${hint##*#}
    # GitHub-style slug: 小写、空格转 -、其他符号去掉
    if grep -qE "^##.*${anchor}" "$file" 2>/dev/null; then
      echo "OK   $hint"
    else
      echo "MISS $hint"
    fi
  done
  ```
- [ ] 对每个 MISS 的 hint：要么改 hint 字符串、要么在 references 里补
      对应小节（**ship commit 同步**，不分两次 commit）

**Validation R1**：脚本输出**全是 OK**，无 MISS。

---

## Phase S — Spec update

### S1：`.trellis/spec/backend/error-handling.md` 追加 80-87 段

- [ ] 按 `20-29` / `30-39` 现有先例风格，在 exit code 表中加一行：
  ```
  | `80`–`87` | `diagnose_pod` 及 sibling k8s-debug 脚本共享的 failure
  classes（namespace 80-89，本 child 用满 80-87）。`80` `host_unreachable`
  （ssh-core 不可达）、`81` `kubectl_missing`、`82` `kubeconfig_missing`、
  `83` `pod_not_found`、`84` `namespace_not_found`、`85` `forbidden`
  （RBAC 或 ssh auth 同源）、`86` `api_timeout`（kubectl
  `--request-timeout=30s` 触发；区别于 Python-side 124）、`87`
  `metrics_unavailable`（软失败，exit 0 + warn finding）。Introduced
  by `diagnose_pod` v1.0（见 `.trellis/tasks/05-26-k8s-debug-diagnose-pod/
  prd.md` R6 + `design.md` D4）。ADR follow-up TBD。|
  ```
- [ ] 在表注释段把 `80`–`87` 加入"per-script ranges"列表

**Validation S1**: `git diff .trellis/spec/backend/error-handling.md` 显示
单行新增，格式与 20-29 / 30-39 一致。

---

## Phase V — 整体验证（task.py end-of-phase gate）

### V1：默认套件全绿

```bash
pytest tests/ 2>&1 | tail -3
# 期望：N passed, 0 failed, 0 error
```

### V2：live_k8s 套件全绿

```bash
pytest -m live_k8s tests/ 2>&1 | tail -3
# 期望：3+ passed
```

### V3：lint / type-check

```bash
ruff check plugins/k8s-debug/skills/k8s-debug/scripts/ tests/test_diagnose_pod_*.py tests/test_kubectl_lib_unit.py
# 期望：All checks passed!

mypy plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py \
     plugins/k8s-debug/skills/k8s-debug/scripts/lib/ \
     --ignore-missing-imports
# 期望：Success: no issues found
```

### V4：manifest smoke

```bash
pytest tests/test_manifests.py -v -k "diagnose_pod or k8s-debug" 2>&1 | tail -5
# 期望：相关 case passed
# 这步确认 `--help` 仍可调用、SKILL.md 仍可被发现
```

### V5：SKILL.md decision tree 一致性

- [ ] 读 `plugins/k8s-debug/skills/k8s-debug/SKILL.md:23-43`，确认决策树
      第二分支（"某个 Pod 状态异常？" → `diagnose_pod.py <host> <namespace> <pod>`）
      与实现完全一致
- [ ] 读 SKILL.md 输出契约段（line 71-94），确认我们的 envelope 字段
      `host/namespace/pod/phase/summary/findings/raw` 与示例一致
- [ ] 任何偏差 → 改 SKILL.md（ship commit 同步），**不**改实现

### V6：CONTRIBUTING.md / 父任务 PRD 自查

- [ ] 父 PRD `05-24-devops-skills-v1-rollout/prd.md` 的 k8s-debug 段
      不需要改（本 child 只交付一个 sibling 的脚本，父任务的"4 个 scripts
      推到 v1.0"目标推进 1/4）
- [ ] 父任务 Phase 2 表里 `k8s-debug-diagnose-pod` 项打勾

---

## Phase commit

### C1：提交（**仅在 V1-V6 全绿后**）

```bash
git add plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py \
        plugins/k8s-debug/skills/k8s-debug/scripts/lib/ \
        plugins/k8s-debug/skills/k8s-debug/references/ \
        tests/test_diagnose_pod_unit.py \
        tests/test_diagnose_pod_integration.py \
        tests/test_kubectl_lib_unit.py \
        tests/fixtures/k8s/ \
        tests/conftest.py \
        pyproject.toml \
        .trellis/spec/backend/error-handling.md
git commit -m "feat(k8s-debug): ship diagnose_pod v1.0 + lib/kubectl shared"
```

Commit message 形态参考最近 5 个 ssh-core ship commit:
```
4f95b26 feat(ssh-core): ship ssh_tunnel v1.0 detached SSH port-forward manager
fe9e17e feat(ssh-guarded): ship scan_software v1.0 batch detection + cache
c2001b8 feat(log-aggregator): ship tail_multi v1.0 + 5 shared libs
5395ded feat(ssh-core): ship ssh_download v1.0 mirroring ssh_upload over lib/sftp_transport
a502afb feat(ssh-core): ship ssh_server_transfer v1.0 stream mode
```

### C2：归档

- [ ] `python3 ./.trellis/scripts/task.py status 05-26-k8s-debug-diagnose-pod completed`
- [ ] （归档由父任务/clean-up 流程批量处理，本任务不主动 archive）

---

## Rollback points

| 阶段 | 触发条件 | 回滚动作 |
|------|----------|----------|
| L1-L2 | `lib/` 复制错了语义（json_result 改了字段） | `git checkout plugins/k8s-debug/skills/k8s-debug/scripts/lib/` |
| P1 | classify_phase 跑 fixture 命中率 < 9/9 | 不进 D 阶段，回头读 K8s API ref 修正判别 |
| D1 | ThreadPoolExecutor 在某些环境（Windows）异常 | 暂时降级 sequential 调用，标记 v1.1 改 |
| T2 | 某个 phase 测试反复 fail | 检查 fixture JSON 是否被 minimize 过头导致漏字段 |
| V2 | live_k8s 在 CI 卡住 | 跳过 CI live_k8s（marker filter），本机已过即可 |
| V5 | SKILL.md 与实现偏差大 | 改实现去对齐 SKILL.md，**不**改 SKILL.md（这是用户契约） |

---

## Validation commands quick-ref

```bash
# Single command to run **all** Phase V validations
pytest tests/ -v 2>&1 | tail -5 && \
pytest -m live_k8s tests/ -v 2>&1 | tail -5 && \
ruff check plugins/k8s-debug/skills/k8s-debug/scripts/ tests/test_diagnose_pod_*.py tests/test_kubectl_lib_unit.py && \
mypy plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py plugins/k8s-debug/skills/k8s-debug/scripts/lib/ --ignore-missing-imports && \
echo "ALL GREEN, READY TO COMMIT"
```
