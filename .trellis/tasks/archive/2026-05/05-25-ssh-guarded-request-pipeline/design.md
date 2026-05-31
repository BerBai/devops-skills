# Design — ssh-guarded request/execute pipeline v1.0

> 配套 PRD: `prd.md`。
> 本 child 一次性 ship 5 个脚本 + 4 个 lib (见 PRD scope)。
> **§1 是 cross-child contract verbatim 折叠**(原 `_cross_cutting.md`,已删)。Children B/C/D (exec_detached / redact_check / scan_software) 在自己的 design.md 中 reference 本文件 §1,不再 renegotiate。

---

## §1. Locked cross-child contract (ssh-guarded v1.0)

> Shared decisions across the 4 ssh-guarded v1.0 children. Treat as ADR-004 candidate; do not renegotiate inside individual child PRDs. Child A folds this into its `design.md` §1; Children B/C/D reference it by section.

### C1. Request artifact JSON schema v1

```json
{
  "version": 1,
  "request_id": "<op>-<8 hex>",
  "operation": "command" | "upload" | "mkdir" | "delete",
  "server": "<alias>",
  "reason": "<free text>",
  "created_at": "<ISO8601 UTC>",
  "expires_at": "<ISO8601 UTC, default created_at + 1h>",
  "payload": { ... per-operation ... },
  "risk_summary": {
    "irreversible": bool,
    "scope": "single-file" | "tree" | "service" | "host",
    "blast_radius": "<one-line human>"
  }
}
```

File path: `reports/requests/<op>-<request_id>.json`. `request_id` = uuid4().hex[:8]. The filename embeds operation kind so `ls reports/requests/` is human-scannable.

### C2. payload per operation

| operation | payload fields |
|---|---|
| `command` | `command` (str), `workdir?` (str), `timeout?` (int sec), `env?` (dict) |
| `upload`  | `local_path`, `local_sha256`, `local_size` (bytes), `remote_path`, `overwrite` (bool), `sensitive_local` (bool) |
| `mkdir`   | `remote_path`, `mode` (octal str, default `"0755"`), `parents` (bool) |
| `delete`  | `remote_path`, `recursive` (bool), `is_directory_pre_check` (bool, set by drafter via SFTP stat at draft time) |

### C3. workdir sandbox

All `remote_path` (upload/mkdir/delete) AND `payload.workdir` (command) MUST be inside `~/workspace/<project_id>/` on the remote host.

- `<project_id>` resolution order (first non-empty wins):
  1. `--project-id <value>` explicit CLI flag
  2. `SSH_GUARDED_PROJECT_ID` env var
  3. Current git repo basename via `git rev-parse --show-toplevel` on the local machine; basename only
- Hard fail (exit 50, failure_class `workdir_outside_sandbox`) on violation in `run_request --execute`. The `request_*` drafters MAY warn but allow drafting an out-of-sandbox path (so user sees the explicit refusal at execute time, not at draft time — pedagogical).

### C4. Denylist (always rejected, even inside sandbox)

Path matches any of these → exit 51, failure_class `path_in_denylist`:

```
~/.ssh/**, ~/.aws/**, ~/.kube/**, ~/.gnupg/**, ~/.config/gh/**
.env, .env.*, **/.env, **/.env.*
id_rsa, id_rsa.*, id_ed25519*, *.pem, *.key, *.p12, *.pfx
authorized_keys, known_hosts
```

Match logic: normalize remote_path (expand `~`, resolve `..`), then both prefix-match (`~/.ssh/foo`) and basename-match (`id_rsa`).

### C5. Per-script failure_class regions + exit codes

Following the ssh-core convention (per-script namespace, per-script exit code window). All eight ssh-guarded scripts get a contiguous 50-block:

| Script | failure_class strings | exit codes |
|---|---|---|
| `request_command`   | command_payload_invalid, reason_missing, workdir_outside_sandbox (re-raised), expiry_invalid                                       | 30–34 |
| `request_upload`    | local_path_missing, local_path_outside_upload_roots, sha256_compute_failed, sensitive_local_unconfirmed, size_limit_exceeded     | 35–39 |
| `request_mkdir`     | mode_invalid, remote_path_outside_sandbox, parents_required_for_nested                                                            | 40–44 |
| `request_delete`    | remote_path_outside_sandbox, recursive_required_for_directory, path_in_denylist, pre_check_stat_failed                            | 45–49 |
| `run_request`       | workdir_outside_sandbox, path_in_denylist, artifact_invalid, artifact_expired, lock_held, sha256_mismatch, transport_failed, delete_confirm_required | 50–59 |
| `exec_detached`     | request_not_executable, nohup_dispatch_failed, pid_capture_failed, remote_log_unreachable, job_vanished, job_state_corrupt        | 60–69 |
| `redact_check`      | catalog_load_failed, catalog_schema_invalid, regex_compile_failed                                                                  | 70–74 |
| `scan_software`     | catalog_load_failed, cache_write_failed, cache_corrupt, ssh_batch_failed                                                           | 75–79 |

Shared exit codes: `0` ok, `1` generic error, `124` timeout (same as ssh-core).

### C6. Lock file

`<artifact-path>.lock` via `fcntl.flock(LOCK_EX | LOCK_NB)`. Held for the duration of `run_request --execute`. Lock failure → exit 54 (`lock_held`), JSON error includes the holding PID (best-effort from `fuser` or `/proc/locks`).

### C7. JSON envelope

`{success: bool, data: {...} | null, error: {message, failure_class, exit_code} | null}` — identical to ssh-core. Every script honors `--json`; non-JSON output is human-readable summary.

### C8. Redact catalog

Default catalog path: `~/.devops/redact.json`. Schema:

```json
{
  "version": 1,
  "patterns": [
    {"kind": "aws_access_key", "regex": "AKIA[0-9A-Z]{16}", "replace_with": "[REDACTED-AWS-ACCESS-KEY]"}
  ]
}
```

