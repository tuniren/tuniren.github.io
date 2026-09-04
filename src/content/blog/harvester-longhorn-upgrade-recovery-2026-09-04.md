---
title: Harvester 升级后 Longhorn 存储与监控故障的完整恢复实践
description: 本文记录一次三节点 Harvester 集群升级后的综合故障恢复过程。故障同时涉及节点重装、Longhorn 磁盘 UUID 变化、Prometheus 卷文件系统异常、失效 Replica、BackingImage 缓存残留以及逻辑容量调度告警。
pubDate: 2026-09-04
tags:
  - harvester
  - longhorn
  - kubernetes
  - recovery
---

> 本文记录一次三节点 Harvester 集群升级后的综合故障恢复过程。故障同时涉及节点重装、Longhorn 磁盘 UUID 变化、Prometheus 卷文件系统异常、失效 Replica、BackingImage 缓存残留以及逻辑容量调度告警。
>
> 本文的重点不是复述命令，而是解释各类对象之间的关系、为什么某些看似直接的修复方式具有数据风险，以及如何通过分阶段检查点避免把控制面残留误判为磁盘数据。

## 1. 事件概要

### 1.1 集群环境

本次处理的集群由三个控制平面节点组成：

| Kubernetes 节点 | 地址 | 最终系统版本 | Kubernetes 版本 |
| --- | --- | --- | --- |
| `master-66-161` | `192.168.66.161` | `HCI v1.7.1-hci.2.1.1.223` | `v1.34.3+rke2r3` |
| `master-66-162` | `192.168.66.162` | `HCI v1.7.1-hci.2.1.1.223` | `v1.34.3+rke2r3` |
| `master-66-164` | `192.168.66.164` | `HCI v1.7.1-hci.2.1.1.223` | `v1.34.3+rke2r3` |

其中 `master-66-161` 在升级过程中使用 223 版本重新安装，然后重新加入集群。重装改变了该节点默认数据路径对应的 Longhorn 磁盘身份，但 Longhorn 控制面仍保留重装前的磁盘 UUID 和部分数据对象引用。

### 1.2 主要故障

故障并不是单一问题，而是由以下几类问题叠加形成：

1. Prometheus 主容器持续 `CrashLoopBackOff`，导致 Harvester 仪表盘集群指标和虚拟机指标不可用。
2. Prometheus 数据卷在 161 上以只读方式挂载，内核持续报告块设备越界访问。
3. 161 默认 Longhorn 磁盘路径中的本地 UUID 与控制面记录 UUID 不一致，触发 `DiskFilesystemChanged`。
4. 旧 UUID 下仍记录四个 Replica 和多项 BackingImage 相关对象，但重装后的本地目录中已经没有对应数据。
5. 162 的一块约 219 GiB 数据盘实际几乎为空，却因为孤立 BackingImage 的逻辑预留超过磁盘容量而触发 `DiskPressure`。

### 1.3 最终结果

恢复完成后的主要状态如下：

- 三个节点均为 `Ready`，版本一致。
- Prometheus 为 `3/3 Running`，数据卷重新创建并以 `ext4 rw` 挂载。
- 161 默认磁盘使用重装后真实 UUID 重新注册，状态为 `Ready=True`、`Schedulable=True`。
- 四个受旧 UUID 影响的卷均完成逐卷启动验证和第二副本重建。
- 旧 UUID 在 Disk、Replica、BackingImage、BackingImageDataSource 和 BackingImageManager 中的引用全部归零。
- 162 小容量磁盘的逻辑预留从约 248.9 GB 降至约 9.3 GB，恢复可调度。
- 整个过程中没有强制移除 finalizer，没有直接删除宿主机 Replica 文件，也没有批量清理虚拟机数据。

## 2. 为什么必须分阶段处理

Longhorn 的数据面和控制面由多类对象共同组成：

```text
Kubernetes PVC/PV
        |
        v
Longhorn Volume
        |
        +--> Engine --> frontend block device --> kubelet/CSI mount
        |
        +--> Replica CR --> replica directory on a Longhorn disk
        |
        +--> BackingImage --> immutable base image cache
                         |
                         +--> BackingImageDataSource
                         +--> BackingImageManager
```

节点重装后，同一个文件系统路径可能仍然存在，但它不再代表原来的 Longhorn 磁盘。Longhorn 使用 `longhorn-disk.cfg` 中的 UUID 识别磁盘身份，而不是只看路径。

如果仅为了消除告警而把新磁盘的 UUID 改回旧 UUID，会产生严重风险：控制面可能把一块空盘误认为原始数据盘，并尝试启动实际上不存在的 Replica 或 BackingImage。正确方法必须先识别旧对象、验证其他副本、重建必要冗余，再移除旧身份。

