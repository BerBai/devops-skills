# ship iac-state state_inspect v1.0

## Goal

把 `plugins/iac-state/skills/iac-state/scripts/state_inspect.py`
（当前 v0.2 stub: 53 LOC argparse skeleton + `unimplemented` + `exit 2`，见
`scripts/state_inspect.py:23-49`）推到 v1.0：对一个 Terraform/OpenTofu
project 拉一组**state 元数据快照**——backend / state_size_bytes /
resource_count / is_locked / lock_info / workspaces——综合评分
`ok | warn | crit` 给出 `findings`。是 SKILL.md 决策树（`SKILL.md:30-32`）
「state 本身的问题」分支的唯一入口。

本任务同时**首次**在 iac-state plugin 落 terraform/tofu 包装 lib
（`scripts/lib/terraform.py`），把
**tool autodetect**（`command -v tofu` → fallback `terraform`，可强制
`--tool` 覆盖）+ **local/alias 二分执行** + **lock 检测解析** 抽到一个
签名后，供 drift_check / module_validate 复用。先例对齐 ssh-core
`lib/sftp_transport.py` 与 k8s-debug `lib/kubectl.py`。

## User value

- 「这个 state 是不是 locked」/ 「为什么 plan 一直说 lock error」→
  ```bash
  state_inspect.py local ~/infra/prod --json
  ```
  → `{summary: {state: warn}, findings: [{kind: lock, severity: crit,
  who: "alice@runner-7", since: "2026-05-26T08:15Z", operation: "OperationTypePlan",
  hint: "see references/state_health.md#stale-lock"}]}`
- 「这个 project 有多少资源 / state 多大」→ 不用人去翻 `.terraform/`
  目录或问 backend
- 「哪些 workspace 在用」→ 多环境管理常用入口
- 远端 runner 场景：CI runner 上跑了 plan 卡住，本地无 state →
  `state_inspect.py runner-7 /srv/infra/prod` 经 ssh-core 直接看

## Confirmed facts (from repo inspection)

1. **CLI surface 已锁定**（`scripts/state_inspect.py:4-5` docstring + argparse
   `:26-31`）：
   ```
   state_inspect.py <host> <project-dir> [--tool auto|tofu|terraform] [--json]
   ```
   4 个 flag。本任务直接沿用。

2. **7 个采集字段已锁定**（`scripts/state_inspect.py:7-14` docstring）：
   `backend / state_size_bytes / resource_count / is_locked / lock_info /
   workspaces / summary`。本任务**不新增**字段。

3. **Tool autodetect 已锁定**（`SKILL.md:54-56`）：
   - `command -v tofu` 优先 → 找到用 OpenTofu
   - 否则 fallback `terraform`
   - `--tool tofu|terraform` 强制覆盖
   - `--tool auto`（默认）→ 走 autodetect
   - 实现落到 `lib/terraform.py:resolve_tool(host, tool_arg) -> str`

4. **Host model 已锁定**（`SKILL.md:46-52`）：
   - `local` → `subprocess.run([<tool>, ...], cwd=project_dir)`
   - 其他 alias → `ssh-core/ssh_execute.py <alias> "cd <dir> && <tool> ..."`
   - 实现落到 `lib/terraform.py:run_tf(host, project_dir, args, tool=None,
     timeout=60, parse_json=False)`

5. **Lock 检测策略**（docstring `:11` 「try a non-mutating operation; parse
   lock error if any」）：
   - 跑 `terraform plan -lock-timeout=1s -refresh=false -input=false` →
     如果返回 lock error，parse stderr 拿 `who/when/operation`
   - 不能用 `terraform plan -lock=false` —— 那样根本不会触发 lock check
   - 退出码：lock error 时 terraform 自己返回 1，本脚本归类为 warning（exit 0）
     + `findings.kind=lock`

6. **Backend type 解析**：
   - 优先读 `.terraform/terraform.tfstate`（包含 `backend.type` + 部分
     `backend.config` 字段）—— 这是 init 后的 cached backend config，
     v1.0 不解析 *.tf 文件本身（避免 HCL parser 依赖）
   - 若 `.terraform/terraform.tfstate` 缺失（project 没 init 过）→ exit 125
     `not_initialized`，提示用户先跑 `terraform init`

7. **State size 与 resource_count**：
   - 本地 backend：`os.stat(.terraform/terraform.tfstate)` 或主 `terraform.tfstate`
   - 远端 backend（s3/gcs/azurerm/remote）：跑 `terraform state list | wc -l`
     拿 resource_count；state_size_bytes 退化为 `null`（v1.0 不通过 backend
     API 抓远端 object size，避免引入云 SDK）+ warning finding 说明

8. **Workspaces**：`terraform workspace list` 解析 `* default` 这种前缀格式

9. **Host model + Output contract** 与已 ship 9 个脚本一致（`SKILL.md:73-95`）。
   `data.summary` 是 `{域: ok|warn|crit}` map（本脚本只有 `state` 一个域），
   `data.findings[]` 含 lock / not_initialized / backend_unknown 等结构化信号。

