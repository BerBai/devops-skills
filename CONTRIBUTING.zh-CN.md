# 为 devops-skills 贡献代码

[English](./CONTRIBUTING.md) | 简体中文

感谢你考虑参与贡献。本文档讲清楚仓库结构、已发布的 V1 状态，以及当前 V1.1 路线图。

## 仓库布局

```
devops-skills/
├── .claude-plugin/marketplace.json   # marketplace 入口 —— 列出所有 plugin
├── plugins/
│   ├── ssh-core/                     # 传输层
│   │   ├── .claude-plugin/plugin.json
│   │   └── skills/ssh-core/
│   │       ├── SKILL.md              # workflow + 速查表（skill 入口）
│   │       ├── references/*.md       # 按需加载的深度参考
│   │       └── scripts/              # Claude Code 调用的 Python CLI
│   ├── ssh-guarded/                  # 安全层
│   ├── remote-debug/                 # 主机调试
│   ├── k8s-debug/                    # Kubernetes 诊断
│   ├── docker-quick/                 # Docker / 容器诊断
│   ├── log-aggregator/               # 多源日志聚合
│   ├── iac-state/                    # Terraform / OpenTofu state
│   └── cicd-debug/                   # GitHub Actions / GitLab CI
└── tests/                            # pytest 套件 + manifest linter
```

每个 plugin **自我封闭** —— 它的 `skills/<name>/scripts/` 只在有明确文档说明的情况下才跨边界。诊断 plugin（`remote-debug` / `k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`）依赖 `ssh-core` 提供远端命令执行，但**不**直接 import 其 Python 模块；而是 shell out 调用用户已安装的 `ssh-core` CLI。`ssh-guarded` 同样如此。

## Skill 编写约定

