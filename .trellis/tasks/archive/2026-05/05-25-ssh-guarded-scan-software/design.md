# Design — ssh-guarded scan_software v1.0

> 配套 PRD: `prd.md`。
> 实施清单: `implement.md`（本任务尚未写，等 plan 接受后再展开）。
> 跨子任务约定: `.trellis/tasks/05-25-ssh-guarded-request-pipeline/_cross_cutting.md` §C5（failure_class 区间） + §C7（JSON envelope） + §C10（test markers）。本设计**不**触达 §C1–C4 / §C8–C9 / §C11（独立于 request/execute pipeline）。

## 1. Architecture overview

```
scan_software.py  (CLI + planner, ~280 loc)
├── _DEFAULT_CATALOG          (静态字面量, 33 条 entry)
├── _load_catalog(path)       → 合并 default + user override
├── _build_remote_script(catalog) → 生成 heredoc shell payload
├── _run_remote_scan(alias, script, timeout)
│      └── subprocess → plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
│            └── ssh -o BatchMode=yes <alias> bash -s  (NDJSON 出 stdout)
├── _parse_ndjson + _apply_version_regex
├── _cache_read(alias) / _cache_write(alias, payload)
└── main()  dispatch:  full-scan | --name | --has
```

数据流（一次 full scan）：

```
argv → argparse → load catalog → build remote shell → ssh_execute.py
                                                    → NDJSON stdout
                                                    → parse + regex extract
                                                    → cache_write (atomic)
                                                    → emit envelope (--json | summary)
```

cache-hit 路径（`--name kubectl` 或 TTL 内 full scan）：

```
argv → argparse → cache_read(alias) → return slice → emit
```

## 2. CLI entry point (`scan_software.py`)

沿用 stub 的 argparse 骨架 (`scan_software.py:39-46`)，追加 2 flag：

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scan_software.py")
    p.add_argument("alias")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--name", help="Report a single tool (read cache, scan-on-miss)")
    g.add_argument("--has",  help="Exit 0 if present, 1 otherwise")
    p.add_argument("--refresh", action="store_true", help="Ignore cache, force rescan")
    p.add_argument("--no-refresh-stale", action="store_true",
                   help="Do not auto-rescan when cache TTL has expired")
    p.add_argument("--catalog", default=None,
                   help="Path to user catalog JSON (default: ~/.devops/software_catalog.json)")
    p.add_argument("--json", action="store_true")
    return p
```

`main()` 的 dispatch 形态（伪码，保留 ssh-core 已确立的 envelope+emit 形态，见 `ssh_execute.py:240-253`）：

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = _load_catalog(args.catalog)        # may raise → catalog_load_failed
    except CatalogLoadError as e:
        return _emit_failure(args, "catalog_load_failed", 75, e)

    if args.has:
        return _dispatch_has(args, catalog)          # exit 0/1
    if args.name:
        return _dispatch_name(args, catalog)         # JSON for 1 tool
    return _dispatch_full(args, catalog)             # full scan
```

## 3. D1. Default catalog (`_DEFAULT_CATALOG`)

33 条 entry，分 7 category；按 `{name, category, probe_cmd, version_cmd, version_regex}` 同构（Xilinx 走 D4 特例）。完整列表（紧凑形式，real code 一行一 dict 字面量）：

| category | n | tools (name → version source) |
|---|---|---|
| `lang` | 11 | `python` (`python --version`), `python3` (`python3 --version`), `pip` (`pip --version`), `conda` (`conda --version`), `nodejs` (`node --version`), `npm` / `yarn` / `pnpm` (`X --version`), `java` (`java -version 2>&1`), `mvn` (`mvn --version`), `gradle` (`gradle --version`) |
| `build` | 5 | `gcc`, `g++`, `clang`, `cmake`, `make` — 全部 `X --version` |
| `gpu` | 2 | `nvidia_driver` probe=`command -v nvidia-smi`, version=`nvidia-smi --query-gpu=driver_version --format=csv,noheader`; `cuda` probe=`command -v nvcc`, version=`nvcc --version` (regex `release\s+([0-9.]+)`) |
| `containers` | 5 | `docker`, `podman`, `kubectl` (version=`kubectl version --client --short 2>/dev/null \|\| kubectl version --client`), `helm`, `kustomize` |
| `iac` | 6 | `terraform`, `tofu`, `ansible`, `helmfile`, `argocd` (`argocd version --client`), `flux` |
| `shell` | 5 | `git`, `jq`, `yq`, `rsync`, `tmux` (`tmux -V`) |
| `xilinx` | 3 | `vivado`, `vitis`, `vivado_hls` — `scan_method="xilinx_dir"`, no probe/version_cmd; see D4 |

