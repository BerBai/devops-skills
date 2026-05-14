# Container issues — symptom → cause → diagnostic → remedy

按实战频率排序。每条 5 段：Symptoms / Causes / Diagnostic / Remedy / Prevention。

---

## OOMKilled (Exit 137)

> **Symptoms**: `docker ps -a` 显示 `Exited (137)`，`docker inspect` 里 `State.OOMKilled: true`。
>
> **Common causes**:
> 1. `--memory` / `mem_limit` 设太低
> 2. 应用真有内存泄漏
> 3. JVM / Go runtime 没看到 cgroup limit，按宿主机内存做堆配置
> 4. 临时大对象（载入大文件、加载大模型）
> 5. 同宿主机其他容器把内存吃完了，本容器只是 OOM 选择的牺牲品（按 oom_score 排序）
>
> **Diagnostic**:
> ```bash
> docker inspect <name> | grep -A20 '"State"'
> docker inspect <name> --format '{{.State.OOMKilled}} {{.State.ExitCode}}'
> # 宿主机 dmesg —— 找 oom-killer 触发记录
> ssh <host> "dmesg -T | grep -A5 -i oom | tail -30"
> # 当前容器内存使用 (容器已死时不可用)
> docker stats --no-stream <name>
> ```
>
> **Remedy**:
> - 上调 limit：`docker run -m 1g`，Compose 里 `mem_limit: 1g`
> - JVM: `-XX:MaxRAMPercentage=75`；Go: `GOMEMLIMIT`
> - 真泄漏 → pprof / VisualVM 分析
>
> **Prevention**: 上线前 stress-test 测峰值；监控 `container_memory_working_set_bytes / memory.max`。

---

## SIGSEGV (Exit 139)

> **Symptoms**: `Exited (139)`，没有 OOMKilled。
>
> **Common causes**:
> 1. 应用自己 crash（C/C++ null ptr、Rust panic、Go SIGSEGV）
> 2. 二进制与运行时不匹配（glibc 版本、CPU 指令集）
> 3. 内存对齐 / 非对齐访问问题（ARM ↔ x86 移植场景）
> 4. 缺动态库
>
> **Diagnostic**:
> ```bash
> docker logs <name> --tail 100
> # 二进制兼容？
> docker run --rm <image> ldd /usr/local/bin/<binary>
> docker run --rm <image> file /usr/local/bin/<binary>
> # CPU 指令集
> docker run --rm <image> /usr/local/bin/<binary> --version  # often hints
> uname -m
> ```
>
> **Remedy**: 用对的镜像 platform (`--platform linux/amd64` / `linux/arm64`)；重新编译对齐目标 CPU；补缺失 lib。
>
> **Prevention**: 多架构镜像 `docker buildx build --platform linux/amd64,linux/arm64`；CI 跑 binary 在 alpine + ubuntu 都试一遍。

---

## Application exit (Exit 1 / 2 / 255)

> **Symptoms**: `Exited (1)` 等非 0 退出码，不是 OOM 也不是 SIGSEGV。
>
> **Common causes**:
> 1. 配置错（env / config file 缺失或字段错）
> 2. 启动依赖未就绪（DB 没起，应用启动失败退出）
> 3. 入口脚本 `set -e` 命中错误
> 4. healthcheck 失败被外部 supervisor 关停
>
> **Diagnostic**:
> ```bash
> docker logs <name> --tail 50
> docker inspect <name> --format '{{.Config.Cmd}} {{.Config.Entrypoint}}'
> docker inspect <name> --format '{{range .Config.Env}}{{println .}}{{end}}'
> ```
>
> **Remedy**: 看 logs 的最后几行，几乎都直接告诉你原因。
>
> **Prevention**: 应用启动加 retry/backoff；wait-for-it 脚本等依赖。

---

## Restart loop

