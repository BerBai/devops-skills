# Design — ssh-core ssh_tunnel v1.0

> 配套 PRD: `prd.md`（D1–D11 设计决定的展开论证）。
> 实施清单: `implement.md`。
> **新增 detach 模式（项目首例）**；不依赖 `ssh_daemon`（v1.1 deferred）。

## 1. Architecture overview

```
ssh_tunnel.py  (CLI, ~280–360 loc, single-file)
   │
   ├── start <alias>
   │     │
   │     ├── port allocator      (D3, socket probe 5 retries)
   │     ├── subprocess.Popen(start_new_session=True, ssh -N -L ...)
   │     │     │
   │     │     └── 子进程脱离 controlling tty → 父退出后被 init 收养
   │     │
   │     ├── 同步等 connect_timeout + 1s 看 Popen.poll()
   │     │     ├── 进程已死 → 读 stderr 分类 → 返回 failure_class
   │     │     └── 仍活 → 写 state file → 父返回
   │     │
   │     └── state file: $TMPDIR/ssh_tunnel/<md5(alias-port)>.json (0o600)
   │
   ├── stop <alias> [--port N]
   │     │
   │     ├── 扫 state dir 找 (alias, port) → SIGTERM → 3s grace → SIGKILL
   │     └── 删 state file（不论 kill 结果）
   │
   ├── status <alias>           (read-only scan + os.kill(pid, 0) probe)
   └── list                     (无 alias 过滤的 status)
```

**关键单一抉择**：detach 用 Python `start_new_session=True`，**不**用 OpenSSH `-fN`。
理由：`Popen.pid` 直接就是被脱离的 ssh PID，无需 `lsof -iTCP:<port>` / `pgrep -f` 反推。
代价：POSIX-only（`os.setsid()` 不存在于 Windows）；v1.0 在 Windows 上 `start` 显式拒绝（`failure_class="posix_only"`，exit 30）。

## 2. CLI entry point (`ssh_tunnel.py`)

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "start":
        if not _posix():
            return _refuse_posix_only(args.json)
        return _cmd_start(args)        # → exit 0 / 30–35 / 124 / 1
    if args.cmd == "stop":
        return _cmd_stop(args)         # → exit 0 / 36–39 / 1
    if args.cmd == "status":
        return _cmd_status(args)       # → exit 0 / 1
    if args.cmd == "list":
        return _cmd_list(args)         # → exit 0
    return 1
```

每个 `_cmd_*` 都返回 `int` 并已 `emit()` JSON envelope。没有任何异常逃逸到 `main()` 之外（error-handling.md Pillar 1 强制 "Do NOT raise exceptions out to the user"）。

`build_parser()` 严格按 PRD D9 — subparsers 含 `--idle-timeout`（D2 no-op 仍保留）与 `--no-detach`（D8 调试开关）。

## 3. Detach mechanism（D1 详述）

```python
proc = subprocess.Popen(
    [
        "ssh",
        "-N",                                    # 不远程执行命令
        "-L", f"{bind}:{lport}:{rhost}:{rport}",
        "-o", "ExitOnForwardFailure=yes",        # 端口被占/对端拒绝时 ssh 立即 exit
        "-o", "BatchMode=yes",                   # key auth only, no prompts
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        alias,
    ],
    start_new_session=True,                       # POSIX: os.setsid() 在 exec 前
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,                       # 父读出早期错误后关闭
    env=msys_safe_env(),                          # 项目惯例（lib/__init__.py:58）
)
```

**父侧等待协议**：
```python
deadline = time.monotonic() + connect_timeout + 1
while time.monotonic() < deadline:
    rc = proc.poll()
    if rc is not None:
        # ssh 已退出 → 读完 stderr, classify, 返回错误
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        return _classify_start_failure(rc, stderr)        # → 33/34/35
    time.sleep(0.1)
# 还活着 = 转发已建立。关 stderr pipe（子继续运行也无所谓 — kernel 会 drop write）。
if proc.stderr:
    proc.stderr.close()