Total 11 + 5 + 2 + 5 + 6 + 5 + 3 = **33**, 1:1 与 SKILL.md `SKILL.md:150-162` + stub docstring `scan_software.py:10-19` 对齐。

`version_regex` 选择原则：抓 `([0-9.]+)` 第一个 group，前缀用关键字 anchor 避免误抓远端 PATH 数字（如 `git version 2.39.2` → `r"git version\s+([0-9.]+)"`）。implement 阶段 D10 的 parametrized regex 测试逐条 lock。

## 4. D2. Catalog entry schema & user override

每条 entry 共 5 字段（Xilinx 类用 `scan_method="xilinx_dir"` + `xilinx_subdir` 取代 probe/version_cmd）：

```json
{
  "name": "kubectl",
  "category": "containers",
  "probe_cmd": "command -v kubectl",
  "version_cmd": "kubectl version --client --short 2>/dev/null || kubectl version --client",
  "version_regex": "^Client Version:\\s*v?([0-9.]+)"
}
```

**User catalog 路径**：`~/.devops/software_catalog.json`（保持与已有 `~/.devops/cache/`、`~/.devops/redact.json` 同根）。`--catalog <path>` 显式覆盖。加载顺序：

1. `_DEFAULT_CATALOG` 字面量先入字典 `{name: entry}`。
2. 用户 catalog 若存在（默认路径或 `--catalog`）→ JSON load → 合并：同名 entry 整体替换默认（用户视图最大化 control，而不是字段级 merge，避免半改 regex 引发难调试）。
3. 用户 catalog 字段缺失 → CatalogLoadError → failure_class `catalog_load_failed`，exit 75。
4. 默认路径不存在 → **静默忽略**，只用 default（不算 failure）；显式 `--catalog /path` 不存在 → exit 75。

## 5. D3. Batch detection — single SSH + heredoc shell

一次 SSH 连接搞定全 33 条。**远端只跑一个 shell 脚本**（heredoc 字面量），emit NDJSON：

```bash
#!/bin/sh
# emitted to remote via `ssh <alias> bash -s` over stdin
emit_present()  { printf '{"name":"%s","present":true,"version_raw":"%s"}\n' "$1" "$2"; }
emit_absent()   { printf '{"name":"%s","present":false}\n' "$1"; }

# generated per catalog entry:
if command -v kubectl >/dev/null 2>&1; then
  v=$(kubectl version --client --short 2>/dev/null || kubectl version --client 2>&1 | head -c 4000)
  emit_present "kubectl" "$(printf '%s' "$v" | head -c 4000 | tr '\n' ' ' | sed 's/"/\\"/g')"
else
  emit_absent "kubectl"
fi
# ... 32 more blocks ...

# Xilinx dir scan (D4):
for d in /opt/Xilinx/Vivado/* /opt/Xilinx/Vitis/* /opt/Xilinx/Vivado_HLS/*; do ...
```

调用形态：

```python
proc = subprocess.run(
    ["python", str(SSH_EXECUTE), alias, "bash -s", "--timeout", "60", "--json"],
    input=remote_script, capture_output=True, text=True, timeout=70,
)
```

ssh_execute 已经支持 `<host> <command>` 但不支持 stdin pipe；本任务取 **不依赖 stdin** 的写法：把整个 shell 脚本 base64 编码后作为单个 argv 命令传入 ssh_execute：

```python
payload_b64 = base64.b64encode(remote_script.encode()).decode()
remote_cmd  = f'echo {payload_b64} | base64 -d | bash'
subprocess.run([sys.executable, str(SSH_EXECUTE), alias, remote_cmd, "--json", "--timeout", "60"])
```