If catalog file missing → fall back to the built-in default catalog (compiled into `redact_check.py` as `_DEFAULT_PATTERNS`). All other ssh-guarded scripts that emit redacted output (`run_request`, `exec_detached tail-log`) reuse the same catalog via `lib/redact.py` (Child A delivers this lib).

`--no-redact` global flag bypasses redaction (audit summaries only; not the default).

### C9. Result artifact (run_request --execute output)

Written next to the request: `<id>.result.json`. Schema:

```json
{
  "version": 1,
  "request_id": "...",
  "executed_at": "<ISO8601 UTC>",
  "exit_code": int,
  "duration_ms": int,
  "stdout_redacted": "...",
  "stderr_redacted": "...",
  "transport": "ssh_execute" | "ssh_upload" | "ssh_remove",
  "transport_exit_code": int
}
```

### C10. Test markers

- Default suite: `pytest tests/` (no SSH)
- Live: `pytest -m live_ssh` (uses pai fixture, same as ssh-core)
- Each child ships its own `tests/unit/test_*.py` + `tests/integration/test_*_live.py`

### C11. Shared library at `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/`

Child A delivers:
- `request_artifact.py` — `RequestArtifact` dataclass + `load(path)`, `save(path)`, `validate(server, project_id)` (sandbox + denylist + expiry + schema)
- `redact.py` — `Redactor(catalog_path | None)`, `.apply(text) -> (redacted, hits)`
- `sandbox.py` — `resolve_project_id(args, env, cwd)`, `is_inside_sandbox(path, project_id)`, `is_in_denylist(path)`
- `lockfile.py` — context manager wrapping fcntl

Children B/C/D import from these; they do not reimplement.

---

## §2. Architecture overview

```
                  ┌──────────────────────────────┐
                  │  request_command.py          │
                  │  request_upload.py           │   drafter layer
                  │  request_mkdir.py            │   (4 thin CLIs)
                  │  request_delete.py           │
                  └────────────┬─────────────────┘
                               │ writes
                               ▼
            reports/requests/<op>-<id>.json   ← artifact (schema v1, C1)
                               │ reads
                               ▼
                  ┌──────────────────────────────┐
                  │  run_request.py              │   executor layer
                  │  --execute                   │   (1 fat CLI)
                  └────────────┬─────────────────┘
                               │ dispatch
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
       ssh_execute.py   ssh_upload.py   ssh_execute.py
        (command,            (upload)      (mkdir, delete
         from ssh-core)                     as one-liners)
                               │
                               ▼
                  reports/requests/<id>.result.json   (schema C9)

shared lib (all 9 scripts can import):
  lib/request_artifact.py   ← load / save / validate
  lib/redact.py             ← Redactor(catalog) → .apply()
  lib/sandbox.py            ← resolve_project_id, is_inside_sandbox, is_in_denylist
  lib/lockfile.py           ← fcntl.flock context manager
```

数据流分两段:**draft 段**纯本地 (drafter 写 artifact,可能调 SFTP stat 做 delete pre-check),**execute 段**走 ssh-core 子进程 (subprocess.run → ssh_execute.py / ssh_upload.py)。两段中间是 artifact JSON file,人类 review 在这一点插入。

---

## §3. Design decisions

### D1 — Shared `lib/request_artifact.py` API

**Decision**: dataclass-based artifact 模型 + 3 个顶层函数。验证逻辑全部进 `RequestArtifact.validate()`,CLI 层只负责构造 + 调用,不重复 if/else。

```python
# lib/request_artifact.py (sketch, ~150 LOC)

from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Literal, Any

Operation = Literal["command", "upload", "mkdir", "delete"]
Scope = Literal["single-file", "tree", "service", "host"]


@dataclass
class RiskSummary:
    irreversible: bool
    scope: Scope
    blast_radius: str


@dataclass
class RequestArtifact:
    version: int                         # always 1; reject other values on load
    request_id: str                      # "<op>-<8hex>"
    operation: Operation
    server: str
    reason: str
    created_at: str                      # ISO8601 UTC
    expires_at: str                      # ISO8601 UTC
    payload: dict[str, Any]              # op-specific; not typed here, validated by op
    risk_summary: RiskSummary

    @classmethod
    def load(cls, path: Path) -> "RequestArtifact": ...
    def save(self, path: Path) -> None: ...     # atomic write, mode 0o600
    def validate(self, *, project_id: str, expected_server: str | None = None
                ) -> None:                      # raises ArtifactError(failure_class, exit_code, msg)
        """Schema → expiry → server consistency → sandbox → denylist.
        SHA-256 verify is OUTSIDE this method (it requires a fresh local read; lives
        in run_request main()). Lock acquisition is also OUTSIDE."""
        ...


class ArtifactError(Exception):
    def __init__(self, failure_class: str, exit_code: int, message: str): ...
```

**Rationale**: 集中 validation = 单元测试一处覆盖 5 个 CLI;`save()` 内置 atomic write 杜绝半写文件;`load()` 拒绝 `version != 1` 让未来 schema 升级不向前 silently 兼容。

**Why / How-to-apply**: drafter `__main__` 里写 `art = build_artifact(args); art.save(path)`;executor `__main__` 里写 `art = RequestArtifact.load(path); art.validate(project_id=pid, expected_server=args.server)`。Exception → JSON envelope `error` field + exit code,不在 main 里手写 if/else。

### D2 — `<project_id>` 解析算法

**Decision**: 严格顺序,first non-empty wins。失败 (3 source 全空) 时 hard fail,与 `references/workdir-sandbox.md:22` 立场一致。