> **Symptoms**: `STATUS` 显示 `Restarting (N)` 反复刷新，RestartCount 持续增长。
>
> **Common causes**: 上述 137/139/1 任意一种 + restart policy 设了 `always` / `unless-stopped` / `on-failure`。
>
> **Diagnostic**:
> ```bash
> docker inspect <name> --format '{{.State.RestartCount}} {{.State.Restarting}} {{.State.Status}} {{.State.ExitCode}}'
> docker logs <name> --tail 50           # 死前日志
> # K8s pod 类似情况：用 k8s-debug 而不是本 plugin
> ```
>
> **Remedy**: 修根因；调整 restart policy 用 `on-failure:5` 避免无限循环。
>
> **Prevention**: 启动失败时尽快显式退出码 != 0，配合 `restart_policy.condition: on-failure`。

---

## Healthcheck unhealthy

> **Symptoms**: `STATUS` 显示 `Up X minutes (unhealthy)`，容器在跑但被标记不健康。
>
> **Common causes**:
> 1. `HEALTHCHECK` 命令本身错（拼错 URL、依赖工具未装）
> 2. `start_period` 太短，应用没启动完就开始检查
> 3. interval / timeout 不合理
> 4. 应用真的不健康
>
> **Diagnostic**:
> ```bash
> docker inspect <name> --format '{{json .State.Health}}' | jq
> # 最近 5 次 probe 输出在 .State.Health.Log[]
> docker exec <name> <healthcheck-command>
> ```
>
> **Remedy**: 用 `docker exec` 跑同样的 healthcheck 命令，看实际输出。`start_period: 60s` 给慢启动应用宽限期。
>
> **Prevention**: healthcheck 用应用层 `/healthz` 端点而非 `ping`；start_period ≥ p95 启动时间。

---

## "bind: address already in use"

> **Symptoms**: `docker run` 或 `docker compose up` 报 `bind: address already in use` / `port is already allocated`。
>
> **Common causes**:
> 1. 同宿主机另一个进程在监听这个端口
> 2. 上一个容器没干净退出，端口还被持有
> 3. systemd-resolved / nginx / 其他服务占用了
>
> **Diagnostic**:
> ```bash
> ssh <host> "ss -tlnp | grep :<port>"
> ssh <host> "lsof -i :<port>"
> docker ps -a | grep <port>             # 是不是另一个容器
> ```
>
> **Remedy**: 改宿主机端口（推荐），或停掉占用者。
>
> **Prevention**: Compose 用 `${X_PORT:-默认}` 让端口可覆盖；CI 用 ephemeral 端口。

---

## "no space left on device"

> **Symptoms**: 任何容器操作报磁盘满。常见在 `/var/lib/docker` 满。
>
> **Common causes**: 镜像层堆积 / 容器 volume 写爆 / build cache 没清。
>
> **Diagnostic**:
> ```bash
> ssh <host> "df -h /var/lib/docker /var/lib/containerd"
> ssh <host> "docker system df"
> ssh <host> "docker system df -v"        # 详细
> ```
>
> **Remedy**（注意：本 plugin 不替你做，下面是给人看的）:
> ```bash
> docker system prune -af --volumes       # 慎重，会删未在用的卷
> docker builder prune -af
> ```
>
> **Prevention**: cron 跑 `docker system prune -f --filter "until=168h"`；监控 `/var/lib/docker` 用量。

---

## Mount failed / volume permission denied

> **Symptoms**: 启动报 `permission denied` 或挂载点空（应用看不到主机文件）。
>
> **Common causes**:
> 1. SELinux 标签不对（`:Z` / `:z` 没加）
> 2. UID 不匹配（容器内 UID 1000 vs 主机文件属主 0）
> 3. macOS Docker Desktop 没把目录共享给 VM
> 4. Bind mount 路径不存在 → docker 给你建了个空目录
>
> **Diagnostic**:
> ```bash
> docker inspect <name> --format '{{range .Mounts}}{{println .Source "→" .Destination .Mode}}{{end}}'
> ssh <host> "ls -la <source-path>"
> # SELinux
> ssh <host> "getenforce; ls -Z <source-path>"
> ```
>
> **Remedy**: 加 `:Z`（SELinux）、`chown` 主机文件、改 `user:` 字段、Docker Desktop Settings → Resources → File Sharing 加路径。
>
> **Prevention**: 镜像里跑非 root 用户时，准备好 `chown` 步骤或 init container 处理权限。
