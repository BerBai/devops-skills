# ssh_cluster: surface sub-host stderr in default output

## Goal

`ssh_cluster` 默认（非 `--json`）输出在子 host 失败时只显示 `[FAIL]` 行和 `failure_class`，**吞掉了子 host 的 stderr**。用户必须重跑加 `--json` 才能看到失败原因（如 "local route does not support shell metacharacter '&&'"）。本任务修复该 observability gap：在 `[FAIL]` 行下方多缩进一行打印 stderr 头一行（截断 200 字符），保留 `--json` 行为不变。

来源：父任务 `.trellis/tasks/05-24-e2e-validate-skills-on-pai/validation-report.md` Finding #1。

## Requirements

1. **Default (human) 输出新增 stderr 续行**：
   - 触发条件：`env.get("success") is False` 且 `env.get("stderr")` 非空字符串
   - 渲染位置：紧跟在该 host 的行（即 `_format_human` 在 `lines.append(f"  [{tag}] ...")` 之后）
   - 格式：`"          stderr: " + first_line` —— 缩进 10 空格（对齐 `[FAIL] ` 之后内容），内容是 stderr 第一行
   - 截断：若该行 > 200 字符，截到 200 后追加 `"..."`；常量命名 `_STDERR_HEAD_CAP = 200`
   - 仅取第一行（`stderr.splitlines()[0]`，空 stderr 不输出该续行，全空白 stderr 也不输出）

2. **--json 输出完全不变**：`emit(json_result(...))` 路径不动；`stderr` 字段在 JSON 里早已存在。

3. **ok host 不受影响**：success=True 的 host 保留现有「单行 row + 头 stdout 摘要」行为，**不** 打印 stderr 续行（即使 stderr 偶尔有 noise，例如远端 motd —— ssh_execute 已经会过滤大部分 noise，剩下的 ok host stderr 不是用户关心点）。

4. **Unit 测试**：在 `tests/test_ssh_cluster_unit.py` 新增 1 个测试函数 `test_format_human_includes_stderr_on_fail`，至少覆盖：
   - 失败 host + 非空 stderr → 输出含 `stderr:` 续行
   - 失败 host + 空 stderr → 不含 stderr 续行
   - 失败 host + >200 字符 stderr → 续行末尾有 `...`
   - ok host + 非空 stderr → 不含 stderr 续行
   - 多行 stderr → 只展示第一行

## Acceptance Criteria

- [ ] `_format_human` 在 FAIL host 下方按规则打印 stderr 续行
- [ ] `--json` 输出（`data` payload、`emit(json_result(...))`）字节级保持不变
- [ ] 新增的 `test_format_human_includes_stderr_on_fail` 通过
- [ ] 仓库现有 483 + live_ssh 40 测试无回归
- [ ] 真机演练：`python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py --hosts pai,local 'hostname && uname -s'` 默认输出的 `[FAIL] local` 行下方能看到 stderr 续行，说明失败原因

## Out-of-scope

- 修改 `--json` payload 结构
- 给 ok host 显示 stderr
- 多行 stderr 全量打印
- 修改 `ssh_execute.py` 或更下层 SSH 层

## Notes

- 单文件变更：`plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py` 修 `_format_human`，加 1 个常量
- 测试单文件变更：`tests/test_ssh_cluster_unit.py` 加 1 个测试函数（约 30 行）
- 风险极低：纯输出文本格式调整，无 IO / 网络 / 并发改动