```python
# lib/sandbox.py

import os
import subprocess
from pathlib import Path


def resolve_project_id(args, env: dict[str, str], cwd: Path) -> str:
    # 1. explicit flag (--project-id)
    explicit = getattr(args, "project_id", None)
    if explicit:
        return explicit
    # 2. env var
    env_val = env.get("SSH_GUARDED_PROJECT_ID")
    if env_val:
        return env_val
    # 3. git repo basename
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), stderr=subprocess.DEVNULL, text=True
        ).strip()
        if root:
            return Path(root).name
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    raise ArtifactError(
        failure_class="project_id_unresolved",
        exit_code=1,
        message="--project-id not given, SSH_GUARDED_PROJECT_ID not set, "
                "and cwd is not a git repo. Resolve one before retrying."
    )
```

**Rationale**: 显式 > env > implicit (git);失败显式抛错避免"silently use 'default' 字面量"事故。

**Why / How-to-apply**: 每个 drafter 在 build payload 前调一次,把结果同时塞进 artifact 不必要 — 只在 validate 时再算一次 (executor 端 cwd 可能不同,这点是 feature:executor 用本机 project_id 验证 artifact 的 sandbox 是否仍然对)。

### D3 — `request_id` 生成与碰撞

**Decision**: `uuid4().hex[:8]`;碰撞处理 = `open(O_CREAT|O_EXCL)`,失败 retry 至多 3 次,3 次全失败 → exit 1 + 通用 "unable to allocate request_id"。不做语义重 (不内嵌时间戳、不计数器)。

**Rationale**: uuid4 8 hex = 32 bit = 4.3 G 唯一值;reports/requests/ 单目录达到 1 万个 artifact (远超人类管理上限) 时碰撞概率仍 < 10⁻¹⁴。`O_CREAT|O_EXCL` 是 POSIX 原子操作,免锁去重。

**Why / How-to-apply**: `lib/request_artifact.py` 提供 `allocate_request_id(operation, requests_dir) -> str`,内部循环 generate + try-open-excl + 占位文件;成功后 close,返回 id。drafter 用这个 id 拼路径,后续 `art.save(path)` 写真实内容覆盖占位 (`os.replace` 原子)。

### D4 — `risk_summary` 作者

**Decision**: drafter 端单方面写 `risk_summary` 3 个字段,用户不能 override。`irreversible` 由 op + payload 决定 (delete 恒 true;upload overwrite=true 时 true;mkdir 永远 false;command 由 stderr 启发式 — 见下表)。`blast_radius` 一行人话,由 drafter 拼模板。`scope` 是 4 个枚举之一。

| Operation | irreversible | scope | blast_radius template |
|---|---|---|---|
| `command` | regex 命中 `\brm\b / \bmv\b / >[^>] / systemctl / sudo\b` 之一 → true | `host` if sudo else `service` if systemctl else `tree` if rm/mv else `single-file` | `"runs '<first 60 chars of command>' as <user>@<server> in <workdir>"` |
| `upload`  | overwrite=true → true; else false | `single-file` | `"writes <local_size> bytes to <remote_path> on <server>; overwrite=<bool>"` |
| `mkdir`   | false | `single-file` | `"creates <remote_path> (mode <mode>) on <server>"` |
| `delete`  | true (always) | `tree` if recursive else `single-file` | `"deletes <remote_path> on <server>; recursive=<bool>"` |

**Rationale**: 把 risk classification 锁在 drafter = 用户和 agent 都不能"事后调低"风险;`run_request --execute` 读 `irreversible=true` 是触发 `--confirm-delete` 要求的唯一开关 (FR-5),所以这字段不能被随意改。

**Why / How-to-apply**: 实现进 `lib/request_artifact.py:_compute_risk(op, payload, server, project_id) -> RiskSummary`,drafter 直接调,不传 user-provided 字段。`run_request` 读到 artifact 时如果 `risk_summary` 字段缺失/非法 → exit 53 `artifact_invalid`。

### D5 — `request_upload` SHA-256 streaming

**Decision**: drafter 端用 `hashlib.sha256()` + 128 KiB block 流式读 `--local`,写 `payload.local_sha256` + `payload.local_size`。Executor 端 (`run_request --execute`) **再算一次**,与 artifact 比对;不匹配 → exit 56 `sha256_mismatch`。

```python
def sha256_streaming(path: Path, block: int = 131072) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(block):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
```

**Rationale**: drafter 与 executor 之间用户可能修改了本地文件 (例如 release tarball 重打了);此时上传的是新版,但 artifact 还在描述旧版,审计链断裂。Re-verify 关闭这个 drift。

**Why / How-to-apply**: 128 KiB block 与 ADR-003 D2 `SftpSession.put_chunked` 默认 block 一致 (`adr-003-sftp-transport.md:78`),减少 I/O syscall 数;不需要 mmap,大文件 256 MiB cap (FR-1 non-functional) 内 `read(131072)` 循环足够快。Executor 端 mismatch 时 envelope 必带 `data.expected_sha256` + `data.actual_sha256`,与 ssh_upload `transfer_verify_failed` (exit 9) 同结构 (ADR-003 D3)。

### D6 — `request_delete` 双层防护

**Decision**: drafter 端做一次 pre-check stat;不带 `--recursive` 删 dir → exit 46。executor 端 **重做一次** stat (drift 防护) + 若 `risk_summary.irreversible=true` 且未传 `--confirm-delete` → exit 59。

**Drafter pre-check stat 实现** (2026-05-27 Phase B drift B3 同步):因 §7 禁止跨 plugin import `lib.sftp_transport`,drafter 改为 subprocess 调 ssh-core 的 `ssh_execute.py` 间接 stat,而非 paramiko SftpSession:

```
argv = ["python", str(SSH_CORE_SCRIPTS_DIR / "ssh_execute.py"), alias,
        f"test -d {shlex.quote(remote)} && echo dir || ([ -e {shlex.quote(remote)} ] && echo file || echo none)",
        "--json"]
proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
env = json.loads(proc.stdout)              # ssh-core envelope
last = env.get("stdout","").strip().splitlines()[-1] if env.get("stdout") else ""
# last ∈ {"dir", "file", "none"}
# subprocess returncode != 0 OR JSON parse fail OR last 非 3 选 1 → exit 49 pre_check_stat_failed
```

