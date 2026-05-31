# Implementation — E2E validate v1.0 skills on pai

## Phase A — Pre-flight

- [ ] A1：`ssh-add -l` 列出 ed25519 key（找到 `~/.ssh/id_ed25519`）
- [ ] A2：`ssh pai 'echo ok && uname -a'` 返回 Debian 信息
- [ ] A3：`docker ps` 不报错；记录当前本地容器列表（避免误清理）
- [ ] A4：`cd /Users/ber/Project/devops-skills && pytest tests/` 全绿（基线保护）；记录测试数
- [ ] A5：在 `.trellis/tasks/05-24-e2e-validate-skills-on-pai/validation-report.md` 写骨架：
  ```
  # Validation report — v1.0 skills on pai
  Date: 2026-05-24
  Scope: 9 v1.0 scripts across 3 skills
  
  ## Environment
  - pai: <uname -a output>
  - local: macOS, Docker 27.5.1, Python <version>
  - ssh-agent: <ssh-add -l output>
  
  ## Phase B — Automated tests (`pytest -m live_ssh`)
  | Test | Verdict | Note |
  | ... | ... | ... |
  
  ## Phase C — Manual happy-path
  ### ssh-core
  #### ssh_execute
  #### ssh_cluster
  ### remote-debug
  #### diagnose_host
  ... (etc)
  
  ## Findings
  ```

**Validation A**：报告骨架就位、基线绿。

## Phase B — Automated test suite

- [ ] B1：`pytest tests/ -m live_ssh -v -s 2>&1 | tee /tmp/devops-skills-phase-b.log`，**不** stop-on-fail
- [ ] B2：解析 log，对 9 个测试逐个在 report 表格里填行：
  - test_ssh_execute_integration
  - test_ssh_cluster_integration
  - test_diagnose_host_integration
  - test_compare_across_hosts_integration
  - test_port_check_integration
  - test_tail_log_integration
  - test_inspect_container_integration
  - test_compose_status_integration
  - test_image_audit_integration
- [ ] B3：对每个 FAIL 行附 stderr 关键 1–3 行；SKIP 行附 skip reason

**Validation B**：9 个测试都有 verdict 行。

## Phase C — Manual happy-path sweep

### C1 — ssh-core（2 脚本）

- [ ] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai 'uptime && df -h /'` → 取 stdout
- [ ] `python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py --hosts pai,local --command 'hostname'`（具体 flag 看 --help 确定）

### C2 — remote-debug（4 脚本）

- [ ] `python3 plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py pai`
- [ ] `python3 plugins/remote-debug/skills/remote-debug/scripts/compare_across_hosts.py pai local`
- [ ] `python3 plugins/remote-debug/skills/remote-debug/scripts/port_check.py pai --port 22 --port 443`
- [ ] `python3 plugins/remote-debug/skills/remote-debug/scripts/tail_log.py pai /var/log/syslog --lines 5`

### C3 — docker-quick（3 脚本，本地 Docker）

- [ ] 决定目标容器：从 `docker ps` 挑一个 read-only 安全的（如 `kline-redis`）；或起一个 `docker run -d --name smoke-target nginx:alpine`，验完清理
- [ ] `python3 plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py --container <name>`
- [ ] `python3 plugins/docker-quick/skills/docker-quick/scripts/compose_status.py --project-dir <项目目录>`：在仓库根 `find / -name compose.yml -maxdepth 4 2>/dev/null` 找一个，或起一个 throwaway compose
- [ ] `python3 plugins/docker-quick/skills/docker-quick/scripts/image_audit.py --image <local image>`：`docker images --format '{{.Repository}}:{{.Tag}}' | head -3` 挑一个

**Validation C**：report 里 9 个脚本各 1 个 verdict 行 + stdout 截断。

## Phase D — Findings + close

- [ ] D1：在 report 末尾写 Findings 一节
  - 形式：bullet 列表，每条 `[skill/script] <description> — 建议动作`
  - 全部 PASS 时明确写 "No findings."
- [ ] D2：本地清理
  - `docker ps --filter 'name=^smoke-' --format '{{.Names}}'` 列出本任务起的容器
  - `docker rm -f $(...)` 清理（如果有）
- [ ] D3：（可选）`python3 ./.trellis/scripts/task.py create "<finding title>" --slug <slug>` 把 Findings spin out

**Validation D**：
- 报告 4 节完整（Environment / Phase B / Phase C / Findings）
- 本地无 `smoke-*` 容器残留

## Final validation commands

```bash
# 仓库基线
pytest tests/                              # 应全绿
# Live 测试
pytest tests/ -m live_ssh -v               # 9 个测试结果在 report
# 残留清理
docker ps --filter 'name=^smoke-'           # 应空
```

## Review gates

- 进 B 前：A 全部完成（基线绿）
- 进 C 前：B 的 9 个测试 verdict 都归档
- 进 D 前：用户 review report 主体
- 完成前：用户确认 Findings 部分准确
