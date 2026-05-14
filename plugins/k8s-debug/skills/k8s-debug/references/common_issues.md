# Common K8s issues — symptom → cause → diagnostic → remedy

按业内实战频率排序。每条带 `Symptoms / Causes / Diagnostic / Remedy / Prevention` 五段，符合既有 plugin 的 `common_issues.md` 模板。

---

## CrashLoopBackOff

> **Symptoms**: Pod `STATUS=CrashLoopBackOff`，`RESTARTS` 持续增长。
>
> **Common causes**（按频率）:
> 1. 应用进程启动后立刻退出（非零 exit code 或 `panic`）
> 2. liveness probe 太激进，进程没起完就被打死
> 3. OOMKilled：内存超 limit，被 kernel kill 后 K8s 重启
> 4. 配置错误：env / configmap / secret 缺失或字段错
> 5. 入口 script / command 写错（路径不存在、缺执行位）
>
> **Diagnostic**:
> ```bash
> kubectl -n <ns> describe pod <pod>            # Events 末尾通常给关键线索
> kubectl -n <ns> logs <pod> --previous          # 上一轮死前的 stdout/stderr
> kubectl -n <ns> logs <pod> --previous --tail=100 -c <container>
> kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -30
> ```
> `--previous` 必看 —— 当前容器是新启动的，没问题日志在上一轮。
>
> **Remedy**:
> - 应用退出 → 看 `--previous` 日志找根因
> - liveness 太激进 → `initialDelaySeconds` 调大、`failureThreshold` 调大
> - OOMKilled → 见下一条
> - 配置错 → `kubectl -n <ns> describe pod <pod>` 看 Events，"FailedMount" / "CreateContainerConfigError" 等
>
> **Prevention**: 启动延迟用 `startupProbe`（K8s ≥1.16）专门给慢启动应用；liveness 只检查"卡死"，不检查"启动中"。

---

## OOMKilled

> **Symptoms**: Pod 一段时间运行后突然死。`kubectl describe` 的 Last State 显示 `Reason: OOMKilled`，`Exit Code: 137`。
>
> **Common causes**:
> 1. 真有内存泄漏
> 2. limit 设太低（应用峰值高于 limit）
> 3. JVM / Go runtime 没看到 cgroup limit，按宿主机内存做堆配置
> 4. 临时大对象（载入大文件、加载大模型）
> 5. cgroup memory.max 比你以为的低（HostPath / 嵌套 cgroup）
>
> **Diagnostic**:
> ```bash
> kubectl -n <ns> describe pod <pod> | grep -A5 "Last State"
> kubectl -n <ns> top pod <pod> --containers     # 需要 metrics-server
> # cgroup 视角（容器内）
> cat /sys/fs/cgroup/memory.max
> cat /sys/fs/cgroup/memory.events
> ```
>
> **Remedy**:
> - 临时把 limit 上调（找到合适的常态后再调回）
> - JVM: `-XX:MaxRAMPercentage=75`；Go: `GOMEMLIMIT=$(awk ... /sys/fs/cgroup/memory.max)`
> - 持续内存增长 → 真泄漏，需要 pprof / VisualVM
>
> **Prevention**: requests/limits 经验上 limits 设为 request 的 1.5–2 倍，预留 burst。

---

## ImagePullBackOff / ErrImagePull

> **Symptoms**: Pod 卡在 `ImagePullBackOff` 或 `ErrImagePull`，从不进入 Running。
>
> **Common causes**:
> 1. 镜像 tag 拼错或不存在
> 2. 私有仓库 `imagePullSecrets` 没配 / 配错
> 3. 镜像仓库网络不通（节点出不去）
> 4. 镜像超过 docker hub rate limit
> 5. registry TLS 证书问题
>
> **Diagnostic**:
> ```bash
> kubectl -n <ns> describe pod <pod> | grep -A3 "Failed"
> # 在节点上手动拉一下
> ssh <node> "crictl pull <image>"  # containerd
> ssh <node> "docker pull <image>"  # dockerd（少见）
> ```
>
> **Remedy**:
> - tag/ name 错 → 改 Deployment / Pod spec
> - 仓库认证 → `kubectl -n <ns> create secret docker-registry ...` 再绑 `imagePullSecrets`
> - 网络不通 → 节点出网调试，往往是 NodePort/NAT/DNS 链路问题
>
> **Prevention**: 关键镜像本地镜像、私有仓库；CI 阶段就拉一遍 tag。

