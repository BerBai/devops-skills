# K8s networking — Service / Ingress / DNS / NetworkPolicy 排查

K8s 网络分三层：**集群内 (Pod ↔ Pod via Service)**、**集群入口 (Ingress / LoadBalancer)**、**DNS (CoreDNS)**。每层都有自己的卡点。

```
外网 → LoadBalancer / Ingress → Service → Endpoints → Pod
                                    ↑
                                  DNS (cluster.local)
```

## Service 层

### Service 有但没 endpoints

详见 `common_issues.md#service-没有-endpoints`。

```bash
kubectl get svc <svc> -n <ns>
kubectl get endpoints <svc> -n <ns>       # 这个为空 = 没 Pod 匹配 selector
kubectl get endpointslice -n <ns> | grep <svc>
```

### Service 有 endpoints 但仍不通

逐层往下排：

```bash
# 1. Pod 自己监听了吗？
kubectl exec <pod> -n <ns> -- ss -tlnp
# 2. Service ClusterIP 在集群里能 ping 吗？
kubectl run debug --image=nicolaka/netshoot -it --rm -- bash
  > nc -zv <svc>.<ns>.svc.cluster.local <port>
  > nc -zv <cluster-ip> <port>
# 3. iptables / IPVS 规则有吗？（看节点）
ssh <node> "iptables-save | grep <svc-name>"
ssh <node> "ipvsadm -L -n | grep <cluster-ip>"
# 4. kube-proxy 还活着吗？
kubectl get pods -n kube-system -l k8s-app=kube-proxy
ssh <node> "journalctl -u kube-proxy --since 10min --no-pager | tail -30"
```

最常见的"endpoints 有但不通"：
- Pod 自己 bind 在 `127.0.0.1` 而不是 `0.0.0.0`
- `targetPort` 写错（Service 指向 8080 但 Pod 监听 8000）
- NetworkPolicy 阻塞（见下）

## Ingress 层

### Ingress 配置完但 503 / 404

```bash
kubectl describe ingress <name> -n <ns>
# 看 Backend Path → Service / port 是否对得上
kubectl get svc -n <ns>
# Ingress controller 状态
kubectl get pods -n ingress-nginx           # 或 traefik / cilium
kubectl logs -n ingress-nginx <controller-pod> --tail=100
```

常见原因：
- Ingress backend Service 不存在或 name 拼错
- TLS secret 缺失或域名不匹配 → 浏览器看到 controller 默认 cert
- 注解错（rewrite / redirect 类的注解每个 controller 不一样）
- IngressClass 不匹配，Ingress 对象存在但没 controller 接管

### LoadBalancer EXTERNAL-IP 一直 `<pending>`

云上：cloud-controller 没有给分 LB（quota / 权限 / 不支持的 region）。

```bash
kubectl describe svc <svc> -n <ns>          # Events 段
kubectl logs -n kube-system cloud-controller-manager-...
```

裸机：装 MetalLB / kube-vip / Cilium 的 LB 模块；否则 LB 永远 pending。

## DNS 层

### Pod 解析失败 / 慢

```bash
# 进一个 Pod 测
kubectl run dns-debug --image=nicolaka/netshoot -it --rm -- bash
  > nslookup kubernetes.default
  > nslookup <svc>.<ns>.svc.cluster.local
  > dig +trace <external-host>
# CoreDNS 状态
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system <coredns-pod> --tail=100
# CoreDNS Configmap
kubectl get cm coredns -n kube-system -o yaml
```

DNS 失败的典型原因：
- CoreDNS Pod 数太少（默认 2 个，大集群要 HPA）
- ndots:5 + 短域名 → 多次失败查询。改 Pod `dnsPolicy: ClusterFirst` 或加 `search` 列表。
- 节点 conntrack 满 → UDP DNS 包丢
- 上游 DNS（forward 段指向 `/etc/resolv.conf`）不通

### DNS 在某些节点慢，其他正常

`conntrack -L | grep :53 | wc -l` 看是否爆。某些 kube-proxy 版本的 NAT 对 UDP DNS 不友好；可以用 `NodeLocal DNSCache` 缓解。

## NetworkPolicy 层

### 加了 NetworkPolicy 后流量被切断

NetworkPolicy 是 default-deny 风格 —— 一旦命中某个 namespace，没被任何 policy 允许的流量都被拒。

```bash
kubectl get networkpolicy -n <ns>
kubectl describe networkpolicy <name> -n <ns>
```

排查顺序：
1. 列出该 ns 所有 policy，看 `podSelector` 哪些 Pod 受影响
2. 看 `policyTypes`：Ingress / Egress 单方向还是双向
3. 看 `ingress.from` / `egress.to`：podSelector / namespaceSelector / ipBlock

```bash
# 命令行模拟流量（需要安装 calicoctl 或类似工具，否则只能"逻辑推"）
# Cilium 集群有 hubble，可以实时看：
hubble observe --from-pod <ns>/<src> --to-pod <ns>/<dst> --verdict DENIED
```

## CNI 层（罕见但致命）

Pod 起不来 / 拿不到 IP / 跨节点不通：

```bash
ssh <node> "ls /etc/cni/net.d/"
ssh <node> "journalctl -u kubelet --since 10min | grep -i cni"
# Calico
ssh <node> "calicoctl node status"
# Cilium
kubectl exec -n kube-system <cilium-agent> -- cilium status
# Flannel
ssh <node> "journalctl -u flanneld --since 10min"
```

CNI 通常静默工作；一旦出错 Pod 完全起不来或网卡飘移。检查节点的 CNI 二进制 + config 文件存在且匹配 K8s 版本。

## 速查：从症状到层

| 症状 | 优先看 |
|---|---|
| 浏览器访问外网域名 404 / 503 | Ingress controller + Ingress 配置 |
| 外网 LB IP 不响应 | cloud-controller / LB 后端注册 |
| Pod A 访问 Pod B 不通（同 ns） | Service endpoints + NetworkPolicy |
| Pod A 访问 Pod B 不通（跨 ns） | Service FQDN + NetworkPolicy + DNS |
| Pod 起不来 | CNI + ContainerCreating Events |
| 短域名解析失败 | CoreDNS + dnsPolicy + ndots |
| 完全随机的连接重置 | conntrack 满 + 节点 PSI |
