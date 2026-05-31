# Implementation — ssh_config_manager v1.0

## Phase 1 — Foundation (parser + data model)

- [ ] **1.1** 在 `plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py` 顶部替换 stub docstring 为 v1.0 docstring（参考 ssh_execute.py 风格）
- [ ] **1.2** 定义 `HostBlock` 和 `ConfigDocument` dataclass（design.md §data-model）
- [ ] **1.3** 实现 `parse_config(text: str, path: Path) -> ConfigDocument`：line-by-line state machine + 元数据 comment 识别
- [ ] **1.4** 实现 `ConfigDocument.find(alias) -> HostBlock | None`、`list_filtered(env, tags) -> list[HostBlock]`
- [ ] **1.5** 实现 `render(doc: ConfigDocument) -> str`：byte-stable + IdentitiesOnly auto-emit
- [ ] **1.6** 实现 `atomic_write_with_backup(path, content) -> Path`：copy backup + tmp + os.replace

**Validation 1**: `python -c "from ssh_config_manager import parse_config; print(parse_config(open(...).read(), Path('~/.ssh/config')))"` 在 fixture 文件上 round-trip byte-equal。

## Phase 2 — Read subcommands

- [ ] **2.1** `cmd_list(args, config_path) -> dict`：返回 `json_result(success=True, data={"hosts": [...]})`
- [ ] **2.2** `cmd_find(args, config_path) -> dict`：found → 完整字段；not_found → `exit_code=4, failure_class="not_found"`
- [ ] **2.3** human-mode 渲染：list 输出对齐表格；find 输出键值对 + raw_lines tail
- [ ] **2.4** 写 `tests/test_ssh_config_manager_unit.py` 骨架：fixture（tmp_path + sample config text）、helper `_write_config(tmp_path, text) -> Path`、import sys.path 注入

**Validation 2**:
```bash
# 真实 ~/.ssh/config 上跑 list / find pai，验证基础走通
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py list --json | head -c 500
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py find pai --json
```

## Phase 3 — Write subcommands

- [ ] **3.1** `cmd_create(args, config_path) -> dict`：alias 冲突 → `exit_code=5`；otherwise upsert + atomic_write_with_backup
- [ ] **3.2** `cmd_update(args, config_path) -> dict`：not_found → 4；otherwise diff & 应用，`changed_fields` 记录
- [ ] **3.3** `cmd_delete(args, config_path) -> dict`：not_found → 4；otherwise 删除 + atomic_write_with_backup
- [ ] **3.4** Failure class 全覆盖：config_missing(6) / parse_error(7) / permission(8) / internal(1) 各有捕获分支

**Validation 3**:
```bash
# 用 tmp config 端到端走一遍 (不动 ~/.ssh/config)
TMP=$(mktemp -d)/config
echo "Host existing\n  HostName 1.1.1.1\n  User u" > $TMP
python3 .../ssh_config_manager.py --config-path $TMP create --alias new --host 2.2.2.2 --user x --tags a,b
python3 .../ssh_config_manager.py --config-path $TMP list --json
python3 .../ssh_config_manager.py --config-path $TMP update --alias new --user y --json
python3 .../ssh_config_manager.py --config-path $TMP delete --alias new --json
ls $TMP.bak.* | wc -l  # 应 ≥ 3（每次 mutate 一个 backup）
```

## Phase 4 — Unit tests

- [ ] **4.1** Parser happy/edge case 12 个
- [ ] **4.2** Render preserves 4 个（含 comment 保留、metadata 固定顺序、IdentitiesOnly auto、byte-equal round-trip）
- [ ] **4.3** Subcommand happy/failure case：list(3) + find(3) + create(4) + update(3) + delete(2) = 15 个
- [ ] **4.4** atomic_write_with_backup 3 个（backup byte-equal、权限继承、tmp 中断兜底）
- [ ] **4.5** Failure class 4 个（config_missing/parse_error/permission/internal）

**Validation 4**: `pytest tests/test_ssh_config_manager_unit.py -v` 全绿，≥ 38 case PASS。

## Phase 5 — Integration tests

- [ ] **5.1** 创建 `tests/test_ssh_config_manager_integration.py`，遵循 `test_ssh_execute_integration.py` 风格
- [ ] **5.2** `@pytest.mark.live_ssh` 三个 case：
  - `test_list_includes_pai`：读真实 `~/.ssh/config`，断言 hosts 至少含 alias=pai
  - `test_find_pai_matches_ssh_dash_G`：`cmd_find('pai')` 结果与 `subprocess.run(['ssh', '-G', 'pai'])` 输出 user/host/port 字段吻合
  - `test_find_pai_json_structure`：`--json` 返回 `success=True, exit_code=0, data` 含全部约定字段
