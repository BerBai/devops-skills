# 扩大 devops 覆盖：新增 k8s / docker / iac / cicd / log 五个 plugin

## Goal

把 `devops-skills` 从「SSH-centric 远程调试 toolkit」推进为「真·DevOps 多领域 skill 集合」，使其名称（devops-skills）与覆盖面相符。新增 K8s、Docker、IaC、CI/CD、Log 五个应用层领域 plugin，让 Claude Code 能在更多 DevOps 场景里被 skill-matcher 命中并提供领域专家级 playbook。

## What I already know

- 仓库当前已有 3 个 plugin：`ssh-core`（传输）、`ssh-guarded`（安全）、`remote-debug`（跨设备调试）。
- 仓库结构为 marketplace + 多 plugin 模式，`.claude-plugin/marketplace.json` 是入口。
- 每个 plugin 自我封闭，跨 plugin 通过 shell-out 调 CLI（不直接 import 对方 Python）。
- 既定的 skill 编写约定（见 `CONTRIBUTING.md` / `CONTRIBUTING.zh-CN.md`）：
  - SKILL.md 只放分诊 + 决策树，深度内容下沉 `references/`
  - 所有脚本支持 `--json`，输出统一契约
  - `subprocess.run(list, ...)`，永不 shell 字符串
  - 中英双语触发词受欢迎
- 参考前作 `ahmedasmar/devops-claude-skills` 是 6 plugin marketplace（k8s-troubleshooter / iac-terraform / aws-cost-optimization / ci-cd / gitops-workflows / monitoring-observability），可借鉴**形态**但代码独立重写。
- 当前所有脚本是「桩 + 完整文档」状态，无真实执行能力；v1.0 路线图记在 `CONTRIBUTING.md`。
- 测试套件 `tests/test_manifests.py` 校验：marketplace ↔ 目录 ↔ plugin.json 三方一致、SKILL.md frontmatter、`--help` smoke。

## Assumptions (temporary)

- 用户希望保持已有 3 plugin 的设计哲学（marketplace + 两层文档 + 决策树 + 桩脚本），只是扩展覆盖面。
- 新 plugin 仍按 scaffold 节奏交付（plugin.json + SKILL.md + references + 脚本桩 + smoke），v1.0 实现留到后续。
- 新 plugin 默认依赖用户机器上已安装的 `kubectl` / `docker` / `terraform` / `gh` / 等 CLI（不打包它们）。
- 远程操作（在远端跑 kubectl 等）走 ssh-core；本地操作直接 subprocess。

## Open Questions

- ~~**范围裁剪**~~ **已决：5 个全上**
- ~~**领域 vs 集成**~~ **已决：v0.2 只读诊断**（写操作 v0.3 再说）
- ~~**远端 vs 本地默认**~~ **已决：默认远端，host alias 为第一参；本机传 `local`**
- ~~**触发词冲突**~~ **已决（待用户确认）**：按主语切分触发词。k8s-debug 关键词限定 K8s 层（Pod / Deployment / kubectl / CrashLoopBackOff / OOMKilled / Pending / Helm / ImagePullBackOff），remote-debug 限定主机层（uptime / disk / memory / port / process）；docker-quick 关键词限定容器层；log-aggregator 关键词限定多源 log；iac-state 限定 terraform / state / drift；cicd-debug 限定 GH Actions / GitLab CI / workflow。每个 SKILL.md 顶部带 `Skip when` 段显式划界。
- ~~**AWS / 云平台 plugin**~~ **已决：v0.3**（v0.2 不动）

## Requirements (evolving)

- 每个新 plugin 满足现有 scaffold 标准：plugin.json / SKILL.md（含 frontmatter）/ ≥3 references / 脚本桩（每个含 `--json` + `--help`）
- marketplace.json 注册新 plugin
- tests/test_manifests.py 在不修改的前提下，对新 plugin 自动覆盖（参数化测试）
- README / README.zh-CN / CONTRIBUTING / CONTRIBUTING.zh-CN 同步增补
- 0 严重 / 0 警告通过 trellis-check 流程

## Acceptance Criteria (evolving)

- [ ] marketplace.json 列出所有最终选定的 plugin
- [ ] 每个新 plugin 通过 `--help` smoke test
- [ ] 每个新 plugin 的 SKILL.md `description` ≥ 60 字符且含中英双语触发词
- [ ] 跨文档无悬空脚本引用
- [ ] README / CONTRIBUTING 双语版本同步更新

