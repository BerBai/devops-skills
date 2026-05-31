# remote-debug docs: prefer --unit on systemd-only distros

## Goal

remote-debug 的 `SKILL.md` 「Log surgery」节用 `/var/log/syslog` 当典型示例，但 Debian 12+ / RHEL 9+ / Ubuntu 22.04+ 等现代发行版默认 journald-only，根本没有 `/var/log/syslog`。文档示例直接照抄会跑出空输出 + exit 1（见父任务 `validation-report.md` Finding #3，Phase C 实测 `tail_log pai /var/log/syslog --lines 5` 在 Debian 13 复现）。本任务补文档，引导用户在 systemd-only 发行版上优先用 `tail_log --unit <service>`（已是 v1.0 一等公民模式，背靠 `journalctl -u`）。

## Scope

仅文档变更，不动 `tail_log.py` 代码、不动测试、不动 `references/` 之外的别的 skill。

## In-scope

1. **`plugins/remote-debug/skills/remote-debug/SKILL.md` 的 Log surgery 节（第 70-83 行）**：
   - 把使用 `/var/log/syslog` 的示例替换或补一行 `--unit` 用法
   - 同时移除 `--follow` 示例（v1.0 把 `--follow` defer 到 v1.1，运行时会打 stderr 提示，docs 示例不该这么写）
   - 在节末加 1–3 句话说明 path 模式 vs unit 模式的取舍：path 适合应用日志（nginx error.log 等），systemd-managed 服务用 `--unit`

2. **可选**：在 `references/common_issues.md` 既有 `journalctl -u <svc>` 处加一行 cross-ref，指向 `tail_log --unit`。若该节自洽不加也行——评审时确认。

## Out-of-scope

- 修 `tail_log.py` 的 `--follow` 实现（属于 v1.1 roadmap）
- 改 `tail_log.py` 的 path 模式失败 UX（已被 commit 268da29 解决，对应 Finding #2）
- 重写整个 SKILL.md 章节 / references 大改版
- 在其他 skill 文档（ssh-core、docker-quick 等）做类似引导

## Constraints

- 不改任何 .py 文件
- 保持现有 path 模式示例（`/var/log/nginx/error.log`）—— 它在 systemd 发行版上仍然合理（nginx error log 不进 journald）
- 文档风格与 SKILL.md 现有 voice 一致（简洁、动机驱动、不要复读 man page）

## Acceptance Criteria

- [ ] `SKILL.md` Log surgery 节不再单独示例 `/var/log/syslog`（或若保留，旁注明确说明 systemd-only 发行版无此文件）
- [ ] `SKILL.md` Log surgery 节包含至少一个 `--unit <service>` 示例
- [ ] `SKILL.md` 移除 `--follow` 示例（v1.0 尚未实现）
- [ ] `SKILL.md` Log surgery 节有 1–3 句话解释 path 模式 vs unit 模式的取舍
- [ ] 现有的 path 模式 nginx 示例（第 73 行）保留可用
- [ ] 关联 spec / references 一致：如改 `references/common_issues.md`，cross-ref 准确指向 `tail_log --unit`
- [ ] `grep -n syslog plugins/remote-debug/skills/remote-debug/SKILL.md` 不再返回未加注解的 `/var/log/syslog` 示例
- [ ] 现有 `pytest tests/` 基线仍全绿（应该天然无影响，因为只动 .md）

## Notes

- 父任务 `05-24-e2e-validate-skills-on-pai` 的 `validation-report.md` Finding #3 是本任务的源 trigger，PR/commit message 应 reference Finding #3
- Lightweight 任务，PRD-only 即可，无需 design.md / implement.md