理由：保留 ssh_execute 现有 surface（避免 v1.1 才能稳定的 stdin 管道），同时 33 个 entry × ~200 字节 ≈ 7 KB shell，base64 后 ~10 KB，远低于 OpenSSH argv 上限（~128 KB）。

本地侧解析 NDJSON：

```python
for line in stdout.splitlines():
    rec = json.loads(line)
    if rec["present"] and "version_raw" in rec:
        m = re.search(catalog_by_name[rec["name"]]["version_regex"], rec["version_raw"])
        rec["version"] = m.group(1) if m else "unknown"
    rec["scan_method"] = "command_v"   # or "xilinx_dir"
    rec["category"]    = catalog_by_name[rec["name"]]["category"]
```

NDJSON 单行解析失败 → failure_class `tool_probe_corrupted` 上报（envelope 仍 success=false 但其他条目保留入 cache，因为本地解析失败不等价于远端扫描失败 —— 此点 D8 复述）。

## 6. D4. Xilinx special case

`/opt/Xilinx/` 下子目录命名约定：`/opt/Xilinx/<Tool>/<version>/`，例如 `/opt/Xilinx/Vivado/2023.2/`。Xilinx 工具几乎不在 PATH（要 `source settings64.sh` 才有）。

远端脚本（嵌在 D3 的 heredoc 末尾）：

```bash
scan_xilinx() {
  tool="$1"      # Vivado / Vitis / Vivado_HLS
  name="$2"      # vivado / vitis / vivado_hls
  if [ -d "/opt/Xilinx/$tool" ]; then
    ver=$(ls -1 "/opt/Xilinx/$tool" 2>/dev/null | sort -V | tail -n 1)
    if [ -n "$ver" ]; then
      emit_present "$name" "/opt/Xilinx/$tool/$ver"
    else
      emit_absent "$name"
    fi
  else
    emit_absent "$name"
  fi
}
scan_xilinx Vivado     vivado
scan_xilinx Vitis      vitis
scan_xilinx Vivado_HLS vivado_hls
```

本地解析时 `scan_method="xilinx_dir"`，`version` 直接从路径末段取（不跑 regex）：`/opt/Xilinx/Vivado/2023.2 → "2023.2"`。

## 7. D5. Cache file format

`~/.devops/cache/<alias>/software.json`（与 SKILL.md `SKILL.md:153-156` 一致）：

```json
{
  "version": 1,
  "scanned_at": "2026-05-25T07:42:00Z",
  "server": "pai",
  "tools": {
    "kubectl": {"present": true, "version": "1.28.4",
                "version_raw": "Client Version: v1.28.4",
                "scan_method": "command_v", "category": "containers"},
    "vivado":  {"present": true, "version": "2023.2",
                "version_raw": "/opt/Xilinx/Vivado/2023.2",
                "scan_method": "xilinx_dir", "category": "xilinx"},
    "totally_absent_tool": {"present": false, "category": "containers"}
  }
}
```

**原子写**：在同一目录写 `software.json.<pid>.tmp`，flush + fsync，最后 `os.replace(tmp, target)`。`os.replace` 是 POSIX 原子 rename（同 partition），避免半写状态。失败 → failure_class `cache_write_failed` + exit 76（不删旧 cache）。

## 8. D6. TTL & staleness fallback

- TTL = 7 天（86400 × 7 = 604800 秒），从 `data.scanned_at` 起算。
- `is_stale(cache) = (now - scanned_at) > 7d`。
- 决策矩阵：

| `--refresh` | `--no-refresh-stale` | cache state | 行为 |
|---|---|---|---|
| ✓ | * | * | rescan，写新 cache |
| ✗ | ✗ | fresh | 读 cache，0 SSH |
| ✗ | ✗ | stale | 自动 rescan，写新 cache |
| ✗ | ✗ | stale + rescan 失败 | fall back stale cache，stderr warn，`data.cache_stale=true`，**exit 0** |
| ✗ | ✓ | stale | 直接读 stale，stderr warn，`data.cache_stale=true`，exit 0 |
| ✗ | ✗ | missing | scan，写 cache |
| ✗ | ✓ | missing | scan，写 cache（`--no-refresh-stale` 不阻塞 cold-start） |

stale fallback 选择 exit 0 而非 1：用户本来就拿到了答案（虽然旧），shell-style 调用方按 `data.cache_stale` 决定要不要质疑数据。Exit 1 会让 `&&` 链路断掉，反而劝退用户。