1. **`SKILL.md` 服务于分诊，不是深度。** 决策树、硬性检查点、一段话的命令速查。其余内容下沉到 `references/`。
2. **`plugin.json` 里的 `description` 决定 Claude Code 是否选中本 skill。** 用动词、用具体关键词开头；避免 "tool" / "helper" 这类泛词。中英文双语触发词都欢迎 —— 多语用户受益。
3. **硬性检查点（hard checkpoints）。** 如果某个 workflow 有不可逆副作用，把检查点序列写进 `SKILL.md` 并拒绝偏离。
4. **JSON 输出契约。** 每个脚本必须支持 `--json` 并输出 `{"success": bool, "exit_code": int, "stdout": "...", "stderr": "...", "data": {...}}`。这是让 Claude Code 能链式调用而无需解析自然语言的关键。
5. **`subprocess.run(list, ...)`，永不传 shell 字符串。** 不要例外，哪怕输入"可信"。测试用例里专门塞了 `;`、`&&` 和 `` ` ``。
6. **skill 输出不要 emoji**（除非用户显式要求）。Skill markdown 同样保持无 emoji。

## 状态

按已接受的 rollout 口径，V1 已完成：

- 8 个 plugin 的 V1 CLI surface 均已发布。
- 每个公开脚本都支持 `--help`；每个运行型脚本都支持 JSON 输出契约。
- 已发布脚本具备 unit 覆盖；依赖环境的 integration 测试保持 skip-safe。
- `ssh-core` 已跟踪独立版 **1.5.0**（ADR-004、ADR-005）。它是本 marketplace 的「远程连接子功能」：传输、认证、会话、隧道、传输文件、`~/.ssh/config` CRUD，以及 `selfcheck.py` / `doctor.py` 能力探针。其他 plugin 只通过 `ssh_execute.py` shell out，不 import 它。

Plugin 能力清单：

| Plugin | 已发布 |
|---|---|
| `ssh-core` | `ssh_execute`、`ssh_session`、`ssh_slice`、`ssh_cluster`、`ssh_tunnel`、`ssh_server_transfer`、`ssh_config_manager`、`ssh_upload`、`ssh_download`、`deploy_pubkey`、`ssh_daemon`、`selfcheck`、`doctor`、`audit`（1.5.0），以及 `run.cmd` / `run.sh` 启动器。 |
| `ssh-guarded` | request drafters、`run_request`、`exec_detached`、`scan_software`、`redact_check`、`gc_artifacts`。 |
| `remote-debug` | `diagnose_host`、`tail_log`、`port_check`、`compare_across_hosts` —— 均支持 `local`；`diagnose_host` 有 Windows 探针族（`--target-os`），`tail_log` 支持流式 `--follow`，`port_check` 支持 `--udp`。 |
| `k8s-debug` | `check_namespace`、`diagnose_pod`、`cluster_health`、`helm_status`（pending release 会列出卡住的 pod）。 |
| `docker-quick` | `inspect_container`、`image_audit`、`compose_status`。 |
| `log-aggregator` | `tail_multi`、`correlate`、`grep_across_sources`。 |
| `iac-state` | `state_inspect`、`drift_check`、`module_validate` —— 静态 HCL 分析对远程模块同样可用（经 ssh-core 取文件、本机解析）。 |
| `cicd-debug` | `pipeline_analyzer`、`runner_check`、`secret_scope_audit` —— 可选首参 `<host>` 会在远端跑 `gh`/`glab`，并先做一次登录预检。 |

## 路线图

优先推进能解锁多个后续能力的传输层基础工作：

1. `ssh-core`：保持插件树与独立版发布字节一致（当前 1.5.0），每次同步后重跑插件测试。
2. `ssh_server_transfer.py`：在更多主机类型上验证 named-OpenSSH/tar/rsync 模式。
3. `ssh_tunnel.py`：连接复用决策之后重新评估 Windows 支持（`--idle-timeout` 已在 1.4.0 落地）。
4. 脱敏强化：默认 catalog 导出、可选 regex timeout 防护。
5. 插件级增强：CI secret-scope 深层风险标记、request upload-root 可配置，以及其他已记录的 follow-up。

## 环境准备

本项目使用 [uv](https://github.com/astral-sh/uv) 管理依赖。clone 之后执行：

```bash
uv sync --all-extras
```

这会创建 `.venv/`，并装好 `pytest`、`ruff`、`mypy` —— 也就是下面测试和 lint 章节假定能直接在 `PATH` 里找到的工具。

## 测试

```bash
pytest tests/
```

CI 在 Linux 上跑三个 job（`.github/workflows/ci.yml`），覆盖四条检查：

1. JSON-lint 每个 `marketplace.json` 和 `plugin.json`
2. 检查每个 `SKILL.md` 是否含 `## When to use` 与流程小节（`## Workflow` 或 `## Decision tree`）—— 由 `tests/test_manifests.py` 强制
3. 对每个公开脚本 smoke 测 `--help`（私有 `_*.py` 辅助模块豁免）
4. 断言各 plugin 内的内嵌辅助文件与模板一致 —— 由 `tests/test_manifests.py::test_vendored_locator_matches_template` 与 `::test_vendored_version_helper_matches_template` 强制

## 内嵌文件

有两个辅助文件被复制进多个 plugin，因为 plugin 独立分发、不能跨 plugin import
（见 `docs/architecture.md`），但靠手工同步多份副本迟早会把真实修复悄悄改回去。
两者各由一份模板生成：

| 模板 | 副本数 | 作用 |
|---|---|---|
| `scripts/templates/_ssh_core_locator.py` | 7 | 定位 ssh-core 安装位置，所有远端调用都依赖它 |
| `scripts/templates/_version.py` | 7 | 从 plugin manifest 回答 `--version` |

```bash
# 只改模板，然后重新渲染所有副本
$EDITOR scripts/templates/_ssh_core_locator.py
python scripts/sync_vendored.py
python scripts/sync_vendored.py --check     # CI 跑的就是这条
```

模板与渲染后的副本要一起提交。**不要原地改副本**：副本一旦与模板不一致 CI 就会失败，
正确的修法是重新渲染，而不是把旧文件覆盖回去。