_write_state(alias, lport, rhost, rport, bind, proc.pid)
return 0
```

**为什么不用 `-fN`**：OpenSSH `-f` 让 ssh 自己 fork daemon，但 `Popen.pid` 是 *parent ssh* 的 pid，daemon 的 pid 没有可移植获取方式（要靠 `lsof -iTCP -sTCP:LISTEN -t :<port>` 这种平台相关命令）。`start_new_session=True` 同样脱离 controlling tty，但 `Popen.pid` 就是 daemon 自己。

**子进程脱离父之后**：父进程 `main()` 返回 → exit。子进程仍持有 stderr fd（父关闭只是关父端），ssh 写 stderr 会被 kernel 缓冲，缓冲满后丢弃 —— 没有 SIGPIPE 风险因为 ssh 走 `-N` 几乎不再写 stderr。

## 4. State management（D3 + D4）

### 4.1 Port allocator

```python
def _alloc_local_port(explicit: int | None) -> int:
    if explicit is not None:
        if not _port_free(explicit):
            raise _TunnelError("port_busy", 31, f"local port {explicit} in use")
        return explicit
    for _ in range(5):
        port = random.randint(10000, 20000)
        if _port_free(port):
            return port
    raise _TunnelError("port_pool_exhausted", 32, "no free port in 10000–20000 (5 retries)")

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
```

`_port_free` 关 socket → 进 TIME_WAIT 状态。和接下来 ssh 的 bind 之间有竞态窗口（μs 级）。**用 `ExitOnForwardFailure=yes` 把这个竞态变成显式失败**：如果竞态发生，ssh 进程立即非零退出 → 父侧捕获 → 返回 `port_busy`。这是 honest race，不靠 sleep 掩盖。

### 4.2 State file 路径

```python
def _state_path(alias: str, port: int) -> Path:
    base = Path(tempfile.gettempdir()) / "ssh_tunnel"
    base.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.md5(f"{alias}-{port}".encode("utf-8")).hexdigest()
    return base / f"{digest}.json"
```

**绕开 `lib.alias_state_path`** 的理由：lib API 只按 alias hash（kind 仅决定目录名），但 ssh_tunnel 需要 `(alias, port)`-keyed，否则同一 alias 的两个 forward 会互相覆写 state。仍复用 `kind="ssh_tunnel"` 的目录约定 + 0o700 权限。

### 4.3 State 写入（best-effort atomic）

```python
def _write_state(alias, lport, rhost, rport, bind, pid):
    payload = {
        "pid": pid, "local_port": lport, "remote_host": rhost,
        "remote_port": rport, "bind_address": bind,
        "alias": alias, "started_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _state_path(alias, lport)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)              # POSIX rename = atomic on same FS
```

两个并发 `start` 命中同 (alias, port) 时，D3 的 port-bind 互斥已经保证只有一个能跑到这一步。无需文件锁。

## 5. Subcommand semantics（D5 + D6）

### 5.1 `stop`

```python
def _cmd_stop(args) -> int:
    matches = [p for p in _scan_state() if p.alias == args.alias
               and (args.port is None or p.local_port == args.port)]
    if not matches:
        return _refuse("tunnel_not_found", 36, ...)
    if args.port is None and len(matches) > 1:
        return _refuse("ambiguous_stop", 37, f"multiple forwards: {[m.local_port for m in matches]}; pass --port")

    stopped, files_removed, errors = [], [], []
    for m in matches:
        outcome = _kill_with_grace(m.pid)         # SIGTERM → 3s wait → SIGKILL
        try:
            m.state_file.unlink()
            files_removed.append(str(m.state_file))
        except FileNotFoundError:
            pass
        if outcome == "stale":                     # pid 已死，state 残留
            errors.append(("stale_state_only", 38, m.local_port))
        elif outcome == "kill_failed":
            errors.append(("kill_failed", 39, m.local_port))
        else:
            stopped.append(m.local_port)
    return _emit_stop(stopped, files_removed, errors, args.json)
```

`stale_state_only` (38) 仍是 non-zero，因为调用者应该知道"我以为在跑但其实早就死了"。

### 5.2 `status` / `list`

```python
def _cmd_status(args) -> int:
    forwards = [_describe(p) for p in _scan_state() if p.alias == args.alias]
    return _emit_listing(forwards, args.json)

def _cmd_list(args) -> int:
    forwards = [_describe(p) for p in _scan_state()]
    return _emit_listing(forwards, args.json)

def _describe(p: _StateEntry) -> dict:
    return {**asdict(p), "running": _pid_alive(p.pid), "state_file": str(p.path)}

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)        # signal 0 = 探测存活
        return True
    except ProcessLookupError:
        return False
    except PermissionError:    # 进程存在但归属别人 → 仍算 alive
        return True