## 9. D7. Three invocation modes

| 模式 | argv 形态 | stdout (non-json) | stdout (--json) | exit |
|---|---|---|---|---|
| **full** | `<alias>` | summary table（category 分组 + present/absent count）| `{success, data.tools{}, data.cache_stale, ...}` | 0 / 75-79 / 124 |
| **name** | `<alias> --name X` | `kubectl: 1.28.4 (containers)` 单行 | `{success, data.tools.X{...}}`（仅一条）| 0；X 不在 catalog → exit 1 + stderr `unknown tool '<X>'` |
| **has**  | `<alias> --has X` | （静默，除非 --json）| `{success, data.tools.X.present}` | 0 if present, **1 if absent**，2 if X 不在 catalog（区别于 absent）|

`--name` cache-miss 时**触发一次完整 scan**（不做 single-tool SSH，避免与 batch 路径分叉两套远端协议）。

`--has` 同上：cache-miss 触发 full scan，scan 后查 cache。这样 `--has docker` 第一次成本与 full scan 相同（一次 SSH），后续都是本地查询。

## 10. D8. Failure classes & exit code map

`_cross_cutting.md` §C5 第 72 行锁定 scan_software 的 namespace 是 4 个；本设计在 75–79 区间内追加第 5 个 `tool_probe_corrupted`（用 79）：

| failure_class | exit | 触发 |
|---|---|---|
| `catalog_load_failed` | 75 | 用户 catalog JSON 解析失败 / schema 缺字段 / `--catalog <path>` 不存在 |
| `cache_write_failed`  | 76 | `os.replace` / mkdir / fsync 抛 OSError |
| `cache_corrupt`       | 77 | cache 文件存在但 JSON 解析失败 / version 字段不为 1 / `tools` 不是 dict |
| `ssh_batch_failed`    | 78 | ssh_execute 返回非 0（auth / network / 远端 shell 语法错）|
| `tool_probe_corrupted`| 79 | NDJSON 单行解析失败（远端 emit 出错位字符 / 截断） |
| `None` (success)      | 0  | 一切顺利或 stale-fallback（cache_stale=true 仍算 success）|
| —                     | 1  | generic（`--name` 拿到不在 catalog 的工具名等用户输入错误）|
| —                     | 124 | ssh_execute 透传 timeout（D6 不 fallback，因为没有可用数据） |

`tool_probe_corrupted` 不阻塞其他条目入 cache：cache 写部分 tools（标记 `present=true/false` 已知者），envelope 同时给出 `success=false` + `failure_class=tool_probe_corrupted` + `data.corrupted_lines=[...]`。用户重跑 `--refresh` 即可重试。

## 11. D9. SSH transport — shell out to ssh_execute.py

遵循 ADR-001 D1（`.trellis/spec/backend/adr-001-ssh-execute.md:21-27`）"subprocess + native ssh 是 v1.0 唯一传输 primitive"。本脚本**不** import paramiko。

```python
SSH_EXECUTE = Path(__file__).resolve().parents[3] / "ssh-core/skills/ssh-core/scripts/ssh_execute.py"
# 或更稳：通过 plugin manifest / 环境变量解析；v1.0 走相对路径，依赖 monorepo 布局
```

调用：

```python
proc = subprocess.run(
    [sys.executable, str(SSH_EXECUTE), alias, remote_cmd, "--json", "--timeout", str(args.timeout or 60)],
    capture_output=True, text=True, timeout=(args.timeout or 60) + 10,
)
envelope = json.loads(proc.stdout)
if envelope["exit_code"] != 0:
    return _emit_failure("ssh_batch_failed", 78,
                         f"ssh_execute exited {envelope['exit_code']}: {envelope.get('stderr','')}")
ndjson_text = envelope["stdout"]
```

`+10` 余量给 ssh_execute 自己的 envelope 序列化时间；外层 `subprocess.run` 不该比内层早超时。

## 12. D10. Test strategy

**Unit (~25 tests, `tests/test_scan_software_unit.py`)** — 全程 mock `subprocess.run`：

