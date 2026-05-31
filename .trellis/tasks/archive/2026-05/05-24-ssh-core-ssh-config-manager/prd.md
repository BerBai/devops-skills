# ship ssh-core ssh_config_manager v1.0

## Goal

把 `plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py` 从 73 loc 的 stub 推进到 v1.0 完成态：对 `~/.ssh/config` 实施 CRUD（create/update/delete/list/find），保留 OpenSSH 原生兼容性的同时，挂载元数据（description/environment/tags/location）作为可被 ssh-core 工具读取的 comment 注解。

## Background

- v1.0 surface 在脚本 docstring 已确定（行 17–24）：5 个子命令、必填/可选 flag 已枚举
- `references/workflows.md` §5 已给出 4 步 write 流程（read AST → mutate → backup → render）
- `references/safety.md` §soft 已规定 `.bak.<unix-ts>` 命名 + 不自动清理旧 backup
- `references/safety.md` §things-we-dont-do 已禁止往 config 写密码字段
- `troubleshooting.md` 已假定 `ssh_config_manager.py find <alias>` 可用并给出排障流程
- SKILL.md workflow 第 1 步 "Discover via `ssh_config_manager.py list`" 是其他 stub 的隐性依赖

意味着 surface 与边界条件已被前置文档锁死，本任务的核心是把 **AST 解析器 + render + backup + subcommand 路由** 实现出来，并通过 unit + integration 测试覆盖每个分支。

## In-scope

### 子命令

| Cmd | Args | 输出 |
|---|---|---|
| `create` | `--alias --host --user [--port --key --proxy-jump --environment --tags --description --location]` | success=true, data={alias, action="created"} |
| `update` | `--alias [<上述任意字段>]` | success=true, data={alias, action="updated", changed_fields=[...]} |
| `delete` | `--alias` | success=true, data={alias, action="deleted"} |
| `list` | `[--environment X --tags a,b]` | success=true, data={hosts: [{alias, host, user, port, environment, tags, location, description}, ...]} |
| `find` | `<alias>` | success=true, data={alias, host, user, port, key, proxy_jump, environment, tags, location, description, raw_lines: [...]} |

所有命令支持 `--json`；human mode 走 lib.emit 渲染。

### 数据模型与文件

- 解析对象：`~/.ssh/config`（默认）；可被 `--config-path` 覆写以便测试
- 元数据格式（紧邻 `Host <alias>` 上方的连续注释行）：
  ```
  # description: prod web on aliyun beijing
  # environment: production
  # tags: web,nginx
  # location: aliyun-beijing
  ```
- OpenSSH `Host` block 内字段：`HostName / User / Port / IdentityFile / ProxyJump`
- 写入规则：含 `IdentityFile` 的新 Host 必须自动追加 `IdentitiesOnly yes`
- Backup：mutate 前 `cp ~/.ssh/config ~/.ssh/config.bak.<unix-ts>`，权限延用原文件（通常 0600）

### 失败语义

| Class | exit_code | data.failure_class |
|---|---|---|
| OK | 0 | `null` |
| Alias 不存在（update/delete/find） | 4 | `"not_found"` |
| Alias 已存在（create） | 5 | `"already_exists"` |
| Config 文件不存在 | 6 | `"config_missing"` |
| Config 文件解析失败 | 7 | `"parse_error"` |
| 文件权限错误（读 or 写） | 8 | `"permission"` |
| 内部异常 | 1 | `"internal"` |

`stderr` 始终包含 human-readable 错误说明；`--json` 完整结构化。

## Out-of-scope

- 多 config 文件 include 解析（`Include` directive）—— v1.0 解析顶层文件即可；引用项不展开。v1.1 评估。
- 密码字段读写（safety.md §things-we-dont-do）—— 即使用户在 comment 里写了密码，本工具不解析也不写入
- Backup 清理 / GC —— user 自行处理
- 加密 / .ssh 目录创建 / 文件权限初始化 —— 由 user 或 deploy_pubkey 负责
- Match block（`Match user X`）—— v1.0 不解析、不破坏，原样保留

## Constraints

1. **OpenSSH 兼容**：写入后 `ssh -F /path/to/config <alias>` 必须不报 syntax error（用 `ssh -G <alias>` 自检）
2. **保序**：已有 Host block 位置不变；create 时 append 到文件末
3. **保留注释**：非元数据 comment（用户自己写的注释）不能被吞
4. **JSON 契约一致**：复用 `lib.json_result / lib.emit`
5. **MSYS 安全**：写文件用 `pathlib.Path.write_text(encoding="utf-8")`，env 走 `msys_safe_env()` 兜底
6. **测试覆盖**：unit test 不依赖真实 `~/.ssh/config`；integration test 默认 skip 除非 `--config-path` 显式指向 tmp file
7. **Atomic write**：先写 `~/.ssh/config.tmp`，再 `os.replace` 到目标路径
8. **测试不动用户的真实 ~/.ssh/config**：所有 fs 测试用 `tmp_path` fixture 或显式 `--config-path`

## Acceptance Criteria

- [ ] `ssh_config_manager.py --help` 与各 subcommand `--help` 全部跑通、参数 1:1 对齐 docstring
- [ ] `ssh_config_manager.py list` 对真实 `~/.ssh/config` 返回当前所有 host alias（至少含 `pai`）
- [ ] `ssh_config_manager.py find pai` 返回 host/user/port/key 等字段，与 `ssh -G pai` 自检吻合
- [ ] `ssh_config_manager.py create` → `ssh_config_manager.py find` → `ssh_config_manager.py update` → `ssh_config_manager.py delete` 端到端流程（在 tmp config 上）成功
- [ ] 每次 write 后存在 `~/.ssh/config.bak.<unix-ts>` 备份且与 mutate 前内容 byte-equal
- [ ] 含 `IdentityFile` 的新建 Host 自动 emit `IdentitiesOnly yes`
- [ ] Unit test 覆盖：解析器 happy/edge case、render preserves comments+ordering、5 个 subcommand 各 happy+failure case，预计 ≥ 30 case
- [ ] Integration test（`-m live_ssh`）覆盖：对真实 `~/.ssh/config` read-only `list` 与 `find pai`，预计 2–3 case
- [ ] `pytest tests/` 默认套件全绿；`pytest -m live_ssh` 增量绿
- [ ] SKILL.md / references 中所有引用 `ssh_config_manager.py` 的命令行示例与实现签名一致
- [ ] git commit message 遵循已有 v1.0 ship 格式：`feat(ssh-core): ship ssh_config_manager v1.0 with AST parser + atomic write`

## Notes

- 估计 loc：350–500（参考 ssh_cluster 619 loc + 配套 lib helper）
- 估计 session 数：1（plan + implement + check + commit）
- 不引入新 dependency（仅使用 stdlib：argparse/pathlib/dataclasses/re）
- v1.1 待办（不在本任务）：Include directive 展开、Match block 解析、backup GC、interactive password prompts