因此本次恢复始终遵守以下顺序：

1. 只读收集证据。
2. 确认数据副本实际存在的位置。
3. 停止持续访问故障卷的组件。
4. 每次只处理一个卷。
5. 每个卷恢复健康后再处理下一个卷。
6. 数据对象安全后再清理磁盘身份和缓存控制对象。
7. 最后恢复调度并执行全链路验收。

## 3. 仪表盘指标不可用的诊断

### 3.1 表面现象

Harvester 仪表盘中集群指标和虚拟机指标均不显示。初步检查监控命名空间时发现：

```text
prometheus-rancher-monitoring-prometheus-0   2/3   CrashLoopBackOff
```

Pod 内三个容器中：

- `config-reloader` 正常运行；
- `prometheus-proxy` 正常运行；
- `prometheus` 主容器持续崩溃。

同时，`rancher-monitoring-prometheus` Service 没有可用 Endpoint。这解释了为什么 UI 查询不到指标：查询入口存在，但后端 Prometheus 主进程没有通过就绪检查。

### 3.2 Prometheus 崩溃日志

Prometheus 主容器日志中的关键错误为：

```text
Error opening query log file
file=/prometheus/queries.active
err="open /prometheus/queries.active: input/output error"

panic: Unable to create mmap-ed active query log
```

这类错误不能简单解释为 Prometheus 配置错误。`queries.active` 位于 Prometheus 数据目录，错误类型是底层文件系统返回的 `EIO`。因此排查方向应从监控配置转向 PVC、CSI 挂载和 Longhorn 块设备。

### 3.3 故障卷

Prometheus 使用的 PVC 和 Longhorn Volume 为：

```text
PVC:
prometheus-rancher-monitoring-prometheus-db-prometheus-rancher-monitoring-prometheus-0

PV / Longhorn Volume:
pvc-1673b6a0-d09f-4891-ac5b-ff57c541e115

容量:
50 GiB
```

Longhorn 控制面显示：

```text
state: attached
robustness: healthy
currentNodeID: master-66-161
ownerID: master-66-161
numberOfReplicas: 2
FilesystemReadOnly: True
```

两个数据副本分别位于：

- `master-66-162`，磁盘 UUID `3537391a-5164-47fc-9892-d2d9636c1750`；
- `master-66-164`，磁盘 UUID `e053fd17-d98d-4ce2-b1de-3bbbea7449ef`。

也就是说，Prometheus 数据副本并不在 161 的异常默认盘上，但 Volume Engine 和 frontend 块设备运行在 161。问题发生在 161 上的 Longhorn endpoint、块设备或挂载链路，而不是“Prometheus 副本一定存放在异常默认盘”。

### 3.4 宿主机证据

161 上的挂载状态为：

```text
/dev/longhorn/pvc-1673... ext4 ro
```

访问 Prometheus 数据目录时出现：

```text
stat: Input/output error
ls: Input/output error
```

内核日志持续出现：

```text
prometheus: attempt to access beyond end of device
```

块设备与 ext4 超级块记录的容量表面一致：

```text
blockdev --getsize64: 53687091200
ext4 block count:      13107200
ext4 block size:       4096
```

两者均为 50 GiB。因此当时能够确认的是“文件系统或块映射访问异常”，但不能仅凭容量一致就证明文件系统没有损坏。

### 3.5 为什么 Longhorn healthy 仍然会 I/O error

Longhorn 的 `robustness=healthy` 主要表达 Engine 与所需 Replica 的连接和副本冗余状态。它不等价于：

- guest 文件系统一定可读写；
- ext4 元数据一定一致；
- frontend 块设备在宿主机侧没有异常；
- 应用数据目录一定可以完成 mmap 或随机读写。

这是本次故障的重要认识：存储健康检查必须跨越多个层次，不能只查看 Longhorn Volume 的颜色或 robustness 字段。

## 4. Prometheus 卷的恢复

### 4.1 方案选择

最初可以考虑的方案包括：

1. 停止 Prometheus 后离线执行 `e2fsck -n`，再决定是否修复；
2. 将卷迁移至其他节点，验证是否仍出现 I/O error；
3. 对卷建立保护快照后执行文件系统修复；
4. 重建 Prometheus PVC。

由于 Prometheus 数据属于可重新采集的监控历史，而不是虚拟机业务数据，最终明确授权重建 Prometheus 专用卷。该方案会丢失历史监控时序，但可以避免在异常文件系统上执行不可预测的写入式修复。

### 4.2 安全删除顺序

删除 PVC 前必须先停止 Prometheus，避免 kubelet 仍在访问挂载点：

