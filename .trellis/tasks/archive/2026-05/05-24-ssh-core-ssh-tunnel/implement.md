# Implementation — ssh-core ssh_tunnel v1.0

> 配套 PRD: `prd.md`；技术设计: `design.md`。
> **本任务无 sibling 阻塞** —— `ssh_daemon.py` 已 deferred 到 v1.1，ssh_tunnel ship 独立。
> 体量预期：单文件 `ssh_tunnel.py` ~280–360 loc；新增 2 个 test 文件；spec/doc sync 5 处。

## Phase 0 — Pre-flight 检查（`task.py start` 之前）

- [ ] `git status` 干净（除了已知的 ssh_download in-progress 改动）；若不干净，先 stash 或 commit 无关变更
- [ ] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py --help` 当前能正常打印 stub 的 surface（baseline）
- [ ] `pytest tests/` 全绿（基准 670 PASS — 不含 ssh_tunnel）
- [ ] `pytest tests/ -m live_ssh` 全绿（基准 49 PASS — 不含 ssh_tunnel）
- [ ] `ssh pai 'echo ok'` 通（POC_HOST 可达）
- [ ] `ssh pai 'ss -tlnp 2>/dev/null | wc -l'` 跑通（integration tests 依赖端口探测）
- [ ] 确认 `~/.ssh/config` 含 alias `pai`（`grep -A2 "^Host pai" ~/.ssh/config` 有输出）
- [ ] `.trellis/spec/backend/error-handling.md` exit-code 表中确认 30–39 区段未被占用（grep `^| `30` 应为空）

Any 项不满足 → STOP，先解决再 `task.py start`。

## Phase A — argparse + main() dispatch

### A1：重写 docstring + imports

- [ ] 替换 stub 顶部 docstring 为 v1.0 描述（模板参照 `ssh_server_transfer.py:1-36`）
  - `v1.0 surface:` 块列出 start/stop/status/list 全部 flag
  - `Implementation notes:` 块写明 detach 模式选择 + `--idle-timeout` 与 `--no-detach` 语义
  - `Failure classes (per-script namespace):` 块列出 30–39 表
  - `See:` 引用 `.trellis/tasks/.../{prd,design,implement}.md` 与 `error-handling.md` + `references/workflows.md §4`
- [ ] imports：`argparse, contextlib, dataclasses, hashlib, json, os, random, signal, socket, subprocess, sys, tempfile, time` + stdlib 的 `pathlib.Path` / `datetime` / `typing`
- [ ] 从 `lib` import `emit, json_result, msys_safe_env, filter_ssh_noise, NETWORK_PATTERN, AUTH_PATTERN`（前 3 个稳定 surface，后两个 lib/__init__.py 已 export）
- [ ] 顶部加常量：`PORT_POOL_MIN=10000, PORT_POOL_MAX=20000, PORT_POOL_RETRIES=5, KILL_GRACE_S=3, EARLY_FAILURE_GRACE_S=1`

**Validation A1**：`python3 ssh_tunnel.py --help` exit 0；ruff clean。

### A2：build_parser（细节 PRD D9）

- [ ] 4 个 subparser：`start / stop / status / list`
- [ ] `start` 含：`alias`、`--remote-port`(required, int)、`--local-port`(int, default None)、`--remote-host`(str, default "localhost")、`--bind-address`(str, default "127.0.0.1")、`--idle-timeout`(int, default 1800)、`--connect-timeout`(int, default 8)、`--no-detach`(store_true)
- [ ] `stop` 含：`alias`、`--port`(int, default None)
- [ ] `status` 含：`alias`
- [ ] `list` 不接位置参
- [ ] 全部 subparser + top-level 都接受 `--json`（顶层 + subparser 各加一份避免 `argparse` parents 陷阱 — 参见 v1-rollout/prd.md notes 关于 `parents=[common]` + `SUPPRESS` 的踩坑记录）

**Validation A2**：4 个 `--help` 全跑通；`python3 -c "from ssh_tunnel import build_parser; build_parser().parse_args(['start','x','--remote-port','22'])"` 不抛。

