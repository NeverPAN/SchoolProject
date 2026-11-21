"""端到端全链路测试脚本（真实服务 + 真实数据）。

流程：注册 → 创建项目 → 上传数据集 → 创建重建任务 → 轮询状态 → 校验产物 → 下载模型。

用法:
  python scripts/e2e_test.py [--base-url http://127.0.0.1:8000] \
      [--dataset sample-data/smoke-30] [--images 30] [--mode colmap] [--no-dense]

前置: 后端与 Worker 已启动（scripts/run_local_backend.sh + scripts/run_worker.sh）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
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
    ap.add_argument("--experiment-name")
    ap.add_argument("--existing-job", type=int, help="只接管校验已有任务，不重新提交")
    ap.add_argument("--preset", default="fast")
    ap.add_argument("--matcher", default="auto")
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--out-dir", type=Path, default=Path("data/e2e-results"))
    ap.add_argument("--require-photo", action="store_true")
    ap.add_argument("--formal", action="store_true", help="Enforce the original-photo size requirements")
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

    if args.existing_job is not None:
        jid = args.existing_job
        existing = session.get(f"{base}/api/v1/jobs/{jid}", headers=headers, timeout=30)
        existing.raise_for_status()
        pid = existing.json()["project_id"]
        project = session.get(f"{base}/api/v1/projects/{pid}", headers=headers, timeout=30)
        project.raise_for_status()
        ok = project.json()["image_count"]
        print(f"核验已提交任务 #{jid}（项目 #{pid}）", flush=True)
    else:
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
        sizes = [p.stat().st_size for p in files_all]
        total_bytes = sum(sizes)
        if args.formal and not (len(sizes) >= 100 and 500_000_000 <= total_bytes <= 1_000_000_000
                                and min(sizes) >= 4_000_000):
            ap.error(f"原片不满足正式数据标准：{len(sizes)} 张，{total_bytes} 字节")
        print(f"待上传 {len(files_all)} 张，共 {total_bytes} 字节")
        ok, fail = 0, 0
        batch: list[tuple[str, bytes]] = []
        for i, p in enumerate(files_all):
            batch.append((p.name, p.read_bytes()))
            if len(batch) == 10 or i == len(files_all) - 1:
                files = [("files", (name, data, "image/jpeg")) for name, data in batch]
                r = session.post(f"{base}/api/v1/projects/{pid}/images", files=files, headers=headers)
                if r.status_code == 201:
                    payload = r.json()
                    accepted = payload if isinstance(payload, list) else payload["items"]
                    ok += len(accepted)
                    fail += len(batch) - len(accepted)
                else:
                    fail += len(batch)
                    print(f"  批次失败: {r.status_code} {r.text[:200]}")
                batch = []
                print(f"  进度: {ok}/{len(files_all)}")
        print(f"上传完成: 成功 {ok} / 失败 {fail}")
        detail = session.get(f"{base}/api/v1/projects/{pid}", headers=headers, timeout=30)
        detail.raise_for_status()
        uploaded = detail.json()
        if fail or ok < 3 or ok != len(files_all) or uploaded["image_count"] != ok or uploaded["total_image_bytes"] != total_bytes:
            print("上传数量或原片字节数不一致，停止验收")
            return 1

        # 5. 质量检查
        step("5. 图片质量检查")
        r = session.post(f"{base}/api/v1/projects/{pid}/quality-check", headers=headers)
        print(f"质量检查: {r.json()}")

        # 6. 提交重建任务
        step("6. 提交重建任务")
        r = session.post(
            f"{base}/api/v1/projects/{pid}/jobs",
            json={"experiment_name": args.experiment_name, "preset": args.preset, "matcher": args.matcher, "enable_dense": not args.no_dense, "enable_mesh": True},
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
    t0 = time.monotonic()
    last_stage = ""
    while time.monotonic() - t0 < args.timeout:
        try:
            r = session.get(f"{base}/api/v1/jobs/{jid}", headers=headers, timeout=15)
            if r.status_code in (502, 503, 504):
                raise requests.ConnectionError(f"HTTP {r.status_code}")
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as exc:
            print(f"轮询暂时断开，保留原任务 #{jid} 并重试：{exc}", flush=True)
            time.sleep(5)
            continue
        j = r.json()
        stage = j.get("current_stage") or "-"
        if stage != last_stage:
            print(f"  [{int(time.monotonic()-t0)}s] status={j['status']} stage={stage} progress={j['progress']:.0f}%")
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
    print(f"任务成功，总耗时 {time.monotonic()-t0:.0f}s")

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
    photo = next((m for m in models if m["stage_origin"] in ("PHOTO_TEXTURE", "PHOTO_TEXTURE_UPRIGHT")), None)
    if args.require_photo and (photo is None or not any(e["stage"] == "TEXTURING" and e["status"] == "completed" for e in events)):
        print("缺少在线照片贴图产物或成功阶段记录")
        return 1
    web_model = photo or next((m for m in models if m["model_type"] == "web"), models[0])
    for m in models:
        print(f"  #{m['id']} {m['model_type']:<20} {m['format']:<4} {m['file_size']/1024/1024:.1f} MB "
              f"顶点 {m['vertex_count']} 面 {m['face_count']} 点 {m['point_count']}")

    # 10. 下载模型
    step("10. 下载模型")
    r = session.get(f"{base}/api/v1/models/{web_model['id']}/download", headers=headers)
    r.raise_for_status()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"model_{web_model['id']}.{web_model['format']}"
    out.write_bytes(r.content)
    magic_ok = r.content.startswith(b"glTF" if web_model["format"] == "glb" else b"ply")
    if not magic_ok or len(r.content) != web_model["file_size"]:
        raise ValueError("下载文件的格式或长度不匹配")
    digest = hashlib.sha256(r.content).hexdigest()
    served = session.get(f"{base}/api/v1/models/{web_model['id']}/file", headers=headers, timeout=120)
    served.raise_for_status()
    if hashlib.sha256(served.content).hexdigest() != digest:
        raise ValueError("查看与下载端点返回了不同文件")
    if args.require_photo:
        magic, version, length, json_length, chunk = struct.unpack_from("<4sIII4s", r.content)
        if (magic, version, length, chunk) != (b"glTF", 2, len(r.content), b"JSON"):
            raise ValueError("GLB 文件头无效")
        gltf = json.loads(r.content[20:20 + json_length])
        if (not gltf.get("images") or not any("TEXCOORD_0" in p["attributes"] for m in gltf["meshes"] for p in m["primitives"])
                or any("uri" in x for x in gltf["images"] + gltf["buffers"])):
            raise ValueError("GLB 缺少内部照片纹理或 UV")
    print(f"已下载 {out}（{len(r.content)/1024/1024:.1f} MB, SHA-256 {digest}）")

    # 11. 模型版本
    r = session.get(f"{base}/api/v1/models/{web_model['id']}", headers=headers)
    versions = r.json().get("versions", [])
    print(f"模型版本: {['v' + str(v['version_number']) for v in versions]}")

    summary = {
        "project_id": pid,
        "job_id": jid,
        "images_uploaded": ok,
        "duration_s": round(time.monotonic() - t0, 1),
        "models": [{"id": m["id"], "type": m["model_type"], "size": m["file_size"]} for m in models],
        "download_file": str(out),
        "all_stages_ok": stages_ok,
    }
    (out_dir / f"e2e-summary-job{jid}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== E2E 全链路测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
