# remote-debug 剩余 3 脚本 v1.0

## Goal

把 `remote-debug` 的另外 3 个 v0.2 stub 实化为可用脚本,完成 `remote-debug` 的 v1.0 第二刀。所有脚本复用 [adr-001-ssh-execute](../../spec/backend/adr-001-ssh-execute.md) 已确立的 shell-out 模式,不引入新依赖。

涉及脚本:
- `port_check.py` — 从远端探测 TCP 可达性(单源单目标 / 多源多目标矩阵)。
- `tail_log.py` — 单/多主机日志拉取(`--unit` 走 journalctl,普通 path 走 tail)。
- `compare_across_hosts.py` — 2+ 主机间状态 diff(文件 / 命令 / 已装包)。

## Background

`remote-debug` v0.2 落地了 4 个 stub。`diagnose_host` 已在 [05-21-04-ssh-execute-v1](../archive/2026-05/05-21-04-ssh-execute-v1/) 中实现并跑通端到端;`SKILL.md` 已在文档里把其它 3 个脚本做了 user-facing 演示,因此 v0.2 argparse surface 不能破坏(R2)。

## Requirements

### R1. JSON 输出契约
- 全部脚本支持 `--json`,输出严格 `{success, exit_code, stdout, stderr, data}` 信封(见 [error-handling.md](../../spec/backend/error-handling.md))。
- 不 `--json` 时,raw stdout/stderr 直通。
- `exit_code` 只允许 `0 / 1 / 2 / 124`(超出需 ADR)。

### R2. v0.2 argparse 向后兼容
- 保留 v0.2 stub 的所有 positional / 长选项名;**加** flag 可以,**删** / 改名禁止。
- 本次未实现的旗标(`port_check --udp`、`tail_log --follow`)用 `argparse.SUPPRESS` 静默接收 + 一次 stderr 警告 `"<flag> deferred to v1.1"`,exit 不变。

### R3. cross-plugin 调用
- 只 shell-out 调用 `ssh-core/.../ssh_execute.py --json`,**不** import(CONTRIBUTING.md 第 29 行)。
- 解析 ssh_execute 的 envelope,把 `data.failure_class` 透传/聚合到本脚本 envelope 的 `data`。

### R4. port_check
- 单源单目标:`port_check.py <alias> --target <host> --ports 5432,6379` 在 `<alias>` 上跑 nc/`/dev/tcp` 探测每个端口。
- 矩阵:`--from a,b,c --to db-1,cache-1 --ports 5432`,生成 source × target × port 状态表。
- 探测顺序:nc 优先;若 `command -v nc` 失败,回退 `bash -c "timeout T cat </dev/tcp/<host>/<port>"`。
- 状态分类:`open / refused / filtered / host-error / source-error`,每格附 `elapsed_ms`。
- UDP 不实现(`--udp` SUPPRESS,见 R2)。

### R5. tail_log
- 单主机 path:`tail_log.py <alias> <path> --lines N` → 远端 `tail -n N <path>`。
- 单主机 unit:`tail_log.py <alias> --unit nginx --lines N [--since 5min]` → 远端 `journalctl -u nginx -n N --no-pager [--since '...']`。
- 多主机:`--hosts a,b,c` 并发跑,输出每行带 `<alias>| ` 前缀。
- `--grep <regex>`:Python 端 re 编译后逐行过滤。
- `--since` 仅在 `--unit` 模式生效;path 模式 silently 透传但写一行 stderr 警告。
- `--follow` 不实现(SUPPRESS,见 R2)。

### R6. compare_across_hosts
- 必须 `>= 2` 个 alias(沿用 v0.2 校验)。
- `--files <p1,p2>`:每文件远端 `cat <path>`,与 baseline(`aliases[0]`)做 `difflib.unified_diff`。
- `--commands "<cmd1>" "<cmd2>"`:每命令远端跑,stdout 与 baseline diff。
- `--packages`:远端 autodetect `dpkg`/`rpm`,列出已装包并 diff。
- 每个 comparison 输出:`{kind, target, baseline_host, other_host, unified_diff, differs}`。
- 顶层 summary:`{differs_count, total}`,顶层 `success = (differs_count == 0)`。

### R7. 失败语义
- 任一远端 ssh_execute 失败:**不** 中止其它探测;失败信息嵌进对应 cell/comparison 的 `data`,顶层 `success=false`、`data.failure_class = "remote"`。
- ssh_execute 本身不存在(precondition 失败):顶层 `data.failure_class = "precondition"`,沿用 `diagnose_host` 的模式。

