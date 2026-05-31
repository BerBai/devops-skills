# Implement: docker-quick compose_status + image_audit v1.0

> 执行清单。先 compose_status,再 image_audit,各自单独 commit。

---

## Phase A — compose_status.py

### A.1 实施 compose_status.py(预计 1 文件,~ 550 行)

- [ ] **A.1.1** 用 [inspect_container.py](../../../plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py) 作模板,复制 inline helpers(`_envelope` / `_emit` / `_ssh_execute_path`)进 `compose_status.py`。
- [ ] **A.1.2** `_run_local(argv, timeout, cwd=None)`:在 inspect_container 版本基础上加 `cwd=` 参数透传给 `subprocess.run`。
- [ ] **A.1.3** `_run_via_ssh_execute(...)`:照搬 inspect_container 实现,字段名一致。
- [ ] **A.1.4** `_list_services(host, project_dir, connect_timeout, *, ssh_exec_override=None)`:
  - `local`:`_run_local(["docker", "compose", "ps", "--format", "json", "--all"], LIST_TIMEOUT, cwd=project_dir)`。
  - `remote`:command string `f"cd {shlex.quote(project_dir)} && docker compose ps --format json --all"`。
- [ ] **A.1.5** `_inspect_container_one(host, container_name, connect_timeout, *, ssh_exec_override=None)`:
  - 与 `inspect_container._inspect_one` 等价,但不带 `runtime` 参数(compose_status 限 docker)。
  - 写在本脚本里(**不** import 别处)。
