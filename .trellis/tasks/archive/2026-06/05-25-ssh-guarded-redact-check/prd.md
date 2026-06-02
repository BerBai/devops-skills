# ship ssh-guarded redact_check v1.0

## Goal

把 `plugins/ssh-guarded/skills/ssh-guarded/scripts/redact_check.py` 从 v0.2 stub (`unimplemented()`, exit 2) 推到 v1.0 完成态：标准输入读文本，按 ssh-guarded 共用的 secret-regex 目录扫描并 redact，标准输出落 redacted 文本、标准错误落命中摘要（或全量走 `--json`）。**零 SSH、零文件写入**（除可选目录加载）。本脚本是 `lib/redact.Redactor` 的薄壳 CLI；同样的 `lib/redact.py` 被 `run_request` / `exec_detached tail-log` 等 ssh-guarded 脚本复用。

## User value

- `cat suspect.log | redact_check.py` 让用户 / Claude Code 在粘贴日志前先看一遍敏感字段会不会漏出去。
- `--json` 让 Claude Code 解析命中清单（kind / 行号 / 长度），决定是否再粘贴。
- 复用同一份 catalog (`~/.devops/redact.json` 或 built-in) 保证「预览」与「实际 run」的 redact 行为一致。

## Confirmed facts (from repo inspection)

1. **CLI surface 已固定** (`redact_check.py:9–18` 现存 docstring)：
   ```
   redact_check.py [--catalog PATH] [--json] [--emit-redacted] [--report-only] < input
   ```
   无 positional 参数，stdin only。
2. **Catalog 契约** 由 `_cross_cutting.md §C8` 锁定：默认路径 `~/.devops/redact.json`、schema `{version: 1, patterns: [{kind, regex, replace_with}]}`、缺省 fallback 到内置 `_DEFAULT_PATTERNS`。
3. **失败分类 / exit 码区间** 由 `_cross_cutting.md §C5` 锁定本脚本占 **70–74**，三个 failure_class：`catalog_load_failed` / `catalog_schema_invalid` / `regex_compile_failed`。
4. **JSON envelope** 由 §C7 锁定为 `{success, data, error}`，与 ssh-core 完全一致。
5. **共享库 `lib/redact.py`** 由 **Child A (request-pipeline)** 交付（见 §C11）；本 child 只 import + 包 CLI 薄壳，不实现 Redactor 类本身。
6. **运行时无远端 IO**：本脚本完全本地，可以在无 ssh fixture 的环境跑测试。

## v1.0 surface

| CLI | 说明 |
|---|---|
| `redact_check.py < input.log` | 默认：redacted 文本到 stdout，`redact_check: N hits (kind=X, ...)` 到 stderr |
| `redact_check.py --catalog PATH < input` | 用 PATH 替换 built-in catalog（不 merge，见 D2） |
| `redact_check.py --json < input` | 全量 JSON 到 stdout（不含 redacted 文本，见 D5） |
| `redact_check.py --json --emit-redacted < input` | JSON 内带 `data.redacted_text` 字段 |
| `redact_check.py --report-only < input` | 跳过 stdout 文本，仅 stderr / JSON 摘要 |

## Decisions