### A3：main() 路由

- [ ] `main(argv=None) -> int`：dispatch 到 `_cmd_start / _cmd_stop / _cmd_status / _cmd_list`
- [ ] 每个 `_cmd_*` 自己负责 `emit()` JSON 并 `return int`
- [ ] `main()` 外层 try/except `Exception`：未预期异常 → `emit(json_result(success=False, exit_code=1, stderr=str(e), data={"failure_class":"remote_error"}))`
- [ ] `if __name__ == "__main__": sys.exit(main())`

**Validation A3**：手测 `python3 ssh_tunnel.py status nonexistent --json` 返回 well-formed JSON（即便 _cmd_status 还是空壳也要返回 0 + empty forwards）。

## Phase B — start subcommand（核心难点）

### B1：state file primitives

- [ ] `_state_path(alias: str, port: int) -> Path`（design §4.2）
- [ ] `dataclass _StateEntry`: `pid, local_port, remote_host, remote_port, bind_address, alias, started_at, path`
- [ ] `_write_state(entry: _StateEntry) -> None`：atomic write via `.tmp` → `chmod 0o600` → `replace`
- [ ] `_read_state(path: Path) -> _StateEntry | None`：JSON load + 字段补齐；缺字段视为 corrupt → 返回 None（不抛）
- [ ] `_scan_state() -> list[_StateEntry]`：扫 `$TMPDIR/ssh_tunnel/*.json`，跳过 None
- [ ] `_pid_alive(pid: int) -> bool`：`os.kill(pid, 0)` 包 `ProcessLookupError/PermissionError`（design §5.2）

**Validation B1**：单元 smoke
```python
e = _StateEntry(pid=os.getpid(), local_port=19999, ...)
_write_state(e)
assert _read_state(e.path).pid == os.getpid()
assert _pid_alive(os.getpid()) is True
e.path.unlink()
```

### B2：端口分配（design §4.1）

- [ ] `_port_free(port: int) -> bool`：transient bind 探测
- [ ] `_alloc_local_port(explicit: int | None) -> int`：显式失败 raise `_TunnelError("port_busy", 31)`；pool exhausted raise `_TunnelError("port_pool_exhausted", 32)`
- [ ] `_TunnelError` 是本文件 sentinel exception，含 `failure_class, exit_code, message` 三个字段

**Validation B2**：`_port_free(80)` 通常 False（除非 sudo）；`_port_free(0)` 应是 True（kernel 自选）；pool 在 random seed 固定下 deterministic。

### B3：detach 启动（design §3 完整 Popen 块）

- [ ] `_posix() -> bool`：`sys.platform != "win32" and hasattr(os, "setsid")`
- [ ] `_spawn_ssh(alias, lport, rhost, rport, bind, connect_timeout) -> subprocess.Popen`：按 design §3 构造 argv + Popen
- [ ] `_wait_for_forward(proc, connect_timeout) -> tuple[bool, str]`：返回 `(alive_at_deadline, stderr_drained)`
  - `deadline = monotonic() + connect_timeout + EARLY_FAILURE_GRACE_S`
  - while not deadline: `proc.poll()`：非 None 退出 → 读 stderr → 返回 `(False, stderr)`
  - 到 deadline 仍 alive → `proc.stderr.close()` → 返回 `(True, "")`
- [ ] `_classify_start_failure(returncode, stderr) -> tuple[str, int]`：返回 `(failure_class, exit_code)`
  - 复用 `lib` 中已 export 的 `NETWORK_PATTERN, AUTH_PATTERN`
  - NETWORK 匹配 → ("ssh_connect_failed", 33)
  - AUTH 匹配 → ("auth", 34)
  - 否则 → ("forward_setup_failed", 35)

**Validation B3**：可暂用一个真实 alias smoke：`ssh_tunnel.py start pai --remote-port 22 --local-port 19998 --json` 应返回 success；`ss -tln | grep 19998` 应有 LISTEN；手动 `kill <pid>` 后 state file 仍在（stop 才清理）。