- [ ] **A.1.6** `_parse_ps_output(stdout: str)`:实现 design §2 双格式解析。失败返 `(None, error_msg)`。
- [ ] **A.1.7** `_score_services(services, service_filter)`:实现 design §6 compose state 规则。返回 `(state, findings)`。
- [ ] **A.1.8** `_format_human(data)`:一屏摘要(host / project_dir / summary / 各服务 state / findings),与 inspect_container 风格一致。
- [ ] **A.1.9** `build_parser()`:照 [PRD R2](prd.md#r2-v02-argparse-向后兼容) 保留 v0.2 surface,加 `--connect-timeout`(默认 8)。
- [ ] **A.1.10** `main(argv=None)`:照 design §9 流程实现失败传播。

**验证命令**:
```bash
ruff check plugins/docker-quick/skills/docker-quick/scripts/compose_status.py
python plugins/docker-quick/skills/docker-quick/scripts/compose_status.py --help
```

### A.2 写 compose_status 单元测试(~ 30-35 tests)

- [ ] **A.2.1** 新建 `tests/test_compose_status_unit.py`,沿用 `test_inspect_container_unit.py` 的 importlib 加载模块 + monkeypatch subprocess.run 风格。
- [ ] **A.2.2** 覆盖测试矩阵(design §12):
  - `_envelope` / `_emit`(3-4)
  - `_parse_ps_output` 双格式 + 边界(8)
  - `_score_services` 各阈值(7)
  - `_list_services` local/remote 路由(6)
  - `_inspect_container_one`(2)
  - `main()` 8 个分支(8)
  - argparse 缺参 exit 2(1)
- [ ] **A.2.3** remote 路径测试用 `tmp_path` 写 fake `ssh_execute.py` placeholder,monkeypatch `_ssh_execute_path` 指向它。

**验证命令**:
```bash
ruff check tests/test_compose_status_unit.py
pytest tests/test_compose_status_unit.py -v
```

### A.3 写 compose_status 集成测试(2 tests)

- [ ] **A.3.1** 新建 `tests/test_compose_status_integration.py`,marker `pytest.mark.live_ssh`,沿 `test_inspect_container_integration.py` 模式做 `_runtime_available()` 自动 skip。
- [ ] **A.3.2** test 1:`tmp_path` 写 minimal `compose.yml`(单 busybox 服务 sleep 60),`docker compose -f <path> up -d`,跑 compose_status 验证 `services_total >= 1` 与 envelope 形状;teardown `docker compose -f <path> down`。
- [ ] **A.3.3** test 2:`compose_status local /no/such --json` → envelope `success=false`、`failure_class="remote_error"`。

**验证命令**:
```bash
pytest tests/test_compose_status_integration.py -v -m live_ssh   # only when local docker present
pytest -m "not live_ssh"                                        # ensure non-live suite still green
```

### A.4 commit

```bash
git add plugins/docker-quick/skills/docker-quick/scripts/compose_status.py \
        tests/test_compose_status_unit.py \
        tests/test_compose_status_integration.py
git commit -m "feat(docker-quick): ship compose_status v1.0 with docker compose ps"
```

---

## Phase B — image_audit.py

### B.1 实施 image_audit.py(预计 1 文件,~ 500 行)

- [ ] **B.1.1** 同 A.1.1:复制 inline helpers 进 `image_audit.py`。
- [ ] **B.1.2** `_run_local(argv, timeout)`:照 inspect_container 版(**无** cwd 参数)。
- [ ] **B.1.3** `_run_via_ssh_execute(...)`:照 inspect_container 版。
- [ ] **B.1.4** `_history_image(host, image, runtime, connect_timeout, ...)`:
  - `local`:`_run_local([runtime, "history", "--no-trunc", "--format", "{{json .}}", image], HISTORY_TIMEOUT)`。
  - `remote`:command string `f"{runtime} history --no-trunc --format '{{{{json .}}}}' {shlex.quote(image)}"`。
- [ ] **B.1.5** `_inspect_image(host, image, runtime, connect_timeout, ...)`:`[runtime, "inspect", "--format", "{{json .}}", image]`(local)或同形 remote。
- [ ] **B.1.6** `_parse_size(s: str)`:实现 design §4 解析,覆盖 `B/KB/MB/GB/TB` + 容错。
- [ ] **B.1.7** `_parse_history_output(stdout: str)`:严格 JSON Lines parse;每行 ≠ JSON 直接整体失败返 `(None, error)`。把 `Size` 字段解析为字节数,加进 layer dict 的 `size_bytes` 字段。
- [ ] **B.1.8** `_parse_inspect_output(stdout: str)`:单 dict parse,沿 inspect_container `_parse_inspect_stdout` 模式。
- [ ] **B.1.9** `_score_layers(layers, threshold_bytes)`:遍历 layers,生成 `large_layer` / `apt_cache_left` / `pip_cache_left` / `npm_cache_left` findings;状态聚合 ok/warn。
- [ ] **B.1.10** `_score_user(inspect_blob)`:返回 info-only findings list(单个 `running_as_root` 或空)。
- [ ] **B.1.11** `_format_human(data)`:一屏摘要(host / image / total_size_mb / layer_count / findings)。
- [ ] **B.1.12** `build_parser()`:照 PRD R2,加 `--runtime` + `--connect-timeout`。
- [ ] **B.1.13** `main(argv=None)`:照 design §10 流程实现。

**验证命令**:
```bash
ruff check plugins/docker-quick/skills/docker-quick/scripts/image_audit.py
python plugins/docker-quick/skills/docker-quick/scripts/image_audit.py --help
```

### B.2 写 image_audit 单元测试(~ 30-35 tests)

- [ ] **B.2.1** 新建 `tests/test_image_audit_unit.py`,沿 `test_inspect_container_unit.py` 风格。
- [ ] **B.2.2** 覆盖测试矩阵(design §12):
  - `_envelope` / `_emit`(2)
  - `_parse_size` 各单位 + 容错(9)
  - `_parse_history_output` JSON Lines + 边界(4)
  - `_parse_inspect_output`(3)
  - `_score_layers` 各阈值(6)
  - `_score_user` info-only(4)
  - `_history_image` + `_inspect_image` local/remote(4)
  - `main()` 6 个分支(6)
  - argparse 缺参 exit 2(1)

**验证命令**:
```bash
ruff check tests/test_image_audit_unit.py
pytest tests/test_image_audit_unit.py -v
```

### B.3 写 image_audit 集成测试(1-2 tests)

- [ ] **B.3.1** 新建 `tests/test_image_audit_integration.py`,marker `live_ssh`,`_runtime_available()` 自动 skip。
- [ ] **B.3.2** test 1:`docker pull busybox:latest`(若未在,亦可跳过),跑 `image_audit local busybox:latest --threshold-mb 1 --json` → envelope `success=true` 或 `warn`(busybox 很小,主要验证 parse),`data.layer_count >= 1`、`data.layers[0].size_bytes` 是 int。
- [ ] **B.3.3** test 2:`image_audit local nonexistent:tag-xyz --json` → envelope `success=false`、`failure_class="remote_error"`。

**验证命令**:
```bash
pytest tests/test_image_audit_integration.py -v -m live_ssh
pytest -m "not live_ssh"
```

### B.4 commit

```bash
git add plugins/docker-quick/skills/docker-quick/scripts/image_audit.py \
        tests/test_image_audit_unit.py \
        tests/test_image_audit_integration.py
git commit -m "feat(docker-quick): ship image_audit v1.0 with docker history layer scan"
```

---

## Phase C — 收官

### C.1 全套验证

- [ ] **C.1.1** `ruff check .` —— 整仓不退步(基线:8 errors)。
- [ ] **C.1.2** `pytest -m "not live_ssh"` —— 全套绿(预期 ≈ 340-360 tests)。
- [ ] **C.1.3** 若本机有 docker:`pytest -m live_ssh tests/test_compose_status_integration.py tests/test_image_audit_integration.py -v`。
- [ ] **C.1.4** 抽检 `python compose_status.py --help` / `python image_audit.py --help` 输出友好。
- [ ] **C.1.5** 在 PRD 上勾掉 25 个 AC。

### C.2 归档

- [ ] **C.2.1** `python3 ./.trellis/scripts/task.py archive 05-22-03-docker-compose-image` → 移到 `.trellis/tasks/archive/2026-05/`。
- [ ] **C.2.2** `git status` 确认 tree clean(`.trellis/` 在 .gitignore 内,archive 不会影响 git diff)。

### C.3 推 origin

- [ ] **C.3.1** `git push origin main`(2 commits)。
- [ ] **C.3.2** 给用户报本刀完成 + v1.0 进度表(docker-quick 3/3 收官)。

---

## Rollback points

- A.1 完成但 A.2 大面积失败 → 不要 commit;先在 `compose_status.py` 局部回滚到 ruff 通过 + tests 全过,再继续。
- A.4 commit 完但 B.1 严重偏离 design → 不影响 A 已 commit 的 compose_status;B 独立 commit,B 失败也不会 revert A。
- C.1 整仓 ruff 退步:在 B.1 commit 前补 ruff,若已 commit 用 `git commit --amend`(同一 commit 内修复,不要新 commit 复盖)。

## Validation matrix

| Phase | 命令 | 通过条件 |
|---|---|---|
| A.1 | `ruff check plugins/docker-quick/.../compose_status.py` | 0 violations |
| A.1 | `python compose_status.py --help` | 列出 v0.2 + 新 flag |
| A.2 | `pytest tests/test_compose_status_unit.py -v` | 全绿 30+ |
| A.3 | `pytest -m "not live_ssh"` | 不退步 |
| B.1 | `ruff check plugins/docker-quick/.../image_audit.py` | 0 violations |
| B.1 | `python image_audit.py --help` | 列出 v0.2 + 新 flag |
| B.2 | `pytest tests/test_image_audit_unit.py -v` | 全绿 30+ |
| B.3 | `pytest -m "not live_ssh"` | 不退步 |
| C.1 | `ruff check .` | baseline ≤ 8 errors |
| C.1 | `pytest -m "not live_ssh"` | 全套绿 ≈ 340+ |
