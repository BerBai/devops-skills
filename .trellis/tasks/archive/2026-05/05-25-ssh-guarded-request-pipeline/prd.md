# ship ssh-guarded request/execute pipeline v1.0

## Goal

把 `ssh-guarded` 的 5 个 request/execute 脚本 — `request_command.py`, `request_upload.py`, `request_mkdir.py`, `request_delete.py`, `run_request.py` — 从 v0.2 scaffold (返回 `exit_code=2 + UNIMPLEMENTED_NOTE`, 见 `plugins/ssh-guarded/skills/ssh-guarded/scripts/request_command.py:23-66`、`request_upload.py:27-68`、`request_mkdir.py:21-55`、`request_delete.py:28-64`、`run_request.py:28-59`) 一次性推到 v1.0 完成态。同时落 4 个共享 lib (`request_artifact.py / redact.py / sandbox.py / lockfile.py`),供后续 child (exec_detached / redact_check / scan_software) 复用。

闭环语义遵循 `plugins/ssh-guarded/skills/ssh-guarded/SKILL.md:28-43` 的 12 个 hard checkpoints:第 7 步 (Draft request) → 第 8 步 (Show request) → 第 9 步 (Execute) 三个阶段各对应 `request_*.py drafter` → 人类 review → `run_request.py --execute`。本 child 把这条链路从"docstring 描述"推到"可在 pai fixture 上跑通"。

5 个脚本绑在同一个 PRD/design 里 ship 的理由 (locked):它们共享同一个 `<id>.json` artifact contract、同一套 sandbox/denylist 验证逻辑、同一个 lockfile 协议。拆 child 会让 contract 反复迁移、lib 抽象提前两轮才稳定下来,得不偿失。这与 ssh-core Phase 1 "一个 stub 一个 child" 的节奏不同 — 那批每个 stub 都有独立 transport 概念 (config-manager / SFTP / 4-mode transfer),此处 5 个脚本是一个 contract 的 5 个 verb。

## Confirmed facts (from repo inspection)

