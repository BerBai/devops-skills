# Design — ssh-guarded redact_check v1.0

> 配套 PRD: `prd.md`。
> 实施清单: `implement.md` (to be written by main-session before `task.py start`).
> Cross-cutting 契约: `.trellis/tasks/05-25-ssh-guarded-request-pipeline/_cross_cutting.md` §C5 / §C7 / §C8 / §C11。
> **依赖**: `lib/redact.py` 由 Child A (request-pipeline) 交付，本 child 仅消费。

## 1. Architecture overview

```
redact_check.py  (CLI, ~120 loc)
└── lib/redact.py                       # Child A 交付，本 task 不写
     ├── class Redactor                 # 构造 / apply / load_catalog / default
     ├── Pattern (dataclass: kind, regex, replace_with)
     ├── Hit (dataclass: kind, line, match_len)
     └── _DEFAULT_PATTERNS (list[Pattern])
```

数据流：

```
stdin → for line in sys.stdin:
         Redactor.apply(line) → (redacted_line, [Hit, ...])
        accumulate hits, write redacted_line to stdout
         → stderr summary OR json envelope
```

## 2. CLI entry point (`scripts/redact_check.py`)

```python
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # 1) Load catalog with fallback (D2/D3)
    try:
        if args.catalog and args.catalog != _DEFAULT_USER_CATALOG_PATH:
            # explicit --catalog
            if not Path(args.catalog).expanduser().exists():
                return _emit_error(70, "catalog_load_failed",
                                   f"catalog file not found: {args.catalog}",
                                   args.json)
            redactor = Redactor.load_catalog(args.catalog)
        elif Path(_DEFAULT_USER_CATALOG_PATH).expanduser().exists():
            redactor = Redactor.load_catalog(_DEFAULT_USER_CATALOG_PATH)
        else:
            redactor = Redactor.default()
    except SchemaError as e:
        return _emit_error(71, "catalog_schema_invalid", str(e), args.json)
    except RegexCompileError as e:
        return _emit_error(72, "regex_compile_failed", str(e), args.json,
                           extra={"offending_kind": e.kind})

    # 2) Stream stdin, apply per-line (D4/D6)
    hits: list[Hit] = []
    redacted_chunks: list[str] = []
    try:
        for lineno, line in enumerate(sys.stdin, start=1):
            new_line, line_hits = redactor.apply(line, line_number=lineno)
            hits.extend(line_hits)
            redacted_chunks.append(new_line)
    except UnicodeDecodeError as e:
        return _emit_error(1, None,
                           f"stdin decode failed (binary input?): {e}", args.json)

    redacted_text = "".join(redacted_chunks)

    # 3) Emit (D5)
    if args.json:
        data = {
            "hits": [{"kind": h.kind, "line": h.line,
                      "match_len": h.match_len} for h in hits],
            "redacted_text_len": len(redacted_text),
            "total_hits": len(hits),
        }
        if args.emit_redacted:
            data["redacted_text"] = redacted_text
        json.dump({"success": True, "data": data, "error": None},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        if not args.report_only:
            sys.stdout.write(redacted_text)
        sys.stderr.write(_summary_line(hits))
    return 0
```

`_emit_error(exit_code, failure_class, message, as_json, extra=None)` returns the
exit_code after writing the envelope. The shape matches §C7:

```python
{"success": False, "data": None,
 "error": {"message": message, "failure_class": failure_class,
           "exit_code": exit_code, **(extra or {})}}
```

## 3. Built-in default catalog (`_DEFAULT_PATTERNS`)

定义在 `lib/redact.py` 内（Child A 拥有，但本 child 必须在 design.md 显式记录所依赖的 6 条；若 Child A 漏掉任何一条，本 task 阻塞）：

