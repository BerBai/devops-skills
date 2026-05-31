# Implementation Plan — ssh-guarded request/execute pipeline v1.0

> 配套 `prd.md`、`design.md`。本文件**只**承载执行序、验证命令、review gate、rollback 点。所有 design 决策已 lock 在 design.md，本文件不再 renegotiate。

## Pre-flight (start 前一次性确认)

- [ ] 确认本机在 `192.168.2.0/24` 网段且 `ssh pai 'echo ok'` 可通；live_ssh 测试需要它。若不在网段，本次只跑 default suite，live 推迟到后续切换网段时补。
- [ ] `git status` clean；`git log -1 --oneline` 显示 `4f95b26 ssh_tunnel v1.0`（或更新）。
- [ ] `uv run ruff check plugins/ tests/` 当前 clean，作为基线。
- [ ] `uv run pytest tests/ -x -q` 当前全绿；记录 case 数（最新 commit 后应为 934 PASS + 4 skipped，见父任务 PRD Notes 2026-05-26 entry）。
- [ ] design §8 的 5 个 Risks 全部已在 design 内有 mitigation；O1-O4 open question 默认按 PRD Notes 倾向落地（O1 退化 .lock；O2 全 5 个 regex；O3 hard fail；O4 transport_argv 入 result.json，受 `--no-redact` 控制）。
- [ ] Review gate before `task.py start`：本文件 + prd.md + design.md 一起过；start 后状态进 in_progress，不再做大设计变更。

## Execution sequence

依赖图（粗）：
```
sandbox  ─┐
         ├──> request_artifact ──┐
redact   ─┘                      │
                                  ├──> drafters (4) ──> executor (run_request)
lockfile ─────────────────────────┘
```

Phase A 先落 4 个 lib（lib 互相不 import，可并行写但建议串行以便复用断言风格）；Phase B 4 个 drafter；Phase C 1 个 executor；Phase D live + 文档收尾。每个 phase 内部"实现 → unit test → ruff → mypy"循环到绿才进下一个。

### Phase A — shared lib（~480 LOC + 42 unit cases）

- [ ] **A1. `lib/__init__.py`**
  - 模仿 `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:1-12` 风格，列出稳定 surface：`resolve_project_id, is_inside_sandbox, is_in_denylist, normalize_remote_path` (sandbox)；`Redactor` (redact)；`exclusive_lock, LockHeld, LockInfo` (lockfile)；`RequestArtifact, RiskSummary, ArtifactError, allocate_request_id, parse_expires_in, save_result, SSH_CORE_SCRIPTS_DIR` (request_artifact)。
  - 不再 re-export 其他符号；后续脚本一律从 `lib import X`。
- [ ] **A2. `lib/sandbox.py`**（design §4.1 + D2，~120 LOC）
  - 实现 `resolve_project_id / normalize_remote_path / is_inside_sandbox / is_in_denylist`。
  - `_DENY_PREFIXES` + `_DENY_BASENAMES_RE` 与 design C4 verbatim。
  - `normalize_remote_path` 用 `PurePosixPath`（远端永远是 POSIX），expand `~` 不解析为本机 home，保留字面 `~/`。
  - 单测 12 case 按 design §3 D12 表（`tests/test_ssh_guarded_lib_sandbox_unit.py`）。
- [ ] **A3. `lib/redact.py`**（design §4.2 + C8，~100 LOC）
  - `_DEFAULT_PATTERNS` 与 design §4.2 中 4 个 verbatim。
  - `Redactor.__init__(catalog_path=None)`：None 或文件不存在 → 用 default；存在但 JSON 损坏 → log to stderr + fallback to default（hard fail 留给 redact_check sibling）。
  - 每个 pattern `re.compile`；compile 失败 → stderr warn + skip（design §8 Risk row 3）。
  - `apply(text)` 返回 `(redacted_text, hits)`，hits 按 `kind` 聚合 count。
  - 单测 8 case：default load / 自定义 catalog / 文件缺失回落 / JSON 损坏回落 / AKIA / JWT / RSA-header / `--no-redact` 由调用者控制（lib 内 `Redactor` 永远不 bypass，bypass 在 CLI 层不实例化 Redactor 即可）。
- [ ] **A4. `lib/lockfile.py`**（design §4.3 + D7 step 7，~80 LOC）
  - `exclusive_lock(path)` 是 `@contextmanager`；POSIX 用 `fcntl.flock(LOCK_EX | LOCK_NB)`，Windows 退化为 `.lock` 文件存在性 + sentinel content=pid。
  - `LockHeld` 异常带 `holder_pid: int | None`；POSIX 尝试 `/proc/locks` → 失败 try `fuser <path>.lock` → 都失败设 None。
  - cleanup 用 `try/finally`，包括异常路径（design §3 D12 unit case "cleanup on exception"）。
  - 单测 6 case：acquire/release / 双 acquire 抛 LockHeld / holder pid 读 / Windows degraded mocked（`sys.platform = 'win32'` monkeypatch）/ 异常 cleanup / stale lock semantics。