| # | Decision |
|---|---|
| D1 | 内置默认目录 `_DEFAULT_PATTERNS` 固定 6 条：aws_access_key / aws_secret_key / github_pat / jwt / ssh_private_key / generic_password_assignment（regex + replace_with 详见 design.md §3）。 |
| D2 | `--catalog PATH` 给定且文件存在 → **替换** built-in（非 merge，保证行为可预期）；给定但文件缺失 → exit 70 `catalog_load_failed`；未给 `--catalog` 时若 `~/.devops/redact.json` 存在则用之，否则 built-in。 |
| D3 | Catalog schema 必须含 `version: 1` 与 `patterns: [{kind, regex, replace_with}]`，否则 exit 71 `catalog_schema_invalid`；任一 regex 编译失败 → exit 72 `regex_compile_failed`，error.data 带 `offending_kind`。 |
| D4 | Streaming model：按行 (`for line in sys.stdin`) 处理；每行串行 `pattern.sub` 应用 catalog 顺序（**不**走 mega-regex / 命名组合并）。Trade-off：sequential sub 在 6 模式 × 一般日志体量下足够快，可读性更好；mega-regex 留 v1.1 评估。 |
| D5 | 输出模式：默认 stdout=redacted 文本 + stderr=`redact_check: N hits (k1=X, k2=Y, ...)`；`--json` → stdout 一行 envelope `{success, data: {hits, redacted_text_len, total_hits}, error}`，**不**含 redacted 文本以免膨胀；`--json --emit-redacted` 时在 `data.redacted_text` 内带；`--report-only` 跳过 stdout 文本但 stderr / JSON 摘要不变。 |
| D6 | 行号 1-indexed；换行符原样穿透（不剥 trailing newline）；stdin 解码用 `utf-8`，binary 输入 / decode 失败 → exit 1 generic error；latin-1 fallback 留 v1.1。 |
| D7 | CLI 最终签名 `redact_check.py [--catalog PATH] [--json] [--emit-redacted] [--report-only]`；无 positional；stdin 强制。 |
| D8 | `lib/redact.Redactor` 契约（Child A 交付，本 child 消费）：`Redactor(catalog: list[Pattern])` 构造、`.apply(text) -> (redacted_text, list[Hit])`、`Hit = {kind: str, line: int, match_len: int}`、`Redactor.load_catalog(path) -> Redactor`、`Redactor.default() -> Redactor` 两个 classmethod。 |
| D9 | Test 策略：~30 个 unit（每个默认 pattern 正/负、catalog override、schema 拒绝、多行输入、JSON 输出 shape）；**零** `live_ssh` 测试。 |

## In-scope

1. CLI argparse（4 flag、无 positional）
2. Catalog 解析 / 加载 / fallback / schema 校验 / regex 编译错误捕获
3. 行级 `Redactor.apply` 调用、行号累积、命中聚合
4. JSON envelope 包装（成功 + 三类错误 70/71/72 + generic 1）
5. Unit test ~30 case

## Out-of-scope

- PII 检测（非 regex catalog 范畴，如人名 / 地址）
- ML-based 检测（向量 / 模型）
- 多文件 batch 模式（v1.0 仅 stdin 单流）
- 文件 in-place 编辑 / 写回（本脚本零落盘）
- Latin-1 / binary fallback decode（v1.1）
- Mega-regex 性能优化（v1.1）
- `lib/redact.py` 本体的实现（**Child A 负责**，本 child 仅 import）

## Acceptance Criteria

- [ ] `python redact_check.py --help` exit 0，列出 4 个 flag、无 positional。
- [ ] `pytest tests/unit/test_redact_check.py` 全绿（~30 case）。
- [ ] `echo "AKIAIOSFODNN7EXAMPLE" | python redact_check.py` → stdout `[REDACTED-AWS-ACCESS-KEY]`，stderr `redact_check: 1 hits (aws_access_key=1)`，exit 0。
- [ ] `... | python redact_check.py --json` → stdout 单行合法 JSON，`data.total_hits == 1`，不含 `data.redacted_text`。
- [ ] `... | python redact_check.py --json --emit-redacted` → 同上 + `data.redacted_text` 含 redacted 文本。
- [ ] `--catalog /nonexistent.json` → exit 70，stderr `failure_class=catalog_load_failed`。
- [ ] catalog schema 不合法 (`version` 缺失) → exit 71 `catalog_schema_invalid`。
- [ ] catalog 含坏 regex (`[unclosed`) → exit 72 `regex_compile_failed`，error.data 带 `offending_kind`。
- [ ] `--report-only` 时 stdout 为空字符串。
- [ ] 6 个默认 pattern 每个都有正负 unit case (正例命中、负例不命中)。
- [ ] 多行输入（10 行，3 行命中）→ JSON `data.hits` 长度 3，`line` 字段对应 1-indexed 真实行号。
- [ ] Zero live_ssh test（本脚本无 SSH 依赖）。

## Notes

- 依赖 **Child A (request-pipeline)** 先 ship `lib/redact.py`；如果 Child A 尚未 ship，本 child 可在 design.md §6 占位但不能 `task.py start`，否则 import 失败。
- Catalog override 选 "replace" 而非 "merge"（D2）是个慎重选择：merge 会让用户写小目录就漏掉内置（语义模糊），replace 强制用户写完整目录，行为可预测。代价是普通用户需 copy built-in 作起点；提供 `--emit-default-catalog` 子命令留 v1.1。
- 与 ssh-core ship 节奏对齐（参考 parent prd `Notes` 节的 5 次 ship 经验），commit 仅含 `scripts/redact_check.py` + `tests/unit/test_redact_check.py`；不动 SKILL.md（surface 已与 docstring 一致）。