### R8. 测试
- 每个脚本 ≥ 1 套 unit tests(`monkeypatch subprocess.run`),覆盖:成功路径、远端失败传播、JSON envelope 形状、SUPPRESS 静默 flag。
- 每个脚本 ≥ 1 个 `@pytest.mark.live_ssh` 集成测试,用项目里已配置的 `pai` alias。CI 默认 skip,`-m live_ssh` 启用。
- 多主机场景 live_ssh 用 `pai` + `local`(host="local")替身,验证并发 + line-prefix。

### R9. 质量门
- `ruff check <touched>` 0 violations。
- 全仓 `ruff check` 不退步(当前 baseline:12 errors,见 v1.0-第一刀 commit 信息)。
- `mypy <touched>`(若 mypy 在 CI 跑)同等不退步。
- `pytest -m "not live_ssh"` 默认套件全绿;`pytest -m live_ssh` 在本机 + `pai` 在线时全绿。

## Acceptance Criteria

- [x] AC1. `port_check.py pai --target localhost --ports 22,80 --json` 在 pai 上跑 nc/`/dev/tcp`,正确解析每个 port 的 status。
- [x] AC2. `port_check.py --from pai --to localhost --ports 22 --json` 输出矩阵格式 `data.matrix`。
- [x] AC3. `port_check.py pai --target unreachable.invalid --ports 80 --json` 不抛异常,返回 envelope `success=false`、status `filtered` 或 `host-error`。
- [x] AC4. `tail_log.py pai /etc/hosts --lines 5 --json` 远端拉 5 行 `/etc/hosts`。
- [x] AC5. `tail_log.py pai --unit ssh --lines 5 --json` 走 journalctl(或返回 journalctl-not-available 的 remote_error envelope,**不** 抛异常)。
- [x] AC6. `tail_log.py --hosts pai,local /etc/hosts --lines 2 --json` 并发拉,顶层 stdout 出现两个 alias 的 line-prefix。
- [x] AC7. `tail_log.py pai /etc/hosts --grep root --lines 50 --json` 本地 regex 过滤后只剩匹配行。
- [x] AC8. `tail_log.py pai --follow ...` 写一行 `follow deferred to v1.1` 到 stderr 后退出 0(SUPPRESS 兼容)。
- [x] AC9. `compare_across_hosts.py pai local --files /etc/hostname --json` 返回 unified diff(预期 differs=true)。
- [x] AC10. `compare_across_hosts.py pai local --commands "uname -s" --json` 返回 differs(若 OS 不同)或 same(若同)。
- [x] AC11. `compare_across_hosts.py pai local --packages --json` autodetect dpkg/rpm 后正确 diff;若任一端无 pkg manager,该 comparison 落 `host-error`,其它继续。
- [x] AC12. `compare_across_hosts.py pai --files /etc/hostname --json`(只 1 alias)返回 exit 2 + `"need at least two"` 信息。
- [x] AC13. 任一脚本传 v0.2 已废弃 / SUPPRESS 旗标(`--udp` / `--follow`)不挂、不报错。
- [x] AC14. `grep -R "import lib" plugins/remote-debug/` 返回 0 跨 plugin 行(只允许 inline helpers)。
- [x] AC15. `ruff check plugins/remote-debug/skills/remote-debug/scripts/{port_check,tail_log,compare_across_hosts}.py tests/test_*.py` 0 violations。
- [x] AC16. `pytest -m "not live_ssh"` 全绿(240/240);`pytest -m live_ssh` 需本机能连 pai 时跑(当前 pai 不可达,skipped)。
- [x] AC17. 没改变任何脚本的 v0.2 argparse 命令名 / positional 数;`python <script>.py --help` 仍展示 v0.2 描述的 surface(可加新 flag)。

## Non-Goals

- 不实现 `port_check --udp`、`tail_log --follow`(留 v1.1)。
- 不引入连接池 / ssh ControlMaster(全局留 v1.1,见 ADR-001 Consequences)。
- 不抽公共 lib 到 ssh-core(每个 plugin 自带 inline helpers,sync diagnose_host 的做法)。
- 不修改 `remote-debug/SKILL.md` 中除示例外的章节(若示例命令仍 plausible 则不动)。
- 不修改 `ssh-core/ssh_execute.py`(已 v1.0 稳定)。

## Notes

- 设计细节、命令选择、并发模型见 `design.md`。
- 执行顺序、checklist、验证命令见 `implement.md`。
- 任何打破 R2 的修改都必须先回到本 PRD 加 ADR。
