# 远程访问指南

> 服务运行在学校 A800 服务器容器内（监听 `0.0.0.0:8000`），
> 集群无公网 Web 入口，**本地浏览器无法直接访问**，需按以下方式之一转发端口。

## 先确认服务状态（在服务器上执行）

```bash
curl http://127.0.0.1:8000/api/v1/health     # 应返回 {"status":"ok", ...}
curl http://127.0.0.1:8000/api/v1/health/worker  # Redis/队列状态
```

如果服务器上也不通，重启服务：

```bash
cd /data/home/2024090904028/reconstration/3d-reconstruction-system
nohup bash scripts/run_local_backend.sh > logs/backend.log 2>&1 &
nohup bash scripts/run_worker.sh      > logs/worker.log 2>&1 &
```

## 方式一：SSH 本地端口转发（推荐，Windows/Mac/Linux 通用）

1. 连接学校 VPN，确保学校允许 SSH 本地端口转发。
2. 在**本机**终端使用平时连接该容器的 SSH 配置。保留原命令的 `-p`（SSH 端口）、`-J`（跳板）或 SSH 主机别名，不要用 Docker 内网 IP 替换登录地址：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:8000 <SSH主机别名或用户名@服务器地址>
```

3. 保持该终端打开。先在浏览器访问 **http://127.0.0.1:18000/api/v1/health**，看到 `"status":"ok"` 后打开 **http://127.0.0.1:18000/**。

`18000` 是本机端口，`8000` 是容器内的应用端口。`-N` 不开启远程 shell，成功后终端保持运行通常是正常的。若平时 SSH 只登录宿主机、之后再用 `docker exec` 进入容器，上述远端 `127.0.0.1` 指的是**宿主机**，不是容器；此时请使用学校批准的容器 SSH 入口或端口映射，不要自行开放公网端口。

Windows 下 XShell/PuTTY：隧道 → 添加「本地 `127.0.0.1:18000` → 目标 `127.0.0.1:8000`」，SSH 连接须终止在运行服务的容器。

## 方式二：VS Code Remote-SSH（组员常用）

用 Remote-SSH 连上服务器后，VS Code 会自动检测 8000 端口并转发
（或手动：端口面板 → 转发端口 → 填 8000），然后本机访问 http://localhost:8000。

## 方式三：同一宿主机内的其他终端/容器

本容器 IP 为 `172.17.0.7`（Docker 内网，**容器重建会变化**）：
同一宿主机上其他容器或终端可临时访问 `http://172.17.0.7:8000`。

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 本机浏览器"127.0.0.1 拒绝连接" | 未建 SSH 隧道，访问的是本机回环 | 用方式一/二 |
| 隧道建立后连接被重置 | VPN 掉线 / SSH 断开 | 重连 VPN 与隧道 |
| 页面能开但任务一直 QUEUED | Worker 未启动 | `bash scripts/run_worker.sh` |
| 重建报 COLMAP 不可用 | `.env` 中 `COLMAP_BIN` 路径变化 | 确认 conda 环境路径 |

> 注意：不得共享 VPN 账号或未经授权对外暴露集群端口（指南 §9.5）。
