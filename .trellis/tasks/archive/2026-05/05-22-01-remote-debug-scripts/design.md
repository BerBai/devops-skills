# Design: remote-debug 剩余 3 脚本 v1.0

> 在 PRD 之上,**技术决策**:命令选择、解析策略、并发模型、失败传播、复用 `diagnose_host` 模式。

---

## 1. 共用模式(每个脚本各自 inline,不抽 lib)

沿用 `diagnose_host.py` 的做法,因为 plugin 间禁止 import(CONTRIBUTING.md L29):

- `_ssh_execute_path() -> Path`:`Path(__file__).resolve().parents[4] / "ssh-core/skills/ssh-core/scripts/ssh_execute.py"`(4 层:scripts → skill → skills → plugin → plugins)。
- `_envelope(success, exit_code, stdout="", stderr="", data=None)`:构造顶层信封。
- `_emit(envelope, as_json)`:`--json` 时 `json.dump`,否则 raw stdout/stderr 直通。
- `_run_remote(alias, command, timeout)`:`subprocess.run(["python3", _ssh_execute_path(), alias, command, "--json", "--timeout", str(timeout)])`,解析返回的 envelope。复用 ssh_execute 的 failure_class 透传。

**为什么不抽 lib**:每个脚本只需 ~20 行 helpers,重复成本 < 跨 plugin import 的复杂度。在 v0.3 / 第三个 plugin 实化时再评估是否抽 `_lib_inline.py` template。

---

## 2. `port_check.py`

### 2.1 探测命令选择
两阶段:先在远端单次询问 `command -v nc`,再生成探测命令。

| nc 可用 | 探测命令(单个 port) |
|---|---|
| 是 | `nc -zv -w <timeout> <target> <port> 2>&1` |
| 否 | `bash -c 'timeout <timeout> bash -c "exec 3<>/dev/tcp/<target>/<port>" 2>&1'` |

**为什么 stderr 重定向到 stdout**:nc 在 macOS / Linux 都把成功 / 失败信息写到 stderr,这里我们想用 ssh_execute envelope 的 `stdout` 拿到全部输出,简化解析。

### 2.2 状态解析

| 状态 | 触发条件 |
|---|---|
| `open` | nc 退出 0(或 /dev/tcp 退出 0) |
| `refused` | stdout 含 `"Connection refused"` |
| `filtered` | nc 超时(stdout 含 `"timed out"`),或 /dev/tcp 命令本身被 `timeout` 杀,退出 124 |
| `host-error` | `getaddrinfo` / `Name or service not known` |
| `source-error` | ssh_execute 自身失败(`data.failure_class in {auth, network, timeout}`) |

正则常量集中放在文件头(便于测试)。

### 2.3 多源多目标矩阵

数据形状:
```python
data["matrix"] = [
    {"source": "web-1", "target": "db-1", "port": 5432, "status": "open",     "elapsed_ms": 12},
    {"source": "web-1", "target": "db-1", "port": 6379, "status": "refused",  "elapsed_ms": 5},
    {"source": "web-2", "target": "db-1", "port": 5432, "status": "filtered", "elapsed_ms": 2003},
    ...
]
```

**串行实现**:外层 for source,内层 for (target, port);每 source × target × port 一次 ssh_execute。
- 这一刀不上 ThreadPoolExecutor:port_check 的瓶颈是 ssh 握手,本任务先求功能正确;并发在 R7 的失败语义之上加复杂度,留作 v1.0.1 调优。
- `tail_log` 的并发是必须的(多主机 follow 语义),会做。

### 2.4 端口列表解析

接受 `5432,6379,9092` 也接受 `8000:8003`(包含两端)。`_parse_ports("5432,8000:8002,9092") → [5432, 8000, 8001, 8002, 9092]`,超过 65535 / 负数 / 反向 range 拒绝(返回 exit 2 + stderr 错误,**不**抛)。

### 2.5 exit code

- `0`:所有 cell `status == open`。
- `1`:任一 cell 非 `open`(包括 `source-error`)。
- `2`:argv 错误(无 `--ports`、port 解析失败、`--from` 缺 `--to` 等)。
- `124`:任一 ssh_execute 超时(透传)。

---

## 3. `tail_log.py`

### 3.1 远端命令路由

| 输入 | 远端命令 |
|---|---|
| path `/var/log/foo.log` | `tail -n <N> <path>` |
| path 且 `--grep` | `tail -n <N> <path>`(grep 在**本地**做,见 3.4) |
| `--unit nginx` | `journalctl -u nginx -n <N> --no-pager` |
| `--unit nginx --since '5min ago'` | `journalctl -u nginx -n <N> --no-pager --since '5min ago'` |
| `--since` 但无 `--unit` | 忽略 `--since`,stderr 警告 |

**`--since` 解析**:不在客户端解析时间字符串;原样透传给 `journalctl --since`(journalctl 自己接 `"5min ago"`、`"yesterday"`、ISO8601)。