- [ ] **A5. `lib/request_artifact.py`**（design §4.4 + D1/D3/D11，~180 LOC）
  - `@dataclass RiskSummary / RequestArtifact`，`Operation` 与 `Scope` Literal。
  - `ArtifactError(Exception)` 带 `failure_class, exit_code, message`。
  - `RequestArtifact.load(path)`：JSON 读 + `version == 1` 否则 `artifact_invalid` exit 53；缺字段同 exit；字段类型不符同 exit。
  - `RequestArtifact.save(path)`：tmp file in same dir + `os.replace`；mode 0o600（Windows skip，design §8 Risk row 5）。
  - `RequestArtifact.validate(*, project_id, expected_server=None)`：按 D7 step 1-5（schema 已经在 load 阶段，validate 内主要做 expiry + server + sandbox + denylist）。SHA-256 与 lock 不在 validate 内（D1 docstring 明示）。
  - `allocate_request_id(operation, requests_dir)`：循环 `uuid4().hex[:8]` → `open(O_CREAT|O_EXCL)` 占位文件；3 次失败 → ArtifactError exit 1。
  - `parse_expires_in(s)`：D11 verbatim（含 60s floor + 7d cap）。
  - `save_result(art, result_dict, redactor)`：atomic write `<id>.result.json`，0o600。redactor=None 时 stdout/stderr 字段名不变内容原样（design D9 + C9）。
  - `_compute_risk(op, payload, server, project_id) -> RiskSummary`：D4 表 verbatim；command 的 regex 用 design D4 5 个字面。
  - `SSH_CORE_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "ssh-core/skills/ssh-core/scripts"`（design D8 末段），测试用 monkeypatch 替换。
  - 单测 16 case 按 §3 D12 表。
- [ ] **A 检查门**：`uv run pytest tests/test_ssh_guarded_lib_*_unit.py -x -q` 全绿；`uv run ruff check plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/ tests/test_ssh_guarded_lib_*.py` clean；`uv run mypy plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/` 无新 warning。

### Phase B — drafters（~740 LOC + 44 unit cases）

每个 drafter 共用 `build_common_parser()`（design D10，SUPPRESS 模式），各自的 subparser `parents=[common]`。CLI `main(argv)` 模板：
```
1. parser = build_parser_for_<name>(); args = parser.parse_args(argv)
2. project_id = resolve_project_id(args, os.environ, Path.cwd())
3. validate inputs (per-script FR-N)；非法 → ArtifactError → envelope + exit
4. build payload + risk_summary（_compute_risk）
5. allocate_request_id → 拼路径
6. art.save(path)
7. emit envelope (--json) or human summary
```

- [ ] **B1. `request_command.py`**（~180 LOC，12 cases）
  - FR per PRD §Functional FR-1/3/10；docstring `v1.0 surface:` 与 `request_command.py:7-14` 一致。
  - 入参检查：`--timeout` ∈ [1, 3600]（违反 exit 30）；`--env` ≤ 32 entries（exit 33）；`--reason` argparse required（缺即 argparse 标准报错 + exit 2，不占 reserved 31）。
  - `risk_summary` 走 D4 command 行 regex；workdir 缺省时风险模板 `"<user>@<server> default workdir"`，user 取 `os.getenv("USER") or "unknown"`。
  - 单测 12 case 按 §3 D12 表。
- [ ] **B2. `request_mkdir.py`**（~150 LOC，8 cases）
  - `--mode` 解析：`int(s, 8)`；out of `0o0000..0o0777` → exit 40。
  - `--parents` 缺省时若路径含 >1 missing parent → exit 42（drafter 不实际 stat 远端，按"含 ≥2 个 path segment 不存在于 sandbox prefix"近似 — 实际是字符串启发：把 remote_path 相对 `~/workspace/<project_id>/` 拆 segment，>1 个 segment 即视为 nested，需 `--parents`。简单可错的方向：保守，多需要时让用户加 `--parents`，不查实际远端）。
  - 单测 8 case 同 §3 D12。
