# Design — ssh_config_manager v1.0

## Architecture overview

```
        ┌─────────────────────────────────────────┐
CLI     │ build_parser → main → dispatch_command  │
        └─────────────────┬───────────────────────┘
                          │
                  ┌───────┴──────────┐
                  │                  │
          read-only path        write path
        ┌─────┴────────┐    ┌──────┴───────────────┐
        │ list / find  │    │ create/update/delete │
        └─────┬────────┘    └──────┬───────────────┘
              │                    │
              ▼                    ▼
        ┌──────────────────────────────────────────┐
        │ ConfigDocument (in-memory AST)           │
        │   .parse(path) -> ConfigDocument         │
        │   .find(alias) -> HostBlock              │
        │   .upsert(HostBlock)                     │
        │   .delete(alias) -> bool                 │
        │   .render() -> str   # bytes-stable      │
        │   .list(env=None, tags=None)             │
        └──────────────────────────────────────────┘
              │
              ▼
        ┌──────────────────────────────────────────┐
        │ atomic_write_with_backup(path, content)  │
        │   1. cp path → path.bak.<unix-ts>        │
        │   2. write path.tmp                      │
        │   3. os.replace(path.tmp, path)          │
        └──────────────────────────────────────────┘
```

## Data model

```python
@dataclass
class HostBlock:
    alias: str
    # OpenSSH 字段（不区分大小写但 render 时统一首字母大写）
    host: str | None = None           # HostName
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None  # IdentityFile
    proxy_jump: str | None = None     # ProxyJump
    # ssh-core 私有元数据（紧邻上方的 comment 行解析得到）
    description: str | None = None
    environment: str | None = None    # "production" | "staging" | "dev" | ...
    tags: list[str] = field(default_factory=list)
    location: str | None = None
    # 不解析但保留的字段（防止破坏 user 已有配置）
    extra_directives: list[tuple[str, str]] = field(default_factory=list)
    # 上下文行（保留 user 自己写的非元数据 comment）
    preceding_comments: list[str] = field(default_factory=list)


@dataclass
class ConfigDocument:
    path: Path
    hosts: list[HostBlock]            # 按文件出现顺序
    prologue: list[str]               # 第一个 Host 之前的原始行
    epilogue: list[str]               # 最后一个 Host 之后的原始行（含 Match block 等）
```

## Parsing strategy

**Line-by-line state machine**：

```
state ∈ {PROLOGUE, COMMENT_BLOCK, HOST_BODY, AFTER_HOSTS}

for line in source:
    stripped = line.strip()
    if state == PROLOGUE:
        if startswith('#'): buffer to pending_comments
        elif "Host " starts a block: flush prologue, enter HOST_BODY
        else: keep in prologue
    if state == COMMENT_BLOCK:
        if startswith('#'): accumulate comments (parse metadata + non-meta separately)
        elif "Host ": create new HostBlock with parsed metadata; enter HOST_BODY
        elif blank: keep in comments
    if state == HOST_BODY:
        if "Host ": close current, start new
        elif "Match ": close current, switch to AFTER_HOSTS (Match 不解析)
        elif key=value or "Key Value": apply to current HostBlock
    if state == AFTER_HOSTS:
        accumulate epilogue verbatim
```

### 元数据 comment 识别规则

```
^#\s*(description|environment|tags|location)\s*:\s*(.+?)\s*$
```

匹配则归入 HostBlock 元数据；不匹配则归入 `preceding_comments`（保留原行）。`tags` 值按 `,` 切分并 strip。

## Rendering strategy

**目标：byte-stable 输出 + 注释保留 + ordering 保留**

```python
def render(doc: ConfigDocument) -> str:
    out = []
    out.extend(doc.prologue)            # 含开头空行 / 注释
    for h in doc.hosts:
        out.extend(h.preceding_comments)  # user 注释 + 重建后的元数据注释
        out.append(f"Host {h.alias}")
        if h.host:           out.append(f"    HostName {h.host}")
        if h.user:           out.append(f"    User {h.user}")
        if h.port:           out.append(f"    Port {h.port}")
        if h.identity_file:  out.append(f"    IdentityFile {h.identity_file}")
        if h.proxy_jump:     out.append(f"    ProxyJump {h.proxy_jump}")
        if h.identity_file:  out.append( "    IdentitiesOnly yes")
        for k, v in h.extra_directives: out.append(f"    {k} {v}")
        out.append("")                  # block 之间空行
    out.extend(doc.epilogue)
    return "\n".join(out) + "\n"
```

**关键设计决策**：

1. `preceding_comments` 包含**重建后**的元数据 comment（不直接 round-trip 原 line，因为字段可能被 update）
2. 元数据 emit 顺序固定：description → environment → tags → location（稳定顺序，便于 diff）
3. 缩进固定 4 空格（OpenSSH 容忍 0 或任意空格，固定能给 diff 友好结果）
4. `IdentitiesOnly yes` 在 `IdentityFile` 后立即 emit（不在 extra_directives 重复）
5. 末行 `\n`（POSIX 习惯）

## Atomic write with backup

```python
def atomic_write_with_backup(path: Path, content: str) -> Path:
    """Returns backup path."""
    ts = int(time.time())
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, backup)          # 保权限/时间
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(path.stat().st_mode)      # 继承原权限（通常 0600）
    os.replace(tmp, path)               # POSIX atomic
    return backup
```

**为什么 copy 而不是 hard-link**：

- hard-link 在跨 fs / NFS 上可能 silently 失败
- copy 保证 backup 独立可读，即使后续 path 被破坏也不影响 backup