**Shell 注入防御**:`--unit` 值用 `shlex.quote`;`--since` 同。`--path` 同(里面可能含空格但不该有元字符;quote 后即使有也无害)。

### 3.2 多主机并发

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
    futures = {pool.submit(_pull_one, host, ...): host for host in hosts}
    for fut in as_completed(futures):
        ...
```

每个 worker 独立 `subprocess.run` ssh_execute(每次新进程,没有全局状态)。

**输出顺序**:不保证按 alias 字母序;按 `as_completed` 完成顺序拼接。`data.hosts[alias]` 字典里有所有数据,顺序敏感的用户可以从那里取。

### 3.3 行前缀

```
pai| 127.0.0.1 localhost
pai| ::1     localhost
local| 127.0.0.1 localhost
local| ::1   localhost
```

每行 `f"{alias}| {line}"`。空行(`""`)也加前缀(`"alias| "`)。

### 3.4 `--grep` 本地过滤

```python
pattern = re.compile(args.grep) if args.grep else None
matched_lines = [ln for ln in raw_lines if not pattern or pattern.search(ln)]
```

**编译错误**(invalid regex)→ exit 2 + stderr 错误,**不** 抛。

### 3.5 数据形状

单主机:
```json
{
  "data": {
    "host": "pai",
    "source": {"kind": "path", "value": "/etc/hosts"},
    "lines": ["127.0.0.1 localhost", "::1 localhost"],
    "raw_text": "127.0.0.1 localhost\n::1 localhost\n",
    "filter": {"grep": null, "matched": 2, "total": 2},
    "remote_exit_code": 0
  }
}
```

多主机:
```json
{
  "data": {
    "hosts": {
      "pai":   { "lines": [...], "raw_text": "...", "remote_exit_code": 0 },
      "local": { "lines": [...], "raw_text": "...", "remote_exit_code": 0 }
    },
    "source": {"kind": "path", "value": "/etc/hosts"},
    "filter": {"grep": null, "total_lines": 4}
  }
}
```

顶层 `stdout` = 拼接好的带前缀文本(便于人看),`data.hosts[*].lines` 是结构化的(便于程序看)。

### 3.6 `--follow` 处理

```python
if args.follow:
    sys.stderr.write("tail_log: --follow deferred to v1.1\n")
    # 继续执行 non-follow 路径,但 N 用 args.lines(默认 200)
```

不退出错误码 — 静默降级到 batch 拉取。

### 3.7 exit code

- `0`:全部 host 成功(每个 host 远端 exit 0)。
- `1`:任一 host 远端 ≠ 0,或任一 host ssh_execute 失败。
- `2`:argv 错误(grep regex 错、缺 path/unit)。
- `124`:任一 host ssh_execute 超时(透传)。

---

## 4. `compare_across_hosts.py`

### 4.1 比较算法

**Baseline**:`aliases[0]`。其它每个 alias 与 baseline 做两两 diff。

> Alternative: pairwise all-vs-all。**否决**:N=5 时 10 对 diff,信息过载;baseline 模型对应"我有一个 known-good 主机,其它都跟它对比"的常见场景,且和 v0.2 SKILL 里展示的 `prod-web-01 prod-web-02 --files ...` 用法一致。

### 4.2 三种比较模式

| Mode | 远端命令 | 解析 |
|---|---|---|
| `--files /etc/foo,/etc/bar` | 每文件 `cat <path>` | stdout 即文件内容,直接 diff |
| `--commands "cmd1" "cmd2"` | 每命令直接跑 | stdout 即输出,直接 diff |
| `--packages` | 先 `command -v dpkg \|\| command -v rpm` 探测,然后 `dpkg -l \| awk '/^ii/{print $2"="$3}' \| sort` 或 `rpm -qa --queryformat "%{NAME}=%{VERSION}\n" \| sort` | 直接 diff |

**Cross-distro 包对比**(一端 dpkg、另一端 rpm):exit 1 + 该 comparison 标记 `kind="packages"`、`differs=true`、`unified_diff="(distro mismatch: dpkg vs rpm)"`。**不** 试图归一化包名 — 这条路太深(v1.1 都未必做)。

### 4.3 Diff 实现

```python
import difflib