```

`status` / `list` **不**自动清理 stale entry（D6 决定）：清理是 `stop` 的副作用，避免并发 status 互相删 state 引起惊讶。Human mode 输出一行一条：
```
prod-db:13306 → 127.0.0.1:3306 (pid 8123, running)
prod-db:13307 → 127.0.0.1:3306 (pid 8201, stale)
```

## 6. Failure class & exit code（D7）

| `failure_class` | 触发 | exit_code | 由谁判定 |
|---|---|---|---|
| `null` | success | 0 | — |
| `posix_only` | Windows / 无 `os.setsid` | 30 | `_cmd_start` 入口 |
| `port_busy` | 显式 `--local-port` 已被占 | 31 | `_alloc_local_port` |
| `port_pool_exhausted` | 5 次随机 bind 都失败 | 32 | `_alloc_local_port` |
| `ssh_connect_failed` | ssh 子进程在 `connect_timeout + 1s` 内非零退出且 stderr 匹配 NETWORK_PATTERN | 33 | `_classify_start_failure` |
| `auth` | 同上但匹配 AUTH_PATTERN | 34 | `_classify_start_failure` |
| `forward_setup_failed` | ssh 非零退出但 stderr 未匹配上面两类（包含 `ExitOnForwardFailure` 触发） | 35 | `_classify_start_failure` |
| `tunnel_not_found` | `stop`/`status` 无匹配 state | 36 | `_cmd_stop` / `_cmd_status` |
| `ambiguous_stop` | `stop` 多 forward + 无 `--port` | 37 | `_cmd_stop` |
| `stale_state_only` | `stop` 发现 pid 已死，已清 state | 38 | `_cmd_stop` |
| `kill_failed` | `os.kill` 双重失败 | 39 | `_kill_with_grace` |
| `timeout` | 父侧 wall-clock 超时（含 `connect_timeout + 1s`） | 124 | `_cmd_start` 兜底 |
| `remote_error` | 其他未分类异常 | 1 | `main()` try/except 兜底 |

**与 error-handling.md 一致性**：30–39 是新增区段，与已分配的 4–9 / 10–15 / 20–29 / 124 不冲突。在 spec update phase 追加一行（参照 ssh_server_transfer 的 20–29 那行）。

## 7. JSON envelope（D10）

**start success**：
```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "",
  "stderr": "",
  "data": {
    "alias": "prod-db",
    "pid": 8123,
    "local_port": 13306,
    "remote_host": "127.0.0.1",
    "remote_port": 3306,
    "bind_address": "127.0.0.1",
    "state_file": "/tmp/ssh_tunnel/abc123.json",
    "started_at": "2026-05-25T12:00:00+00:00",
    "flag_deferred": ["--idle-timeout"],
    "failure_class": null
  }
}
```
`flag_deferred` 只在用户显式传了 `--idle-timeout` 时出现（参照 `ssh_server_transfer` 对 `--use-rsync` 的处理）。

**stop success**：`data` 含 `ports_stopped: [int...]`、`state_files_removed: [path...]`、`failure_class: null`。
**status / list**：`data.forwards: [{...}]`（每项含 `running` 布尔）。

## 8. Module layout

```
plugins/ssh-core/skills/ssh-core/scripts/
└── ssh_tunnel.py                 (rewrite: stub 62 loc → ~280–360 loc)

.trellis/spec/backend/
└── error-handling.md             (+1 row in exit-code 表)

plugins/ssh-core/skills/ssh-core/
├── SKILL.md                      (decision tree + quick-ref 加 v1.0 注释)
└── references/
    ├── workflows.md              (§4 整段重写 — drop "Same daemon shape"; 加 v1.0/v1.1 status)
    ├── safety.md                 (L22 state-file 路径 — 仅需确认)
    └── troubleshooting.md        (L33-34 daemon 引用 → v1.0 reality)