| kind | regex | replace_with |
|---|---|---|
| `aws_access_key` | `AKIA[0-9A-Z]{16}` | `[REDACTED-AWS-ACCESS-KEY]` |
| `aws_secret_key` | `(?i)aws.{0,20}secret.{0,20}[=:\s]+([A-Za-z0-9/+]{40})` | 保留前缀，replace 仅第 1 个 group → `\g<0_prefix>[REDACTED-AWS-SECRET]` |
| `github_pat` | `gh[pousr]_[A-Za-z0-9]{36,}` | `[REDACTED-GITHUB-PAT]` |
| `jwt` | `eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+` | `[REDACTED-JWT]` |
| `ssh_private_key` | `-----BEGIN (RSA \|OPENSSH \|EC \|DSA )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----` | `[REDACTED-PRIVATE-KEY-BLOCK]` |
| `generic_password_assignment` | `(?i)(password\|passwd\|pwd)[\s]*[=:][\s]*['"]?([^\s'"]{6,})` | `\1=[REDACTED]` (保留 label) |

Group-aware replacement（`aws_secret_key` 和 `generic_password_assignment`）由 `Redactor` 用 `re.sub(pattern, lambda m: ..., text)` 实现 —— `replace_with` 字段支持 `\g<N>` 反向引用。`ssh_private_key` 用 `[\s\S]+?` 非贪婪跨多行；处理多行时 `Redactor.apply` 需要先把 stdin 合成块再切，或允许 `--multiline-block` flag —— **v1.0 选择**：`Redactor.apply` 按调用方传入的 text 应用所有 pattern，CLI 在调 apply 时**按行**调（D4），所以 `ssh_private_key` 的跨行 BEGIN/END 块在 v1.0 **不会**被一次性识别（只在单行 BEGIN-PRIVATE-KEY 行匹配 prefix）。这是已知限制，design.md §8 记入 risks，留 v1.1 改为 buffer-and-flush 模型。

## 4. Catalog loading (`Redactor.load_catalog(path)` — Child A 实现，本节描述契约)

```python
def load_catalog(path: str) -> Redactor:
    p = Path(path).expanduser()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogLoadError(f"catalog file not found: {p}")
    except json.JSONDecodeError as e:
        raise SchemaError(f"catalog JSON invalid: {e}")

    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise SchemaError("catalog missing 'version: 1'")
    patterns_raw = raw.get("patterns")
    if not isinstance(patterns_raw, list):
        raise SchemaError("catalog 'patterns' must be a list")

    patterns = []
    for entry in patterns_raw:
        if not all(k in entry for k in ("kind", "regex", "replace_with")):
            raise SchemaError(f"pattern missing required keys: {entry}")
        try:
            compiled = re.compile(entry["regex"])
        except re.error as e:
            raise RegexCompileError(kind=entry["kind"],
                                    message=str(e))
        patterns.append(Pattern(kind=entry["kind"], regex=compiled,
                                replace_with=entry["replace_with"]))
    return Redactor(patterns)
```

错误类型映射 (D3 / §C5)：
- `CatalogLoadError` → exit 70 `catalog_load_failed`
- `SchemaError` → exit 71 `catalog_schema_invalid`
- `RegexCompileError` → exit 72 `regex_compile_failed`（error.data.offending_kind）

## 5. Error mapping (本脚本完整表)

| 来源 | exit_code | failure_class | stderr / error.message 模板 |
|---|---|---|---|
| `--catalog` 文件缺失 | 70 | `catalog_load_failed` | `catalog file not found: <path>` |
| catalog JSON 解析失败 | 71 | `catalog_schema_invalid` | `catalog JSON invalid: <jsonerror>` |
| catalog schema 字段缺失 / 类型错 | 71 | `catalog_schema_invalid` | `<具体字段错>` |
| pattern 中 regex 编译失败 | 72 | `regex_compile_failed` | `regex compile failed for kind='<kind>': <msg>` + `data.offending_kind` |
| stdin 非 UTF-8 / 二进制 | 1 | `null` (generic) | `stdin decode failed (binary input?): <msg>` |
| 正常 | 0 | `null` | — |

无 124 timeout（本脚本不发起子进程，全本地）。

## 6. Module layout (file diff)

```
plugins/ssh-guarded/skills/ssh-guarded/scripts/
├── redact_check.py                    (rewrite: stub → ~120 loc)
└── lib/
    ├── __init__.py                    (touched by Child A, not here)
    └── redact.py                      (CHILD A 交付; 本 task 仅 import)

tests/
└── unit/
    └── test_redact_check.py           (NEW, ~30 case)

tests/integration/                     (本 task 不新增 integration test;
                                        无 SSH 依赖)
```