```text
Prometheus CR replicas: 1 -> 0
Pod: removed
VolumeAttachment: 0
host mounts: 0
Longhorn Volume state: detached
```

只有满足以下三个条件后才删除 PVC：

1. Prometheus Pod 已消失；
2. Kubernetes VolumeAttachment 已消失；
3. Longhorn Volume 已进入 `detached`。

随后删除唯一目标 PVC。PV 的回收策略为 `Delete`，因此由 Kubernetes 和 Longhorn 控制器正常回收 PV 与旧 Volume，不需要手工删除 Replica 或宿主机文件。

### 4.3 新卷结果

恢复 Prometheus 副本数后，StatefulSet 通过原有 `volumeClaimTemplate` 创建新 PVC。新 Longhorn Volume 为：

```text
pvc-9915e06c-93af-4b11-becb-ea1c9f3d5cca
```

新卷结果：

- 容量 50 GiB；
- `state=attached`；
- `robustness=healthy`；
- 在 161、162、164 各有一个健康副本；
- 161 上以 `ext4 rw` 挂载；
- Prometheus Pod 为 `3/3 Running`，重启次数为 0。

### 4.4 监控链路验收

恢复后不仅检查 Pod Ready，还直接查询 Prometheus：

```text
Prometheus Server is Ready.
query: count(up)
result: 28

query: count(node_cpu_seconds_total)
result: 320
```

同时确认：

- Prometheus Service Endpoint 已恢复；
- `custom.metrics.k8s.io` 可访问；
- `kubectl top nodes` 能返回三个节点的 CPU 和内存；
- KubeVirt 相关采集目标全部为 `up`；
- Prometheus 中重新出现 56 个以 `kubevirt_` 开头的指标名。

当时所有虚拟机均处于 `Stopped`，集群中没有 VirtualMachineInstance，所以 VMI 运行态指标为空属于正常结果。不能把“没有运行中的 VMI 样本”误判为 KubeVirt 监控仍然故障。

## 5. 161 默认磁盘 UUID 不匹配

### 5.1 控制面与本地身份

异常磁盘名称和路径：

```text
disk name: default-disk-aa706bf94d83c884
path:      /var/lib/harvester/defaultdisk
```

Longhorn 控制面记录：

```text
diskUUID: 9e7521fd-eb56-4c58-9b41-1b0b8bcaf7b0
Ready: False
reason: DiskFilesystemChanged
```

重装后本地 `longhorn-disk.cfg`：

```json
{
  "diskName": "default-disk-aa706bf94d83c884",
  "diskUUID": "0d76ee59-5396-4cfa-8cdc-1e0b5184dd78",
  "diskDriver": "",
  "state": "ready"
}
```

这说明路径名称被保留，但磁盘身份发生变化。Longhorn 正确地拒绝把新磁盘当作旧磁盘。

### 5.2 本地数据核验

现场检查显示：

```text
/var/lib/harvester/defaultdisk/replicas:       0 个目录
/var/lib/harvester/defaultdisk/backing-images: 0 个数据文件
```

对应文件系统本身可正常读写，剩余空间充足。因此问题不是需要 `fsck` 的文件系统损坏，而是控制面保留了已经不存在的旧磁盘身份和对象引用。

### 5.3 为什么不能修改 longhorn-disk.cfg

直接把本地 UUID 改回 `9e7521fd...` 是错误方案，原因如下：

- 本地 Replica 数据已经不存在；
- 本地 BackingImage 文件已经不存在；
- 控制面仍可能把旧 Replica 视为可启动对象；
- 伪造 UUID 会破坏 Longhorn 对“磁盘是否还是原盘”的保护机制。

正确方案是先消除旧身份引用，然后删除旧磁盘记录，最后按本地真实 UUID 重新注册。

## 6. 四个受影响卷的逐卷恢复

### 6.1 旧 UUID 上的 Replica

旧 UUID 仍关联四个 Replica：

| Volume | 旧 Replica | 旧节点 | 旧本地数据 |
| --- | --- | --- | --- |
| `pvc-0a44619a-791f-4475-8a51-2e53c9eea20c` | `...r-371a67e1` | 161 | 不存在 |
| `pvc-49bbd948-6ebd-44f7-8d8d-ed0049f337a8` | `...r-ea8b433a` | 161 | 不存在 |
| `pvc-b7d5f033-1117-44f9-80bb-a328a4b6d832` | `...r-4d93a9d5` | 161 | 不存在 |
| `pvc-c989dcf0-6896-430f-9870-3c739bb25618` | `...r-54d864fe` | 161 | 不存在 |

四个卷在 162 上各保留一个实际 Replica。检查内容包括：