tests/
├── test_ssh_tunnel_unit.py       (NEW, ~30 cases, ≥90% statement cov)
└── test_ssh_tunnel_integration.py (NEW, 5 live_ssh cases, POC_HOST=pai)
```

`lib/` 不动。`ssh_daemon.py` 不动（仍是 v0.2 stub + v1.1 deferred 文档）。

## 9. Compatibility & deferrals

### POSIX-only
- `start_new_session=True` 在 Windows 上 `Popen` 会 raise `AttributeError`（`os.setsid` 不存在）。我们在 `_cmd_start` 入口先用 `sys.platform != "win32" and hasattr(os, "setsid")` 检查，提前 refuse 而非让 Popen 崩。
- `stop` / `status` / `list` 在 Windows 上**仍可运行**（只读 state file + `os.kill(pid, 0)`），用来清理跨平台拷贝过来的残留 state。

### `--idle-timeout` no-op（D2）
- argparse 仍接受（surface stability，调用者代码不需要改）。
- 用户传了 → `data.flag_deferred=["--idle-timeout"]` + stderr 警告一行。
- 文档同步：SKILL.md / workflows.md / troubleshooting.md 移除 "exits after --idle-timeout" 措辞，改为 "tunnel runs until `stop`; v1.1 will reintroduce idle exit via ControlMaster"。

### v1.1 候选
- `--idle-timeout` 真实生效（依赖 ControlMaster 或 ssh_daemon revival）
- Windows 支持（替换 detach 模式或走 `pythonw.exe` + `CREATE_NEW_PROCESS_GROUP`）
- Reverse / SOCKS / UNIX-socket tunnels（surface 当前不接受）

## 10. Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Port bind race（D3 的 close → ssh bind 间隙） | M | `ExitOnForwardFailure=yes` 把竞态转成显式失败，捕获并返回 `port_busy` |
| 父进程提前 exit 时子 ssh 写满 stderr pipe → SIGPIPE | L | `-N` 模式 ssh 几乎不再写 stderr；即使写也是 kernel buffer 满后 EPIPE，对 `-o ExitOnForwardFailure=yes` 后 ssh 不再敏感；实测无 hang |
| stale state file（父崩前来不及写完） | L | `os.rename` 是 atomic — 要么有完整文件要么完全没有；不会出现半截 JSON |
| 同 alias 多 forward 的 `stop` 全杀 | M | D5 已强制 ambiguous 行为：多 forward 必须传 `--port`，否则 refuse |
| `os.kill(pid, 0)` 在 pid 被复用时误判 alive | L | 概率极低（pid 复用在系统重启或 pid wrap 后），且后果只是 status 显示 "running"；不会导致错杀 |
| Integration test 泄漏 tunnel（test 崩了没 cleanup） | H | 必须用 `try/finally` fixture，每个 test 末尾强制 `stop` 自身；conftest.py 收口 |
| Sub-agent 把 `start_new_session=True` 错写成 `preexec_fn=os.setsid` | L | 二者等价但前者是 stdlib 推荐写法（Python 3.2+），design.md / implement.md 明示选 `start_new_session` |

## 11. Test strategy（PRD D11 收口）

### Unit (`test_ssh_tunnel_unit.py`)

主要 mock：`subprocess.Popen`、`socket.socket`、`os.kill`、`time.sleep`、`Path.replace`、`sys.platform`、`random.randint`。

| 类别 | 覆盖点 |
|---|---|
| argv 构造 | start 默认 / 自定义 lport / 自定义 bind / 自定义 connect_timeout / `--idle-timeout` warns / `--no-detach` |
| 端口分配 | explicit free → 用；explicit busy → 31；pool 第 1 次中 → 用；pool 5 次全 fail → 32 |
| state IO | 写 → 读 round-trip 全字段保留；目录不存在自动建；权限 0o600 |
| stop | 单匹配 SIGTERM 成功 → 0；多匹配无 port → 37；无匹配 → 36；pid 已死 → 38；SIGTERM+SIGKILL 都失败 → 39 |
| status/list | 空 → forwards=[]；多个 → 全列出 + running 字段正确 |
| failure_class | 30–39 + 124 + 1 每个都 trigger 一次 |
| Windows refuse | mock `sys.platform="win32"` + `hasattr(os, "setsid")=False` → 30 |

目标：**≥90% statement coverage** on `ssh_tunnel.py`。

### Integration (`test_ssh_tunnel_integration.py`, `@pytest.mark.live_ssh`)

`POC_HOST=pai`（与 ssh_upload / ssh_server_transfer 一致）。每个 test 包 `try/finally` 强制 cleanup。

| Test | 验证什么 |
|---|---|
| `test_start_stop_roundtrip` | `start pai --remote-port 22 --local-port=<random>` → 验证本地端口 listening + `ssh -p <lp> 127.0.0.1 echo ok` 通 → `stop` → 端口不再 listen + state file 删除 |
| `test_no_detach_lifecycle` | `--no-detach` 在 subprocess 跑 → 2s 后 SIGTERM → 验证 exit code 反映 SIGTERM |
| `test_status_empty` | `status` 在 pai 无 tunnel 时 → `forwards=[]`, exit 0 |
| `test_stop_not_found` | 无 tunnel 时 `stop pai` → exit 36 + `tunnel_not_found` |
| `test_idle_timeout_noop` | `start ... --idle-timeout 5` → 10s 后 tunnel 仍活 + `data.flag_deferred=["--idle-timeout"]` |

**Cleanup contract**：conftest 提供 `_kill_alias_tunnels(alias)` fixture，每个 test 结束 unconditionally 调用，保证 pai 上没有 leaked ssh。

## 12. Compatibility notes（对已 shipped 脚本）

- 不修改 `lib/__init__.py` 或 `lib/sftp_transport.py`
- 不修改 `ssh_execute.py` / `ssh_cluster.py` / `ssh_config_manager.py` / `deploy_pubkey.py` / `ssh_upload.py` / `ssh_download.py` / `ssh_server_transfer.py`
- 不破坏 default `pytest tests/`（670 PASS）或 `pytest -m live_ssh`（49 PASS — 基准在 ssh_server_transfer ship 后）
- 不修改 `pyproject.toml`、`uv.lock`、`pytest.ini`