`scripts/lib/__init__.py` 可能未存在；Child A 在它的 design.md 内负责创建。如果本 child 在 Child A 之前实施（不允许，但作 fail-safe），main session 应阻塞 `task.py start`。

## 7. Test plan (`tests/unit/test_redact_check.py`, ~30 case)

按维度切：

| 类别 | 用例数 | 覆盖 |
|---|---|---|
| help / surface | 2 | `--help` 不 crash；4 个 flag 都在 help 文本 |
| 默认 6 pattern × 正负 | 12 | aws_access_key 正例命中 + 负例不命中；其余 5 个同样 |
| Catalog override (D2) | 4 | 给定 `--catalog tmp.json` → 替换 built-in；缺失文件 → exit 70；不给 `--catalog` 但 `~/.devops/redact.json` 不存在 → 自动用 built-in；存在且合法 → 用之 |
| Schema 验证 (D3) | 4 | version 缺 → 71；patterns 非 list → 71；entry 字段缺 → 71；regex 编译失败 → 72 + `offending_kind` |
| 输出模式 (D5) | 4 | 默认 stdout=redacted + stderr=summary；`--json` 不带 redacted_text；`--json --emit-redacted` 带 redacted_text；`--report-only` stdout 空 |
| 行号 / 多行 (D6) | 2 | 10 行 / 3 命中分布在 line 2, 5, 9 → hits[].line 一一对应；空 stdin → exit 0 + total_hits=0 |
| Binary input | 1 | 非 UTF-8 bytes via subprocess → exit 1 |
| Summary 格式 | 1 | stderr 一行 `redact_check: N hits (kind=X, ...)`，按 kind 字典序 |

Mock 策略：所有用例用 `pytest`'s `capsys` + `monkeypatch.setattr("sys.stdin", StringIO(...))` 或 `subprocess.run([sys.executable, "redact_check.py"], input=..., capture_output=True)`；catalog 文件用 `tmp_path` fixture 写临时 JSON。**零** ssh fixture，**零** network。

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| `ssh_private_key` 跨行 BEGIN/END 在 line-by-line 模型下漏匹配 | v1.0 已知限制，design.md §3 标注；v1.1 改 buffer-and-flush 或 mega-regex 模型；unit test 加 negative case 显式记录这一限制 |
| Child A 实现 `lib/redact.py` 时漏字段（如 `Hit.line` 没传） | 本 design.md §3 + prd.md D8 已经把 API 契约写成「Child A 必须满足」，Child A 的 design.md 反向引用本文档 |
| 用户写错 `~/.devops/redact.json` schema → 整个 ssh-guarded 流水线挂 | exit 71 + 明确 stderr 指引；`run_request` / `exec_detached` 调 `Redactor` 时同样会拿到 71，统一行为 |
| Catalog override "replace" 而非 "merge" 让普通用户漏内置 pattern | 文档化（prd.md Notes）；v1.1 出 `--emit-default-catalog` 给用户作起点 |
| Regex catastrophic backtracking（如恶意 stdin × 复杂模式） | v1.0 6 个默认 pattern 都已审计无明显 ReDoS；用户自定 catalog 自负责；v1.1 评估 `regex` 包带超时 |
| stdin 极大（GB 级日志） | line-by-line streaming 已是 O(1) memory；唯一例外是 `redacted_chunks` 累积 —— 当 `--report-only` 时跳过 `sys.stdout.write` 但仍累积 list，浪费内存。**优化**：`--report-only` 模式下不 append 到 `redacted_chunks`，只算长度。design.md §2 的代码骨架需在实施时按此分支 |

## 9. Compatibility notes

- 不影响 ssh-core 已 ship 9 个 v1.0 脚本。
- 不修改任何 ADR（本 task 复用 Child A 的 lib/redact.py，无新 ADR 需求）。
- 不修改 pyproject.toml（纯标准库 `re` / `json` / `argparse` / `pathlib`）。
- 不修改 SKILL.md（surface 与 docstring 一致；redaction policy 章节已在 SKILL.md:97–108 描述）。
