# Module audit — what to check before using / upgrading a module

Terraform module 是复用单元。坏 module 比手写 HCL 还危险 —— 复制 N 份后修起来更累。

## 通过性 checklist（写代码前）

`module_validate.py` 自动检查的项：

### 结构
- [ ] 至少有 `main.tf` / `variables.tf` / `outputs.tf` 三件套
- [ ] 有 `README.md` 说明用途、输入、输出
- [ ] 有 `versions.tf` 锁定 `required_version` + `required_providers`
- [ ] **没有** `providers {}` 块在 module 内（应由 root 注入）
- [ ] **没有** `terraform { backend ... }` 在 module 内（同上）

### 输入 / 输出
- [ ] 每个 `variable` 有 `type` + `description` + 合理的 default 或显式 required
- [ ] 每个 `output` 有 `description`
- [ ] 敏感 output 标 `sensitive = true`
- [ ] `variable validation {}` 用得合理（不要过度，但 region / instance_type 这种值得做）

### 资源命名
- [ ] 所有资源用 `var.name_prefix` / `var.tags` 等可注入项命名，**不**硬编码
- [ ] tags 至少有 `Name`、`Environment`、`ManagedBy`

### 版本钉死
- [ ] `required_version = ">= 1.5"` 这种**下界明确**
- [ ] provider source 用全限定（`hashicorp/aws`，不是裸 `aws`）
- [ ] provider 版本约束**有上界**（`~> 5.0` 而非 `>= 5.0`），避免 5.x → 6.x 大变更

### 反模式
- [ ] **没有** `count = 0` 用作"禁用某资源"的奇技淫巧（用 `for_each = {}` 更标准）
- [ ] **没有** `depends_on` 在 resource 之间（让数据流自然依赖）
- [ ] **没有** `local-exec` / `remote-exec` 在 module 里（强耦合执行环境）
- [ ] **没有** 直接 `aws_iam_policy_document` data source 内嵌策略字符串（用 `jsonencode()`）

## 升级 checklist（改版本之前）

```bash
# 1. 看版本范围里现在有哪些 breaking change
gh release list --repo <module-repo>
# 2. 在 fork / 分支上测
terraform init -upgrade
terraform plan       # 看 diff 是否有非预期变更
# 3. 必看：plan 里有没有 forces replacement
terraform show -json plan.bin | jq '.resource_changes[] | select(.change.actions[] | contains("delete"))'
```

`module_validate.py --target-version <X.Y.Z>` 模式会做：
- 拉新版 module 的 `versions.tf` / `variables.tf`
- 对比当前调用的 `variable` 是否还存在 / 类型是否改
- 标记被移除的 variable + 标记新增 required variable

## Source 类型决策

| source 写法 | 用于 | 风险 |
|---|---|---|
| `git::ssh://...` + `ref=<sha>` | 内部 module | 低（pin 到 commit） |
| `git::ssh://...` + `ref=<tag>` | 内部 module | 中（tag 可被移动） |
| `git::ssh://...` 无 ref | 内部 module | 高（每次 init 拉默认分支） |
| `registry.terraform.io/<ns>/<name>/<provider>` | 公开 module | 中（注册中心可能下架） |
| 相对路径 `./modules/foo` | 同仓库 | 低 |
| Terraform Cloud private registry | 团队私有 | 低 |

**总是 pin 到 commit SHA 或不可变 tag**。`terraform init` 一次 OK，等 6 个月再跑 CI 时 module 已飘了。

## 跨 module 反模式

- **巨型 module**：500+ 行 HCL，30+ variable —— 拆分成多个 module
- **module 调 module 调 module**：层级 ≥ 3 时 plan 失败信息几乎不可读。控制在 2 层内
- **module 之间互相依赖**：A 的 output 喂 B，B 的 output 喂 A —— 这是 monolith 想披 module 皮，重构
- **module 里包含 provider {}**：让上层无法选 region / profile，只能 hardcode

## 安全相关

`module_validate.py` 顺手报告：
- module 创建的 IAM 资源是不是 `*` 权限（`Resource = "*"` 或 `Action = "*"`）
- 创建的 SG 有没有 `0.0.0.0/0` ingress
- S3 bucket 有没有 public ACL
- DB 有没有 `publicly_accessible = true`

这些只是提示，正式合规扫描用 Checkov / tfsec / Snyk IaC。

## 文档要求

好 module 的 README 应该有：

```markdown
# <module-name>

短描述。

## Usage

(完整 `module {}` 块，能 copy-paste 到 root)

## Inputs

(自动生成，比如用 terraform-docs)

## Outputs

(自动生成)

## Examples

链接到 `examples/` 目录下的可运行例子。

## Migration notes

不同主版本之间的迁移说明，含 plan diff 例子。
```

没有 `Examples/` 目录或没法在 5 分钟内跑通的 module，应被视为不可用。
