# ship iac-state drift_check v1.0

## Goal

把 `plugins/iac-state/skills/iac-state/scripts/drift_check.py`
（当前 v0.2 stub: 59 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/drift_check.py:26-55`）推到 v1.0：跑
`terraform plan -detailed-exitcode + show -json`，解析
`resource_changes[]`，把每条变更分类为 `no-op | create | update-in-place |
replace | destroy`，按 resource_type 评 severity，每条带 `hint` 指向
`references/drift_patterns.md` 的对应锚点。是 SKILL.md 决策树
（`SKILL.md:34-37`）「怀疑漂移」分支的唯一入口。

复用 `iac-state-state-inspect` ship 时落地的 `scripts/lib/terraform.py`。

## User value

- 「production 是不是漂移了 / 有人手改了控制台」→
  ```bash
  drift_check.py local ~/infra/prod --json
  ```
  → `{summary: {drift: warn}, findings: [{kind: drift, severity: warn,
  resource: "aws_security_group.web", action: "update-in-place",
  diff_summary: "ingress 0.0.0.0/0:443 missing in state",
  hint: "references/drift_patterns.md#unmanaged-rule-changes"}]}`
- 「单个资源漂没漂」→ `--target aws_instance.web` 把 plan 范围缩到一个
  资源，省 plan 时间（大 project 一次全量 plan 可能 5-10min）
- 上游路由：Claude 看到 `findings` 全是 `update-in-place` 且都是 tag/
  description 字段 → 提示用户「无害漂移，加 `ignore_changes`」；看到
  `destroy` 出现 → 立刻警告「state 里有 unmanaged 资源，apply 会删」
- 排除「plan 输出长得吓人」的误解：把 100 行 plan 总结成 5 条 finding
  按 severity 排序，避免人眼扫描 plan diff 漏关键变更

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/drift_check.py:4-7` docstring + argparse
   `:29-37`）：
   ```
   drift_check.py <host> <project-dir> [--target ADDR]
                                        [--tool auto|tofu|terraform] [--json]
   ```
   v0.2 stub 已经接 5 个 flag，本任务直接沿用。

2. **Workflow 6 步已锁定**（`scripts/drift_check.py:8-17` docstring）：
   ```
   1. terraform init -input=false -lock=false -upgrade=false
   2. terraform plan -detailed-exitcode -out=/tmp/plan.bin [-target=<addr>]
   3. terraform show -json /tmp/plan.bin
   4. Parse resource_changes[]; classify each action
   5. Assign severity from priority_table(resource_type) + hint into refs
   6. Summary: count by action + crit/warn/info histogram
   ```
   本任务**不重新设计** workflow，直接实现这 6 步。

3. **`-detailed-exitcode` 三态语义**（terraform CLI 文档）：
   - exit 0 → no changes
   - exit 1 → error
   - exit 2 → diff exists（非错误）
   本脚本：
   - terraform exit 0 → `summary.drift=ok`，本脚本 exit 0
   - terraform exit 1 → 解析 stderr 归类 failure_class，本脚本 exit 130-139
   - terraform exit 2 → 解析 plan json，severity 决定本脚本 exit code
     （crit findings 才升 exit；warn/info 仍 exit 0）

4. **5 个 action 已锁定**（docstring 第 13 行）：`no-op | create |
   update-in-place | replace | destroy`。来源是
   `terraform show -json` 的 `resource_changes[].change.actions` 字段
   （terraform JSON schema 文档已稳定）。

5. **Severity priority table**（PRD 仅给方向，最终 design.md D-X 锁定）：
   - `destroy` → `crit`（无论 resource_type）
   - `replace` → `crit`（数据丢失风险）
   - `create` → `warn`（未管理资源被引入）
   - `update-in-place` → 看 resource_type：
     - `aws_security_group_rule` / `aws_iam_*` / `*_acl` → `warn`（安全相关）
     - `*_tag` / `*_description` / metadata → `info`
     - 其他 → `warn`
   - `no-op` → 不入 findings

6. **`references/drift_patterns.md` 已覆盖主要漂移模式**（基于 SKILL.md `:100`
   引用 + 文件存在）。本任务**不重写**该文件，只在实现发现新 pattern
   时增量补节（ship commit 同步）。

7. **`-out=/tmp/plan.bin` 安全性**：v1.0 强制 tmp 路径，避免 plan.bin
   被外部脚本误读为最新计划。`-out` 是 terraform 推荐写法（plan + show
   分离），不强制 -out 时 plan json 不可读（`terraform show` 拒绝读
   "in-flight" plan）。

8. **Host model + Output contract** 同 state_inspect。`data.raw.plan_exit_code`
   + `data.raw.plan_json`（截 10MB，超出 marker）让 LLM 自由解析；
   `data.findings` 是结构化信号。