- [ ] **B3. `request_delete.py`**（~200 LOC，10 cases）
  - SFTP stat 通过 `from lib.sftp_transport import SftpSession`（注意：这是 **ssh-core** 的 lib，本 task 用 subprocess 调 ssh-core 脚本的 lib，不直接 import；要么把 stat 逻辑写成调用 `python ssh_execute pai "test -d <quoted> && echo dir || ([ -e <quoted> ] && echo file || echo none)" --json` 间接获取——后者零跨 plugin import，更稳）。
  - **决策**：用 ssh_execute 间接 stat（避免本 plugin 直接 import ssh-core lib，符合 design §7 "本 task 不 import ssh-core lib"）。stat 失败 → exit 49 `pre_check_stat_failed`；结果是 `dir` 且未 `--recursive` → exit 46。
  - 这与 PRD FR-5 文字"SftpSession(alias).stat"略有偏离（PRD 写的是参考写法，design D6 "SFTP stat" 是抽象表达）。**改动需在 implement 期间 → PRD/design 反馈**：把 D6 改成"ssh_execute test -d/-e 间接 stat"。先把决定写到 task Notes，等 phase B 完成后回去更 PRD/design。
  - 单测 10 case：mock subprocess.run 模拟 dir / file / none / 网络失败四种返回。
- [ ] **B4. `request_upload.py`**（~210 LOC，14 cases）
  - sha256 streaming 用 `lib.request_artifact._sha256_streaming` 或新增 `lib.sandbox._sha256`（放 sandbox 不合理 → 放 request_artifact）。
  - upload_roots：v1.0 hard code `["~/Downloads", "~/Desktop", cwd, "~/workspace"]`（与 `references/request-execute.md` 一致；若 references 没写，按这 4 个落地并把决定记 Notes）。
  - 256 MiB cap 检查在 sha256 之后（design FR-1 non-functional），违 → exit 39。
  - 单测 14 case 同 §3 D12。
- [ ] **B 检查门**：`pytest tests/test_ssh_guarded_request_*_unit.py -x -q` 全绿；4 个脚本 `--help` 各 exit 0 且打印 v1.0 surface flag（不含 v1.1）；JSON envelope shape 与 PRD Acceptance Criteria 第 4 行 verbatim 匹配。

### Phase C — executor（~280 LOC + 18 unit cases）

- [ ] **C1. `run_request.py`**（design §3 D7 / D8 / D9）
  - 7 步顺序硬编码（D7）；每步独立 `try/except ArtifactError`；envelope 统一在 `main()` 末端 emit。
  - dispatch 走 `SSH_CORE_SCRIPTS_DIR / "ssh_execute.py" or "ssh_upload.py"`；argv 用 list（D8 表）；shlex.quote 包 remote_path / command。
  - `subprocess.run(timeout=art.payload.get("timeout") or 600)`；TimeoutExpired → exit 124（与 ssh-core 一致）。
  - 解析子进程 stdout 为 JSON envelope；非 JSON 或 envelope shape 错 → exit 57 `transport_failed` + envelope 携带 `data.transport_returncode / data.transport_stderr`。
  - `data.transport_argv` 写入 result.json（按 O4 倾向），受 `--no-redact` 控制（不传 `--no-redact` 时 argv 中潜在 secret 被 redact）。
  - lock 用 `with exclusive_lock(art_path)`；LockHeld → exit 54 + `data.holder_pid`。
  - `--confirm-delete` 强制（D6 step 3）；`art.risk_summary.irreversible and operation == "delete" and not args.confirm_delete` → exit 59。
  - 单测 18 case 同 §3 D12 行 `run_request.py`；subprocess.run 全 monkeypatch（live test 才真发 ssh）。
- [ ] **C 检查门**：`pytest tests/test_ssh_guarded_run_request_unit.py -x -q` 全绿；`pytest tests/test_ssh_guarded_*_unit.py -x -q` 累计 104 case 全绿；ruff + mypy clean。

### Phase D — live integration + 文档（6 cases + README + Notes）

- [ ] **D1. `tests/test_ssh_guarded_pipeline_live.py`**（marker `live_ssh`，6 case 按 PRD Acceptance Criteria 行 3）
  - **修正 design §3 D12 末段"复用 conftest"的提法**：仓库无 `tests/conftest.py`。mirror `tests/test_ssh_upload_integration.py` 的本地 helper 风格（`POC_HOST` env / `_ssh_agent_has_key` / `_host_reachable` / `_require_live_ssh` / `_remote_cleanup`），把它们再实现一次（或更优：抽到本 task 新建的 `tests/_ssh_guarded_live_helpers.py` 内，给后续 ssh-guarded sibling task 复用 — 但本 task 不强制，保持单文件自包含也可）。决策：**单文件自包含**，与 ssh-core 现状一致；抽 helper 留给 sibling exec-detached/redact-check 共同 PR 时再做。
  - 每个 case 跑 drafter + run_request --execute；test_delete 用 mkdir + delete --recursive --confirm-delete 串联。
  - test_denylist / test_sandbox：drafter 写 artifact OK（warn-but-write），executor exit 51 / 50。
- [ ] **D2. `tests/README.md`** "What's covered today" 追加：
  ```
  ssh-guarded:
    - request_command / request_upload / request_mkdir / request_delete / run_request unit (lib + per-script, 104 cases)
    - request/execute pipeline live (6 cases on pai: command happy / upload happy / mkdir happy / delete happy / denylist refusal / sandbox refusal)
  ```
