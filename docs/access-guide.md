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

1. 连接学校 VPN；
2. 在**本机**终端执行（把 `<服务器地址>` 换成实际 SSH 主机，用户名同 SSH 登录名）：

```bash
ssh -L 8000:127.0.0.1:8000 <用户名>@<服务器地址>
```

3. 保持该 SSH 窗口不断开，本机浏览器打开 **http://127.0.0.1:8000** 即完整系统
   （API 文档在 http://127.0.0.1:8000/api/docs）。

Windows 下 XShell/PuTTY：隧道 → 添加"本地 8000 → 目标 127.0.0.1:8000"。

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
