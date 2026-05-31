# ship iac-state module_validate v1.0

## Goal

把 `plugins/iac-state/skills/iac-state/scripts/module_validate.py`
（当前 v0.2 stub: 63 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/module_validate.py:30-59`）推到 v1.0：对一个 Terraform 模块目录跑
**静态审计**——`fmt` + `validate` + 7 类 audit check（structure / inputs /
outputs / naming / pinning / antipatterns / security_hints，stub docstring
已锁定）——以及可选 **`--target-version` upgrade_diff**（对比 registry 上指定
版本的 variable 变化）。是 SKILL.md 决策树（`SKILL.md:39-43`）「module 本身
的健康」分支的唯一入口。

复用 `iac-state-state-inspect` ship 时落地的 `scripts/lib/terraform.py`
（仅 tool autodetect + local/alias dispatch 部分；本脚本**不跑 plan**，
所以不用 run_tf 的长 timeout 路径）。

## User value

- 「我们的 eks-cluster module 升级到 5.0 安全吗」→
  ```bash
  module_validate.py local ~/modules/eks-cluster --target-version 5.0.0 --json
  ```
  → `{summary: {modules: warn}, findings: [{kind: upgrade_diff,
  severity: crit, removed_var: "cluster_iam_role_arn"}, {kind: pinning,
  severity: warn, hint: "..."}]}`
- 「这个第三方 module 能用吗」→ 一次跑 7 类 check，把「这 module 看起来
  写得 sloppy」从主观判断变成结构化 findings
- 「Security 上为啥总有 finding」→ `security_hints` 类自动标 `Resource="*"`
  / `0.0.0.0/0` / `publicly_accessible` 等典型 IAM/网络反模式（参
  `module_validate.py:18`）
- 开发期 lint：CI 前跑一遍，提前发现 fmt / validate / antipattern 问题，
  避免 PR review 反复

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/module_validate.py:4-8` docstring +
   argparse `:33-40`）：
   ```
   module_validate.py <host> <module-path>
       [--target-version X.Y.Z]
       [--tool auto|tofu|terraform]
       [--json]
   ```
   5 个 flag。本任务直接沿用。

2. **7 类 check 已锁定**（`scripts/module_validate.py:10-18` docstring）：
   - `structure` —— main.tf / variables.tf / outputs.tf / README / versions.tf
   - `inputs` —— type/description/default/required, validation reasonable
   - `outputs` —— description present, sensitive flagged
   - `naming` —— name_prefix / tags consistent, no hardcoded names
   - `pinning` —— required_version, provider source FQN, versions bounded
   - `antipatterns` —— provider block in module, backend in module,
     count=0 disable, local-exec inside, depends_on misuse
   - `security_hints` —— `Resource="*"` / `Action="*"` / `0.0.0.0/0` /
     `public_acl` / `publicly_accessible`
   本任务**不新增** check 类，每类的具体规则在 design.md D-X 明文锁定。

3. **`--target-version` upgrade_diff 已锁定**（docstring 第 20-22 行）：
   - 仅在 `--target-version` 给出时触发
   - 输出 `removed variables` + `new required variables`
   - 数据源：Terraform Registry API `/v1/modules/<ns>/<name>/<provider>/<version>`
     —— 但本任务**仅支持 registry source**（即 module source 形如
     `terraform-aws-modules/eks/aws`），其他 source（git / 本地路径）
     `--target-version` 报 152

4. **HCL parser 决策**：v1.0 用 `python-hcl2`（PyPI 包，已被业界 Terraform
   tooling 广泛使用，如 driftctl / checkov）做静态解析。**不**自己写
   tokenizer。新增 dep 落 pyproject.toml（与 ssh-core ADR-003 引入
   paramiko 同等节奏）。

5. **`fmt` + `validate` 走 terraform CLI**：
   - `terraform fmt -check -recursive` → exit 0 ok / exit 3 needs format
   - `terraform validate -json` → exit 0 ok / exit 1 + JSON diagnostics
   - 都不需要 `init`（validate 在 0.12+ 不再强依赖 init，除非有 module
     source 拉取 —— 本脚本检测到 module 内嵌 module call 时跳过 validate
     + finding 注明）

6. **Host model + Output contract** 同其他 sibling。`data.raw.fmt_diff` +
   `data.raw.validate_json` 保留 terraform 原始输出。