### B4：_cmd_start 全装配

- [ ] 顺序：(1) `--no-detach` 分流 → B6 处理；(2) `_posix()` 检查 → 否则 30；(3) `--idle-timeout` 用户传过就构造 `flag_deferred=["--idle-timeout"]` + stderr warn；(4) `_alloc_local_port` → 31/32；(5) `_spawn_ssh`；(6) `_wait_for_forward`：失败 → `_classify_start_failure` → terminate proc → 返回；成功 → `_write_state` → `emit` success → return 0
- [ ] 用 `time.monotonic()` 测 elapsed_ms，加入 `data.duration_ms`（与 `ssh_execute` envelope 风格对齐）
- [ ] 任何 Phase 失败前**必须**清理已启动的 proc：`proc.terminate(); proc.wait(timeout=2)` 或 `proc.kill()`

**Validation B4**：5 个 smoke
1. `python3 ssh_tunnel.py start pai --remote-port 22 --local-port 19997 --json` → success
2. `python3 ssh_tunnel.py start pai --remote-port 22 --local-port 19997 --json` 再跑 → exit 31 `port_busy`
3. `python3 ssh_tunnel.py start pai --remote-port 22 --json` → success（pool 自选 port）
4. `python3 ssh_tunnel.py start nonexistent-host --remote-port 22 --json` → exit 33 `ssh_connect_failed`（或 35 forward_setup_failed，看 stderr 内容）
5. `python3 ssh_tunnel.py start pai --remote-port 22 --idle-timeout 5 --json` → success + stderr warn + `data.flag_deferred=["--idle-timeout"]`
6. cleanup：手动 `kill` + `rm $TMPDIR/ssh_tunnel/*.json`（或等 B5 stop 实现后用 stop）

### B5：stop semantics（design §5.1）

- [ ] `_kill_with_grace(pid: int) -> str`：返回 `"killed" | "stale" | "kill_failed"`
  - `_pid_alive(pid)` False → 直接 "stale"
  - `os.kill(pid, SIGTERM)` raise → ("kill_failed")
  - polling sleep(0.1) 最多 `KILL_GRACE_S` 秒等死
  - 还活 → `os.kill(pid, SIGKILL)`；仍 alive after 1s → "kill_failed"
  - 否则 "killed"
- [ ] `_cmd_stop` 按 design §5.1 全装配
- [ ] envelope：`data.ports_stopped: [int...]`、`data.state_files_removed: [path...]`、`data.failure_class`

**Validation B5**：基于 B4 smoke 1 启动的 tunnel
```bash
python3 ssh_tunnel.py stop pai --port 19997 --json
# 期望 success + ports_stopped=[19997]
ss -tln | grep 19997   # 应空
ls $TMPDIR/ssh_tunnel/  # 应空
```
另外：`stop pai` 无 tunnel 时 → exit 36；`stop pai` 多 tunnel 不传 --port → exit 37。

### B6：`--no-detach`（design §9 / PRD D8）

- [ ] `_cmd_start_no_detach`：foreground spawn（无 `start_new_session`），父 `Popen.wait()` 阻塞
- [ ] 不写 state 文件（无脱离 → 无 state 语义）
- [ ] exit code = ssh exit code；ctrl-c → SIGINT 透传给子 → 父 wait 返回 130 之类
- [ ] 若同时传 `--json`：start 之前 emit 一行 `{"data":{"no_detach":true,"local_port":...}}`，wait 完后**不**再 emit（人类用例为主）

**Validation B6**：`python3 ssh_tunnel.py start pai --remote-port 22 --local-port 19996 --no-detach` 应 foreground 跑；Ctrl-C 立即返回；`ss -tln | grep 19996` 期间应 LISTEN，之后空。

## Phase C — status / list

### C1：_cmd_status / _cmd_list