- Replica 目录存在；
- `volume.meta` 可以解析；
- Volume Size 与 Longhorn CR 一致；
- Head 文件逻辑大小正确；
- `Rebuilding=false`；
- `Error` 为空。

但是这些卷当时均为 detached，Longhorn 显示 `robustness=unknown`。这里必须注意：detached 状态下的 `unknown` 不等于副本损坏，也不能证明副本一定可启动。真正的验证必须通过 attach 后的 Engine replica mode 完成。

### 6.2 第一次 attach 为什么没有生效

最初直接修改 Longhorn Volume 的 `spec.nodeID`，但该字段很快被控制器清空，Volume 保持 detached。

原因是当前 Longhorn 版本使用独立的 `VolumeAttachment` CR 和 attachment ticket 管理挂载请求：

```text
apiVersion: longhorn.io/v1beta2
kind: VolumeAttachment
spec:
  attachmentTickets:
    <ticket-id>:
      id: <ticket-id>
      nodeID: master-66-162
      type: longhorn-api
      parameters:
        disableFrontend: "false"
```

直接修改 `Volume.spec.nodeID` 会被 AttachmentTicket 控制器的期望状态覆盖。正确做法是创建专用 ticket，并在恢复结束后删除该 ticket。

### 6.3 WaitForBackingImage

第一个卷首次恢复时出现：

```text
WaitForBackingImage=True
```

原因是控制器尝试同时启动旧 161 Replica，而该 Replica 依赖旧磁盘上的 BackingImage。旧磁盘身份和本地数据都已不存在，因此启动流程被阻塞。

处理过程：

1. 再次确认 161 旧 Replica 数据目录不存在；
2. 确认 162 存活 Replica 及其 BackingImage 文件存在；
3. 仅删除旧 UUID 对应的 Replica CR；
4. 等待 `WaitForBackingImage=False`；
5. 通过 attachment ticket attach 到 162；
6. 等待 Longhorn 从 162 副本重建第二副本；
7. 只有达到 `healthy`、两个 RW、零 ERR 后才移除 ticket；
8. 确认 Volume 正常 detached 后处理下一个卷。

### 6.4 为什么必须逐卷处理

每个卷开始处理时只有一个实际副本。批量删除旧 Replica 虽然表面上是在清理已经不存在的数据，但会同时让多个卷进入单副本恢复窗口。一旦 162 节点或对应磁盘发生问题，可能造成不可恢复的数据损失。

逐卷处理将风险窗口限制在一个卷内，并保证每个卷恢复冗余后再继续。

### 6.5 重建结果

前三个卷的第二副本很快在 161 的健康额外盘上完成。该健康磁盘 UUID 为：

```text
3c1ac296-9e6d-4888-94ad-7dbb45c4f44e
```

第四个 100 GiB 卷包含快照链，重建过程经历：

```text
10% -> 53% -> 90% -> 100%
```

最终四个卷都在 attach 状态下达到：

```text
robustness: healthy
RW replicas: 2
ERR replicas: 0
```

随后全部移除恢复 ticket 并正常 detached。恢复 ticket 最终数量为 0，旧 UUID 的 Replica 引用数量为 0。

## 7. BackingImage 与旧 UUID 清理

### 7.1 BackingImage 相关对象的职责

需要区分三个对象：

- `BackingImage`：描述不可变基础镜像及其在各磁盘上的期望缓存；
- `BackingImageDataSource`：负责导入、下载、恢复、克隆或从卷导出源数据；
- `BackingImageManager`：运行在节点和磁盘层面，管理实际 BackingImage 文件和跨节点同步。

磁盘重装后可能出现以下不一致：

- `BackingImage.spec.diskFileSpecMap` 仍包含旧 UUID；
- `BackingImage.status.diskFileStatusMap` 对旧 UUID 显示 unknown；
- DataSource 仍指向旧 UUID；
- Manager 已经不存在；
- 宿主机实际文件已经不存在。

### 7.2 先验证其他健康副本

清理旧 UUID 前，先检查受影响 BackingImage 在其他磁盘上的状态。业务相关镜像在其他磁盘具有多个 `ready` 副本，另有一个没有 Volume 或 VirtualMachineImage 使用、来源可重新下载的孤立缓存。

因此处理时只删除旧 UUID 的缓存映射，不删除仍被业务卷使用的 BackingImage。

### 7.3 DataSource 的迁移

旧 UUID 最终仍关联九个 DataSource。清理前逐项确认：

- 对应 BackingImage 是否仍被 Volume 使用；
- 其他磁盘是否有 `ready` 文件；
- 源类型是 upload、download、restore、clone 还是 export-from-volume；
- 是否仍有 Harvester VirtualMachineImage 资源引用。