7. **Failure_class 命名空间（本 child 独占 140-149，最终在 design.md D-X 锁定）**：
   - `140 module_path_invalid` —— 路径不存在 / 不是目录 / 无 *.tf 文件
   - `141 hcl_parse_failed` —— python-hcl2 parse 失败（warning，降级到
     只跑 fmt + validate，跳过 7 类 check）
   - `142 fmt_failed` —— fmt 报告需要 format（warning，exit 0 + finding）
   - `143 validate_failed` —— `terraform validate` 报错（terraform exit 1）
   - `144 registry_unreachable` —— `--target-version` 时 registry API
     超时 / 网络错（warning，跳过 upgrade_diff）
   - `145 registry_version_not_found` —— `--target-version` 在 registry
     不存在
   - `146 unsupported_source` —— `--target-version` 但 module source
     不是 registry 形式
   - `147-149` 预留扩展
   - 120-127（lib/terraform 透传）共存

8. **测试基线**：`tests/test_module_validate_unit.py`（≥40 unit，覆盖 7
   check 类各自的 ok/violation fixture + 8 failure_class）+
   `tests/test_module_validate_integration.py -m live_terraform`（≥2 PASS：
   well-formed fixture module + intentionally-broken fixture module）。
   `--target-version` 走 registry 的 case 用 `responses` / `httpretty`
   mock，**不**在 integration 真打 registry（避免外网依赖）。

## Out of scope

- **跨 module 关联**（A module 调用 B module 的 wiring 检查）—— v1.1
- **Plan / apply** —— 本脚本只做静态，不跑 plan
- **State 检查** —— 归 state_inspect
- **OPA / Sentinel / Checkov 策略合规** —— `SKILL.md:107` 明示「不做策略
  合规扫描」，留给专用工具
- **自动 fmt fix** —— 本脚本只 `fmt -check`，不写文件
- **Module 升级**（实际改 source / version）—— 写操作

## Constraints

- **新增 dep `python-hcl2`** 落 pyproject.toml；ADR follow-up TBD（参
  ADR-003 paramiko 引入节奏）
- **HCL parse 失败软降级**：parse 失败不影响 fmt + validate（这两个走
  terraform CLI，不依赖 python parser）；仅跳过 7 类静态 check
- **registry 调用必须有超时 + 网络错处理**：默认 10s 超时，失败归 144
  warning，不阻塞整体 audit
- **`fmt -check -recursive`**：扫整个 module dir（包含 submodule），符合
  Terraform 社区惯例
- **失败 class 140-149 独占**

## Acceptance Criteria

- [ ] 7 类 check 全部实现，每类规则在 design.md D-X 明文锁定
- [ ] fmt + validate 通过 terraform CLI 调用，不重新实现
- [ ] `--target-version` 走 registry，超时 / 404 / 非 registry source 三种
      失败路径分别 144 / 145 / 146
- [ ] `tests/test_module_validate_unit.py` ≥ 40 PASS
- [ ] `tests/test_module_validate_integration.py -m live_terraform` ≥ 2 PASS
- [ ] registry mock 用 `responses`，不真打外网
- [ ] python-hcl2 入 pyproject.toml，uv.lock 同步
- [ ] HCL parse 失败 → 软降级（fmt + validate 仍跑）
- [ ] `.trellis/spec/backend/error-handling.md` 追加 140-149 段
- [ ] SKILL.md decision tree / quick reference 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_terraform` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`
- 同批 sibling：`iac-state-state-inspect`（foundation）、`iac-state-drift-check`
- 推荐 ship 顺序：state_inspect → drift_check → **module_validate**
  （引入新 dep python-hcl2，留最后让 lib 与 fixture 都稳定）

## Notes

- 2026-05-26 PRD 初稿。`python-hcl2` 是 v1.0 关键新增依赖；选择它而非自己
  写 parser 的理由：terraform HCL2 语法有 lambda / dynamic block / for
  expression 等复杂结构，重写 tokenizer 是大坑；社区已有成熟实现（driftctl
  / checkov 都用），引入风险可控。ADR follow-up 写到 design.md。
- 7 类 check 每类的具体规则在 design.md D-X 锁定 —— PRD 阶段列方向避免
  双写漂移；规则一旦写到 PRD 后续微调还要同步两处。
- `--target-version` 仅支持 registry source 是有意限制 —— git source 拿
  历史版本需要 clone + checkout，复杂度跳跃，留 v1.1。