- [ ] `_describe(entry) -> dict`：补 `running: bool` 与 `state_file: str(path)`
- [ ] `_cmd_status`：filter by alias
- [ ] `_cmd_list`：no filter
- [ ] envelope：`data.forwards: [...]`、`failure_class: null`
- [ ] Human mode：一行一条 `<alias>:<lport> → <rhost>:<rport> (pid <pid>, <running|stale>)`

**Validation C1**：
```bash
python3 ssh_tunnel.py start pai --remote-port 22 --local-port 19995 --json
python3 ssh_tunnel.py list --json | jq '.data.forwards | length'  # ≥ 1
python3 ssh_tunnel.py status pai --json | jq '.data.forwards[0].running'  # true
python3 ssh_tunnel.py stop pai --port 19995 --json
python3 ssh_tunnel.py list --json | jq '.data.forwards | length'  # 减 1
```

## Phase D — Spec + doc sync

### D1：`error-handling.md` 追加 30–39 行

- [ ] 在 exit-code 表（line 50 附近）`20`–`29` 行下方加新行：
  ```
  | `30`–`39` | `ssh_tunnel` per-script failure classes. `30` `posix_only` (Windows / no `os.setsid`), `31` `port_busy`, `32` `port_pool_exhausted`, `33` `ssh_connect_failed`, `34` `auth`, `35` `forward_setup_failed`, `36` `tunnel_not_found`, `37` `ambiguous_stop`, `38` `stale_state_only`, `39` `kill_failed`. Introduced by `ssh_tunnel` v1.0 (see `.trellis/tasks/05-24-ssh-core-ssh-tunnel/prd.md` D7). ADR follow-up TBD. |
  ```
- [ ] 同步 line 56-61 的 "Do not invent new exit codes" 段尾的范围列表：`(0 / 1 / 2 / 4–9 / 10–15 / 20–29 / 30–39 / 124)`

**Validation D1**：`grep -E '^\| `30`' .trellis/spec/backend/error-handling.md` 命中一行。

### D2：SKILL.md decision tree + quick reference

- [ ] 当前 L42-46（decision tree）已含 `ssh_tunnel.py start ... --remote-port 3306 --local-port 13306` —— 检查是否需要加 v1.0 注释（"`--idle-timeout` is v1.1 deferred"）
- [ ] L81-82 quick reference 同样检查 + 加注释

**Validation D2**：`grep -A2 "Open a tunnel" plugins/ssh-core/skills/ssh-core/SKILL.md` 包含 v1.0 注释。

### D3：`references/workflows.md §4` 整段重写

- [ ] 当前 L57-71 该段说 "Same daemon shape as `ssh_execute`" + "exits after `--idle-timeout` (default 30 min)" —— **两条都是误导**
- [ ] 改写为：
  ```markdown
  ## 4. Port-forward tunnel

  ```
  ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306
  ```

  v1.0 ships with a single detached forward per invocation: a Python `subprocess.Popen(start_new_session=True)`-style detach (POSIX-only) launches `ssh -N -L ...` which inherits a fresh session and is reaped by init after the parent exits. The forward's `(alias, local_port)` state lives at `$TMPDIR/ssh_tunnel/<md5(alias-port)>.json`.

  Local ports come from pool `10000–20000` when `--local-port` is omitted. The state file records the chosen port so Claude can answer "where's the tunnel?".

  ```
  ssh_tunnel.py list                  # all active forwards
  ssh_tunnel.py status prod-db        # this alias's forwards
  ssh_tunnel.py stop prod-db --port 13306
  ```

  **v1.0 status**: `--idle-timeout` is accepted (surface-stable) but is a no-op; the tunnel runs until you call `stop` or kill its PID. v1.1 will reintroduce idle exit, most likely via `ControlMaster` integration alongside the v1.1 `ssh_daemon` revival. Until then, callers should clean up explicitly.
  ```

**Validation D3**：`grep -c "Same daemon shape" plugins/ssh-core/skills/ssh-core/references/workflows.md` 应为 0；`grep -c "v1.0 status" .../workflows.md` 应有 ≥2（§3 已有一处 + §4 新增）。

