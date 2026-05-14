# Secret scopes — who can see what, and when

CI secret 的可见性比看起来复杂。错配等于把生产 token 暴露给所有 PR。

## GitHub Actions：四层 + PR 安全模型

### 四层 scope

```
Organization secrets
    └─ visible to: 选定 repo / 所有 repo / 私有 repo
        │
Repository secrets
    └─ visible to: 该 repo 的所有 workflow
        │
Environment secrets
    └─ visible to: 显式指定 environment 的 job
        │
Dependabot secrets
    └─ visible to: 只在 Dependabot-triggered workflow
```

**冲突时优先级**：env > repo > org。同名时 env 的胜出。

```bash
gh secret list --repo <owner>/<repo>
gh secret list --org <org>
gh api repos/<owner>/<repo>/environments --jq '.environments[].name'
# 列环境 secret
gh api repos/<owner>/<repo>/environments/<env>/secrets
```

### `pull_request` from fork

**关键**：`pull_request` 事件来自 fork 时**所有 secret 都不注入**。Action 拿到的 `secrets.X` 是空字符串。

这是 GitHub 故意的：防止恶意 PR `echo $TOKEN` 偷密钥。

哪些事件来自 fork 时**仍然**带 secret？
- `pull_request_target`（用 base branch 的 workflow，但能 access secret —— **危险**）
- `workflow_run`（被前一个 workflow 触发，独立运行，能 access）
- 显式安装 GitHub App 的某些 webhook

### `pull_request_target` 的陷阱

```yaml
on: pull_request_target
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}      # ❌ checkout PR 代码
      - run: npm install                                       # ← 这里跑了 PR 代码
      - run: npm test                                          # ← 这里 secret 在
```

恶意 PR 可以在 `package.json` 的 `postinstall` 里 `echo $SECRET` 上传到 webhook。**任何 `pull_request_target` 中 checkout PR 代码并执行的 workflow 都是漏洞**。

正确模式：分两个 workflow。
- `pull_request`：拉 PR 代码、跑 lint/build/test，但**没有 secret**
- `workflow_run` → 触发部署 workflow，能用 secret，但**不**直接跑 PR 代码

### 哪些 workflow 用了哪些 secret

`secret_scope_audit.py` 列出：
- 每个 secret 在哪些 `.github/workflows/*.yml` 被引用
- 每个引用是否在 PR-from-fork 可触发的 event 里
- 每个引用是否在 `pull_request_target` 后做了 `checkout PR`（标 crit）

---

## GitLab CI：Variables 与 protected branch

GitLab 的变量分三类：

### 1. Project / Group / Instance 变量

UI Settings → CI/CD → Variables 配置。每个变量有四个属性：

- **Protected**：只在 protected branch / tag 触发的 pipeline 才注入
- **Masked**：log 里出现时被替换成 `[MASKED]`（仅当值满足特定字符集时才能 mask）
- **Expanded**：值里的 `$VAR` 会展开
- **Environment scope**：限定到某个 `environment:name`

```bash
glab api projects/<project-id>/variables --jq '.[] | {key, protected, masked, environment_scope}'
glab api groups/<group-id>/variables
```

### 2. CI/CD 文件里 `variables:`

```yaml
variables:
  IMAGE_TAG: latest
job:
  variables:
    LOG_LEVEL: debug
```

明文写在 yaml 里，**不能放 secret**。

### 3. Pipeline 触发时传

```bash
glab ci trigger run --variables KEY=VALUE
```

### Protected branch 的意义

只有 `main`、`production` 这类 protected branch 上跑的 pipeline 才能看到 protected 变量。其他分支（feature/fix）即使有权限触发 pipeline，也拿不到 protected secret。

**等同 GitHub 的 `environment` 保护**：把生产 token 圈在 protected 分支内。

### Audit

`secret_scope_audit.py` 对 GitLab 列出：
- 每个变量的 protected / masked / env scope
- 引用该变量的 job 列表（解析 .gitlab-ci.yml）
- 哪些 job 不在 protected 上下文但引用了 protected 变量（运行时会拿到空 → 用户排查浪费时间）

---

## 通用反模式

1. **secret 在 commit log / Slack / log 文件里被记到**
   - 防：CI 默认 mask；不在 echo 里出现 secret
2. **secret 给 fork PR 暴露**
   - 防：上面 `pull_request_target` 段
3. **过宽 scope**：org-level secret 给了所有 repo，包括公共 demo repo
   - 防：用 selected repo
4. **过期 secret 不旋转**
   - 防：标 created_at；定期审计
5. **token 有写权限但 job 只需读**
   - 防：least privilege；GitHub fine-grained PAT；GitLab project access token

## OIDC：少用 long-lived token

最理想：CI 用 OIDC 拿短期 token（AWS STS / GCP Workload Identity / Azure federated credentials），完全不存长 token 在 CI secret 里。

```yaml
# GitHub Actions 示例
permissions:
  id-token: write
  contents: read
jobs:
  deploy:
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::xxx:role/gh-actions
          aws-region: us-east-1
```

`secret_scope_audit.py` 顺手检测：是否用了 OIDC，还是依赖 long-lived `AWS_ACCESS_KEY_ID` —— 后者每个 30 天该旋转的 secret 都是定时炸弹。