`last == "dir"` 且未 `--recursive` → exit 46;`last == "none"` → drafter stderr warn ("does not exist on <alias>") 但仍写 artifact (executor 用 `rm -f` 自然吞掉)。

```
Drafter (request_delete.py):
    1. resolve project_id → validate workdir sandbox (warn-only, per C3 last bullet)
    2. subprocess-call ssh_execute test → parse last stdout line
       - "dir" + not args.recursive → exit 46 recursive_required_for_directory
       - "none" → stderr warn + payload.is_directory_pre_check=false + continue
       - subprocess fail / non-JSON / unrecognized → exit 49 pre_check_stat_failed
    3. denylist check → if hit, warn + still write artifact (so user sees executor refusal)
    4. set payload.is_directory_pre_check = (last == "dir")
    5. risk_summary.irreversible = True (always)
    6. save artifact

Executor (run_request.py --execute, when artifact.operation == "delete"):
    1. art.validate(...)   # schema + expiry + server + sandbox + denylist
    2. (drift D6 simplification: executor 不重做 stat — payload.is_directory_pre_check
        + sandbox/denylist 已足够;TOCTOU 在 ssh-core dispatch 端由 `rm -rf -- <quoted>`
        natural semantics 承担。如 v1.1 需要 strict re-stat,可在 lock 内插入)
    3. if art.risk_summary.irreversible and not args.confirm_delete → exit 59
    4. acquire lock → dispatch ssh_execute "rm [-rf] -- <quoted>"
```

**Rationale**: drafter 的 stat 给用户一个 review-time 信号 ("这是个 dir,你确定要 --recursive 吗");executor-side re-stat 在 Phase C 实施中被收敛掉 — payload.is_directory_pre_check 已经在 artifact 里 immutable (artifact 已 0o600 + lockfile),sandbox/denylist 验证已足够,re-stat 收益边际而代价高 (再多一次 ssh round-trip)。`--confirm-delete` 强制把"我确认要删"放在 execute argv 里,审计链上看得见。

**Why / How-to-apply**: pre-check stat 取代 SftpSession 的设计权衡见 prd.md Notes Phase B drift B3。`last == "none"` 路径是 Phase B 的兜底实现,design 原文未明示,见 prd.md Notes Phase B drift B2。stat subprocess 失败 → exit 49 with stderr "drafter cannot reach remote for pre-check; pass --force to skip (deferred to v1.1)"。

### D7 — `run_request` 验证顺序

**Decision**: 7 步线性,每步独立 exit code:

1. **schema** → `RequestArtifact.load(path)` 失败 (JSON 非法 / version 错 / 字段缺) → exit 53 `artifact_invalid`
2. **expiry** → `datetime.now(UTC) >= expires_at` → exit 53 `artifact_expired` (注意区分 message,同 exit code 但 data.reason 不同)
3. **server consistency** → `args.server` 给定且 `!= artifact.server` → exit 53 `artifact_invalid` + `data.reason="server_mismatch"`
4. **denylist** → `is_in_denylist(payload.remote_path or payload.workdir)` → exit 51
5. **sandbox** → `is_inside_sandbox(payload.remote_path or payload.workdir, project_id)` → exit 50
6. **SHA-256 (upload only)** → re-compute local sha256 + size; 不匹配 → exit 56
7. **lock acquire** → fcntl.flock LOCK_EX|LOCK_NB → 失败 exit 54

成功后 dispatch (D8)。

**Rationale**: 先 schema 再 expiry 是为了让 "artifact 文件本身就坏了" 的错误优先于 "时间过了" 的错误,便于 caller debug。Denylist **先于** sandbox 是 2026-05-28 drift 修正:denylist 是"红线"(`~/.ssh/`, `~/.aws/` 等具体危险路径),sandbox 是"默认边界"(workspace 之外)。两者对 `~/.ssh/authorized_keys` 都会拒绝,但红线优先意味着审计日志归因到"碰了禁字"而非"出了 workspace",对用户更直接;反过来若 sandbox 先则永远拿不到 exit 51。原始 design "absolute reject 优先" 的 rationale 在落地时被 PRD 第 199 行的 `test_denylist_refusal_live` 期望反推,顺序对调即可,语义没冲突。SHA-256 放后面避免 expired artifact 还浪费一次磁盘 I/O;lock 最后是为了 lock hold time 最小化 (验证失败也不会 hold 锁)。

**Why / How-to-apply**: 用 `try/except ArtifactError` 围 1-6,统一 catch 后 → emit envelope + return exit_code。7 单独 `with fcntl_lock(path) as lk:` block,块内才 dispatch。`RequestArtifact.validate()` 的 docstring step 顺序与本表 verbatim。

### D7a — Transport-time `~` 展开 (2026-05-28 drift)

**Decision**: artifact 中 `remote_path` / `workdir` 永远是字面 `~/workspace/...`(便于 audit / blast_radius 渲染);**transport 边界**(run_request `--execute`,获得锁之后、dispatch 之前)调用 `_resolve_remote_home(server)` 一次,把 `~/` 在 argv 构造时展开为远端绝对路径,再交给 ssh-core。

**Why**: 落地后 live test 暴露两个独立 bug,根因相同——`~/` 在两条 transport 路径都不展开:
1. paramiko SFTP put 把 `~/workspace/x` 字面写到远端 `/root/~/workspace/x`(创建字面 `~` 目录)
2. `shlex.quote("~/workspace/x")` 加单引号后,远端 shell 也不再展开 `~`,`mkdir -- '~/workspace/x'` 同样创建字面目录

ssh_execute 跑无引号 `cd ~/foo` 没问题(shell-level expansion),但 quoted argument 不行。把展开收口在 ssh-guarded transport 层,既不污染 ssh-core,也不破坏 artifact 字面值。

