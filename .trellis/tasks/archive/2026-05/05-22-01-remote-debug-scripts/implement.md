# Implement: remote-debug 剩余 3 脚本 v1.0

> 设计已在 `design.md`,本文是**执行计划**:阶段顺序、checklist、验证命令、review gate、rollback。

---

## 阶段顺序

```
A. port_check.py    (最简,作为模式建立 — 串行 ssh_execute,矩阵 + 状态解析)
B. tail_log.py      (中等 — ThreadPoolExecutor 并发 + journalctl 路由)
C. compare_across_hosts.py  (最复杂 — difflib + 三模式 + cross-distro 处理)
D. 集成扫尾         (live_ssh 全跑,ruff/mypy baseline,SKILL.md 检查)
```

每阶段独立 commit,粒度小。每阶段先 unit 全绿 + ruff 触碰文件 0,**再**走下一阶段。

---

## 阶段 A — `port_check.py`

### A.1 实现

- [ ] Read 当前 stub,保留 docstring 顶部的 surface 注释更新到 v1.0(类 `diagnose_host`)。
- [ ] argparse:保留全部 v0.2 flag。新增 / 透传:
  - `--connect-timeout`(默认 8)→ 透传给 ssh_execute。
  - `--udp` → `argparse.SUPPRESS` 静默,但 `args.udp` 仍可读,触发时 stderr 一次 `"udp deferred to v1.1"`。
- [ ] 实现 `_parse_ports(spec: str) -> list[int]`,支持 `5432,6379` 和 `8000:8003`,负值 / 反向 / >65535 拒绝。
- [ ] 实现 `_run_via_ssh_execute(alias, command, timeout) -> dict`(envelope)。inline,不 import lib。
- [ ] 实现 `_probe_one(source: str, target: str, port: int) -> dict`:
  - 一次性探测命令 `bash -c 'if command -v nc >/dev/null 2>&1; then nc -zv -w T <t> <p> 2>&1; else timeout T bash -c "exec 3<>/dev/tcp/<t>/<p>" 2>&1; fi'`(单次 ssh round-trip)。
  - 解析 status(open/refused/filtered/host-error)+ elapsed_ms。
- [ ] 主路径:
  - 单源单目标:`alias + --target target + --ports`:遍历每 port,生成 N 个 cell。
  - 矩阵:`--from a,b,c + --to d,e + --ports`:嵌套循环。
- [ ] 数据形状参 design.md §2.3。
- [ ] exit code 参 design.md §2.5。

### A.2 测试

- [ ] `tests/test_port_check_unit.py`:
  - happy path(mock 返回 nc 成功 stdout)→ status=open
  - mock 返回 "Connection refused" → status=refused
  - mock 返回 "timed out" → status=filtered
  - mock 返回 "Name or service not known" → status=host-error
  - mock ssh_execute envelope failure_class=auth → status=source-error
  - `_parse_ports` 单/多/range/错值
  - 矩阵模式生成正确 cell 数
  - `--udp` SUPPRESS 不挂 + stderr 警告
  - JSON envelope 形状完整
- [ ] `tests/test_port_check_integration.py`(`@pytest.mark.live_ssh`):
  - `pai --target localhost --ports 22`(predicted open)
  - `pai --target localhost --ports 9999`(predicted refused 或 filtered,只断言非 open)

### A.3 Gate
- [ ] `ruff check plugins/remote-debug/.../port_check.py tests/test_port_check_*.py` 0 violations
- [ ] `pytest tests/test_port_check_unit.py -v` 全绿
- [ ] commit `feat(remote-debug): implement port_check v1.0 (R4/R7/R8)`

---

## 阶段 B — `tail_log.py`

### B.1 实现

- [ ] argparse:保留全部 v0.2 flag。处理:
  - `--follow` → SUPPRESS,触发时 stderr 一次 `"follow deferred to v1.1"`,继续走 batch 模式。
  - `--since` 无 `--unit` → 忽略 + stderr 警告(参 design §3.1)。
- [ ] inline helpers(同 A,可复制粘贴一致)。
- [ ] 实现 `_build_remote_cmd(path, unit, lines, since) -> str`,用 `shlex.quote`。
- [ ] 实现 `_pull_one(alias, remote_cmd, timeout) -> dict`(单 host 拉取 + envelope 解析)。
- [ ] 实现 `_apply_grep(lines: list[str], grep_pattern: re.Pattern | None) -> list[str]`。
- [ ] 多主机路径:`ThreadPoolExecutor(max_workers=min(8, len(hosts)))`,`as_completed` 收集。
- [ ] 输出拼接:每行 `f"{alias}| {line}"`,顶层 stdout 是拼接结果,`data.hosts[alias].lines` 是结构化。
- [ ] 数据形状参 design §3.5。
- [ ] exit code 参 design §3.7。

### B.2 测试