其中三项 BackingImage 没有 Volume 和 VirtualMachineImage 引用，因此删除 BackingImage 后，其 DataSource 由 ownerReference 正常级联删除。

剩余六项 BackingImage 仍有健康数据文件。删除旧 DataSource 后，Longhorn 控制器自动在健康磁盘重新创建 DataSource：

| 新 DataSource 位置 | 节点 |
| --- | --- |
| `3c1ac296...` | 161 健康额外盘 |
| `cd3fe31...` | 162 默认盘 |
| `e053fd17...` | 164 默认盘 |

六项新 DataSource 最终均为 `ready`，旧 UUID DataSource 引用归零。

### 7.4 孤儿 PVC 与无引用 BackingImage

映射旧 DataSource 到 Volume、PV、PVC 和 VirtualMachine 后，发现：

- `gyh/vm-ssqiri` 已不存在；
- PVC `gyh/vm-ssqiri-disk-0-3vimi` 仍然 Bound；
- 对应 Longhorn Volume 为 `pvc-a5e8a1d9-e87f-4d08-a136-11c2b41a826f`；
- PVC 没有 OwnerReference；
- Volume 保留三个副本。

经单独授权后删除该孤儿 PVC，并由 `Delete` 回收策略正常删除 PV 和 Longhorn Volume。

同时删除三个无 Volume、无 Harvester VirtualMachineImage 引用的 BackingImage：

```text
vmi-55534452-6ad2-4a31-97b6-655ace87c5c1
vmi-967c4f54-2ac9-40d6-a99b-df29d85012cd
vmi-fe269fa5-518a-4287-bfc7-07dd395de1bd
```

这里没有把“历史 BackingImage 间接关联过某台 VM”直接等同于“该 VM 异常”。十台仍存在的 VM，其 PVC 均有健康副本且旧 UUID Replica 引用为 0，因此没有删除这些 VM。

## 8. 重新注册 161 默认磁盘

### 8.1 删除旧记录前的保护条件

只有在以下引用都为 0 后，才从 Longhorn Node 的 `spec.disks` 删除旧磁盘：

```text
old Replica references:             0
old BackingImage spec references:   0
old BackingImage status references: 0
old BackingImageManager references: 0
```

Longhorn webhook 正常接受磁盘删除请求，随后旧磁盘从 Node `spec` 和 `status` 中消失。

### 8.2 先禁用调度再注册

重新添加相同路径时，先设置：

```yaml
allowScheduling: false
path: /var/lib/harvester/defaultdisk
diskType: filesystem
```

控制器识别到本地真实 UUID：

```text
0d76ee59-5396-4cfa-8cdc-1e0b5184dd78
```

确认 `Ready=True` 且容量正确后，再把 `allowScheduling` 设置为 `true`。最终状态：

```text
Ready=True
Schedulable=True
allowScheduling=true
DiskFilesystemChanged events=0
```

这种“两阶段注册”方式可以避免控制器尚未确认磁盘身份时立即向该路径调度新数据。

## 9. 162 小容量盘的 DiskPressure

### 9.1 异常磁盘

```text
node: master-66-162
path: /var/lib/harvester/extra-disks/8402f3910fa344484de76a432703da2a
device: /dev/sdf
disk UUID: 61606acf-e121-42a3-ba36-ca7a4f88857b
```

该磁盘出现了容易误解的组合：

```text
spec.allowScheduling=true
status Ready=True
status Schedulable=False
reason=DiskPressure
```

### 9.2 allowScheduling 与 Schedulable 的区别

`spec.allowScheduling` 是管理员意图，表示是否允许 Longhorn 尝试调度。

`status.conditions[type=Schedulable]` 是控制器根据容量、保留空间和磁盘状态计算出的实际结果。

因此界面中的“允许调度”开关可以开启，但磁盘仍然因为 `DiskPressure` 实际不可调度。排障时必须同时查看 spec 和 status。

### 9.3 实际空间与逻辑预留

宿主机文件系统数据：

```text
容量:       235,152,510,976 字节，约 219 GiB
实际使用:   约 84 KiB
可用空间:   约 223 GB
Replica:    0
```

Longhorn 调度账本：

```text
storageMaximum:   235,152,510,976
storageScheduled: 248,928,845,824
over-provisioning percentage: 100
```

逻辑预留超过磁盘上限约 13.8 GB，因此控制器报告：

```text
Scheduling space condition failed:
ScheduledTotal is greater than ProvisionedLimit
```

这个告警与 `df` 的实际使用率无关。Longhorn 调度使用逻辑容量承诺，稀疏文件即使没有实际占用数据块，也会消耗调度账本。

### 9.4 逻辑预留来自哪里

该磁盘没有任何 Replica，全部预留来自十一项 BackingImage 缓存期望：