**How**: `run_request._resolve_remote_home(server)` 执行 `ssh -o BatchMode=yes -o ConnectTimeout=10 <server> 'printf %s "$HOME"'`;`returncode != 0` / 超时 / stdout 不是绝对路径 → `ArtifactError(transport_failed, 57)`,catch 时归到 `data.transport_returncode` / `data.transport_stderr`(与子进程 transport 失败同壳)。`_build_argv(art, remote_home=...)` 接受可选 home,内部 `_expand_remote_tilde(path, home)` 处理:`~` / `~/` → `home`,`~/<rest>` → `home/<rest>`,其它(包括 `~user/`、绝对路径、相对路径)原样过。

**Cost**: 每次 `--execute` 多一次 ~50ms 的 SSH probe;happy path 可接受。v1.1 可缓存到 result.json 旁的 sidecar(目前不必要)。

### D8 — Transport routing

**Decision**: 4 个 op → 2 个 ssh-core 脚本,mkdir/delete 复用 `ssh_execute.py` 跑 one-liner。

| Op | Transport invocation (argv list) |
|---|---|
| `command` | `["python", ssh_execute_path, server, command, "--timeout", str(timeout), "--json"]` |
| `upload`  | `["python", ssh_upload_path, server, local_path, remote_path] + (["--overwrite"] if overwrite else []) + ["--json"]` |
| `mkdir`   | `["python", ssh_execute_path, server, f"mkdir {'-p ' if parents else ''}-m {mode} -- {shlex.quote(remote_path)}", "--json"]` |
| `delete`  | `["python", ssh_execute_path, server, f"rm {'-rf' if recursive else '-f'} -- {shlex.quote(remote_path)}", "--json"]` |

`run_request` 用 `subprocess.run(argv, capture_output=True, text=True, timeout=<artifact.payload.timeout or 600>)`,解析子进程 stdout JSON,把 `exit_code / stdout / stderr` 摘出来落到 result.json (D9)。失败 (子进程 returncode != 0 且不是 remote command 自身非零) → exit 57 `transport_failed`,envelope 携带 `data.transport_returncode / data.transport_stderr`。

**Rationale**: 不引入新 transport 层 = 复用 ssh-core 已经稳定的 JSON envelope、failure_class、known-hosts noise filtering;mkdir/delete 用 `ssh_execute` 跑 one-liner 而不写 ssh_remove.py 是因为这 2 个操作太薄,且 quoting 通过 `shlex.quote` 已经安全 (ADR-001 D1 + CONTRIBUTING.md:37 "argv list, never shell strings" — 这里 string 是 remote shell 的 input,不是 local shell 的)。

**Why / How-to-apply**: 路径解析 (`ssh_execute_path` / `ssh_upload_path`):lib 位于 `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/request_artifact.py`,需 `Path(__file__).resolve().parents[5] / "ssh-core/skills/ssh-core/scripts"`(parents[5] = `plugins/`,parents[3] 设计原稿算错,2026-05-26 Phase A 修正);为了让测试覆盖,在 `lib/request_artifact.py` 顶 expose `SSH_CORE_SCRIPTS_DIR` 变量,测试用 monkeypatch 替换。

### D9 — Result artifact write

**Decision**: 子进程返回后,`run_request` 把:
- `executed_at`: 子进程启动前的 `datetime.now(UTC).isoformat()`
- `exit_code`: 子进程 envelope 的 `exit_code` (即 remote command exit code)
- `duration_ms`: `int((monotonic_end - monotonic_start) * 1000)`
- `stdout_redacted`: `Redactor(catalog).apply(子进程 envelope.stdout)[0]`
- `stderr_redacted`: 同上
- `transport`: `"ssh_execute" | "ssh_upload"` (D8)
- `transport_exit_code`: 子进程的 OS-level returncode (与 `exit_code` 通常一致,但 timeout 时 124 vs envelope exit)
- `transport_argv`: **默认 `null`**,仅当 `--no-redact` 全局 flag 设置时填真实 argv list (O4 默认,2026-05-27 Phase C drift C2 确认);字段名稳定,reader 不因 redact 模式分支。Rationale:argv 中可能含 alias/路径等 audit-sensitive 信息,默认不写盘,审计模式 `--no-redact` 显式开启。

打包成 dict → 原子写 `<id>.result.json` (temp + rename, 同 dir,mode 0o600)。`--no-redact` 全局 flag 时 `stdout_redacted` / `stderr_redacted` 字段名不变 (兼容 reader) 但内容原样。

**Rationale**: 原子写防止 SIGINT 留半截 result.json 误导后续 audit;mode 0o600 与 artifact 一致 (`ssh_config_manager` ADR-002 D4 同款 atomic write_with_backup);字段名不变避免下游 (exec_detached / 人类) 因 `--no-redact` 走不同 schema 分支。

**Why / How-to-apply**: 实现一个 `lib/request_artifact.py:save_result(art, result_dict, redactor, out_dir=None)` helper;`redactor=None` 时 bypass。`out_dir=None` 默认 `reports/requests/`(Phase A);executor 实际调用时 override `out_dir=art_path.parent` 把 result 写到 artifact 所在目录 (2026-05-27 Phase C drift C7)。result_dict 不再 dataclass (字段稳定,但允许未来 additive without schema churn)。

### D10 — Argparse parent parser pattern (SUPPRESS)

**Decision**: 4 个 drafter + 1 个 executor 共享的 flag (`--json`, `--project-id`, `--no-redact`) 走 `parents=[common]`,common parser 用 `default=argparse.SUPPRESS`,`main()` 用 `getattr(args, "field", default)` 读。

```python
def build_common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--project-id", default=argparse.SUPPRESS)
    p.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    return p

def build_parser_for_request_command() -> argparse.ArgumentParser:
    common = build_common_parser()
    p = argparse.ArgumentParser(prog="request_command.py", parents=[common])
    p.add_argument("alias")
    ...
    return p

# in main():
as_json = getattr(args, "json", False)
project_id_explicit = getattr(args, "project_id", None)
```