- [ ] `tests/test_tail_log_unit.py`:
  - path 单主机 happy(mock tail 返回 N 行)
  - `--unit` 单主机 happy(mock journalctl)
  - `--grep` 本地过滤验证(mock 返回 5 行,grep "foo" → 2 行匹配)
  - `--grep` 无效 regex → exit 2 + stderr 错误
  - `--since` 无 `--unit` → stderr 警告但仍执行
  - 多主机 mock:返回 2 个 host,顶层 stdout 含两个 `<alias>| ` 前缀
  - 多主机其中 1 个失败:顶层 success=false,`data.hosts[failed].remote_exit_code != 0`,其它仍有数据
  - `--follow` SUPPRESS 不挂 + stderr 警告
  - JSON envelope 形状完整
- [ ] `tests/test_tail_log_integration.py`(`@pytest.mark.live_ssh`):
  - `pai /etc/hosts --lines 3`(数 lines)
  - `--hosts pai,local /etc/hosts --lines 2`(并发 + 前缀)
  - `pai --unit ssh --lines 3`(尽量不挂;若远端无 ssh.service,断言走 remote_error envelope 而不是抛)

### B.3 Gate
- [ ] `ruff check plugins/remote-debug/.../tail_log.py tests/test_tail_log_*.py` 0
- [ ] `pytest tests/test_tail_log_unit.py -v` 全绿
- [ ] commit `feat(remote-debug): implement tail_log v1.0 (R5/R7/R8)`

---

## 阶段 C — `compare_across_hosts.py`

### C.1 实现

- [ ] argparse:保留全部 v0.2 flag。无新增 SUPPRESS。
- [ ] inline helpers。
- [ ] `_fetch_file(alias, path, timeout) -> tuple[str | None, dict]`(content, error_info)。
- [ ] `_fetch_command(alias, command, timeout) -> tuple[str | None, dict]`。
- [ ] `_fetch_packages(alias, timeout) -> tuple[str | None, str | None, dict]`(content, distro, error_info)。
  - 远端命令:`if command -v dpkg >/dev/null 2>&1; then dpkg -l | awk '/^ii/{print $2"="$3}' | sort; elif command -v rpm >/dev/null 2>&1; then rpm -qa --queryformat "%{NAME}=%{VERSION}\n" | sort; else echo "NONE"; exit 99; fi`
  - 解析 stdout 前若 exit 99 → distro=None。
  - 同时 capture 哪种 manager(添加 `data.comparisons[i].distro_baseline / distro_other`)。
- [ ] `_unified(a, b, a_label, b_label, context) -> str`(difflib)。
- [ ] 主路径:baseline = aliases[0],遍历 others,每种 mode 跑一次,push 到 `data.comparisons`。
- [ ] cross-distro packages:differs=true + unified_diff 写 `"(distro mismatch: dpkg vs rpm)"`,不试归一化。
- [ ] summary:`differs_count` / `total` / `by_kind`。
- [ ] exit code 参 design §4.6。

### C.2 测试

- [ ] `tests/test_compare_across_hosts_unit.py`:
  - `--files` happy(mock 两边 cat 不同内容)→ differs=true + unified_diff 非空
  - `--files` 内容相同 → differs=false + unified_diff=""
  - `--files` baseline cat 失败 → cell 有 `error.side="baseline"`,顶层 success=false
  - `--commands` happy
  - `--packages` 同 distro(mock 两边都 dpkg)
  - `--packages` cross-distro(mock baseline=dpkg, other=rpm)→ differs=true + 特殊 diff
  - `--packages` 都无 pkg manager → cell error
  - argv:1 alias → exit 2
  - argv:没给 --files / --commands / --packages → exit 2(否则跑啥都没,设计上禁)
  - summary 计数正确
  - JSON envelope 形状
- [ ] `tests/test_compare_across_hosts_integration.py`(`@pytest.mark.live_ssh`):
  - `pai local --files /etc/hostname`(差异强烈 → differs=true)
  - `pai local --commands "uname -s"`(可能 differs 也可能 same;只断言 envelope 良好 + summary 字段有值)

### C.3 Gate
- [ ] `ruff check plugins/remote-debug/.../compare_across_hosts.py tests/test_compare_*.py` 0
- [ ] `pytest tests/test_compare_across_hosts_unit.py -v` 全绿
- [ ] commit `feat(remote-debug): implement compare_across_hosts v1.0 (R6/R7/R8)`

---

## 阶段 D — 集成扫尾

### D.1 Live SSH 全跑
- [ ] `pytest -m live_ssh -v`(本机 + pai 在线)— 第一刀的 20 个 live test + 本任务新增的 ~6 个全绿。
- [ ] 任一 failure 回到对应阶段修。

