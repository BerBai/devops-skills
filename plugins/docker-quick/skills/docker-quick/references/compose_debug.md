# docker-compose debug

Compose 多服务编排的常见卡点。所有命令都假设你在 `docker-compose.yml` 所在目录，或显式 `-f path/to/compose.yml`。

## 状态速查

```bash
docker compose ps                       # 哪些 service 起了，状态
docker compose ps --format json | jq    # 适合 chain 到下一步
docker compose logs --tail 50 <svc>     # 一个 service 的日志
docker compose logs -f --since 5m       # 全 stack follow
docker compose config                   # 把 .yml + .env + override 全部合并后输出
docker compose top                      # 每个容器里跑的进程
docker compose events                   # 实时事件流（容器生死、health 变化）
```

`compose config` 是最被低估的命令 —— 当你怀疑 `.env` / `override.yml` / interpolation 出了问题，先看它最终生成的"实际配置"。

## depends_on 不等于"等服务就绪"

```yaml
services:
  api:
    depends_on:
      - db                      # ❌ 这只保证 db 容器进入 Running
  api2:
    depends_on:
      db:
        condition: service_healthy   # ✅ 这才等到 db healthcheck PASS
```

如果 db 容器起来了但 mysqld 还没接收连接，`api` 就启动并立即 fail。修法：给 db 写 healthcheck + `condition: service_healthy`，或在 `api` 入口加 wait-for-it 脚本。

```yaml
db:
  image: postgres:16
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U postgres"]
    interval: 5s
    timeout: 3s
    retries: 12
    start_period: 10s
```

## Healthcheck 失败定位

```bash
docker inspect <project>_<svc>_1 --format '{{json .State.Health}}' | jq
# 看 .Log[]，最近 5 次 probe 的输出
# 手动跑 healthcheck 命令
docker compose exec <svc> sh -c "<test-command>"
```

90% 的 unhealthy 原因：
1. `test:` 命令所需的工具镜像里没有（`curl` / `wget` / `nc` / `pg_isready`）
2. healthcheck 测的是 `127.0.0.1` 但服务 bind 在 `0.0.0.0:其他网卡`（容器内一般还好）
3. healthcheck 测的端点本身依赖未就绪（链式失败）

## 网络命名陷阱

Compose 把 service name 注入 DNS。`api` 想连 `db`，可以直接 `db:5432`。但有几个坑：

- service name 不能有下划线（DNS 不合规）。改用 `-`。
- 跨 stack（两个 compose project）通信需要显式 `networks:`，否则不在同一 bridge。
- 注释里的端口和实际 expose 端口要分清：`5432:5432` 是 host:container；只在 stack 内通信不需要 host 侧映射。

```bash
docker compose exec api sh -c "nslookup db; nc -zv db 5432"
docker network ls
docker network inspect <project>_default
```

## Volume 名空间

```yaml
volumes:
  data:                       # 实际叫 <project>_data
```

切项目目录 / 改 `COMPOSE_PROJECT_NAME` 后，volume name 会变，看着像数据丢了。

```bash
docker volume ls | grep data
docker volume inspect <project>_data
```

如果要保留特定名：
```yaml
volumes:
  data:
    name: my-app-data         # 显式锁定
```

## 端口冲突 / 已被占用

`compose up` 报 `port is already allocated`：

```bash
docker ps -a | grep ":<port>"          # 同 docker daemon 里其他容器
ssh <host> "ss -tlnp | grep :<port>"   # 其他系统进程
docker compose down                     # 把当前 stack 完全停掉再起
docker compose up --remove-orphans      # 清掉孤儿容器（旧 service 改名后留下的）
```

## override 文件优先级

```
compose.yml (base)
 → compose.override.yml (自动加载)
 → -f extra.yml (显式)
 → 命令行 -e / --env-file
```

后者覆盖前者。`compose config` 看最终结果。

## 资源限制不像 K8s 那样默认存在

`mem_limit` / `cpus` / `pids_limit` 不写就**无限制**。一个 service 跑飞可以拖死整个宿主。最小经验：

```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 2g
    reservations:
      memory: 256m
```

注意：`deploy:` 字段在 `docker compose up`（非 swarm）下**部分生效**。`mem_limit` 是 v2 兼容字段，两个都写最稳：

```yaml
mem_limit: 2g
deploy:
  resources:
    limits:
      memory: 2g
```

## 完整排查模板

当 `docker compose up` 起不来或某个 service 异常：

```bash
# 1. 配置渲染对了吗？
docker compose config

# 2. 各服务状态？
docker compose ps

# 3. 谁失败了？
docker compose ps --format '{{.Name}}: {{.State}} ({{.Status}})' | grep -vi 'running'

# 4. 失败者日志
docker compose logs --tail 100 <failing-svc>

# 5. 失败者 inspect
docker inspect <project>_<failing-svc>_1 | jq '.State, .Mounts, .NetworkSettings'

# 6. 失败者能 exec 进去吗？（如果还在 restart loop 时间窗口）
docker compose exec <failing-svc> sh
```

## Compose v1 vs v2

`docker-compose` (v1，独立 Python 工具) 已 EOL。`docker compose` (v2，docker CLI 子命令) 是当前。命令几乎一样，但：

- v2 默认用项目名 = 目录名 lowercase（v1 是带下划线的形式）
- v2 配置 schema 更严格，某些 v1 容忍的字段会报错
- v2 支持 `compose.yaml` 命名（不强制 `docker-compose.yml`）

如果在老服务器上看到 `docker-compose` 命令，是 v1，建议优先升级到 v2。