**Rationale**: 直接来自 ADR-002 D7 教训 — 不用 SUPPRESS 会 silently clobber 父值,初版实测写穿 `~/.ssh/config`。本任务 8 个共享 flag (5 个脚本 × 平均 2-3 个 shared) 任一处 miss 都重蹈覆辙。

**Why / How-to-apply**: 5 个脚本统一 import 同一个 `build_common_parser()`;不允许 redeclare。CI / pre-commit 加一条 ruff 规则 (或 pytest meta test) "no `--json` declared outside `build_common_parser`" 是 v1.1 改进项,v1.0 靠 PR review。

### D11 — `--expires-in 1h` 解析

**Decision**: 接受 `<int><unit>` 字面量,unit ∈ {m, h, d},converted to seconds → cap to 7d (604800 s) → default 1h (3600 s)。非法格式 → exit 34 `expiry_invalid`。

```python
import re
_EXPIRES_RE = re.compile(r"^(\d+)([mhd])$")

def parse_expires_in(s: str | None) -> int:
    if s is None:
        return 3600
    m = _EXPIRES_RE.fullmatch(s.strip())
    if not m:
        raise ArtifactError("expiry_invalid", 34, f"--expires-in: '{s}' not in <n><m|h|d> form")
    n, unit = int(m.group(1)), m.group(2)
    seconds = n * {"m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 604800:
        raise ArtifactError("expiry_invalid", 34, f"--expires-in: '{s}' exceeds 7d cap")
    if seconds < 60:
        raise ArtifactError("expiry_invalid", 34, f"--expires-in: '{s}' below 60s floor")
    return seconds
```

**Rationale**: 字面量比 ISO 8601 duration (`PT1H`) 用户体验好;7d cap 防止用户/agent 误写 `--expires-in 365d` 把 artifact 锁久过 audit 周期;60s floor 防止 typo (`--expires-in 1` 被解读成 1m 还是 1s 太模糊,统一拒绝)。

**Why / How-to-apply**: 进 `lib/request_artifact.py:parse_expires_in()`,drafter 端调用,executor 端只读 artifact 里 `expires_at` ISO8601 字符串 (drafter 算好的)。

### D12 — 测试策略

**Decision**: ~80-100 unit + 6 live_ssh,分布如下:

| Module | Unit cases | Notes |
|---|---|---|
| `lib/sandbox.py` | 12 | `resolve_project_id` (4 branches × 各 1-2) + `is_inside_sandbox` (5 happy + 3 escape) + `is_in_denylist` (5 patterns × 1) |
| `lib/redact.py` | 8 | default catalog load + custom catalog + missing file fallback + each pattern type (AKIA/JWT/RSA-header) + `--no-redact` bypass |
| `lib/lockfile.py` | 6 | acquire/release + double-acquire (LOCK_NB raises) + holder pid read + Windows degraded path (mocked) + cleanup on exception + stale lock semantics |
| `lib/request_artifact.py` | 16 | load valid + load bad-version + load missing field + save atomic (temp leftover check) + validate happy × 4 op + validate expiry + validate sandbox refusal + validate denylist refusal + risk_summary computation × 4 op |
| `request_command.py` | 12 | help + happy path artifact written + workdir warn-but-write + reason missing → 31 + timeout above max → 30 + env max 32 + --expires-in parse × 3 + JSON envelope shape + SUPPRESS pattern smoke |
| `request_upload.py` | 14 | help + happy path + local missing → 35 + outside upload_roots → 36 + sensitive without --confirm → 38 + size > 256MiB → 39 + sha256_compute_failed mocked → 37 + overwrite flag in risk_summary + remote outside sandbox warn-write + JSON envelope + SUPPRESS smoke + ... |
| `request_mkdir.py` | 8 | help + happy + mode invalid → 40 + remote outside sandbox warn-write + parents required → 42 + JSON envelope + SUPPRESS smoke + ... |
| `request_delete.py` | 10 | help + happy file + happy dir+recursive + dir w/o recursive → 46 + denylist warn-write + SFTP stat fail → 49 + JSON envelope + SUPPRESS smoke + irreversible always true + recursive flag in scope |
| `run_request.py` | 18 | help + schema bad → 53 + expired → 53 + server mismatch → 53 + sandbox refusal → 50 + denylist refusal → 51 + sha256 mismatch → 56 + lock held → 54 + delete needs --confirm → 59 + happy dispatch command (mocked) + happy dispatch upload (mocked) + happy dispatch mkdir + happy dispatch delete + transport failed → 57 + result.json schema check + redaction applied to stdout + --no-redact bypass + --show-sensitive display flag |
| **Total unit** | **104** | within 80-100 target +4 |

Live SSH (`tests/integration/test_ssh_guarded_pipeline_live.py`,marker `live_ssh`):
1. `test_command_happy_path_live`
2. `test_upload_happy_path_live`
3. `test_mkdir_happy_path_live`
4. `test_delete_happy_path_live` (mkdir → delete --recursive --confirm-delete)
5. `test_denylist_refusal_live` (`request_delete pai --remote ~/.ssh/authorized_keys`)
6. `test_sandbox_refusal_live` (`request_command pai "echo x" --workdir /etc`)

**Rationale**: lib 单独测覆盖率高 + 测试快;CLI 测试 mock ssh-core dispatch (`subprocess.run` monkeypatch) 防止变 live;live 集中在 6 个 pipeline-level 验证,与 ssh-core ssh_upload integration test 节奏 (~6 cases) 持平。

**Why / How-to-apply**: 测试目录 `tests/test_ssh_guarded_<name>_unit.py` (per script + per lib) + `tests/test_ssh_guarded_pipeline_live.py`。pai fixture 复用 ssh-core 的 (`tests/conftest.py` 既有 `pai_alias` / `pai_workspace` fixture)。