**为什么 `os.replace` 而不是 `pathlib.rename`**：

- `os.replace` 在所有平台都 overwrite；`Path.rename` 在 Windows 上不会
- 单步 atomic（kernel-level）—— 中断不会留下半写文件

## Subcommand dispatch

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config_path or "~/.ssh/config").expanduser()

    handler = {
        "create": cmd_create,
        "update": cmd_update,
        "delete": cmd_delete,
        "list":   cmd_list,
        "find":   cmd_find,
    }[args.cmd]
    result = handler(args, config_path)
    emit(result, as_json=args.json)
    return result["exit_code"]
```

每个 `cmd_*` 返回 `json_result(...)` dict。

## list 过滤逻辑

```
candidates = doc.hosts
if --environment X: filter h.environment == X
if --tags a,b:      filter all(t in h.tags for t in given_tags)  # AND, not OR
```

human-mode 渲染：
```
alias              environment   tags          user           host
prod-web-01        production    web,nginx     deploy         10.0.1.11
backup-db          production    db            postgres       10.0.2.5
```

`--tags` AND 语义（而非 OR）跟用户 mental model 一致："find me hosts with both `web` and `prod`"。

## IdentitiesOnly auto-set 规则

**何时 emit**：

- 新建 Host 含 `IdentityFile` → 自动加 `IdentitiesOnly yes`
- update 时新增 `IdentityFile` → 同上
- update 时仅修改其他字段 → 不动 `IdentitiesOnly`
- user 已显式写了 `IdentitiesOnly no` → 不覆盖（保留在 `extra_directives`）

**为什么默认 yes**：避免 `Too many authentication failures` —— ssh client 在 `IdentitiesOnly no`（默认）情况下会 hand 给 agent 里所有 key 试一遍，多 key 用户必踩坑。troubleshooting.md:84 已记录此问题。

## Test strategy

### Unit (`tests/test_ssh_config_manager_unit.py`)

不动真实 `~/.ssh/config`，全部用 `tmp_path` fixture。覆盖范围：

| 类别 | 用例数 | 关键 case |
|---|---|---|
| Parser happy | 6 | minimal Host / 全字段 / 多 Host / 含元数据注释 / 含 user 自定义注释 / 含空行 |
| Parser edge | 6 | Match block 保留到 epilogue / 缩进多变 / 大小写混合 key / 重复 alias / 仅 prologue / 空文件 |
| Render preserves | 4 | round-trip byte-equal / comment 不丢 / metadata 顺序固定 / IdentitiesOnly 自动 |
| `cmd_list` | 3 | 无 filter / env filter / tag AND filter |
| `cmd_find` | 3 | hit / not_found / 含元数据 |
| `cmd_create` | 4 | happy / already_exists / 仅必填 / 含 IdentityFile auto IdentitiesOnly |
| `cmd_update` | 3 | hit / not_found / changed_fields 字段计数正确 |
| `cmd_delete` | 2 | hit / not_found |
| `atomic_write_with_backup` | 3 | backup byte-equal / 原文件 mode 保留 / tmp 中断不污染主文件 |
| Failure classes | 4 | config_missing / parse_error / permission / internal |

**目标 ≥ 38 case**（比 ssh_execute v1.0 的 30+ unit 略多，因为 subcommand 维度更宽）。

### Integration (`tests/test_ssh_config_manager_integration.py`, `-m live_ssh`)

仅 read-only 测试真实 `~/.ssh/config`，不动任何 user 数据：

| Case | 断言 |
|---|---|
| `list` returns pai alias | data.hosts 含 alias=pai 的 entry |
| `find pai` matches `ssh -G pai` | data.user / data.host / data.port 与 `ssh -G pai` 输出吻合 |
| `find pai --json` 结构完整 | exit_code=0, success=true, data 含全部约定字段 |

**3 case 估计；运行 < 1s**（无网络）。

### 不做的事

- 不在 integration test 里 mutate 真实 ~/.ssh/config（即使带 backup 也避免）
- 不模拟 NFS / 跨 fs（atomic_write 设计有覆盖，但 stress test 留给 v1.1）

## Risks & mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| Parser 漏识别某种 OpenSSH 语法 | render 后 `ssh -F` 报错 | epilogue 兜底（Match block 等不解析直接保留）；CI 在 ssh -G 上自检 |
| User 在 IdentityFile 上方手写 IdentitiesOnly no | render 后被自动追加 yes，user 配置被改 | extra_directives 检测已存在 `IdentitiesOnly` 时不再 emit |
| Atomic write 跨 fs 失败 | 中断后留下 .tmp | finally 块清理 tmp；测试 fs.copy 路径 |
| 元数据 comment 与 user 自己写的 description 注释冲突 | 解析模糊 | 正则严格匹配 `# key: value`；其他全归 user 注释 |
| Windows path 处理（CRLF/反斜杠） | render 输出错乱 | 文件读用 `read_text(encoding="utf-8")`；不自动转换 line ending |
| Race condition：另一进程同时写 ~/.ssh/config | atomic_write 覆盖对方修改 | v1.0 不加锁；文档警告"非并发安全"；future 用 fcntl.flock |

## Compatibility / Rollback

- **完全独立的脚本**，对其他 v1.0 脚本（ssh_execute / ssh_cluster）无影响
- **回滚方式**：删除新代码 + restore stub；user 已有 `~/.ssh/config` 不动（本工具默认不写）
- **向后兼容**：本脚本即 v1.0 首版，无破坏老契约风险
- **向前**：v1.1 加 Include directive 时，AST 数据模型已留 epilogue / extra_directives 扩展点