## Definition of Done (team quality bar)

- 所有脚本 `--help` 通过
- manifest 测试通过
- 跨文档引用 0 悬空
- README/CONTRIBUTING 双语同步
- v1.0 roadmap 已写入 CONTRIBUTING（含新 plugin）

## Out of Scope (explicit)

- 任何脚本的真实实现（继续是 v1.0 工作）
- 新 plugin 的真实 CI / 集成测试（仅 `--help` smoke）
- `tests/test_manifests.py` 之外的新测试
- 新增对 paramiko / kubectl / boto3 / docker SDK 等第三方库的依赖（脚本桩不需要）

## Technical Notes

- 候选 plugin（待裁剪）：
  1. `k8s-debug` —— Pod/Node/Service 诊断、CrashLoopBackOff / OOMKilled / Pending 排查
  2. `docker-quick` —— 容器排查、镜像 size、layer cache、compose 调试
  3. `log-aggregator` —— 多源 log 聚合 + 关联分析（journalctl / docker logs / k8s logs / 远端 tail）
  4. `iac-state` —— Terraform state 诊断 + drift 检测
  5. `cicd-debug` —— GH Actions / GitLab CI 故障定位
- 参考前作：`ahmedasmar/devops-claude-skills` 的 6 plugin（不直接抄代码，借形态）

## Decision (ADR-lite)

**Context**: 项目自称 DevOps 但只覆盖 SSH + 远程调试。要么收窄宣传要么扩大覆盖；用户选扩大。

**Decision**: v0.2 新增 5 个 plugin，统一架构原则：
1. **形态对齐 v0.1**：marketplace + 两层文档 + 决策树 + 桩脚本
2. **执行位置**：每个脚本第一参为 host alias；本机场景传 `local`
3. **读写边界**：v0.2 只做 diagnose（read-only），写操作留 v0.3
4. **触发词主语切分**：每个 SKILL.md 顶部带 `Skip when` 段，避免与既有 plugin 双触发
5. **依赖**：脚本仅 shell-out 调用用户机器已装的 CLI（kubectl / docker / terraform / gh / journalctl），不打包它们
6. **远端调用**：`local` 直接 subprocess；非 local 通过 `ssh-core` 的 `ssh_execute.py` 转发

**Consequences**:
- 仓库从 3 plugin 变 8 plugin，marketplace.json 显著增长
- README / CONTRIBUTING 双语版本需要同步更新
- tests/test_manifests.py 参数化测试自动覆盖新 plugin
- 后续 v1.0 实现各脚本时，5 个新 plugin 与 3 个老 plugin 共 21 个新增脚本桩待落地

## Plugin 详细规格

### 1. `k8s-debug`
- description 触发词：`kubernetes / k8s / kubectl / Pod / Deployment / Service / Namespace / CrashLoopBackOff / OOMKilled / ImagePullBackOff / Pending / Helm / Ingress / 集群 / 故障 / 诊断`
- references: `common_issues.md` / `pod_lifecycle.md` / `networking.md`
- scripts: `check_namespace.py` / `diagnose_pod.py` / `cluster_health.py` / `helm_status.py`

### 2. `docker-quick`
- description 触发词：`docker / container / 容器 / image / compose / layer / Dockerfile / OOM / restart / dead / unhealthy`
- references: `container_issues.md` / `image_optimization.md` / `compose_debug.md`
- scripts: `inspect_container.py` / `image_audit.py` / `compose_status.py`

### 3. `log-aggregator`
- description 触发词：`日志 / log / journalctl / tail / aggregate / 聚合 / 关联 / correlate / multi-source`
- references: `sources.md` / `correlation_patterns.md` / `noise_reduction.md`
- scripts: `tail_multi.py` / `correlate.py` / `grep_across_sources.py`

### 4. `iac-state`
- description 触发词：`terraform / opentofu / state / drift / plan / apply / IaC / 基础设施 / 漂移`
- references: `state_health.md` / `drift_patterns.md` / `module_audit.md`
- scripts: `state_inspect.py` / `drift_check.py` / `module_validate.py`

### 5. `cicd-debug`
- description 触发词：`CI / CD / pipeline / GitHub Actions / GitLab CI / workflow / 流水线 / job / runner / failed`
- references: `pipeline_failures.md` / `runner_debug.md` / `secret_scopes.md`
- scripts: `pipeline_analyzer.py` / `runner_check.py` / `secret_scope_audit.py`

## Research References

（待 trellis-research 子代理完成后填充）