---

## §4. Shared library sketches

### 4.1 `lib/sandbox.py` (~120 LOC)

```python
"""Workdir sandbox + denylist enforcement for ssh-guarded.

Stable surface:
    resolve_project_id(args, env, cwd) -> str
    is_inside_sandbox(remote_path: str, project_id: str) -> bool
    is_in_denylist(remote_path: str) -> bool
    normalize_remote_path(remote_path: str) -> str       # expand ~, resolve .., reject ..
"""
from __future__ import annotations
import os, re, subprocess
from pathlib import Path, PurePosixPath

_DENY_PREFIXES = (
    "~/.ssh/", "~/.aws/", "~/.kube/", "~/.gnupg/", "~/.config/gh/",
)
_DENY_BASENAMES_RE = re.compile(
    r"^(?:\.env(\..+)?|id_rsa(\..+)?|id_ed25519.*|.+\.pem|.+\.key|"
    r".+\.p12|.+\.pfx|authorized_keys|known_hosts)$"
)

def resolve_project_id(args, env: dict[str, str], cwd: Path) -> str: ...
def normalize_remote_path(p: str) -> str: ...
def is_inside_sandbox(remote_path: str, project_id: str) -> bool: ...
def is_in_denylist(remote_path: str) -> bool: ...
```

### 4.2 `lib/redact.py` (~100 LOC)

```python
"""Catalog-driven redaction for ssh-guarded outputs.

Stable surface:
    class Redactor:
        def __init__(self, catalog_path: Path | None = None): ...
        def apply(self, text: str) -> tuple[str, list[dict]]:
            # returns (redacted_text, hits)
            # hits: list of {kind, count, replace_with}
"""
from __future__ import annotations
import json, re
from pathlib import Path

_DEFAULT_PATTERNS = [
    {"kind": "aws_access_key", "regex": r"AKIA[0-9A-Z]{16}",
     "replace_with": "[REDACTED-AWS-ACCESS-KEY]"},
    {"kind": "github_token", "regex": r"gh[pus]_[A-Za-z0-9]{36}",
     "replace_with": "[REDACTED-GH-TOKEN]"},
    {"kind": "jwt", "regex": r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}",
     "replace_with": "[REDACTED-JWT]"},
    {"kind": "rsa_private", "regex": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
     "replace_with": "[REDACTED-PRIVATE-KEY-HEADER]"},
]

class Redactor:
    def __init__(self, catalog_path: Path | None = None): ...
    def apply(self, text: str) -> tuple[str, list[dict]]: ...
```

### 4.3 `lib/lockfile.py` (~80 LOC)

```python
"""Cross-platform exclusive file lock for run_request --execute.

Stable surface:
    @contextmanager
    def exclusive_lock(path: Path) -> Iterator[LockInfo]:
        # acquires `path` + ".lock" via fcntl.flock(LOCK_EX|LOCK_NB) on POSIX.
        # On Windows: falls back to <path>.lock existence + pid sentinel.
        # raises LockHeld(holder_pid: int | None) on contention.
"""
from __future__ import annotations
import os, sys
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass

@dataclass
class LockInfo:
    path: Path
    holder_pid: int

class LockHeld(Exception):
    def __init__(self, holder_pid: int | None): ...

@contextmanager
def exclusive_lock(path: Path): ...
```

### 4.4 `lib/request_artifact.py` (~180 LOC)

(see D1 sketch — adds `allocate_request_id`, `parse_expires_in`, `save_result`, `compute_risk` private helpers.)

---

## §5. Module / file layout

```
plugins/ssh-guarded/skills/ssh-guarded/scripts/
├── __init__.py                       (unchanged)
├── request_command.py                ← rewrite stub → ~180 LOC
├── request_upload.py                 ← rewrite stub → ~210 LOC (sha256 + size + sensitive)
├── request_mkdir.py                  ← rewrite stub → ~150 LOC
├── request_delete.py                 ← rewrite stub → ~200 LOC (SFTP stat pre-check)
├── run_request.py                    ← rewrite stub → ~280 LOC (7-step validate + dispatch + result)
├── exec_detached.py                  (unchanged; sibling child task)
├── redact_check.py                   (unchanged; sibling child task)
├── scan_software.py                  (unchanged; sibling child task)
└── lib/                              ← NEW DIR (Phase A delivered 2026-05-26)
    ├── __init__.py                   ← re-export stable surface (mimics ssh-core's lib/__init__.py:1-12)
    ├── _errors.py                    ← NEW, off-design (Phase A): holds ArtifactError only, breaks sandbox<->request_artifact circular import; __init__.py re-exports it so public surface unchanged
    ├── request_artifact.py           ← NEW (~600 LOC actual; design budget 180; over-budget driven by explicit per-field schema validation in load(), 4 risk-compute helpers, iso/uuid helpers, atomic-write helper)
    ├── sandbox.py                    ← NEW (~200 LOC actual; budget 120; over-budget driven by normalize/expand-tilde edge cases and detailed denylist match logic)
    ├── redact.py                     ← NEW (~150 LOC actual; budget 100)
    └── lockfile.py                   ← NEW (~220 LOC actual; budget 80; over-budget driven by POSIX flock + Windows degraded + holder_pid lookup paths)

tests/
├── test_ssh_guarded_lib_sandbox_unit.py            (NEW; 12 cases)
├── test_ssh_guarded_lib_redact_unit.py             (NEW; 8 cases)
├── test_ssh_guarded_lib_lockfile_unit.py           (NEW; 6 cases)
├── test_ssh_guarded_lib_request_artifact_unit.py   (NEW; 16 cases)
├── test_ssh_guarded_request_command_unit.py        (NEW; 12 cases)
├── test_ssh_guarded_request_upload_unit.py         (NEW; 14 cases)
├── test_ssh_guarded_request_mkdir_unit.py          (NEW; 8 cases)
├── test_ssh_guarded_request_delete_unit.py         (NEW; 10 cases)
├── test_ssh_guarded_run_request_unit.py            (NEW; 18 cases)
└── test_ssh_guarded_pipeline_live.py               (NEW; 6 live_ssh cases)
```