- [ ] **5.3** `pytest tests/test_ssh_config_manager_integration.py -m live_ssh -v` 全绿

**Validation 5**: 整套 `pytest -m live_ssh` 在 ssh-agent loaded 情况下从 40 case 增到 43 case，全绿。

## Phase 6 — Docs sync

- [ ] **6.1** 复核 `plugins/ssh-core/skills/ssh-core/SKILL.md` 行 77–78 的命令示例与最终实现签名一致（参数名、顺序）
- [ ] **6.2** 复核 `references/workflows.md` §5 描述与实现 invariant 吻合（4 步 write 流程、append 排序）
- [ ] **6.3** 检查 `references/troubleshooting.md` 行 9 / 84 引用是否仍正确
- [ ] **6.4** 在 `.trellis/spec/backend/` 写 `adr-002-ssh-config-manager.md`，参考 `adr-001-ssh-execute.md` 结构，把 D1–DN 决策落档（AST 解析、metadata 注释格式、atomic write、IdentitiesOnly 默认、失败分类）
- [ ] **6.5** （可选）`tests/README.md` 提一句新增 3 个 live_ssh case

**Validation 6**: `grep -rn ssh_config_manager plugins/ssh-core/skills/ssh-core/{SKILL.md,references/}` 所有命令示例都能在 shell 里跑通（at least `--help` 不报错）。

## Phase 7 — Commit

- [ ] **7.1** `git status` 看 changed files，确认无意外文件
- [ ] **7.2** `git add` 仅相关文件（脚本 + 测试 + adr + docs）
- [ ] **7.3** commit 消息格式：
  ```
  feat(ssh-core): ship ssh_config_manager v1.0 with AST parser + atomic write

  - Parse ~/.ssh/config into HostBlock AST preserving comments + ordering
  - Metadata via OpenSSH-compat comments (description/environment/tags/location)
  - Atomic write with .bak.<unix-ts> backup + mode preservation
  - IdentitiesOnly auto-set on new hosts with IdentityFile (per troubleshooting.md)
  - 5 subcommands: create/update/delete/list/find (per docstring contract)
  - Failure classes: not_found / already_exists / config_missing / parse_error / permission
  - 38+ unit + 3 live_ssh integration cases (read-only on real ~/.ssh/config)
  - Refs: ADR-002, references/workflows.md §5
  ```
- [ ] **7.4** Post-commit：`pytest tests/` + `pytest -m live_ssh` 都全绿后才算 done

**Validation 7**: `git log --oneline -1` 看到新 commit；CI / `pytest` baseline 不破。

## Phase 8 — Wrap-up

- [ ] **8.1** Update parent task `05-24-devops-skills-v1-rollout` 的 prd.md Notes 节，记录 "ssh_config_manager v1.0 shipped — commit <hash>"
- [ ] **8.2** `task.py finish` 切换 active task 离开本 child（不需要 archive，留给后续 batch 处理）
- [ ] **8.3** 在 session 末尾给 user 一个总结：done count、next stub 候选（deploy_pubkey）

**Validation 8**: `task.py current` 不再指向本 task；`task.py list --mine` 显示本 task 为 completed。

## Final validation commands

```bash
# 基线保护
pytest tests/                                   # 应全绿
pytest -m live_ssh -v                           # 43 case 全绿 (40 老 + 3 新)

# 脚本契约
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py --help
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py list --help
# ... 各 subcommand --help

# 真实使用（read-only）
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py list
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py find pai
```

## Review gates

| Gate | When | What |
|---|---|---|
| G1 | 进 Phase 2 前 | parser/render round-trip byte-equal 在 fixture 文件上跑通 |
| G2 | 进 Phase 3 前 | list / find 在真实 ~/.ssh/config 上跑通且不写文件 |
| G3 | 进 Phase 4 前 | create→update→delete 端到端在 tmp config 上跑通，backup 文件 byte-equal |
| G4 | 进 Phase 5 前 | unit test ≥ 38 case 全绿 |
| G5 | 进 Phase 6 前 | integration test 3 case 全绿 + `pytest tests/` 默认套件不破 |
| G6 | 进 Phase 7 前 | docs / ADR 落档 + user review SKILL.md diff |
| G7 | Done | commit 落地 + baseline pytest 不破 |

## Rollback points

- Phase 任一 fail：当前未 commit，`git restore` 单文件恢复
- Phase 7 commit 后发现问题：`git revert <hash>` 单 commit 撤销，不影响 parent task 状态