1. **5 个 stub 当前形态**:全部是 ~60 loc argparse skeleton + `UNIMPLEMENTED_NOTE` 占位,`exit_code=2`。`v1.0 surface:` docstring 在 `request_mkdir.py:4-13`、`request_delete.py:7-20`、`request_upload.py:6-18`、`request_command.py:7-14`、`run_request.py:3-19` 已写定,本任务 1:1 落到实现。
2. **没有 `fcntl.flock` 现有实现**:`grep -rn "fcntl.flock" plugins/ tests/` 返回零命中。本任务首次引入,落在 `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/lockfile.py`。Windows 路径退化为"`<path>.lock` 文件存在性 + sentinel content=pid" — 与 ssh-core 现有 Windows 边角案处理 (`lib/__init__.py:58-66` MSYS guard) 同等保守。
3. **ssh-core lib 约定**:`plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:1-12` 定义稳定 surface (`json_result`, `emit`, `filter_ssh_noise`, `classify_failure` 等);本任务的 `lib/redact.py`/`lib/sandbox.py`/`lib/request_artifact.py`/`lib/lockfile.py` 沿用同一文件结构 (每个 lib 一文件, `__init__.py` 列出稳定签名)。
4. **现有 reports 目录**:`plugins/ssh-guarded/skills/ssh-guarded/reports/` 已存在 (见 `references/request-execute.md:13-22`)。`reports/requests/`、`reports/jobs/` 在 drafter / executor 首次使用时 `mkdir(parents=True, exist_ok=True)` 创建,不靠 git 预置。
5. **JSON envelope 与 ssh-core 一致**:`{success, exit_code, stdout, stderr, data}`,见 ADR-001 D3 (`.trellis/spec/backend/adr-001-ssh-execute.md:42-69`)。本 task 不引入新顶层字段,所有 ssh-guarded 特异信息 (request_id, sandbox 决策、denylist 命中) 落在 `data.*`。
6. **Per-script `failure_class` namespace 是项目惯例**:`ssh_config_manager` (exit 4-8)、`deploy_pubkey` (10-15)、`ssh_server_transfer` (20-29) 三个先例,见 `.trellis/tasks/05-24-devops-skills-v1-rollout/prd.md:86`。本任务的 5 个脚本占用 30-59 共 30 个 exit code,对齐这个节奏 (见 design §1 C5)。
7. **paramiko 已是 runtime dep**:ADR-003 D1 (`.trellis/spec/backend/adr-003-sftp-transport.md:54-58`) 已把 `paramiko>=3.0,<4` 加进 `pyproject.toml`。`request_delete` 的 SFTP stat (drafter 端做 `is_directory_pre_check`) 直接 `from lib.sftp_transport import SftpSession`,不再引入新依赖。
8. **argparse SUPPRESS 模式必须沿用**:见 `.trellis/tasks/05-24-devops-skills-v1-rollout/prd.md:83`。5 个脚本里 `--json`、`--project-id`、`--ssh-config`、`--no-redact` 是共享 flag,通过 `parents=[common]` 复用时必须用 `default=argparse.SUPPRESS`,`main()` 用 `getattr(args, "field", default)` 读。violation 会在第一次端到端测试 silent clobber 真实 `~/.devops/redact.json`,与 ssh_config_manager 写穿 `~/.ssh/config` 是同一个事故类 (ADR-002 D7)。
9. **redact 默认 catalog 与回落**:`~/.devops/redact.json` 缺失时,fall back 到编译进 `redact_check.py` 的 `_DEFAULT_PATTERNS` (见 design §1 C8)。`redact_check.py` 不归本 child 实现 (属于下一个 ssh-guarded child),但 `lib/redact.py` 必须 import-safe 在 catalog 不存在时也能工作 — `Redactor(None)` 走 default pattern set。
10. **SKILL.md 决策树已 reference 这些脚本**:`SKILL.md:54-60` 命名了 `request_command.py / request_upload.py / request_mkdir.py / request_delete.py`,`SKILL.md:89-93` 命名了 `run_request.py --execute`。本 task 不改 SKILL.md (surface 已准确);只在 ship commit 时按 sibling task 节奏决定是否加 1 行 Quick reference。

## v1.0 surface

5 个脚本最终 CLI 签名。`SUPPRESS` 标注的是 `parents=[common]` 通过 SUPPRESS 注入的共享 flag,各 subparser 不再 redeclare。`(v1.1)` 标注的是显式延后到下一版的 flag — drafter 不实现也不接受。

### `request_command.py`

```
request_command.py <alias> "<command>"
    --reason "<intent>"                   (required)
    [--workdir ~/workspace/<id>]
    [--timeout 60]                        (seconds, int, default 60, max 3600)
    [--expires-in 1h]                     (30m | 2h | 1d; default 1h; max 7d)
    [--env KEY=VAL ...]                   (repeatable; max 32 entries)
    [--project-id <id>]                   (SUPPRESS; overrides SSH_GUARDED_PROJECT_ID env + git repo basename)
    [--json]                              (SUPPRESS)
    [--dry-run]                           (v1.1 — defer)
    [--reviewer <handle>]                 (v1.1 — defer; multi-user approval)
```

### `request_upload.py`

```
request_upload.py <alias>
    --local <abs-or-rel-path>             (required)
    --remote <remote-path>                (required; must resolve inside ~/workspace/<project_id>/)
    --reason "<intent>"                   (required)
    [--overwrite]                         (default False; risk_summary auto-flags if remote exists per drafter SFTP stat)
    [--confirm-sensitive-local-upload]    (required if --local outside paths.upload_roots; auto-sets payload.sensitive_local=true)
    [--expires-in 1h]
    [--project-id <id>]                   (SUPPRESS)
    [--json]                              (SUPPRESS)
    [--size-limit 100M]                   (v1.1 — defer; v1.0 hard cap is 256 MiB in lib)
```

### `request_mkdir.py`