```text
vmi-10710851-4f20-4c13-9fb9-bc11a9b150dc
vmi-409c9d2f-73a6-4d80-b565-b9f6d82d0b0a
vmi-4b2b892a-7996-466e-add2-fef362c1b0d3
vmi-94eb6636-10f5-482c-aede-00370fd44bb0
vmi-af2de1be-5582-401b-a3f9-761790df21d7
vmi-b49d0279-0bc5-4fd1-b04e-c0692b3cd533
vmi-c2f4ac69-62b7-4c09-a535-9621cdd03555
vmi-c397bb4c-1c1f-4a6b-ae4b-5903ba4a8ab9
vmi-c60bd471-0c13-4a68-ae8e-fd237a4d2d4f
vmi-cb6c17d0-4590-48e5-879b-f2f8b9d0f891
vmi-e62c6496-c6be-423d-979d-c91f51e6e1bd
```

它们具有以下共同特征：

- 没有任何 Longhorn Volume 使用；
- 没有任何 Harvester VirtualMachineImage 引用；
- 没有 ownerReference；
- 在该磁盘上多数为 `failed`，个别为 `starting` 或无状态；
- 失败信息多为 `syncing file should be directly reused but failed`；
- 文件目录实际只有几十 KiB，但逻辑大小仍计入 `storageScheduled`。

### 9.5 为什么不能提高超分比例解决

提高 `storage-over-provisioning-percentage` 可以让告警暂时消失，但不能清理失效的 BackingImage 对象，也不能修复同步失败。这样做只是扩大磁盘可以承诺的逻辑容量，可能把真正的容量风险延后到业务写入阶段。

本次选择删除已经确认无任何业务引用的十一项孤立 BackingImage。

### 9.6 删除过程中的 finalizer 阻塞

十项 BackingImage 正常完成删除，最后一项：

```text
vmi-4b2b892a-7996-466e-add2-fef362c1b0d3
```

长时间停留在 Terminating。Longhorn Manager 日志说明：

```text
Waiting until backing image data source is cleaned before removing the finalizer
backing image data source status is ready not failed-and-cleanup
```

该 BackingImage 已无 Volume 和 VirtualMachineImage 引用，但其 DataSource 位于 164 且仍为 `ready`，控制器因此不移除 BackingImage finalizer。

处理方式不是直接 patch 掉 finalizer，而是显式删除该无引用 DataSource，让控制器按照正常依赖顺序完成清理。DataSource 删除后，BackingImage 自动结束 Terminating。

### 9.7 清理结果

最终：

```text
target BackingImages remaining: 0
target DataSources remaining:   0
storageScheduled:               9,316,798,464
scheduledReplicaCount:          0
scheduledBackingImageCount:     4
Ready:                          True
Schedulable:                    True
```

磁盘逻辑预留从约 248.9 GB 降至约 9.3 GB，DiskPressure 告警解除。

## 10. 现场排障命令模板

以下命令用于说明排障方法。执行前必须替换集群、命名空间、Volume、磁盘名称和 UUID，不得直接照搬现场对象 ID。

### 10.1 kubectl 环境

```bash
K=/var/lib/rancher/rke2/bin/kubectl
C=/etc/rancher/rke2/rke2.yaml

$K --kubeconfig="$C" get nodes -o wide
```

### 10.2 检查 Longhorn 磁盘两层状态

```bash
NODE=master-66-162
DISK=<longhorn-disk-name>

$K --kubeconfig="$C" -n longhorn-system \
  get nodes.longhorn.io "$NODE" -o json |
  jq --arg d "$DISK" '{
    spec: .spec.disks[$d],
    status: .status.diskStatus[$d]
  }'
```

必须同时检查：

- `.spec.disks[...].allowScheduling`；
- `.status.diskStatus[...].conditions`；
- `storageMaximum`；
- `storageAvailable`；
- `storageScheduled`；
- `scheduledReplica`；
- `scheduledBackingImage`。

### 10.3 检查本地磁盘身份

```bash
DISK_PATH=/var/lib/harvester/defaultdisk

jq . "$DISK_PATH/longhorn-disk.cfg"
findmnt -T "$DISK_PATH"
df -B1 "$DISK_PATH"
```

不要在没有完整引用分析的情况下修改 `longhorn-disk.cfg`。

### 10.4 查询某个 UUID 上的 Replica

```bash
DISK_UUID=<uuid>

$K --kubeconfig="$C" -n longhorn-system \
  get replicas.longhorn.io -o json |
  jq --arg u "$DISK_UUID" '[
    .items[] |
    select(.spec.diskID == $u) |
    {
      name: .metadata.name,
      volume: .spec.volumeName,
      nodeID: .spec.nodeID,
      state: .status.currentState,
      healthyAt: .spec.healthyAt,
      failedAt: .spec.failedAt,
      dataDirectoryName: .spec.dataDirectoryName
    }
  ]'
```

