# Image optimization — make Docker images smaller and builds faster

Smaller images: pull 更快、安全攻击面更小、部署快、CI cache 命中率高。
Faster builds: 写代码反馈快、CI 成本低、迭代密。

## Layer 思维模型

每条 `RUN` / `COPY` / `ADD` 都生成一层。镜像 size = 所有层 size 之和。**层删除不可回收**：在第二层 `rm` 第一层加的文件，第一层仍占空间。

```dockerfile
# ❌ 多余的层：先装再删，删了也没用
RUN apt-get update && apt-get install -y curl
RUN curl -o /tmp/big.tar https://...
RUN tar xf /tmp/big.tar -C /opt
RUN rm /tmp/big.tar       # 第 2 层的 big.tar 仍在镜像里
```

```dockerfile
# ✅ 同层做完
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && curl -o /tmp/big.tar https://... \
    && tar xf /tmp/big.tar -C /opt \
    && rm /tmp/big.tar \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*
```

## 优化检查表（image_audit.py 会逐项检查）

### 1. 基础镜像选小的
| 镜像 | size |
|---|---|
| `ubuntu:22.04` | ~75 MB |
| `debian:bookworm-slim` | ~75 MB |
| `python:3.13` | ~990 MB |
| `python:3.13-slim` | ~150 MB |
| `python:3.13-alpine` | ~50 MB |
| `distroless/python3-debian12` | ~25 MB |

Alpine 注意：musl libc 与 glibc 不兼容，部分 Python wheel / Node native module 装不上。distroless 没 shell，调试需要 debugger image。

### 2. Multi-stage build

把 build 工具与 runtime 分离。runtime 镜像里不要有 gcc / make / npm。

```dockerfile
FROM node:20 AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
```

最终镜像不含 node_modules、不含 npm —— 只剩 nginx + 静态文件。

### 3. `.dockerignore` 必须有

没有 `.dockerignore` 时，`COPY . .` 会把 `.git/`、`node_modules/`、`__pycache__/`、本地 `.env` 文件全打进去。

最小可用 `.dockerignore`:
```
.git
.gitignore
node_modules
__pycache__
*.pyc
.venv
venv
.env
.env.*
.vscode
.idea
.DS_Store
target/
dist/
build/
*.log
```

### 4. BuildKit cache mount

对包管理器缓存目录用 `--mount=type=cache`：

```dockerfile
# syntax=docker/dockerfile:1.6
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    apt-get update && apt-get install -y curl

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt
```

cache mount 是 host 侧持久化的，不进镜像。

### 5. 把变化少的层放前面

Dockerfile 从上到下，被改的层之下的所有层都重新跑。`COPY . .` 放后面：

```dockerfile
# 依赖文件先 COPY 再 install —— 改业务代码不会重装依赖
COPY package*.json ./
RUN npm ci
COPY . .            # 这一层经常 cache 失效，但前面的层不会
```

### 6. 别在 root 用户运行

不算 size 优化，但属于"良好镜像"。`USER nobody` 或 `USER 1001`。

## 常见浪费 (image_audit.py 检测)

| 浪费 | 节省幅度 | 修法 |
|---|---|---|
| apt cache 留在层里（`/var/lib/apt/lists/*`）| 20–80 MB | `&& rm -rf /var/lib/apt/lists/*` |
| pip cache 留在层里（`~/.cache/pip`）| 10–200 MB | `pip install --no-cache-dir ...` |
| npm cache 留在层里（`~/.npm`）| 50–500 MB | `npm ci --no-audit` + multi-stage |
| .git 整个目录被 COPY 进去 | 视项目几十 MB–几 GB | `.dockerignore` 加 `.git` |
| 编译工具留在 runtime 镜像（gcc, make） | 100–500 MB | multi-stage |
| 静态资源没压缩 | 视情况 | 在 build 阶段就 minify / gzip |

## Layer 分析命令

```bash
# 看每层 cmd 和 size
docker history <image> --no-trunc

# 更友好（layer file tree + waste detection）
dive <image>                 # https://github.com/wagoodman/dive

# 对比两个版本
docker history <image>:v1
docker history <image>:v2
```

## 推送 / 拉取慢的额外考量

- 注册中心地理位置：跨区拉镜像总会慢。本地缓存或私有 mirror。
- 同时拉多层：默认 docker daemon 并发 3，可改 `--max-concurrent-downloads`。
- 镜像 manifest 用 schema 2（v2.2），支持并行；schema 1 已废弃。

## 安全相关（非 size，但相关）

`image_audit.py` 顺手报告：
- 镜像里有没有 `passwd` / `shadow` 写权限给非 root（不该）
- `/root/.ssh` / `*.pem` 等敏感路径是否进了镜像
- 是否 `USER root` 运行
- 是否带了 setuid binary

这些只是"提示"，不是替代专业扫描工具（trivy / grype / snyk）。