- 默认 catalog 正确性（5）：help / 33-entry total / category counts / 默认无 user catalog / `version_regex` per-tool parametrized
- Catalog override (3): user override-by-name / 默认路径缺失静默 / 显式 `--catalog` 缺失 → 75 / 非法 JSON → 75
- Cache I/O (4): atomic tempfile+replace / 读 missing 返 None / corrupt → 77 后 rescan / write OSError → 76
- TTL (3): fresh skip SSH / stale auto-rescan / `--no-refresh-stale` 拿 stale + warn
- Refresh (1): `--refresh` 忽略 fresh
- `--has` (3): present exit 0 / absent exit 1 / unknown-tool exit 2
- `--name` (2): cache hit 返单条 / cache miss 触发 full scan
- 远端 (3): batch ssh 失败 → 78 / ssh timeout → 124 / NDJSON 单行损坏 → 79 + partial cache 保留
- Xilinx (1): `/opt/Xilinx/Vivado/2023.2` → version=`2023.2`
- 输出 (1): non-json summary table 按 category 分组

**Integration (~2 tests, `tests/test_scan_software_integration.py`, marker `live_ssh`)** — 真连 pai：

- `test_full_scan_on_pai`：`scan_software.py pai --json` → success=true，`data.tools` 33 个 key，`bash`/`git`（pai 必有）present=true 且 version 非空，写入 `~/.devops/cache/pai/software.json` 并能 reload。
- `test_has_subcommand_on_pai`：`--has bash` exit 0；`--has totally-fake-tool-${uuid}` 不在 catalog → exit 2；先注入一个用户 catalog 写一条 `{"name":"totally-fake-tool","probe_cmd":"command -v totally-fake-tool",...}` 再 `--has totally-fake-tool` → exit 1。

`tests/README.md` "What's covered today" 节追加 scan_software unit + integration（同 deploy_pubkey / ssh_server_transfer 的惯例）。

## 13. Module layout (file diff)

```
plugins/ssh-guarded/skills/ssh-guarded/scripts/
└── scan_software.py                  (rewrite: stub ~67 loc → ~280 loc)

tests/
├── test_scan_software_unit.py        (NEW)
└── test_scan_software_integration.py (NEW)

tests/README.md                       (append section)
plugins/ssh-guarded/skills/ssh-guarded/SKILL.md  (verify §Software scan still accurate; minor wording sync if needed)
```

不新增 lib/、不新增 ADR、不改 pyproject、不改 ssh_execute.py。

## 14. Compatibility & risks

| Risk | Mitigation |
|---|---|
| `ssh_execute.py` 相对路径在打包安装时找不到 | v1.0 走 monorepo 相对路径；future packaging 任务再抽 entry-point。`Path(__file__).resolve().parents[3]` 在源码布局下稳定。|
| 远端 shell 不是 bash（dash / busybox） | heredoc 用 POSIX sh 语法（`command -v`、`[ -d ]`、`printf`），避免 `[[ ]]` / arrays。Xilinx host 通常 bash 没问题。 |
| 33 条 entry × shell `if` block 拼接出来的脚本 ~10 KB | base64 + ssh argv 仍在 OpenSSH 上限内（< 128 KB），D3 已验。|
| `kubectl version --client --short` 在 1.28+ 已 deprecated | catalog entry 用 `... || kubectl version --client` 兜底；regex 同时匹配两种输出（已含 `v?`）。|
| 用户 catalog override 误伤默认 entry | D2 决策：同名整体替换 + 字段 schema 校验（缺字段抛 catalog_load_failed），用户能从 stderr 快速发现。|

## 15. Open questions (defer until implementation)

- 是否要在 envelope 里加 `data.catalog_version`（catalog hash 或显式 version 号）以便 cache 与 catalog 不匹配时主动 invalidate？v1.0 选择不加，理由：catalog 更新场景罕见，用户 `--refresh` 一次就解决。
- `_DEFAULT_CATALOG` 体量（33 × ~150 字符 ≈ 5 KB literal）是否值得移到独立 `_default_catalog.py` 模块？v1.0 留在脚本顶部，可读性优于跳文件；后续若超过 60 条再拆。
- Summary table 用 `rich` 还是纯 ASCII？沿用 ssh_server_transfer / deploy_pubkey 的风格 —— **纯 stdlib**，不引依赖。