```
request_mkdir.py <alias>
    --remote <remote-path>                (required)
    --reason "<intent>"                   (required)
    [--mode 0755]                         (octal str; default "0755"; rejected if outside 0o0000..0o0777)
    [--parents]                           (mkdir -p semantics; required if path has >1 missing parent)
    [--expires-in 1h]
    [--project-id <id>]                   (SUPPRESS)
    [--json]                              (SUPPRESS)
```

### `request_delete.py`

```
request_delete.py <alias>
    --remote <remote-path>                (required)
    --reason "<intent>"                   (required)
    [--recursive]                         (required if remote path is a directory per drafter SFTP stat)
    [--expires-in 1h]
    [--project-id <id>]                   (SUPPRESS)
    [--json]                              (SUPPRESS)
    [--force]                             (v1.1 — defer; would bypass drafter SFTP stat round-trip)
```

### `run_request.py`

```
run_request.py --request <path-to-artifact.json>
    [--execute]                           (default: validate-only; --execute hands off to ssh-core)
    [--confirm-delete]                    (REQUIRED when artifact.risk_summary.irreversible=true AND operation=delete)
    [--server <alias>]                    (optional; if given, must equal artifact.server — guard against arg/file drift)
    [--show-sensitive]                    (disables redaction in printed summary; raw result.json always unredacted)
    [--no-redact]                         (SUPPRESS; disables redaction in *both* printed summary and result.json — audit only)
    [--project-id <id>]                   (SUPPRESS)
    [--json]                              (SUPPRESS)
    [--timeout-execute 600]               (v1.1 — defer; v1.0 inherits artifact.payload.timeout for command op)
```

## In-scope

1. 5 个脚本各自 ~150-220 loc 的实现 (drafter + run_request + lib helpers)。
2. 4 个新 lib 文件 `lib/{request_artifact,redact,sandbox,lockfile}.py`,带 type hints + docstring,被 5 个脚本 import。
3. 每个 drafter 把 artifact 原子写到 `reports/requests/<op>-<request_id>.json` (temp + rename)。
4. `run_request.py --execute` 走 7 步 validate (schema → expiry → server → sandbox → denylist → SHA-256 only-for-upload → lock),再 dispatch 到 `ssh-core/ssh_execute.py` 或 `ssh-core/ssh_upload.py`,把结果原子写到 `<id>.result.json`。
5. 共享 cross-child contract (artifact schema v1, payload per op, sandbox rule, denylist set, exit code 区段 30-59, lockfile, JSON envelope, redact catalog, result.json schema, test markers) — verbatim 折叠进 `design.md §1`。
6. 80-100 个 unit test + 6 个 `live_ssh` integration test (1 per operation happy path + 1 denylist refusal + 1 sandbox refusal)。
7. ruff clean、mypy clean (允许 pre-existing project-wide `lib` import warning)。
8. `tests/README.md` "What's covered today" 追加 ssh-guarded 行。

## Out-of-scope