### D4：`references/safety.md` L22 确认

- [ ] L22 "Per-alias state files (daemons, tunnels) live under `$TMPDIR/ssh_<kind>/`" —— ssh_tunnel 的 `$TMPDIR/ssh_tunnel/` 正好符合，无需改文本
- [ ] 但 confirm 顺带：mode 0o700 directory + 0o600 file 与 safety.md 描述一致

**Validation D4**：人工对比 + `ls -la $TMPDIR/ssh_tunnel/` 显示 700 权限。

### D5：`references/troubleshooting.md` L33-34

- [ ] 当前 "3. The daemon died after starting the listener. Check `ssh_tunnel.py status`." —— 表述上"daemon"不准确（v1.0 没有 daemon，只有 detached ssh）
- [ ] 改为："3. The forwarder ssh process died after binding the listener. Check `ssh_tunnel.py status` — `running: false` means the ssh exited; the state file lingers until `stop` is called."
- [ ] 同段 L24-32 通读一遍，若有"daemon"措辞统一改"detached ssh / forwarder"

**Validation D5**：`grep -n "daemon" plugins/ssh-core/skills/ssh-core/references/troubleshooting.md` 应该已经不含 ssh_tunnel 上下文里的 daemon 措辞（其他文件可保留 — 比如 v1.1 deferred 提及）。

## Phase T — Tests

### T1：Unit tests (`tests/test_ssh_tunnel_unit.py`)

- [ ] 新建文件，顶部 `pytestmark = []`（**不**打 live_ssh —— 全部 mock）
- [ ] 30 个 case 按 design §11 分类
- [ ] Mock 策略：
  - `subprocess.Popen` → 返回 fake 对象，`poll()` 可配置返回 None/0/255，`stderr.read()` 可配置
  - `socket.socket().bind` → raise OSError 或 pass
  - `os.kill` → raise / pass
  - `time.sleep` / `time.monotonic` → 跳过等待 / 时间快进
  - `sys.platform` → patch 为 "win32"
  - `random.randint` → 控制 pool 选择
  - `pathlib.Path.replace` / `unlink` → 真实 fs（用 tmp_path fixture）
- [ ] 关键 assertion 模板（与 `test_ssh_server_transfer_unit.py` 对齐）：
  ```python
  def test_start_port_busy_explicit(monkeypatch, tmp_path):
      monkeypatch.setattr("ssh_tunnel._port_free", lambda p: False)
      result = run_main(["start", "pai", "--remote-port", "22", "--local-port", "19990", "--json"])
      data = json.loads(result.stdout)
      assert data["exit_code"] == 31
      assert data["data"]["failure_class"] == "port_busy"
  ```
- [ ] 目标 **≥90% statement coverage** on ssh_tunnel.py：
  ```
  pytest tests/test_ssh_tunnel_unit.py --cov=plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel --cov-report=term-missing
  ```

**Validation T1**：`pytest tests/test_ssh_tunnel_unit.py -v` 全绿；coverage ≥ 90%。

### T2：Integration tests (`tests/test_ssh_tunnel_integration.py`)

- [ ] 新建文件，顶部 `pytestmark = pytest.mark.live_ssh`
- [ ] Skip 守卫复用 `_host_reachable("pai")` + `_ssh_agent_has_key()`（从 `test_ssh_upload_integration.py` 拷过来 / 提到 conftest.py）
- [ ] Cleanup fixture：
  ```python
  @pytest.fixture(autouse=True)
  def _cleanup_tunnels():
      yield
      # 暴力清理所有 pai 上的本地 listener（仅本机 state file）
      for p in (Path(tempfile.gettempdir()) / "ssh_tunnel").glob("*.json"):
          try:
              entry = json.loads(p.read_text())
              if entry.get("alias") == "pai":
                  with contextlib.suppress(ProcessLookupError, PermissionError):
                      os.kill(entry["pid"], signal.SIGTERM)
                  p.unlink(missing_ok=True)
          except Exception:
              p.unlink(missing_ok=True)
  ```
