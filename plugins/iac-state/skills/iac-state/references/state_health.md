# Terraform state health

state 文件是 Terraform 的"真相数据库"。它有问题，整个 IaC 工作流就垮。

## state 是什么

state 是一个 JSON 文件，记录每个 Terraform-managed 资源的：
- terraform 资源地址（`aws_instance.web`）
- 云端真实 ID（`i-0abc1234`）
- 资源属性快照（terraform 上一次知道的样子）
- 依赖关系（用于 destroy 顺序、apply 并行）

```json
{
  "version": 4,
  "terraform_version": "1.7.5",
  "serial": 142,
  "lineage": "abc-def-...",
  "resources": [
    {
      "mode": "managed",
      "type": "aws_instance",
      "name": "web",
      "provider": "provider[\"registry.terraform.io/hashicorp/aws\"]",
      "instances": [{"attributes": {"id": "i-0abc1234", ...}}]
    }
  ]
}
```

**serial** 每次写就 +1，**lineage** 是 state 文件的"身份证"。两个不同 state 不应有同 lineage，但 serial 单调递增。

## Backend 类型对比

| Backend | Lock | 并发安全 | 适合 |
|---|---|---|---|
| `local` (file) | OS 文件 lock | 单机 | 实验、个人项目 |
| `s3` + DynamoDB | DynamoDB | 团队 | AWS 项目标配 |
| `gcs` | GCS object lock | 团队 | GCP 项目 |
| `azurerm` | Azure blob lease | 团队 | Azure 项目 |
| `remote` (TFC/TFE) | 服务端 | 团队 | 用 Terraform Cloud / Enterprise |
| `http` | 取决于服务端 | 取决于 | GitLab managed state 等 |
| `consul` | Consul session | 团队（少见） | Consul 用户 |

对**任何**多人/多 CI 场景，**必须**用带 lock 的远端 backend。本地 file backend 在多人环境是炸弹。

## 锁的常见问题

### `Error acquiring the state lock`

```
ID:        2026-05-14-abc123
Path:      s3://my-bucket/prod/terraform.tfstate
Operation: OperationTypeApply
Who:       jason@host
Created:   2026-05-14 13:55:01.123 UTC
Info:      ...
```

含义：另一个进程（人 / CI / 同事的 laptop）拿着锁，没释放。

**正常路径**：等它结束（看 `Who` 和 `Created`，判断要不要打电话）。

**异常路径**（持锁者已死，锁没解）：
```bash
terraform force-unlock <ID>      # 写操作！本 plugin 不替你执行
```

`force-unlock` 是危险操作。**只有在确认持锁进程已死**时才用。下错了能导致 state 损坏。

### 锁文件类型

- DynamoDB：一行带 LockID。死锁 → DynamoDB console 删那行（等于 force-unlock）
- S3 file lock（无 DynamoDB）：S3 上 `<key>.tflock` 对象。删那对象 = 解锁
- GCS：GCS object 自带 generation；锁通过 generation precondition 实现，无独立锁文件

## state 文件大小

健康项目 state < 5 MB。10 MB+ 就需要警觉，可能：
- 资源数量极多（拆 module / 拆 workspace）
- 单资源属性巨大（如 ECS task definition / OpenAPI gateway 配置嵌套）
- 历史包袱（旧资源没清，留在 state 里）

```bash
terraform state list | wc -l
terraform state pull | jq '.resources | length'
ls -lh terraform.tfstate
```

state 太大 → plan / apply 慢。但**别**轻易 `state rm` 清理 —— 那只从 state 里删，云资源还在，会变成"幽灵资源"无法管理。

## 你应该（按顺序）做的事

诊断 state 问题时：

1. **`state_inspect.py`** —— 先看 backend、size、资源数、是否 locked
2. 如果 locked → 看 `Who` / `Created`，对照实际持锁者是否存活
3. 如果 size 异常 → 看 `terraform state list` 是否有过期资源
4. 如果 plan 行为意外 → 走 `drift_check.py`

## 危险信号

- state 在 git 里（**绝对不要**这么做，仅 file backend 时也别提交）
- state 在公共 S3 桶里（state 含 secret）
- 多个 lineage 的 state 在用（migrate 失误，至少一个分支基于旧 state）
- serial 倒退（state revert / 手动覆盖，几乎一定数据已损坏）
- 团队没人懂 backend 配置（事故等着发生）