### D.2 静态质量
- [ ] `ruff check plugins/remote-debug/skills/remote-debug/scripts/{port_check,tail_log,compare_across_hosts}.py tests/test_port_check_*.py tests/test_tail_log_*.py tests/test_compare_across_hosts_*.py` 0。
- [ ] `ruff check`(全仓 baseline)errors 数 ≤ 12(进入本任务前的数);理想是更少。
- [ ] `mypy plugins/remote-debug/.../{port_check,tail_log,compare_across_hosts}.py`(若 mypy 配置存在)→ 0 errors。

### D.3 文档与 SKILL.md
- [ ] `grep -n` 检查 `plugins/remote-debug/skills/remote-debug/SKILL.md` 里 3 个脚本的演示命令,确认 v1.0 surface 仍兼容。
- [ ] **不**修改 SKILL.md(除非示例失效)。
- [ ] **不**修改 spec(本任务无 ADR 级决策)。
- [ ] **不**修改 ssh_execute.py 或 ssh-core lib。

### D.4 反向验证
- [ ] `python plugins/remote-debug/skills/remote-debug/scripts/port_check.py pai --target localhost --ports 22 --json | jq`(jq 解析成功)
- [ ] `python plugins/remote-debug/skills/remote-debug/scripts/tail_log.py pai /etc/hosts --lines 3 --json | jq`
- [ ] `python plugins/remote-debug/skills/remote-debug/scripts/compare_across_hosts.py pai local --files /etc/hostname --json | jq`

### D.5 兼容回归
- [ ] 跑 v0.2 stub 风格命令(只用 v0.2 已展示的 flag):
  - `python ...port_check.py pai --target localhost --ports 22 --udp`(--udp 不挂)
  - `python ...tail_log.py pai /etc/hosts --follow`(--follow 不挂,SUPPRESS)
  - 都退出 0 / 1(不是 2)。

### D.6 Spec 与 commit 收尾
- [ ] `git log --oneline ^main` 整理 4 个 commit。
- [ ] 检查 `.trellis/spec/backend/index.md` — 不需要更新(本任务没有 ADR / 新规范)。
- [ ] `git push origin main`。
- [ ] `python3 ./.trellis/scripts/task.py archive 05-22-01-remote-debug-scripts`。

---

## 验证命令(一键)

```bash
# 单元测试(默认套,无 live)
pytest -m "not live_ssh" tests/test_port_check_unit.py tests/test_tail_log_unit.py tests/test_compare_across_hosts_unit.py -v

# Live integration(需要本机 + pai 在线)
pytest -m live_ssh tests/test_port_check_integration.py tests/test_tail_log_integration.py tests/test_compare_across_hosts_integration.py -v

# Ruff 触碰文件
ruff check \
  plugins/remote-debug/skills/remote-debug/scripts/{port_check,tail_log,compare_across_hosts}.py \
  tests/test_port_check_*.py tests/test_tail_log_*.py tests/test_compare_across_hosts_*.py

# Ruff baseline
ruff check 2>&1 | tail -5

# Reverse 反向验证
for s in port_check tail_log compare_across_hosts; do
  python plugins/remote-debug/skills/remote-debug/scripts/${s}.py --help >/dev/null && echo "$s OK" || echo "$s FAIL"
done
```

---

## Review gates(自检 checklist)

进入下一阶段前,本阶段必须:

1. ✅ unit tests 全绿
2. ✅ ruff 触碰文件 0
3. ✅ envelope 形状手测过 1 次(jq 解析)
4. ✅ commit message 含 PRD 需求编号

每阶段大约 1-1.5h 工作量;完成后回报"阶段 X 完成"再问"继续 Y"。

---

## Rollback

| 出问题阶段 | 操作 |
|---|---|
| A 阶段 | `git restore plugins/remote-debug/.../port_check.py tests/test_port_check_*.py` |
| B 阶段 | 同上换 tail_log |
| C 阶段 | 同上换 compare_across_hosts |
| 已 push 后发现 bug | 修复并 `feat(...): hotfix ...` 新 commit;不 force-push |

---

## 风险与对策

| 风险 | 对策 |
|---|---|
| `pai` 是 1 个真实远端,无法测真正"多主机"语义 | 用 `pai + local`(host="local")替身;ssh_execute 已支持 local 路由 |
| `nc` 在某些容器镜像里没有,fallback 必走 /dev/tcp | unit 用 mock 覆盖两条路径;live 测仅断言 status 正确,不锁工具 |
| `journalctl -u ssh`(若远端 systemd 单元名不是 `ssh`)失败 | 测试断言 "envelope 良好"而非 "exit=0";接受 remote_error |
| `compare_across_hosts --packages` 跨发行版误判 | design §4.2 已明确 cross-distro 处理 |
| `tail_log --hosts` 主机数太多导致 fd 耗尽 | ThreadPoolExecutor max_workers ≤ 8;ssh_execute 每次新进程,无连接池意味着 fd 在 subprocess 内 |