def _unified(a: str, b: str, a_label: str, b_label: str, context: int = 3) -> str:
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a_lines, b_lines, fromfile=a_label, tofile=b_label, n=context))
```

空 diff(`""`)→ `differs=False`。任何非空 → `differs=True`。

### 4.4 数据形状

```json
{
  "data": {
    "baseline": "pai",
    "others": ["local"],
    "comparisons": [
      {
        "kind": "file",
        "target": "/etc/hostname",
        "baseline_host": "pai",
        "other_host": "local",
        "unified_diff": "--- pai:/etc/hostname\n+++ local:/etc/hostname\n@@ ...",
        "differs": true,
        "baseline_exit_code": 0,
        "other_exit_code": 0
      },
      {
        "kind": "command",
        "target": "uname -s",
        ...
      },
      {
        "kind": "packages",
        "target": "(autodetect)",
        ...
      }
    ],
    "summary": {
      "differs_count": 2,
      "total": 3,
      "by_kind": {"file": 1, "command": 1, "packages": 0}
    }
  }
}
```

### 4.5 失败 cell 处理

任一 cell 的 baseline 或 other 抓取失败(ssh_execute 失败或远端 exit ≠ 0):

```json
{
  "kind": "file",
  "target": "/etc/missing",
  "baseline_host": "pai",
  "other_host": "local",
  "unified_diff": "",
  "differs": false,
  "error": {"side": "baseline", "remote_exit_code": 1, "remote_stderr": "cat: /etc/missing: No such file or directory"}
}
```

带 `error` 的 cell:`differs=false`(无意义),但顶层 `success=false`(因为我们无法判定)。

### 4.6 exit code

- `0`:全部 cell 无 error 且 `differs=false`(完全一致)。
- `1`:任一 cell `differs=true` 或 `error`。
- `2`:argv 错误(只 1 alias、`--files` 与 `--commands` 都没给等)。

---

## 5. 测试矩阵

### 5.1 Unit (默认套,`pytest -m "not live_ssh"`)

每个脚本:

| 用例 | 验证 |
|---|---|
| happy path | mock ssh_execute 返回 exit 0 + stdout,脚本 envelope success=true |
| 远端 exit ≠ 0 | mock 返回 exit 1,顶层 success=false、`data.<cell>.error` |
| ssh_execute 不存在 | mock 让 `_ssh_execute_path()` 指向不存在的路径,envelope `failure_class=precondition` |
| ssh_execute 超时透传 | mock 返回 exit 124,顶层 exit_code=124、failure_class=timeout |
| JSON envelope 形状 | 必含 success/exit_code/stdout/stderr/data,data 含脚本特定字段 |
| SUPPRESS flag | 传 `--udp` / `--follow`,不挂、走兼容路径 |
| argparse 错误 | 缺 required、错 regex、错 port range,exit 2 |

### 5.2 Live ssh (`pytest -m live_ssh`,本机 pai 在线时)

| 脚本 | 测试 |
|---|---|
| port_check | 单源单目标 `pai` → `localhost:22`(predicted open)+ `localhost:9999`(predicted refused/filtered) |
| tail_log | 单主机 `pai /etc/hosts --lines 3`,多主机 `pai,local /etc/hosts --lines 2`(前缀验证) |
| compare_across_hosts | `pai local --files /etc/hostname`(预期 differs)+ `pai local --commands "uname -s"`(若不同 OS 则 differs) |

集成测试用 `pai` + `local` 作为 "two host" 模拟,因为环境只有 1 个真实远端 alias。

### 5.3 编排

测试文件命名:`tests/test_<script>_unit.py` + `tests/test_<script>_integration.py`(沿用第一刀)。
集成测试用 `pytest.mark.live_ssh` 标记,`conftest.py` 已有逻辑(若没有则补)。

---

## 6. 失败传播总结(整合表)

| 情况 | port_check | tail_log | compare_across_hosts |
|---|---|---|---|
| ssh_execute 不存在 | precondition, exit 1 | precondition, exit 1 | precondition, exit 1 |
| 远端 alias 不可达(auth/network) | matrix cell `source-error` | data.hosts[alias].error | comparison.error.side |
| 远端命令 exit ≠ 0 | matrix cell `host-error` / `refused` / `filtered`(按 stdout 内容) | data.hosts[alias] 带 remote_exit_code + remote_stderr | comparison.error |
| ssh_execute 超时 | matrix cell `filtered` + elapsed=timeout | data.hosts[alias].timed_out=true | comparison.error.kind="timeout" |
| local 端 argv 错误 | exit 2 | exit 2 | exit 2 |

顶层 `success` 永远是 "所有 cell 没出错且语义满足" 的逻辑且:
- port_check: `all cells.status == open`
- tail_log: `all hosts succeeded`
- compare_across_hosts: `all cells differ=false && no error`

---

## 7. Compatibility checklist

| 脚本 | v0.2 argv → v1.0 处理 |
|---|---|
| port_check | positional `alias`,`--from` / `--target` / `--to` / `--ports` / `--udp` / `--timeout` / `--json` — 全部保留;`--udp` SUPPRESS;其它实现 |
| tail_log | positional `alias` / `path`,`--unit` / `--hosts` / `--lines` / `--since` / `--grep` / `--follow` / `--json` — 全部保留;`--follow` SUPPRESS |
| compare_across_hosts | positional `aliases` (>=2),`--files` / `--commands` / `--packages` / `--context` / `--json` — 全部保留;全部实现 |

---

## 8. Rollback shape

每脚本一次 commit:
- `feat(remote-debug): implement port_check v1.0 (R4/R7/R8)`
- `feat(remote-debug): implement tail_log v1.0 (R5/R7/R8)`
- `feat(remote-debug): implement compare_across_hosts v1.0 (R6/R7/R8)`
- `test(remote-debug): live_ssh integration suite for v1.0 trio`(若需要)

回滚粒度到脚本级。整体回滚 `git revert <range>`。
