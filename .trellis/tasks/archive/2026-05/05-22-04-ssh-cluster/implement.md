# Implement: ssh-core ssh_cluster v1.0

> 执行清单。Phase A → 实施 + 单元测试,Phase B → 集成测试 + 收官 commit。

---

## Phase A — ssh_cluster.py 实施

### A.1 实施(预计 1 文件,~ 450 行)

- [ ] **A.1.1** 替换 v0.2 stub 的 `from lib import unimplemented`,改为 `from lib import json_result, emit`。
- [ ] **A.1.2** `_ssh_execute_path()`:返回 `Path(__file__).resolve().parent / "ssh_execute.py"`(同目录,**不**用 parents[4])。
- [ ] **A.1.3** `_parse_targets(hosts_arg, tags_arg, env_arg)`:实现 design §2 — 去重保序 / 空白容忍 / 拒空 / 仅 tags-env 返 error。
- [ ] **A.1.4** `_per_host_env(...)`:固定 8 字段 schema(design §3)。
- [ ] **A.1.5** `_run_one(ssh_exec, alias, command, timeout, connect_timeout)`:照 design §3 实现 subprocess shell-out 给 ssh_execute,捕获 elapsed_ms。
- [ ] **A.1.6** `_health_check(ssh_exec, aliases, connect_timeout, max_workers)`:design §5 实现。
- [ ] **A.1.7** `_broadcast(ssh_exec, aliases, command, timeout, connect_timeout, max_workers, fail_fast)`:design §4 — 串行与并发分支 / fail-fast 在并发用 as_completed / 未启动用 cancel。
- [ ] **A.1.8** `_classify_top(results)` / `_summarize(results, started_at)`:design §6/§7。
- [ ] **A.1.9** `_format_human(data)`:每 host 一行表格 + summary 行。
- [ ] **A.1.10** `build_parser()`:照 design §8 保 v0.2 surface + 新 `--connect-timeout`;`--tags`/`--environment` 接受但 main 警告。
- [ ] **A.1.11** `main(argv=None)`:照 design §10 失败传播。
- [ ] **A.1.12** `ruff check` 0 violations on touched file。

**验证命令**:
```bash
uvx ruff check plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py
python plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py --help
```

### A.2 单元测试(~ 35-45 tests)

- [ ] **A.2.1** 新建 `tests/test_ssh_cluster_unit.py`,沿 `test_inspect_container_unit.py` 风格(`sys.path.insert` + `import ssh_cluster as sc`)。
- [ ] **A.2.2** target resolution(8):
  - 单 host / 多 host / 去重 / 空白容忍 / 空 / None / 仅 tags / 仅 environment
- [ ] **A.2.3** `_per_host_env`(2):字段全在 / failure_class 透传
- [ ] **A.2.4** `_run_one` mock(4):
  - happy:subprocess.run 返合法 envelope → per-host success
  - timeout:`subprocess.TimeoutExpired` → failure_class=timeout, exit 124
  - JSON broken:返非 JSON → failure_class=ssh_execute_broken
  - ssh_execute 内部失败:env.success=false → 透传 failure_class
- [ ] **A.2.5** `_broadcast`(7):
  - max_workers=1 串行保序
  - max_workers=4 N>4 hosts(mock 立即返,验调用次数)
  - fail-fast 串行:首失败后 skipped
  - fail-fast 并发:首失败后 cancel 未启动(用 monkeypatch + Event 控时序)
  - 全成功
  - 全失败
  - partial
- [ ] **A.2.6** `_health_check`(3):全通过 / 部分失败 / 全失败 → dead dict 包含 failure_class=health_check_failed
- [ ] **A.2.7** `_classify_top` + `_summarize`(4):各组合 + 空 results
- [ ] **A.2.8** `main()` E2E(8-10):
  - happy single host
  - happy multi host
  - 无 --hosts → exit 2
  - 仅 --tags → exit 2 + 警告
  - --max-workers 1 串行
  - --fail-fast + 部分失败
  - --health-check 一半通过
  - --json envelope schema 完整
  - ssh_execute missing → failure_class=ssh_execute_missing
  - argparse defaults
- [ ] **A.2.9** ruff 0 violations(避免 magic numbers 加 `# noqa: PLR2004`)

**验证命令**:
```bash
uvx ruff check tests/test_ssh_cluster_unit.py
uvx pytest tests/test_ssh_cluster_unit.py -v
```

---

## Phase B — 集成测试 + commit

### B.1 集成测试(3 tests)

- [ ] **B.1.1** 新建 `tests/test_ssh_cluster_integration.py`,marker `live_ssh`,沿 `test_compare_across_hosts_integration.py` 模式做 `_ssh_agent_has_key()` + `_host_reachable(POC_HOST)` 检测。
- [ ] **B.1.2** test 1:`--hosts pai,local "hostname" --json` → results 含 pai/local,stdout 各异;两 hostname 字符串非空 + differs。
- [ ] **B.1.3** test 2:`--hosts pai,no-such-alias-xyz "true" --json` → top success=false、failure_class=partial_failure;pai success / fake fail。
- [ ] **B.1.4** test 3:`--health-check --hosts pai,local "ls /" --json` → 两 host 都跑,results stdout 非空。

**验证命令**:
```bash
uvx pytest tests/test_ssh_cluster_integration.py -v -m live_ssh
uvx pytest -m "not live_ssh"
```

### B.2 收官 commit

```bash
git add plugins/ssh-core/skills/ssh-core/scripts/ssh_cluster.py \
        tests/test_ssh_cluster_unit.py \
        tests/test_ssh_cluster_integration.py
git commit -m "feat(ssh-core): ship ssh_cluster v1.0 with concurrent broadcast"
```

---

## Phase C — 收尾

### C.1 全套验证

- [ ] **C.1.1** `ruff check .` —— 整仓不退步(基线:8 errors)。
- [ ] **C.1.2** `pytest -m "not live_ssh"` —— 全套绿(预期 ≈ 440)。
- [ ] **C.1.3** `pytest -m live_ssh` —— 应 40/40(原 37 + 新 3)。
- [ ] **C.1.4** 在 PRD 上勾掉 23 个 AC。

### C.2 归档

- [ ] **C.2.1** `python3 ./.trellis/scripts/task.py archive 05-22-04-ssh-cluster`。
- [ ] **C.2.2** `git status` 确认 tree clean。

### C.3 推 origin

- [ ] **C.3.1** `git push origin main`(1 commit)。
- [ ] **C.3.2** 给用户报本刀完成 + v1.0 进度表。

---

## Rollback points

- A.1 完成但 A.2 大面积失败 → 不 commit;局部回滚到 ruff 通过 + tests 全过再继续。
- B.2 commit 后若 C.1.2 整仓 ruff 退步:`git commit --amend` 修补,避免新 commit 覆盖。
- B.1 集成测试 fail(pai 不通):**不阻塞** unit + commit;集成 fail 列入 known issue 后续解决。

## Validation matrix

| Phase | 命令 | 通过条件 |
|---|---|---|
| A.1 | `ruff check ssh_cluster.py` | 0 violations |
| A.1 | `python ssh_cluster.py --help` | v0.2 surface 完整 |
| A.2 | `pytest tests/test_ssh_cluster_unit.py -v` | 全绿 35+ |
| B.1 | `pytest -m live_ssh tests/test_ssh_cluster_integration.py` | 3/3 或部分 skip(若 pai 不通) |
| C.1 | `ruff check .` | baseline ≤ 8 errors |
| C.1 | `pytest -m "not live_ssh"` | ≈ 440 全绿 |
