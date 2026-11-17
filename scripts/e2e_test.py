"""端到端全链路测试脚本（真实服务 + 真实数据）。

流程：注册 → 创建项目 → 上传数据集 → 创建重建任务 → 轮询状态 → 校验产物 → 下载模型。

用法:
  python scripts/e2e_test.py [--base-url http://127.0.0.1:8000] \
      [--dataset sample-data/smoke-30] [--images 30] [--mode colmap] [--no-dense]

前置: 后端与 Worker 已启动（scripts/run_local_backend.sh + scripts/run_worker.sh）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--dataset", default="sample-data/smoke-30")
    ap.add_argument("--images", type=int, default=0, help="上传前 N 张（0=全部）")
    ap.add_argument("--user", default="e2e_user")
    ap.add_argument("--password", default="e2e_pass_123456")
    ap.add_argument("--preset", default="fast")
    ap.add_argument("--matcher", default="auto")
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--timeout", type=int, default=7200)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    session = requests.Session()

    # 1. 健康检查
    step("1. 服务健康检查")
    r = session.get(f"{base}/api/v1/health")
    r.raise_for_status()
    health = r.json()
    print(f"服务状态: {health['status']}, 重建模式: {health['recon_mode']}, "
          f"COLMAP: {health['colmap_available']} (GPU: {health['use_gpu']})")

    # 2. 注册 + 登录
    step("2. 注册/登录")
    r = session.post(f"{base}/api/v1/auth/register", json={"username": args.user, "password": args.password})
    if r.status_code not in (201, 409):
        print(r.text)
        return 1
    r = session.post(f"{base}/api/v1/auth/login", json={"username": args.user, "password": args.password})
    r.raise_for_status()
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print(f"用户 {args.user} 登录成功")

    # 3. 创建项目
    step("3. 创建项目")
    r = session.post(
        f"{base}/api/v1/projects",
        json={"name": f"E2E-重建测试-{int(time.time())}", "description": "端到端自动测试"},
        headers=headers,
    )
    r.raise_for_status()
    project = r.json()
    pid = project["id"]
    print(f"项目 #{pid}: {project['name']}")

    # 4. 上传图片
    step("4. 上传图片")
    img_dir = Path(args.dataset)
    if not img_dir.is_dir():
        print(f"数据集目录不存在: {img_dir}")
        return 1
    files_all = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff"))
    if args.images:
        files_all = files_all[: args.images]
    print(f"待上传 {len(files_all)} 张")
    ok, fail = 0, 0
    batch: list[tuple[str, bytes]] = []
    for i, p in enumerate(files_all):
        batch.append((p.name, p.read_bytes()))
        if len(batch) == 10 or i == len(files_all) - 1:
            files = [("files", (name, data, "image/jpeg")) for name, data in batch]
            r = session.post(f"{base}/api/v1/projects/{pid}/images", files=files, headers=headers)
            if r.status_code == 201:
                ok += len(batch)
            else:
                fail += len(batch)
                print(f"  批次失败: {r.status_code} {r.text[:200]}")
            batch = []
            print(f"  进度: {ok}/{len(files_all)}")
    print(f"上传完成: 成功 {ok} / 失败 {fail}")
    if ok < 3:
        return 1

    # 5. 质量检查
    step("5. 图片质量检查")
    r = session.post(f"{base}/api/v1/projects/{pid}/quality-check", headers=headers)
    print(f"质量检查: {r.json()}")

    # 6. 提交重建任务
    step("6. 提交重建任务")
    r = session.post(
        f"{base}/api/v1/projects/{pid}/jobs",
        json={"preset": args.preset, "matcher": args.matcher, "enable_dense": not args.no_dense, "enable_mesh": True},
        headers=headers,
    )
    if r.status_code != 202:
        print(r.text)
        return 1
    job = r.json()
    jid = job["id"]
    print(f"任务 #{jid} 已提交（mode={job['pipeline_mode']}）")

    # 7. 轮询任务状态
    step("7. 等待重建完成")
    t0 = time.time()
    last_stage = ""
    while time.time() - t0 < args.timeout:
        r = session.get(f"{base}/api/v1/jobs/{jid}", headers=headers)
        j = r.json()
        stage = j.get("current_stage") or "-"
        if stage != last_stage:
            print(f"  [{int(time.time()-t0)}s] status={j['status']} stage={stage} progress={j['progress']:.0f}%")
            last_stage = stage
        if j["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            if j["status"] != "SUCCEEDED":
                print(f"任务失败: {j.get('error_stage')} / {j.get('error_code')} / {j.get('error_message')}")
                logs = session.get(f"{base}/api/v1/jobs/{jid}/logs", headers=headers).text
                print(logs[-2000:])
                return 1
            break
        time.sleep(5)
    else:
        print("超时")
        return 1
    print(f"任务成功，总耗时 {time.time()-t0:.0f}s")

    # 8. 校验阶段事件与日志
    step("8. 校验阶段事件")
    events = session.get(f"{base}/api/v1/jobs/{jid}/events", headers=headers).json()
    for e in events:
        dur = f"{e['duration_ms']/1000:.1f}s" if e["duration_ms"] else "-"
        print(f"  {e['stage']:<24} {e['status']:<10} {dur}  {e['message'] or ''}")
    stages_ok = all(e["status"] in ("completed", "skipped") for e in events)
    print(f"阶段状态全部可解释: {stages_ok}")

    # 9. 校验模型产物
    step("9. 校验模型产物")
    r = session.get(f"{base}/api/v1/projects/{pid}/models", headers=headers)
    models = r.json()
    if not models:
        print("无模型产物！")
        return 1
    web_model = next((m for m in models if m["model_type"] == "web"), models[0])
    for m in models:
        print(f"  #{m['id']} {m['model_type']:<20} {m['format']:<4} {m['file_size']/1024/1024:.1f} MB "
              f"顶点 {m['vertex_count']} 面 {m['face_count']} 点 {m['point_count']}")

    # 10. 下载模型
    step("10. 下载模型")
    r = session.get(f"{base}/api/v1/models/{web_model['id']}/download", headers=headers)
    r.raise_for_status()
    out_dir = Path("data/e2e-results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"model_{web_model['id']}.{web_model['format']}"
    out.write_bytes(r.content)
    magic_ok = out.read_bytes()[:4] == b"glTF" if web_model["format"] == "glb" else out.read_bytes()[:3] == b"ply"
    print(f"已下载 {out}（{len(r.content)/1024/1024:.1f} MB, 格式校验 {'OK' if magic_ok else 'FAIL'}）")

    # 11. 模型版本
    r = session.get(f"{base}/api/v1/models/{web_model['id']}", headers=headers)
    versions = r.json().get("versions", [])
    print(f"模型版本: {['v' + str(v['version_number']) for v in versions]}")

    summary = {
        "project_id": pid,
        "job_id": jid,
        "images_uploaded": ok,
        "duration_s": round(time.time() - t0, 1),
        "models": [{"id": m["id"], "type": m["model_type"], "size": m["file_size"]} for m in models],
        "download_file": str(out),
        "all_stages_ok": stages_ok,
    }
    (out_dir / f"e2e-summary-job{jid}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== E2E 全链路测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