### 10.5 查询某个 UUID 上的 BackingImage

```bash
DISK_UUID=<uuid>

$K --kubeconfig="$C" -n longhorn-system \
  get backingimages.longhorn.io -o json |
  jq --arg u "$DISK_UUID" '[
    .items[] |
    select(
      (.spec.diskFileSpecMap[$u] // null) != null or
      (.status.diskFileStatusMap[$u] // null) != null
    ) |
    {
      name: .metadata.name,
      spec: .spec.diskFileSpecMap[$u],
      status: .status.diskFileStatusMap[$u]
    }
  ]'
```

### 10.6 查询 BackingImage 是否被 Volume 使用

```bash
BACKING_IMAGE=<backing-image-name>

$K --kubeconfig="$C" -n longhorn-system \
  get volumes.longhorn.io -o json |
  jq --arg n "$BACKING_IMAGE" '[
    .items[] |
    select(.spec.backingImage == $n) |
    {
      volume: .metadata.name,
      namespace: .status.kubernetesStatus.namespace,
      pvc: .status.kubernetesStatus.pvcName,
      workloads: .status.kubernetesStatus.workloadsStatus
    }
  ]'
```

### 10.7 检查 Harvester VMImage 引用

```bash
BACKING_IMAGE=<backing-image-name>

$K --kubeconfig="$C" \
  get virtualmachineimages.harvesterhci.io -A -o json |
  jq --arg n "$BACKING_IMAGE" '[
    .items[] |
    select(
      .status.storageName == $n or
      .metadata.name == $n
    ) |
    {
      namespace: .metadata.namespace,
      name: .metadata.name,
      displayName: .spec.displayName,
      storageName: .status.storageName
    }
  ]'
```

### 10.8 检查 Longhorn AttachmentTicket

```bash
VOLUME=<volume-name>

$K --kubeconfig="$C" -n longhorn-system \
  get volumeattachments.longhorn.io "$VOLUME" -o yaml
```

当前版本进行手工 attach 时，应通过 attachment ticket，而不是反复直接修改 Volume `spec.nodeID`。

### 10.9 检查 Prometheus 数据路径

```bash
POD=prometheus-rancher-monitoring-prometheus-0

$K --kubeconfig="$C" -n cattle-monitoring-system \
  logs "$POD" -c prometheus --previous --tail=120

findmnt -rn -S /dev/longhorn/<volume-name> \
  -o TARGET,SOURCE,FSTYPE,OPTIONS

dmesg -T | grep -E \
  'prometheus|attempt to access beyond end of device|I/O error|EXT4-fs'
```

## 11. 有副作用操作的保护模板

本节不是可直接执行的脚本，而是说明每类变更前必须满足的保护条件。

### 11.1 删除故障 Replica 前

必须确认：

- Replica 的 `diskID` 精确等于旧 UUID；
- Volume 当前 detached，或者 Engine 已明确使用其他 RW 副本；
- 旧 `dataDirectoryName` 在宿主机路径确实不存在；
- 至少有一个其他节点上的 Replica 目录和元数据存在；
- 删除对象名称经过精确匹配，不使用模糊选择器批量删除。

### 11.2 删除孤儿 PVC 前

必须确认：

- 原 VirtualMachine 已不存在；
- PVC 未被 Pod、VM 或其他控制器使用；
- PVC 对应的 PV 与目标 Longhorn Volume 一致；
- Volume 已 detached；
- VolumeAttachment ticket 数量为 0；
- 明确接受删除 PVC 后的永久数据丢失。

### 11.3 删除 BackingImage 前

必须确认：

- Longhorn Volume 引用数为 0；
- Harvester VirtualMachineImage 引用数为 0；
- 不存在需要保留的上传源；
- 删除范围是精确对象列表；
- 不通过强制删除或移除 finalizer 绕过控制器。

### 11.4 删除旧磁盘记录前

必须确认旧 UUID 的以下引用均已处理：

```text
Replica
BackingImage.spec.diskFileSpecMap
BackingImage.status.diskFileStatusMap
BackingImageDataSource.spec.diskUUID
BackingImageManager.spec.diskUUID
Longhorn Node status.diskStatus
```

## 12. 不应采用的处理方式

### 12.1 把新磁盘 UUID 改回旧 UUID

这会绕过 Longhorn 的磁盘更换保护，并可能把不存在的数据报告为可用。

### 12.2 直接删除 PVC 来修复所有 I/O error