9. **Failure_class 命名空间（本 child 独占 130-139，最终在 design.md D-X 锁定）**：
   - `130 init_failed` —— `terraform init` 失败（最常见：provider 拉不到、
     backend 配置错）
   - `131 plan_failed` —— `terraform plan` 自身失败（terraform exit 1）
   - `132 show_parse_failed` —— `terraform show -json` 输出不是合法 JSON
     或 schema 不匹配（防 terraform 未来版本 schema 变化）
   - `133 target_not_found` —— `--target` 给的资源在 state 不存在
   - `134 lock_held` —— init 或 plan 触发 lock 等待超时（建议用户先跑
     state_inspect 看 lock 详情）
   - `135 provider_auth_failed` —— provider credential 缺失或过期（warning
     hint 提示用户检查 AWS_*  / GOOGLE_* env）
   - `136 plan_too_large` —— plan json > 10 MB 截断（warning，仍能 parse
     部分 resource_changes）
   - `137-139` 预留扩展
   - 120-127（lib/terraform 透传，特别是 121 tool_missing / 122
     project_dir_invalid / 123 not_initialized）共存

10. **测试基线**：`tests/test_drift_check_unit.py`（≥35 unit，覆盖 5 action
    × 3 severity + 7 failure_class + plan json schema fixture）+
    `tests/test_drift_check_integration.py -m live_terraform`（≥3 PASS：
    no-drift fixture + update-in-place fixture + destroy fixture，全部
    用 `null_resource` 制造）。

## Out of scope

- **Apply / destroy 写操作** —— 归 `ssh-guarded`
- **跨 project 批量 drift 扫** —— v1.1（独立 child，扫所有 project 给
  执行摘要）
- **Drift 自动修复**（`terraform refresh` / 自动 import） —— 写操作，
  永远不在本 plugin
- **Plan 性能优化**（`-parallelism=N` / `-refresh=false` trade-off）—— 用户
  自己加 flag 透传，v1.0 不内置策略
- **State diff（非 plan-based）** —— `terraform state list` 对比 cloud
  resource 列表是另一种 drift 检测，v1.1 考虑（更轻量但少很多信息）

## Constraints

- **`-detailed-exitcode` 必须用**：否则 terraform exit 0 既可能是「无变化」
  也可能是「有变化」（不带 -detailed-exitcode），无法区分
- **`init -lock=false`**：init 不需要 lock，并发场景常见（多 CI runner
  同时初始化）；plan 阶段才需要 lock
- **`-out=/tmp/plan.bin` 不复用**：每次跑生成唯一文件名（如 `tempfile.mktemp(suffix=".plan")`），
  避免并发冲突；用完 unlink
- **plan json 解析必须健壮**：terraform JSON schema 在 0.13 / 1.0 /
  1.5 都有过 minor 变化；design.md 要测过的最低版本注明（建议 ≥ 1.5 +
  OpenTofu ≥ 1.6）
- **不写 state**：所有 plan 都用 `-lock=false` 或读后立即 unlock；fixture
  测试要断言 .terraform/ 与 *.tfstate hash 不变
- **失败 class 130-139 独占**；与 lib/terraform 120-127 共存

## Acceptance Criteria

- [ ] 6 步 workflow 全部实现，每步可独立单测
- [ ] 5 action 全部覆盖；priority_table 在 design.md D-X 明文锁定
- [ ] `tests/test_drift_check_unit.py` ≥ 35 PASS
- [ ] `tests/test_drift_check_integration.py -m live_terraform` ≥ 3 PASS
- [ ] `--target` 路径下 plan 范围真的缩小（unit test 断言 `-target=` 出现）
- [ ] terraform exit 2 → 本脚本根据 severity 决定 exit code（crit 升 exit，
      warn/info 仍 exit 0）
- [ ] tempfile 路径每次唯一，运行结束 unlink（unit test 断言）
- [ ] `.trellis/spec/backend/error-handling.md` 追加 130-139 段
- [ ] SKILL.md decision tree / quick reference 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_terraform` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`
- 同批 sibling：`iac-state-state-inspect`（foundation）、`iac-state-module-validate`
- 推荐 ship 顺序：state_inspect → drift_check → module_validate（lib
  依赖方向 + 复杂度递增）

## Notes

- 2026-05-26 PRD 初稿。priority_table 中 `update-in-place` 的细化是关键
  设计点 —— 全部归 warn 会让 LLM 无法区分「无害 tag 改」与「危险 sg
  规则改」；按 resource_type 细分是必须，但完整列表留 design.md。
- terraform JSON schema 跨版本兼容是已知坑，类似 helm_status 的 issue。
  design 阶段要明确 v1.0 最低支持版本，schema 不兼容时 132 报错 + hint
  提示用户升级，而不是默默错位解析。
- 2026-06-02 **drift_check v1.0 shipped** — commit `0534087`. Script + plugin-local cleanup
  helper + 36 unit tests + 3 skip-safe live_terraform integration tests.
  `uv run pytest tests/test_drift_check_unit.py -q` PASS; integration tests
  skip in this environment because neither `tofu` nor `terraform` is installed.
  `uv run ruff check plugins/iac-state/ tests/test_drift_check_unit.py
  tests/test_drift_check_integration.py` PASS; mypy on drift_check +
  lib/terraform PASS; `uv run pytest tests/ -q` PASS with environment skips
  (Docker daemon unavailable, pai unreachable, CICD env missing, Terraform
  missing). Key decisions: plan uses `-lock-timeout=1s` to surface `lock_held`
  instead of bypassing locks; crit destroy/replace drift exits 1 with
  `failure_class=null`; retained raw plan JSON is capped at 10 MB while parsing
  uses the full stdout.