---

## §6. Exit code summary (本 child 范围)

| Exit | Script | failure_class | Trigger |
|---|---|---|---|
| 0 | (any) | None | Success (drafter wrote / executor finished w/ remote exit 0) |
| 1 | (any) | generic | Unhandled / project_id unresolved (D2) / request_id allocation exhausted (D3) |
| 30 | request_command | command_payload_invalid | empty command / timeout > 3600 |
| 31 | request_command | reason_missing | (argparse `--reason` 缺,实际由 argparse 拒绝; reserved) |
| 32 | request_command | workdir_outside_sandbox | drafter warn re-raise (only when also fatal at drafter time, rare) |
| 33 | request_command | env_too_many | >32 entries |
| 34 | request_command | expiry_invalid | D11 violations |
| 35 | request_upload | local_path_missing | `--local` 文件不存在 |
| 36 | request_upload | local_path_outside_upload_roots | 未 --confirm-sensitive-local-upload 且超 upload_roots |
| 37 | request_upload | sha256_compute_failed | IOError mid-stream |
| 38 | request_upload | sensitive_local_unconfirmed | --confirm-sensitive-local-upload 缺 |
| 39 | request_upload | size_limit_exceeded | >256 MiB |
| 40 | request_mkdir | mode_invalid | octal 非 0..0777 |
| 41 | request_mkdir | remote_path_outside_sandbox | drafter warn re-raise (rare) |
| 42 | request_mkdir | parents_required_for_nested | path 含 >1 missing parent 且未 --parents |
| 45 | request_delete | remote_path_outside_sandbox | warn re-raise (rare) |
| 46 | request_delete | recursive_required_for_directory | is_dir + 未 --recursive |
| 47 | request_delete | path_in_denylist | 命中 C4 (drafter side, warn-but-write 例外:此处 drafter 仅 warn) |
| 49 | request_delete | pre_check_stat_failed | SFTP stat 失败 (网络/auth/permission) |
| 50 | run_request | workdir_outside_sandbox | sandbox refusal at execute |
| 51 | run_request | path_in_denylist | denylist refusal at execute |
| 53 | run_request | artifact_invalid / artifact_expired | schema fail OR expiry past OR server mismatch (data.reason 区分) |
| 54 | run_request | lock_held | fcntl.flock LOCK_NB 拒绝;envelope `data.holder_pid` (POSIX 尽力填,Windows degraded 读 sentinel pid;读不到设 null) |
| 56 | run_request | sha256_mismatch | upload 重算 sha256 与 artifact 不一致;envelope 必带 `data.expected_sha256 / data.actual_sha256` (与 ADR-003 D3 transfer_verify_failed envelope shape 同款) |
| 57 | run_request | transport_failed | subprocess.run 失败 / non-zero non-envelope;envelope 带 `data.transport_returncode / data.transport_stderr` |
| 59 | run_request | delete_confirm_required | irreversible=true 且未 --confirm-delete (仅 operation=delete,见 prd.md Notes Phase C drift C4) |
| 124 | (any) | timeout | subprocess.TimeoutExpired (与 ADR-001 D6 一致) |

---

## §7. Compatibility notes

- 不影响 ssh-core 任何已 ship 脚本 — 本 task 通过 `subprocess.run(["python", ssh_execute_path, ...])` 调用,不 import ssh-core lib (避免 sys.path hack)。
- 不影响 ADR-001/002/003 — 本 task 是 ADR-004 候选,不修改既有 ADR。
- 不修改 `pyproject.toml` — paramiko 已就位 (ADR-003 D1)。
- SKILL.md surface 已与 v1.0 implementation 一致 (`SKILL.md:54-93`);commit 时**不**改 SKILL.md。

---

## §8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| `lib/sandbox.resolve_project_id` 在 CI / 非 git 目录被调用 → 直接 hard fail 影响测试 | unit test 用 `monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "test-project")` 绕过;integration test 在真 repo 内跑,git rev-parse 自然有结果 |
| Windows lockfile degraded 模式 stale lock | 文档化:`.lock` 文件 24h 未变 + sentinel pid 不存活 → 视为 stale,allow override。v1.0 不自动清理,作为 follow-up |
| `~/.devops/redact.json` 用户配置错 → 加载报 regex_compile_failed | `lib/redact.py` 在加载时 try/except per pattern;失败的 pattern skip + warn 到 stderr,其他 pattern 继续。Hard fail 留给 `redact_check.py` (sibling child) |
| drafter SFTP stat 在 delete pre-check 网络慢 → 用户体验差 | 文档化:delete drafter 比其他 3 个 drafter 慢 ~300 ms 起步;`--force` (deferred v1.1) 是逃生口。v1.0 不优化 |
| `run_request --execute` 调 `ssh_execute.py` 时 PYTHONPATH 找不到 `lib/` | 子进程从 `ssh-core/scripts/` 目录跑,`ssh_execute.py` 自己解决 lib import (已 ship,无影响);本 task 不依赖 ssh-guarded lib 被 ssh-core 看见 |
| artifact mode 0o600 在 Windows 写不上 | os.chmod 在 Windows 是 best-effort (NTFS ACL 不映射);测试 skip windows 该断言,行为对齐 ssh_config_manager atomic_write (ADR-002 D4) |
| 5 个脚本 SUPPRESS 模式跑偏 | unit test 加一个 `test_no_shared_flag_redeclared_outside_common_parser` 元测试 (用 `inspect` 扫 build_parser) — v1.0 ship 后第一次 violation 时再加,不预先复杂化 |