Prometheus 卷可以在明确接受历史指标丢失后重建，但虚拟机卷不能套用同一策略。必须先区分可再生数据与业务数据。

### 12.3 批量删除所有 stopped Replica

detached 卷的正常 Replica 也可能显示 stopped。`stopped` 是进程状态，不是数据失效结论。

### 12.4 看到 robustness=unknown 就判定卷损坏

Longhorn detached 卷通常显示 unknown。应通过实际 Replica 目录、元数据以及受控 attach 后的 RW/ERR 模式判断。

### 12.5 提高磁盘超分比例掩盖 DiskPressure

当逻辑预留来自孤立对象时，提高超分比例只会掩盖账本污染。应先查明 `scheduledReplica` 和 `scheduledBackingImage` 的来源。

### 12.6 强制删除 finalizer

finalizer 表示控制器仍有依赖资源或数据清理工作。应先查看控制器日志，删除真正阻塞的子对象。只有控制器完全失效且完成独立数据保护后，才可能讨论手工处理 finalizer；本次没有使用该方法。

## 13. 通用排障方法论

### 13.1 从症状沿数据路径向下追踪

对于仪表盘指标缺失，应按以下顺序定位：

```text
UI 查询
  -> Prometheus Service/Endpoint
  -> Prometheus Pod readiness
  -> Prometheus container logs
  -> PVC/PV
  -> Longhorn Volume/Engine
  -> CSI mount
  -> host block device and filesystem
  -> Replica and disk
```

每一层都要有证据，避免在 UI、Kubernetes 或 Longhorn 任意一层过早下结论。

### 13.2 区分控制面引用和物理数据

本次多次出现“CR 说存在，但磁盘上不存在”的情况。判断对象能否删除时，需要同时检查：

- Kubernetes/Longhorn CR；
- ownerReference 和 finalizer；
- Volume/VM 业务引用；
- 宿主机目录和文件；
- 其他节点上的冗余副本。

只检查其中一项都不足以支持破坏性操作。

### 13.3 用终态而不是命令成功判断完成

例如：

- `kubectl delete` 返回并不等于 Longhorn 数据已经清理；
- Replica CR 创建不等于重建完成；
- Pod Running 不等于 Prometheus 可以查询；
- 磁盘 `allowScheduling=true` 不等于实际 `Schedulable=True`。

本次每个阶段都使用实际终态作为完成标准。

## 14. 最终验收清单

### 集群与监控

- [x] 三个 Kubernetes 节点 Ready
- [x] 三个节点版本一致
- [x] Prometheus `3/3 Running`
- [x] Prometheus 无新增 I/O error
- [x] Prometheus Service Endpoint 存在
- [x] PromQL 可以返回节点指标
- [x] KubeVirt targets 为 up

### 161 存储

- [x] 默认磁盘使用真实新 UUID
- [x] `Ready=True`
- [x] `Schedulable=True`
- [x] `DiskFilesystemChanged` 当前事件为 0
- [x] 旧 UUID Replica 引用为 0
- [x] 旧 UUID BackingImage 引用为 0
- [x] 旧 UUID DataSource 引用为 0
- [x] 旧 UUID Manager 引用为 0
- [x] 四个受影响卷均完成两个 RW 副本验证
- [x] 所有恢复 attachment ticket 已删除

### 162 存储

- [x] `/dev/sdf` 文件系统可读写
- [x] 实际空间充足
- [x] 十一个孤立 BackingImage 已删除
- [x] 对应 DataSource 已删除
- [x] `storageScheduled` 从约 248.9 GB 降至约 9.3 GB
- [x] `Ready=True`
- [x] `Schedulable=True`

## 15. 结论

本次故障的核心不是单一硬盘损坏，而是节点重装后多个控制面状态与实际数据状态发生分离：

- 旧磁盘 UUID 仍被 Longhorn 对象引用；
- 实际磁盘已经生成新 UUID；
- 部分 Replica 只剩控制面记录；
- BackingImage 缓存期望造成逻辑容量污染；
- Prometheus 卷在 161 frontend 路径上出现只读和 I/O error；
- detached 状态与历史字段使部分对象看起来仍然健康。

可靠恢复依赖于三项原则：

1. 先证明数据在哪里，再删除控制面残留；
2. 每次只恢复一个失去冗余的卷，并以两个 RW 副本作为检查点；
3. 让控制器按正常依赖关系完成清理，不通过伪造 UUID、强制删除或移除 finalizer 获得表面上的绿色状态。

这套方法同样适用于 Harvester 节点重装、磁盘更换、Longhorn DiskFilesystemChanged、BackingImage 同步失败、孤儿 PVC 以及“磁盘实际为空但仍 DiskPressure”等场景。
