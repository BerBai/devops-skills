# Drift patterns — when reality and state disagree

"Drift" = 云上真实资源 ≠ Terraform state 记录。`plan` 会显示一堆"未请求的变更"。

## 检测：plan 是诊断工具，不是动作

```bash
terraform plan -detailed-exitcode
# 退出码：
# 0 = state 与现实一致
# 1 = error
# 2 = 有差异（drift 或想做的变更）
```

`-detailed-exitcode` 是漂移检测的核心。配 CI 可以做"每天跑 plan，退出 2 就告警"。

JSON 输出便于程序解析：
```bash
terraform plan -out=plan.bin
terraform show -json plan.bin > plan.json
```

`plan.json` 里 `resource_changes[]` 是每个资源的 before/after。本 plugin 的 `drift_check.py` 就是包装这个 + 分类输出。

## 常见漂移模式

### 1. 手动改控制台

最常见。`aws_security_group` 在 AWS console 加了一条规则没回写 IaC。

```
~ resource "aws_security_group" "web" {
    ingress {
    +   cidr_blocks = ["0.0.0.0/0"]
    +   from_port   = 443
    +   to_port     = 443
    +   protocol    = "tcp"
    }
  }
```

修：要么把规则补进代码（按 IaC 路径），要么 `terraform apply` 把云改回符合代码（按 IaC 权威）。**永远不要纵容混合方式**。

### 2. 其他 IaC 工具混管

CloudFormation 也管同一个资源、Pulumi 也管、Ansible 也改。

诊断：看资源的标签（tags）。规范 IaC 工具会打 `ManagedBy: terraform` 类标签。两个标签同时存在 → 两个工具都在改。

修：选一个工具为单一来源，从另一个里 detach（`state rm` 那一侧 / CloudFormation `DeletionPolicy: Retain` 后 delete stack）。

### 3. `lifecycle.ignore_changes` 用错

```hcl
resource "aws_launch_template" "web" {
  image_id = var.ami_id
  lifecycle {
    ignore_changes = [image_id]
  }
}
```

意图：让 ASG / 部署系统改 image_id 不被 terraform 回滚。
后果：state 永远显示老 image_id，看不出真实 AMI。

修：要么不 ignore（terraform 是 source of truth），要么把"真实 AMI"通过 `data.aws_launch_template` 在别处也 expose 出来。

### 4. AWS 服务自己改了字段

最坑的一类。某些 AWS 服务会**自动**修改字段：
- `aws_eks_node_group` 的 `instance_types` 受 capacity provider 影响
- `aws_lambda_function` 的 `version` 自动递增
- `aws_db_instance` 的 minor engine version 在 maintenance window 自动升

每次 plan 都报"漂移"，但每次 apply 都改不回去（云端自动又改回来）。

修：`ignore_changes` 这些字段，并在代码注释里写明原因。

### 5. 资源被 import 但属性不全

```bash
terraform import aws_instance.web i-0abc1234
```

`import` 只把资源放进 state，**不**反推 Terraform 代码。如果你写的 HCL 与现实差别大，下次 plan 就一堆 in-place 变更（漂移幻觉）。

修：import 后立刻 `plan`，对照 plan diff 把 HCL 改到一致；这一步常被跳过。

### 6. 资源被云端**删了**

某条 lambda 在控制台被手删。Terraform state 里还有。

```
- aws_lambda_function.handler will be destroyed
  (because the resource no longer exists in the state ← 这条提示就是给你看的)
```

terraform 重新 apply 会**重建**它。如果不想重建，`state rm` 把它从 state 里删（资源不会被新建也不会被删）。

### 7. 命名空间被占（`name_prefix` 不一致）

资源名 `mybucket-prod` 已被别人在云上占了，Terraform 创建失败但 state 里却有空 instance entry。

诊断：`terraform state show <addr>` 看是否有 `attributes` 字段缺失。

修：`terraform state rm <addr>`（已有但未真正建），然后改名重新 apply。

## 解读 plan diff 的速查

| Diff 符号 | 含义 |
|---|---|
| `+` | 新建 |
| `-` | 销毁 |
| `~` | in-place 更新 |
| `-/+` | **销毁后重建**（最危险，丢实例 id / 数据） |
| `<=` | data source 在 plan 阶段读取 |

`-/+` 出现时**先停下**，看看是哪个属性触发的强制重建（plan 输出会标 `forces replacement`）。常见误触发：
- 标签 case sensitive 改了
- AMI ID 写错（`ami-abc` vs `ami-ABC`）
- region 字段被外部改

## 多资源 drift 的优先级

drift_check.py 按危险度排序输出：

| 优先级 | 资源类型示例 | 为什么危险 |
|---|---|---|
| **crit** | DB / 持久化存储 / IAM 策略 | 修复操作可能丢数据或影响安全 |
| **warn** | 计算资源 (instance / lambda / ECS) | 修复重建会断流量 |
| **info** | 日志组 / SNS topic / 标签 | 重建影响小 |

## 防漂移制度

不是技术，是流程：

1. 云控制台**只读权限**给开发，写权限只给 CI（PR-based apply）
2. 每天定时 `plan -detailed-exitcode` → 退出 2 进 Slack 告警
3. `terraform import` 之后**必须**跑一次 plan 并把 HCL 修到一致才合并
4. `lifecycle.ignore_changes` 必须在代码注释里说明原因 + ticket 链接
5. CI 跑完 apply 后**强制** push state（远端 backend），别留 stale local copy