10. **Failure_class 命名空间（本 child 独占 120-129，最终在 design.md D-X 锁定）**：
    - `120 host_unreachable` —— ssh-core 远端不可达（与 k8s-debug 80 同语义，
      但 namespace 独占以便日志归集）
    - `121 tool_missing` —— 本机/远端找不到 tofu 也找不到 terraform
    - `122 project_dir_invalid` —— 路径不存在 / 不是目录 / 无 `*.tf` 文件
    - `123 not_initialized` —— `.terraform/` 不存在（未跑 init）
    - `124` 保留全仓 timeout（GNU convention）
    - `125 backend_unknown` —— `.terraform/terraform.tfstate` 存在但
      backend 字段解析失败（warning，exit 0）
    - `126 lock_parse_failed` —— 检测到 lock 但 stderr 无法 parse `who/when`
      （warning，exit 0，原始 stderr 落 `data.raw.lock_stderr`）
    - `127 workspace_list_failed` —— workspace 列表拿不到（warning，
      `workspaces=[]`）
    - `128-129` 预留扩展

11. **测试基线**：`tests/test_state_inspect_unit.py`（≥30 unit，覆盖 7 字段
    + 8 failure_class）+ `tests/test_state_inspect_integration.py
    -m live_terraform` 新 marker（≥2 PASS：local fixture project（用
    `null_resource` / `random_*` provider，零云依赖）+ locked fixture
    state）。

## Out of scope

- **State 写操作**（rm / mv / push / pull）—— 归 `ssh-guarded` 提 request
- **跨 backend migrate**（s3 → gcs 等）—— 高危写操作，永远不在本 plugin
- **State 加密 / 解密**（用 `terraform output -json` 直接看 sensitive 值）
  —— 安全模糊地带，v1.0 不做
- **远端 backend object size 精确值** —— v1.0 退化为 `null`，需要时由 v1.1
  引入云 SDK（s3 / gcs / azurerm 三套）
- **HCL parser** —— 本脚本只读 cached state，不解析 *.tf；HCL 解析归
  module_validate

## Constraints

- **ADR-001 D1 风格**：subprocess + 原生 tool；不引入 python-hcl2 等 HCL
  parser dep（留给 module_validate）
- **`lib/terraform.py` 抽出是硬性要求**：drift_check 与 module_validate 都
  要复用 tool autodetect + local/alias dispatch
- **lock 检测必须是非破坏性**：用 `plan -lock-timeout=1s -refresh=false`，
  绝不写 state；用例覆盖到「lock-timeout=1s 拿不到 lock 后立即退出」
- **远端 backend object size 退化要明示**：返回 `null` 时同时给 warning
  finding，避免 LLM 误读为「state 是 0 bytes」
- **JSON envelope** 沿用全仓契约
- **failure_class 120-129 namespace 独占**；ship commit 同步追加到
  `.trellis/spec/backend/error-handling.md`

## Acceptance Criteria

- [ ] 7 字段全部采集；远端 backend 时 state_size_bytes=null + warning
      finding
- [ ] `scripts/lib/terraform.py` 落地，签名 `resolve_tool(host, tool_arg)`
      + `run_tf(host, project_dir, args, tool=None, timeout=60, parse_json=False)`，
      覆盖 local + alias 两条路径
- [ ] `tests/test_state_inspect_unit.py` ≥ 30 PASS，覆盖 7 字段 + 8
      failure_class
- [ ] `tests/test_state_inspect_integration.py -m live_terraform` ≥ 2 PASS
      （local null_resource fixture + locked fixture）
- [ ] lock 检测是非破坏性（unit test 断言 `-lock-timeout=1s -refresh=false`
      在 argv 中出现，且无 `-lock=false`）
- [ ] backend 解析覆盖 ≥4 种类型：`local` / `s3` / `remote` / `gcs`
- [ ] JSON envelope 通过 schema 校验
- [ ] `.trellis/spec/backend/error-handling.md` 追加 120-129 段
- [ ] SKILL.md decision tree / quick reference 与实现保持一致
- [ ] `pytest tests/` 默认套件全绿、`pytest -m live_terraform` 全绿
- [ ] ruff clean、mypy 不引入新 warning

## Tracking

- 父任务：`05-24-devops-skills-v1-rollout`（Phase 3 提前规划，phase 排期
  保持不变 —— 见父 PRD Notes）
- 同批 sibling：`iac-state-drift-check`、`iac-state-module-validate`
- 本 child 是 iac-state Phase 3 的 **foundation**（产出 terraform lib），
  建议**先于其他 2 个 sibling ship**

## Notes

- 2026-05-26 PRD 初稿。`lib/terraform.py` 抽取是新增决策；动机：3 个 iac-state
  脚本都要 tool autodetect + local/alias 二分 + 长 timeout（plan 可能跑
  几分钟），不抽 lib 会出现 3 份近似但微妙不同的复制粘贴。
- live_terraform fixture 用 `null_resource` / `random_*` provider 是有意
  选择 —— 零云账号依赖，CI 与本机都能跑；具体 fixture 实现（kind 式
  spin-up 速度、cache 策略）留 design.md。