- **HSM-backed signing of artifacts** — 用户级真实性 (artifact 是 agent 起草还是人偷塞的) 不在本 child 覆盖范围。v2 候选。
- **多用户审批工作流** (`--reviewer @alice` 之类) — 单用户 ack 在 v1.0 已经够用;后续可在不破 schema 的前提下 additive。
- **Web UI / TUI 审查界面** — 本 child 只产生 JSON artifact;CLI cat / Claude Code paste 即可,不写 Python web 服务。
- **Request 模板** (复用历史 reason / payload) — 用户用 `cp` 或 yasnippet,不在工具层覆盖。
- **Request 链** (一个 artifact 产出后 auto-trigger 下一个) — 容易把 audit 链条变得不可解释,显式延后。
- **`exec_detached.py` / `redact_check.py` / `scan_software.py`** — 本 child 只交付 4 个 drafter + 1 个 executor + 4 个 lib。后续 3 个脚本是兄弟 child task,共享 lib 而不共享 PRD。
- **修改 SKILL.md / references/*** — surface 与 references 已与本 task 完全一致。如果实施过程中发现 references 描述 drift,记到 Notes,作为 follow-up commit 单独推。
- **修改 `pyproject.toml`** — 所有依赖 (paramiko / pytest) 已就位。

## Requirements

### Functional

**FR-1 — Drafter JSON artifact contract (4 个 request_* 共享)**
- 写入路径 `plugins/ssh-guarded/skills/ssh-guarded/reports/requests/<op>-<request_id>.json`。
- 文件原子写 (temp file in 同 dir + `os.replace`),权限 `0o600`。
- `request_id` = `uuid4().hex[:8]`;碰撞处理:`open(O_CREAT|O_EXCL)`,失败则重新生成,3 次失败 → exit_code=1 + 通用错误 (实测概率 < 10⁻²⁰)。
- 字段完全遵循 design §1 C1 schema v1。`version=1` 硬编码,reader 必须拒绝其他值。

**FR-2 — Sandbox + denylist (drafter 端 warn, executor 端 hard fail)**
- `request_*` drafter:遇到 sandbox/denylist 违规 → `stderr` warn + 仍写 artifact (pedagogical:用户看到显式 refusal 在 execute time)。`risk_summary.irreversible` 维持 op-specific (delete=true / command=由 stderr 启发式判断 / upload/mkdir=false)。
- `run_request --execute`:遇到 sandbox 违规 → exit 50 `workdir_outside_sandbox`;遇到 denylist 命中 → exit 51 `path_in_denylist`。两者均不进 ssh-core dispatch。

**FR-3 — Project ID 解析顺序**
顺序见 design §1 C3。`lib/sandbox.resolve_project_id(args, env, cwd)` 是唯一入口;5 个脚本不再各自 reimplement。

**FR-4 — SHA-256 与 size capture (upload only)**
- Drafter 在产出 artifact 前流式读取 `--local`,128 KiB block,算 SHA-256 + 字节数,写入 `payload.local_sha256` + `payload.local_size`。
- `--local` 不存在 → exit 35 `local_path_missing`;读取过程 IOError → exit 37 `sha256_compute_failed`。
- `run_request --execute` 在 dispatch 到 `ssh_upload` 前重新算一次本地 SHA-256;不匹配 → exit 56 `sha256_mismatch`,带 `data.expected_sha256 / data.actual_sha256`。这覆盖"drafter 与 executor 之间用户修改了本地文件"的场景。

**FR-5 — Delete 双层防护**
- Drafter (2026-05-27 Phase B drift B3 同步):pre-check stat 改为 subprocess 调 ssh-core 的 `ssh_execute.py` 间接 stat (避免跨 plugin import `lib.sftp_transport`,与 design §7 一致):

  ```
  argv = ["python", str(SSH_CORE_SCRIPTS_DIR / "ssh_execute.py"), alias,
          f"test -d {q} && echo dir || ([ -e {q} ] && echo file || echo none)",
          "--json"]
  ```

  解析 ssh-core envelope 的 stdout 末尾非空行,取 `"dir" / "file" / "none"`:
  - `"dir"` + 未传 `--recursive` → exit 46 `recursive_required_for_directory`,artifact 不写。
  - `"none"` → drafter stderr warn ("does not exist on `<alias>`") 但仍写 artifact (executor 的 `rm -f` 自然吞掉)。
  - subprocess returncode != 0 / 非 JSON / 末行非 3 选 1 → exit 49 `pre_check_stat_failed`。
  - `"dir"` + 已传 `--recursive` → `payload.is_directory_pre_check=true`;`"file"` → `false`;`"none"` → `false`。

- `run_request --execute`:不重做 stat (Phase C drift D6 简化 — payload 已 immutable,sandbox/denylist 已验证,re-stat 增量收益小);若 `risk_summary.irreversible=true` (delete op 恒 true) 且未传 `--confirm-delete` → exit 59 `delete_confirm_required`。

**FR-6 — `run_request` 验证顺序 (locked)**
schema → expiry → server consistency → sandbox → denylist → SHA-256 (upload only) → lock acquire → ssh-core dispatch。设计依据见 design D7。每一步独立 exit code,便于 caller branch。

**FR-7 — Lockfile**
`<artifact-path>.lock` via `fcntl.flock(LOCK_EX | LOCK_NB)`,context manager (`lib/lockfile.py`)。失败 → exit 54 `lock_held`,JSON `data.holder_pid` 尽力填充 (从 `/proc/locks` 或 `fuser` 读取;读不到设 `null`)。Windows:fallback 到"`<path>.lock` 存在性检查 + sentinel content = pid" — degraded 模式,记 warning,不阻塞 happy path。

**FR-8 — Transport routing in `run_request --execute`**

| Operation | Transport |
|---|---|
| `command` | `python ssh_execute.py <server> "<command>" --timeout <payload.timeout> --json` |
| `upload`  | `python ssh_upload.py <server> <local> <remote> [--overwrite] --json` |
| `mkdir`   | `python ssh_execute.py <server> "mkdir [-p] -m <mode> -- <quoted-path>" --json` |
| `delete`  | `python ssh_execute.py <server> "rm [-rf] -- <quoted-path>" --json` |

shell quoting 一律 `shlex.quote`,argv 一律 list (cf. ADR-001 D1 + CONTRIBUTING.md:37)。

**FR-9 — Result artifact**
`reports/requests/<id>.result.json`,schema 见 design §1 C9。`stdout_redacted` / `stderr_redacted` 走 `lib/redact.Redactor` 后写;`--no-redact` 全局 flag 跳过 (audit only)。

**FR-10 — JSON envelope 兼容性**
全部 5 个脚本均支持 `--json`;不带 `--json` 时人类摘要 (drafter 打印 artifact 路径 + payload summary;executor 打印 result.json 路径 + exit code + redacted tail)。

### Non-functional

- **代码量**:5 个脚本各 ~150-220 loc;4 个 lib 各 ~80-150 loc;合计 ~1300-1500 loc 实现 + ~1200 loc test。
- **ruff** + **mypy** clean (sibling tasks 同等标准)。
- **Windows 兼容**:drafter 必须能在 MSYS bash 跑通 (drafter 只读本地 + 写 JSON,无 SSH);executor 在 Windows 上 lockfile 退化为 degraded mode (FR-7),其他逻辑等效。
- **不并发安全的范围**:同一 artifact 的两个 `--execute` 并发 → lockfile 阻挡。不同 artifact 的并发 → 各自独立 lock,允许。
- **artifact 文件最大寿命**:不主动 GC;`expires_at` 字段是 executor side 的语义检查 (默认 +1h,max +7d)。

## Acceptance Criteria

- [ ] `python request_command.py --help` / `request_upload.py --help` / `request_mkdir.py --help` / `request_delete.py --help` / `run_request.py --help` 均 exit 0 并列出 v1.0 surface 所有 flag (不含 v1.1 deferred)。
- [ ] `pytest tests/test_ssh_guarded_*_unit.py` 全绿,合计 ~80-100 cases (单脚本平均 12-20 个;lib 平均 8-12 个)。
- [ ] `pytest -m live_ssh tests/test_ssh_guarded_*_integration.py` 全绿在 pai,6 个 cases:
  - `test_command_happy_path_live`: draft + execute `echo hello`,验证 result.json `exit_code=0` + `stdout_redacted` 含 "hello"。
  - `test_upload_happy_path_live`: draft + execute 上传 1 KiB tmp file 到 `~/workspace/devops-skills/upload-test.bin`,verify remote sha256。
  - `test_mkdir_happy_path_live`: draft + execute `mkdir -p ~/workspace/devops-skills/staging/<uuid>`,verify `ssh pai test -d ...`。
  - `test_delete_happy_path_live`: 先 mkdir,再 draft + execute `--recursive` delete + `--confirm-delete`,verify `! test -e`。
  - `test_denylist_refusal_live`: draft `request_delete pai --remote ~/.ssh/authorized_keys --reason x` → drafter warn,artifact 写;executor exit 51 `path_in_denylist`,不发 ssh。
  - `test_sandbox_refusal_live`: draft `request_command pai "echo x" --workdir /etc --reason x` → drafter warn,artifact 写;executor exit 50 `workdir_outside_sandbox`。
- [ ] **Happy path JSON envelope shape** (`--json` 输出):drafter `success=true, exit_code=0, data.artifact_path=reports/requests/<id>.json, data.request_id=<id>, data.risk_summary={...}`;executor `success=true, exit_code=<remote exit>, data.result_path=<id>.result.json, data.transport=ssh_execute|ssh_upload, data.duration_ms=<int>`。
- [ ] **Lockfile**:并发跑两次 `run_request --execute --request same.json`,第二次 exit 54 `lock_held`。
- [ ] **SHA-256 drift**:`request_upload` 写 artifact 后修改本地文件,`run_request --execute` exit 56 `sha256_mismatch` + envelope 含两个 sha256。
- [ ] **`--confirm-delete` 缺失**:`run_request --execute --request delete-*.json` (无 `--confirm-delete`) → exit 59 `delete_confirm_required`。
- [ ] **Server consistency**:`run_request --execute --request command-*.json --server other-alias` (与 artifact.server 不一致) → exit 53 `artifact_invalid` + envelope `data.reason="server_mismatch"`。
- [ ] **Expiry**:手改 artifact `expires_at` 到过去 → `run_request --execute` exit 53 `artifact_expired`。
- [ ] ruff clean、mypy clean (允许 pre-existing project-wide `lib` import warning,与 deploy_pubkey commit 节奏一致)。
- [ ] `tests/README.md` "What's covered today" 追加 ssh-guarded 5 个脚本 unit + integration 行。
- [ ] **不**修改:`SKILL.md` (surface 已对齐)、`references/{request-execute,redact,workdir-sandbox}.md` (现状 verbatim 即可)、`pyproject.toml` (依赖已就位)。

## Notes

(决策日志占位,后续按 sibling task 节奏维护。)

- 2026-05-25 创建。`_cross_cutting.md` 内容已并入 `design.md §1` 并删除原文件。
- 2026-05-26 **Phase A complete**:6 lib + 4 test files,82 unit PASS,ruff/mypy clean,全局 934→1016 (+82)。LOC 1265 vs 480 budget (2.6x),原因记录在 design §5。
  - Drift D1: `SSH_CORE_SCRIPTS_DIR` design D8 原写 `parents[3]`,实际 `parents[5]`,design 已就地修正。
  - Drift D2: 新增 `lib/_errors.py` 拆 `ArtifactError`,解 sandbox<->request_artifact 循环 import;design §5 file layout 已更新。
  - Drift D3: POSIX `exclusive_lock` 释放仅 LOCK_UN,不 unlink `.lock` sentinel (fd 语义),与 design §1 C6 隐含一致。
  - Drift D4: lib test 用 `sys.modules` evict-restore 模式防 lib package 与 ssh-core/scripts/lib 同名冲突 — 仓库已有先例 `tests/test_tail_multi_unit.py:52-91`。
  - Drift D5: O1 (Windows lockfile) 落地"`.lock` + sentinel pid"方案,无新 dep;stale-lock 自动清理仍 v1.1 候选。
- 2026-05-27 **Phase B complete**:4 drafter rewrite (1169 LOC) + 4 unit test files (1264 LOC, 51 cases),全局 1016→1067 (+51)。ruff baseline 7→5。--help × 4 全 exit 0。
  - Drift B1: `request_upload` 256MiB 检查放在 sha256 之后,oversized 文件付一次全 I/O 但 artifact size 字段保证真实;v1.1 候选 stat-first 短路。
  - Drift B2: `request_delete` 远端 stat 返回 "none" 时,drafter stderr warn 并写 artifact (executor 用 `rm -f` 自然吞掉) — design D6 未明示,作为 happy path 兜底。
  - Drift B3: `request_delete` SFTP stat 改为 `subprocess.run(["python", ssh_execute, ...])` 间接 stat,与 implement.md B3 决策一致;PRD FR-5 / design D6 措辞由 Phase A 修正之外尚未同步,Phase D 一并改。
  - Drift B4: `request_mkdir --parents` 启发跳过 out-of-sandbox 路径 (避免 double-fault — executor 反正会 exit 50);仅 in-sandbox 才数 segment。
  - Drift B5: `--no-redact` 全 4 drafter 接受但不使用,只为保持 SUPPRESS 共享 surface 一致;executor 才真消费。
  - Drift B6: SUPPRESS + `getattr(args, X, default)` 模式实战通过 (`test_project_id_flag_overrides_env`),复用 ADR-002 D7 教训生效。
- 2026-05-27 **Phase C complete**:`run_request.py` rewrite (588 LOC) + unit test file (729 LOC, 21 cases),全局 1067→1088 (+21)。ruff baseline 5→4。--help 列全 v1.0 surface 无 v1.1 flag。
  - Drift C1: SHA-256 verify 在 validate-only 模式也跑(D7 step 6 在 lock step 7 之前,lock 仅 dispatch 必需);验证由 `test_sha256_mismatch_exits_56_with_envelope_fields` (不传 `--execute`) 触发 exit 56 兜底。
  - Drift C2: `transport_argv` 默认 `null` (O4 默认),`--no-redact` 时写真实 argv list。result.json 字段名稳定。
  - Drift C3: ssh-core child envelope 顶层 `stdout`/`stderr`/`exit_code` (不嵌套在 `data` 下),与最初 design D9 提法对齐但 dispatch prompt 中假设的 `data.stdout` 路径是错的;executor 直接读顶层。
  - Drift C4: `--confirm-delete` 仅在 `operation == "delete" and risk_summary.irreversible` 触发;`request_upload --overwrite` 虽然 risk irreversible 也**不**触发此 gate (PRD FR-5 + design D6 step 3 命名只 delete-only)。
  - Drift C5: 不引入新 failure_class / 新 exit code;严格复用 design §6 表的 8 个 string + 124 (timeout)。
  - Drift C6: `--show-sensitive` 仅影响人类模式打印 (stderr_tail redact 与否),不影响 `result.json` 内容 (后者只受 `--no-redact` 控);与 design D9 + C9 一致。
  - Drift C7: `save_result(art, result, redactor, out_dir=art_path.parent)` — Phase A `save_result` 默认 `out_dir=Path("reports/requests")`,executor 必须 override 把 result.json 放到 artifact 所在目录;Phase A 接口已预留 `out_dir` 参数,无需改 lib。
- 2026-05-27 **Phase D ship 文档,live 待网段验证**:
  - D1: `tests/test_ssh_guarded_pipeline_live.py` (407 LOC, 6 live_ssh cases) 已写,mirror `test_ssh_upload_integration.py` helper 风格。collect 6 cases ✓,runtime 因本机 IP 已切到 192.168.84.73 (不在 192.168.2.0/24 网段),`_require_live_ssh` 优雅 skip。
  - D2: `tests/README.md` 追加 ssh-guarded unit (82+72=154) + live (6 cases on pai) 块,以及 v1.0 roadmap 调整。
  - D3: `design.md §3 D6` 改写为 ssh_execute 间接 stat 路径,`§3 D9` 补 `transport_argv` 默认 null + `out_dir` 参数,`§6` 给 row 54/56/57 补 envelope `data.*` 字段。
  - D4: `prd.md FR-5` 改写为 subprocess-to-ssh_execute,补 "none" 兜底路径。
  - D5: 全默认 suite 1025 PASS + 67 SKIP (含 67 个 SSH integration 因网络 skip,与本任务无关);ssh-guarded unit 154 PASS;ssh-guarded live 6 SKIP (网络);ruff 本任务范围 clean,repo baseline 4 errors (exec_detached I001 + test_manifests pre-existing) 不属本 task。
  - **Commit gate**:CLAUDE.md 规定本机不在 192.168.2.0/24 网段时跳过验证,后续验证才 commit。下一次回到 pai 网段时执行 `.venv/bin/pytest tests/test_ssh_guarded_pipeline_live.py -v -m live_ssh`,6 case 全 PASS 后才 commit。当前 working tree 5 modified + 14 untracked,不 commit。
- 2026-05-28 **Live 验证暴露两类 transport bug,在本 task 内修齐**:本机切回 192.168.2.108 网段后跑 live,6 cases 4 fail。两类根因合并修复:
  - **Bug B1+B2 — `~/` transport-time 展开缺位**:`paramiko.SFTPClient.put` 把 `~/workspace/x` 字面写到 `/root/~/workspace/x`(创建字面 `~` 目录);同时 `shlex.quote("~/...")` 在 ssh_execute one-liner 内变成 `'~/...'`,远端 shell 也不展开。两条 transport 路径都不展开 `~`,但 artifact / blast_radius 又必须保留字面 `~/`(audit 可读 + 不耦合远端 home)。**修复**:`run_request._resolve_remote_home(server)` 单次 SSH probe (`ssh -o BatchMode=yes <server> 'printf %s "$HOME"'`),失败 → exit 57;`_build_argv(art, remote_home=...)` 在构造 argv 时把 payload `remote_path` 中 `~/` 替换为远端绝对路径,artifact 不变。design.md 新增 §D7a 完整阐述决策。
  - **Bug B3 — D7 顺序与 PRD test expectation 冲突**:design D7 原本 sandbox(step 4)先于 denylist(step 5),导致 `~/.ssh/authorized_keys` 命中 exit 50 而非 PRD §Acceptance Criteria 行 `test_denylist_refusal_live` 期望的 exit 51。**修复**:对调为 denylist→sandbox(`RequestArtifact.validate` + `run_request` docstring + design D7 同步更新)。语义重新定位:denylist 是"红线"(具体危险路径,触线立刻拒),sandbox 是"默认边界"(workspace 外);红线优先匹配审计直觉。
  - **测试增量**:`tests/test_ssh_guarded_run_request_unit.py` 新增 15 case(154→169 total):`_expand_remote_tilde` 参数化 7 case + `_build_argv` upload/mkdir/无 home 3 case + `_resolve_remote_home` 4 case(happy/ssh fail/non-abs/timeout) + 1 端到端 `--execute` 调用 resolver 用例。Autouse fixture `_stub_remote_home` 默认把 resolver mock 为 `/root`,避免 unit 真 ssh。
  - **Live 重跑预期**:upload/mkdir/delete 写到远端 `/root/workspace/devops-skills/...` 而非 `/root/~/...`;denylist case 返回 exit 51 而非 50。本 task 内 commit 前需要 6/6 PASS。
- **Open question O1**:Windows 下 `lib/lockfile.py` 的 degraded 模式 — 用 `.lock` 文件存在性 + content=pid,还是 `msvcrt.locking`?前者简单但 stale lock 处理需文档化;后者更接近 POSIX flock 语义但要新依赖处理。本 task 倾向前者 (零新依赖),交付时再 confirm。
- **Open question O2**:`request_command` 的 `risk_summary.irreversible` 启发式 — 现在 `references/request-execute.md:78-90` 列了 `rm/mv/>/systemctl/sudo` 5 个正则,本 task 是否原样实现,还是收敛到 3 个 (rm + systemctl + sudo)?保守起见 v1.0 实现全部 5 个,实测后调。
- **Open question O3**:`<project_id>` 取自 git repo basename 时,如果 cwd 不在 git repo 且无 `--project-id` 也无 env → 应 hard fail (exit 1) 还是用 `default` 字面量?倾向 hard fail,与 `references/workdir-sandbox.md:22` "missing → 拒绝 all writes" 的立场一致。
- **Open question O4**:`run_request --execute` 是否要把 `data.transport_argv` 完整记录到 result.json?有利于 audit + reproduce,但会增加 file size + 暴露 alias。倾向记录,被 `--no-redact` 控制;交付时再 confirm。
- 跨 child 经验沉淀提醒:5 个脚本走 `parents=[common]` 时一律 SUPPRESS,见 `.trellis/tasks/05-24-devops-skills-v1-rollout/prd.md:83`。
- 跨 child 经验沉淀提醒:`uv run pytest/ruff` 会触发 uv.lock 重 resolve,commit 前 `git restore uv.lock` 隔离 (同上 prd.md:87)。
