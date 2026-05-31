# E2E validate all skills on pai

## Goal

对仓库内**已经开发完成的 9 个 v1.0 CLI 脚本**做端到端真机验证，确认它们在真实环境（pai = Debian 13 + 本地 macOS + Docker）里能正确工作；产出一份可审阅的验证报告，记录每个脚本「跑了什么、结果如何、是否符合 SKILL.md 设计意图、是否暴露 bug 或测试缺口」。

## Scope decision: 只跑已完成的 skill

仓库里共 8 个 plugin、~35 个脚本。**绝大多数仍是 surface stub**（docstring 标 `v1.0 surface:` 或 `v1.0 design:`，loc < 100，无实际实现）。经过盘点，**仅以下 9 个脚本是 v1.0 完成态**（docstring 标 `(v1.0).`，loc 250–740，配套 unit + integration test）：

| Skill | 已完成脚本 | 测试路由 |
|---|---|---|
| ssh-core | `ssh_execute.py` (257 loc), `ssh_cluster.py` (619 loc) | pai (POC_HOST=pai) |
| remote-debug | `diagnose_host.py` (472), `compare_across_hosts.py` (657), `port_check.py` (532), `tail_log.py` (542) | pai |
| docker-quick | `inspect_container.py` (700), `compose_status.py` (729), `image_audit.py` (736) | 本地 Docker (local route，无 SSH) |

**未完成的 ssh-guarded / log-aggregator / k8s-debug / iac-state / cicd-debug 不在本任务范围**，等它们落 v1.0 实现后再开新验证任务。

## Background

- `tests/README.md` 说 v1.0 roadmap 是 "added incrementally as v1.0 scripts land"——本任务即是这套增量 v1.0 落地后的首次系统验证。
- pai 已就绪：Debian 13 + Docker 29.5.1 + Python 3.13.5，SSH 别名 `pai` 配好（root@192.168.2.115）。
- 本地：Docker 27.5.1 已运行，3 个无关容器在跑（pytest 用唯一名 throwaway 容器，不会冲突）。

## In-scope

1. **基线检查**（无副作用）：
   - `ssh-add -l` 有 ed25519 key
   - `ssh pai 'echo ok'` 通
   - `docker ps` 本地可用
   - `pytest tests/` 默认套件全绿（保护基线）

2. **自动化集成测试**：
   - `pytest tests/ -m live_ssh -v -s`，归档 9 个 integration test 结果

3. **手动 happy-path 演练**（为报告补 stdout 截断 + 人工判断）：
   - 9 个脚本各 `--help` 一次（契约确认）
   - 9 个脚本各跑 1 个最具代表性场景，取真实 stdout

4. **产出**：
   - `.trellis/tasks/05-24-e2e-validate-skills-on-pai/validation-report.md`
   - 包含：per-skill 表格、每脚本 verdict、Findings 一节（bug / 测试缺口 / SKILL.md drift）

## Out-of-scope

- 验证 stub 脚本（ssh-guarded / log-aggregator / k8s-debug / iac-state / cicd-debug 的所有脚本，以及 ssh-core 的 7 个 stub）
- 修复发现的 bug（列入 Findings，后续任务处理）
- 新增 integration test 代码
- 性能 / 压力测试
- 装 k3s / terraform / 起 docker fixture（v1.0 范围不需要）
- 修改 SKILL.md / references

## Constraints

- pai 现状不动（除 SSH 测试本身的 read-only 操作）
- 本地不影响现有 3 个容器（kline-timescaledb 等）
- pytest 跑完所有 throwaway 容器必须被清理（这是测试自身责任，本任务只 verify）
- 验证若发现破坏性行为或安全问题，立即停下报告

## Acceptance Criteria

- [ ] `pytest tests/` 默认套件全绿
- [ ] `pytest tests/ -m live_ssh -v` 跑完，9 个测试每个都有 verdict（PASS / FAIL / SKIP + 原因）写入报告
- [ ] 9 个 v1.0 脚本每个都被手动跑过至少 1 次 happy-path，stdout 关键截断进报告
- [ ] 报告内含 Findings 一节，列出本次发现的 bug、测试缺口、SKILL.md 与实际行为偏差（即便为空也明确写"none"）
- [ ] 本地 / pai 无残留 throwaway 容器 / 临时文件
- [ ] 报告路径 `.trellis/tasks/05-24-e2e-validate-skills-on-pai/validation-report.md` 已生成