- [ ] **D3. Notes 回写 prd.md**：
  - O1-O4 实际落地决定（O1 退化 .lock + 24h stale 文档化；O2 5 regex；O3 hard fail；O4 transport_argv 入 result.json）。
  - B3 决策：`request_delete` SFTP stat 改用 ssh_execute 间接（更新 design D6 文本，标 `2026-05-26 实施期间收敛`）。
  - 测试 case 总计：104 unit + 6 live（与 D12 表 +4 偏差吻合）。
- [ ] **D 检查门**：在 pai 网段下 `uv run pytest -m live_ssh tests/test_ssh_guarded_pipeline_live.py -x -q` 6/6 PASS；`uv run pytest tests/ -x -q` 全局 PASS 数 = 之前基线 + 104（默认 suite，不含 live）；`uv run ruff check plugins/ tests/` clean；`uv run mypy plugins/ssh-guarded/skills/ssh-guarded/scripts/` 无新 warning。

## Final verification（commit 前）

- [ ] PRD Acceptance Criteria 11 行逐条 ✓（含 `--help` 5/5 / unit 104 / live 6 / envelope shape / lockfile / SHA-256 drift / `--confirm-delete` / server consistency / expiry / ruff+mypy / README / **不改** SKILL.md/references/pyproject.toml）。
- [ ] `git diff --stat` 只触：
  - 新增 `plugins/ssh-guarded/skills/ssh-guarded/scripts/lib/` 5 个文件
  - 改写 5 个 stub（行数从 ~60 → ~150-280 each）
  - 新增 10 个 test 文件
  - 改 `tests/README.md` 1 段
  - 改 `prd.md` Notes 段
- [ ] `git restore uv.lock` 隔离 uv 自动重 resolve（父任务 PRD Notes 2026-05-25 已立此规矩）。
- [ ] 父任务 progress 更新：`05-24-devops-skills-v1-rollout` PRD §Notes 追加一行 `2026-05-XX request-pipeline ship: 104+6 PASS, Phase 2 进度 1/3`。

## Rollback points

| 阶段失败 | 回退动作 |
|---|---|
| Phase A（lib）任一 sub 红 | `rm -rf plugins/ssh-guarded/skills/ssh-guarded/scripts/lib && git restore tests/test_ssh_guarded_lib_*.py` |
| Phase B 单 drafter 红 | `git restore plugins/ssh-guarded/skills/ssh-guarded/scripts/request_<name>.py tests/test_ssh_guarded_request_<name>_unit.py` |
| Phase C executor 红 | `git restore plugins/ssh-guarded/skills/ssh-guarded/scripts/run_request.py tests/test_ssh_guarded_run_request_unit.py` |
| Phase D live 红（但 unit 全绿）| 不回退实现；停在 unit-only ship，把 live 列入 follow-up child；commit message 注明 `live test deferred: <reason>` |
| 全盘 abort | `git restore plugins/ssh-guarded tests/ && rm -rf plugins/ssh-guarded/skills/ssh-guarded/scripts/lib && rm tests/test_ssh_guarded_*.py` |

## Sub-agent dispatch notes（curate before start）

- `implement.jsonl` / `check.jsonl` 当前为 254B 占位。start 前需要根据本文件 Phase 拆分填实，每个 entry 至少含：
  - `task_path`: `.trellis/tasks/05-25-ssh-guarded-request-pipeline`
  - `phase`: `A` / `B` / `C` / `D`
  - `scope`: 该 phase 触达的文件 glob
  - `acceptance`: 该 phase 检查门命令
- `trellis-implement` dispatch prompt 模板首行：`Active task: .trellis/tasks/05-25-ssh-guarded-request-pipeline`，紧跟当前 phase 的实施段 + 引用 design §X / PRD FR-N。
- `trellis-check` 在每个 phase 检查门触发；不跨 phase 累积。

## Open items（start 前 user confirm）

1. **B3 SFTP stat → ssh_execute 间接 stat 改动**：本 implement 期间 propose 的偏离（PRD FR-5 / design D6 文本），是否同意 ship 时同步改 PRD/design？倾向同意（design §7 已禁止跨 plugin import，FR-5 文字与 §7 冲突）。
2. **upload_roots 写死 4 项**（`~/Downloads / ~/Desktop / cwd / ~/workspace`）：是否在 v1.0 写死，v1.1 再让 `~/.devops/upload_roots.json` 可配置？倾向写死。
3. **`request_mkdir --parents` 启发**：字符串拆 segment 近似 vs 真去 stat 远端目录？倾向字符串近似（drafter 不真触网，与其他 3 个 drafter 一致；delete 因为有强 safety 需求才破例触网）。