---

## Pending — never schedules

> **Symptoms**: 新建 Pod 卡 `Pending`，没分配节点。
>
> **Common causes**:
> 1. 没节点资源（CPU/memory request 累计超过空闲量）
> 2. nodeSelector / nodeAffinity / taints 没有匹配节点
> 3. PVC 卡住（StorageClass 没找到供应者）
> 4. ResourceQuota / LimitRange 阻挡
> 5. 调度器自己挂了（极少）
>
> **Diagnostic**:
> ```bash
> kubectl -n <ns> describe pod <pod> | tail -30      # Events 段
> kubectl get nodes -o wide
> kubectl describe nodes | grep -A5 "Allocated"
> kubectl get pvc -n <ns>
> ```
> `kubectl describe pod` 的 Events 段几乎总是直接告诉你原因（`0/3 nodes are available: 3 Insufficient memory`）。
>
> **Remedy**: 加节点 / 降 request / 改 affinity / 修 PVC。
>
> **Prevention**: 集群层 HPA + Cluster Autoscaler；PVC StorageClass 默认设好。

---

## Node NotReady

> **Symptoms**: `kubectl get nodes` 显示 `NotReady`，节点上的 Pod 开始 `NodeLost` / 重新调度。
>
> **Common causes**:
> 1. kubelet 挂了（systemd 看 kubelet.service）
> 2. 节点 OOM（内核杀进程，kubelet 是受害者之一）
> 3. 磁盘压力（kubelet eviction：disk pressure / memory pressure）
> 4. CNI 插件出问题
> 5. 容器运行时挂了（containerd / dockerd）
>
> **Diagnostic**:
> ```bash
> kubectl describe node <node> | tail -40            # Conditions + Events
> ssh <node> "systemctl status kubelet"
> ssh <node> "journalctl -u kubelet --since 10min --no-pager | tail -50"
> ssh <node> "dmesg -T | tail -30"
> ssh <node> "df -h /var /var/lib/containerd"
> ```
>
> **Remedy**: 重启 kubelet / 清磁盘 / 修 CNI / 替换节点。
>
> **Prevention**: 监控节点 PSI，监控 `kubelet_running_pods`，alert on `KubeletDown`。

---

## Service 没有 endpoints

> **Symptoms**: `kubectl get svc` 正常，`kubectl get endpoints <svc>` 显示空，Pod 间访问 503/connection refused。
>
> **Common causes**:
> 1. Service selector 与 Pod label 不匹配
> 2. Pod readinessProbe 失败（Pod 在但不算 Ready）
> 3. `targetPort` 端口号错
> 4. Pod 在不同 namespace
>
> **Diagnostic**:
> ```bash
> kubectl -n <ns> get svc <svc> -o yaml | grep -A5 selector
> kubectl -n <ns> get pods --show-labels | grep -i <selector-key>
> kubectl -n <ns> get pods -l <selector> -o wide
> kubectl -n <ns> describe pod <pod> | grep -A3 "Readiness"
> ```
>
> **Remedy**: 对齐 selector / label；修 readinessProbe；改 `targetPort`。
>
> **Prevention**: Helm chart / Kustomize 模板里 selector / label 用同一 var 驱动。

---

## Helm release stuck `pending-upgrade` / `pending-rollback`

> **Symptoms**: `helm status` 显示 `pending-*`，新一次 `helm upgrade` 报 "another operation in progress"。
>
> **Common causes**:
> 1. 上次 `helm upgrade` 被中断（CI runner 死了、网络断了）
> 2. Hook 卡住（pre-install / post-upgrade hook 的 Job 一直 Running 或 Failed）
> 3. 资源被外部修改导致 helm 看不懂状态
>
> **Diagnostic**:
> ```bash
> helm -n <ns> history <release>
> helm -n <ns> status <release>
> kubectl -n <ns> get jobs -l app.kubernetes.io/instance=<release>
> ```
>
> **Remedy**:
> - 修 hook：`kubectl -n <ns> delete job <stuck-job>`
> - 状态卡 pending：`helm -n <ns> rollback <release> <last-good-revision>`
> - 不可恢复：`helm -n <ns> uninstall <release>` + 重新 install（注意数据）
>
> **Prevention**: helm hook 设 `helm.sh/hook-delete-policy: hook-succeeded,before-hook-creation`；上线脚本 `helm upgrade --atomic --timeout 5m`。