- [ ] 5 个 case 按 design §11 列出：
  1. `test_start_stop_roundtrip` — start pai:22 → 验证 listening → `ssh -p <lport> 127.0.0.1 echo ok` → stop → 验证不再 listen + state file gone
  2. `test_no_detach_lifecycle` — `--no-detach` 起进程 → sleep 2 → SIGTERM → assert exit code
  3. `test_status_empty` — clean state dir 后 status → forwards=[]
  4. `test_stop_not_found` — clean state 后 stop pai → exit 36
  5. `test_idle_timeout_noop` — start --idle-timeout 5 → sleep 10 → 仍 alive + `flag_deferred=["--idle-timeout"]`
- [ ] 追加 `tests/README.md` "What's covered today" 一项：`ssh_tunnel` live_ssh 5 cases

**Validation T2**：
```
pytest tests/test_ssh_tunnel_integration.py -m live_ssh -v
ssh pai 'ps -ef | grep -i ssh | grep -v grep'   # 不应有 leak（pai 那侧也不应该）
ls $TMPDIR/ssh_tunnel/                          # 应为空
```

## Phase F — Final validation

```bash
# 基线
pytest tests/                                                          # 应 >= 700 PASS（含 ssh_tunnel ~30 unit）
pytest tests/ -m live_ssh -v                                          # 应 >= 54 PASS（含 ssh_tunnel 5）
pytest tests/test_ssh_tunnel_unit.py -v --cov=plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel --cov-report=term-missing
pytest tests/test_ssh_tunnel_integration.py -m live_ssh -v

# 静态
ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py
mypy plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py

# Smoke (real)
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py --help
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py list --json

# 不应有泄漏
ls $TMPDIR/ssh_tunnel/ 2>/dev/null   # 空
ps -ef | grep "ssh -N -L" | grep -v grep   # 空（本机本会话）

# Spec/doc sync 核查
grep -c '^| `30`–`39`' .trellis/spec/backend/error-handling.md   # 1
grep -c 'Same daemon shape' plugins/ssh-core/skills/ssh-core/references/workflows.md   # 0
grep -c 'v1.0 status' plugins/ssh-core/skills/ssh-core/references/workflows.md   # ≥ 2
```

## Review gates

- 进 Phase B 前：A1+A2+A3 全部 validation 通过；ruff clean
- 进 Phase C 前：B1-B5 全部 smoke + validation 通过；B4 5 个 smoke 全绿
- 进 Phase D 前：B+C 完整，可以手起手停手看 status/list
- 进 Phase T 前：D1-D5 全部 spec sync 完成；`error-handling.md` 表已更新
- 进 Phase F 前：T1 unit ≥ 90% cov；T2 integration 5 case 全绿
- ship 前：全部 Final validation 命令通过；envelope 字段比对 PRD D10
- commit 前：`git restore uv.lock`（与历史 ship commit 一致，避免无关变更，参考 v1-rollout/prd.md 2026-05-25 notes）

## Rollback points

- Phase A 失败：`git checkout plugins/ssh-core/skills/ssh-core/scripts/ssh_tunnel.py` 回到 stub，不影响别处
- Phase B 失败：同上 + 删 `$TMPDIR/ssh_tunnel/*` 清残留 state；任何 detached ssh `pkill -f 'ssh -N -L'`（**只在本机本会话**慎用）
- Phase D doc sync 失败：`git checkout plugins/ssh-core/skills/ssh-core/SKILL.md plugins/ssh-core/skills/ssh-core/references/ .trellis/spec/backend/error-handling.md`
- Phase T 失败：`git rm tests/test_ssh_tunnel_*.py` 保留实现暂不交付（v1.0 ship 必须含 test —— 不算 ship 完成）
- 整体 abort：`git checkout plugins/ssh-core/ .trellis/spec/backend/error-handling.md tests/test_ssh_tunnel_*.py`；其余 child task 工件不受影响
