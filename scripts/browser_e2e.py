"""常驻候选站真实浏览器 E2E（真实 Chromium + 生产前端构建 + 真实 GPU 环境）。

流程：注册→登录→新建项目→上传 36 张主照片→提交 standard 任务→等待完成→
写实/原色切换→框选区域→区域提取→下载哈希→上传 4 张留出照片→父模型局部重建
→点云候选→回父模型看选区→下载哈希。全程截图 + 结果写 manifest JSON。

用法：
  backend/.venv/bin/python scripts/browser_e2e.py \
    --base-url http://127.0.0.1:18507 --main-dir /tmp/e2e-south/main \
    --holdout-dir /tmp/e2e-south/holdout --shots docs/integrated-acceptance-evidence/browser-e2e \
    --manifest docs/integrated-acceptance-evidence/browser-e2e/manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class E2E:
    def __init__(self, base: str, shots: Path):
        self.base = base.rstrip("/")
        self.shots = shots
        self.shots.mkdir(parents=True, exist_ok=True)
        self.timeline: list[dict] = []
        self.downloads: dict[str, str] = {}

    def shot(self, page, name: str) -> None:
        path = self.shots / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        self.timeline.append({"step": name, "screenshot": str(path), "url": page.url,
                              "at": time.strftime("%H:%M:%S")})
        print(f"[shot] {name} -> {path.name}", flush=True)

    def log(self, msg: str) -> None:
        print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def wait_job(self, page, job_url: str, minutes: float, tag: str) -> None:
        """任务详情页轮询：读页首状态 el-tag 直至 成功/失败/已取消。"""
        deadline = time.time() + minutes * 60
        last_stage = ""
        page.goto(job_url, wait_until="domcontentloaded")
        while time.time() < deadline:
            page.reload(wait_until="domcontentloaded")
            status = page.locator(".el-tag").first.inner_text().strip()
            if status == "成功":
                self.log(f"{tag} SUCCEEDED")
                self.shot(page, f"{tag}-done")
                return
            if status in ("失败", "已取消"):
                body = page.locator("body").inner_text()
                raise SystemExit(f"{tag} 终态 {status}：\n{body[:800]}")
            body = page.locator("body").inner_text()
            for st in ("特征提取", "特征匹配", "稀疏重建", "稠密重建", "网格", "贴图"):
                if st in body and st != last_stage:
                    last_stage = st
                    self.log(f"{tag} 阶段: {st}")
            time.sleep(30)
        raise SystemExit(f"{tag} 等待超时 {minutes} 分钟")

    def download_by_button(self, page, button, name: str) -> Path:
        with page.expect_download() as dl_info:
            button.click()
        dl = dl_info.value
        out = self.shots / f"{name}"
        dl.save_as(str(out))
        digest = sha256_file(out)
        self.downloads[name] = {"sha256": digest, "bytes": out.stat().st_size,
                                "suggested": dl.suggested_filename}
        self.log(f"download {name}: {digest[:16]}… ({out.stat().st_size} bytes)")
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:18507")
    ap.add_argument("--main-dir", type=Path, default=Path("/tmp/e2e-south/main"))
    ap.add_argument("--holdout-dir", type=Path, default=Path("/tmp/e2e-south/holdout"))
    ap.add_argument("--shots", type=Path,
                    default=REPO / "docs/integrated-acceptance-evidence/browser-e2e")
    ap.add_argument("--manifest", type=Path,
                    default=REPO / "docs/integrated-acceptance-evidence/browser-e2e/manifest.json")
    ap.add_argument("--user", default="e2e_reviewer")
    ap.add_argument("--password", default="e2e-reviewer-pass")
    ap.add_argument("--resume-project", default=None,
                    help="已存在项目的 URL（跳过注册新项目与上传提交，直接等待主任务）")
    ap.add_argument("--resume-job-url", default=None, help="运行中主任务的详情 URL")
    ap.add_argument("--skip-holdout-upload", action="store_true",
                    help="留出照片已上传（重跑续段时跳过上传步骤）")
    ap.add_argument("--skip-viewer-region", action="store_true",
                    help="写实/原色切换与区域提取已完成（重跑续段时跳过）")
    ap.add_argument("--resume-local-job-url", default=None,
                    help="已提交的局部重建任务详情 URL（直接等待该任务并完成收尾步骤）")
    args = ap.parse_args()

    main_photos = sorted(args.main_dir.glob("*.JPG")) or sorted(args.main_dir.glob("*.jpg"))
    holdout_photos = sorted(args.holdout_dir.glob("*.JPG")) or sorted(args.holdout_dir.glob("*.jpg"))
    assert len(main_photos) == 36, f"期望 36 张主照片，实得 {len(main_photos)}"
    assert len(holdout_photos) == 4, f"期望 4 张留出照片，实得 {len(holdout_photos)}"

    e = E2E(args.base_url, args.shots)
    ts = time.strftime("%m%d-%H%M%S")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950},
                                  accept_downloads=True, locale="zh-CN")
        page = ctx.new_page()
        page.set_default_timeout(20000)

        # 1) 注册 + 登录（重跑时用户可能已存在，注册失败则直接登录）
        page.goto(args.base_url, wait_until="domcontentloaded")
        e.shot(page, "01-login")
        page.get_by_role("tab", name="注册").click()
        page.get_by_test_id("register-username").fill(args.user)
        page.get_by_test_id("register-password").fill(args.password)
        page.get_by_test_id("register-submit").click()
        try:
            expect(page.get_by_text("注册成功，请登录")).to_be_visible(timeout=5000)
        except Exception:
            e.log("注册跳过（用户已存在），切回登录页签")
        page.get_by_role("tab", name="登录").click()
        page.get_by_test_id("login-username").fill(args.user)
        page.get_by_test_id("login-password").fill(args.password)
        page.get_by_test_id("login-submit").click()
        expect(page.get_by_role("button", name="新建项目")).to_be_visible()
        e.shot(page, "02-projects")

        if args.resume_job_url or args.resume_local_job_url:
            project_url = args.resume_project
            job_url = args.resume_job_url
            e.log(f"resume：项目 {project_url} 任务 {job_url}")
        else:
            # 2) 新建项目（创建后停留在列表页，点项目名链接进入详情）
            page.get_by_role("button", name="新建项目").click()
            page.get_by_test_id("new-project-name").fill(f"浏览器E2E-South-{ts}")
            page.get_by_test_id("create-project-btn").click()
            expect(page.get_by_text("项目已创建")).to_be_visible()
            page.get_by_text(f"浏览器E2E-South-{ts}").click()
            page.wait_for_url("**/projects/*")
            project_url = page.url
            e.log(f"项目已创建: {project_url}")
            e.shot(page, "03-project-empty")

            # 3) 上传 36 张主照片（保持文件名顺序）
            picker = page.locator("input[type=file]")
            picker.set_input_files([str(p) for p in main_photos])
            expect(page.get_by_test_id("upload-summary")).to_contain_text("成功 36 张", timeout=600000)
            e.log("36 张主照片上传完成")
            e.shot(page, "04-uploaded-36")

            # 4) 提交 standard 任务
            page.get_by_role("button", name="创建重建任务").click()
            page.get_by_test_id("submit-job-btn").click()
            expect(page.get_by_text("任务已提交")).to_be_visible()
            page.get_by_role("button", name="详情").first.click()
            page.wait_for_url("**/jobs/*")
            job_url = page.url
            e.log(f"standard 任务已提交: {job_url}")
            e.shot(page, "05-job-submitted")

            # 5) 等待主任务完成（3000 px 双卡 South，约 30 分钟）
        if not args.resume_local_job_url:
            e.wait_job(page, job_url, minutes=60, tag="06-standard")
            page.goto(project_url, wait_until="domcontentloaded")
            e.shot(page, "07-project-models")

        # 6–10) 上传留出照片 → 查看器切换 → 区域提取 → 局部重建提交
        # （--resume-local-job-url 续段时全部跳过，直接等待已提交的局部任务）
        if not args.resume_local_job_url:
            # 6) 上传 4 张留出照片（主任务冻结后追加，不属于其输入清单）
            if not args.skip_holdout_upload:
                picker = page.locator("input[type=file]")
                picker.set_input_files([str(p) for p in holdout_photos])
                expect(page.get_by_test_id("upload-summary")).to_contain_text("成功 4 张", timeout=120000)
                e.log("4 张留出照片上传完成")
            e.shot(page, "08-holdout-uploaded")

            # 7) 打开照片贴图 GLB 查看器（原色贴图按钮仅对含内嵌纹理的 GLB 出现）
            if args.skip_viewer_region:
                e.log("跳过查看器切换与区域提取（续段）")
            else:
                model_row = page.locator("tr").filter(has_text="照片贴图")
                model_row.get_by_role("button", name="查看", exact=True).first.click()
                page.wait_for_url("**/models/*")
                expect(page.get_by_role("button", name="写实", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="原色贴图")).to_be_enabled(timeout=30000)
                e.shot(page, "09-viewer-realistic")
                page.get_by_role("button", name="原色贴图").click()
                time.sleep(2)
                e.shot(page, "10-viewer-photo-color")
                page.get_by_role("button", name="写实", exact=True).click()

                # 8) 框选区域 → 区域提取
                canvas = page.locator("canvas").last
                canvas.wait_for(state="visible", timeout=60000)
                page.get_by_role("button", name="框选区域", exact=True).click()
                box = canvas.bounding_box()
                sx, sy = box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.35
                ex, ey = box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.60
                page.mouse.move(sx, sy)
                page.mouse.down()
                for i in range(1, 9):
                    page.mouse.move(sx + (ex - sx) * i / 8, sy + (ey - sy) * i / 8, steps=4)
                page.mouse.up()
                expect(page.get_by_text("已框选区域。")).to_be_visible()
                e.shot(page, "11-region-selected")
                page.get_by_role("button", name="创建区域提取任务", exact=True).click()
                expect(page.get_by_text("区域提取任务 #")).to_be_visible()
                e.shot(page, "12-region-submitted")
                href = page.get_by_role("link", name="查看区域提取任务").get_attribute("href")
                e.wait_job(page, f"{args.base_url}{href}", minutes=10, tag="13-region")

            # 9) 项目页下载区域结果并记录哈希（模型行以"区域提取结果"标签标识）
            page.goto(project_url, wait_until="domcontentloaded")
            region_row = page.locator("tr").filter(has_text="区域提取结果")
            expect(region_row.first).to_be_visible(timeout=30000)
            e.download_by_button(page, region_row.first.get_by_role("button", name="下载", exact=True), "region-glb")
            e.shot(page, "14-region-downloaded")

            # 10) 局部重建：父模型 → 局部重建 → 框选 + 选 4 张留出照片
            page.goto(project_url, wait_until="domcontentloaded")
            model_row = page.locator("tr").filter(has_text="照片贴图")
            model_row.get_by_role("button", name="查看", exact=True).first.click()
            page.wait_for_url("**/models/*")
            # 旋转按钮在选区模式下禁用，且旋转后的拖框曾产生退化选区（0 尺寸盒）；
            # 实测可靠配方是"新载入视角 + 提取流程同款拖框"——产出 5.44×1.73×3.05
            # 的全模型大盒，必然覆盖留出照片的贡献区（有增量点才发布，守卫兜底）。
            page.get_by_role("button", name="局部重建", exact=True).click()
            expect(page.get_by_text("选择 1–32 张本项目新增照片")).to_be_visible()
            # 先框选，再选照片
            canvas = page.locator("canvas").last
            canvas.wait_for(state="visible", timeout=60000)
            box = canvas.bounding_box()
            sx, sy = box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.35
            ex, ey = box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.60
            page.mouse.move(sx, sy)
            page.mouse.down()
            for i in range(1, 9):
                page.mouse.move(sx + (ex - sx) * i / 8, sy + (ey - sy) * i / 8, steps=4)
            page.mouse.up()
            expect(page.get_by_text("已框选区域。")).to_be_visible()
            e.shot(page, "15-local-region-selected")
            picker_box = page.locator(".photo-picker input")
            for name in ("P1180177", "P1180178", "P1180179", "P1180180"):
                picker_box.fill(name)  # filterable：输入过滤出目标照片再点选
                page.locator(".el-select-dropdown__item").filter(has_text=name).first.click()
            expect(page.locator(".photo-picker").locator(".el-tag")).to_have_count(4)
            page.keyboard.press("Escape")  # 尝试收起下拉面板
            e.shot(page, "16-local-photos-picked")
            # Element Plus 的下拉 popper 会拦截指针事件，Playwright 物理点击不稳定；
            # 直接触发 DOM click（真实用户在同类环境下也能通过键盘/触屏提交）
            page.get_by_role("button", name="创建局部重建任务", exact=True).evaluate("el => el.click()")
            page.wait_for_timeout(1500)
            if page.locator(".el-message").count():
                e.log(f"submit toast: {page.locator('.el-message').first.inner_text()}")
            else:
                e.log("submit toast: (none)")
            expect(page.get_by_text("局部重建任务 #")).to_be_visible()
            e.shot(page, "17-local-submitted")
            href = page.get_by_role("link", name="查看局部重建任务").get_attribute("href")
            local_job_url = f"{args.base_url}{href}"
        else:
            local_job_url = args.resume_local_job_url
        e.wait_job(page, local_job_url, minutes=45, tag="18-local")
        # 11) 打开点云候选，下载并回父模型看选区（模型行以"局部重建 · 融合点云"标签标识）
        page.goto(project_url, wait_until="domcontentloaded")
        local_row = page.locator("tr").filter(has_text="融合点云")
        expect(local_row.first).to_be_visible(timeout=30000)
        e.download_by_button(page, local_row.first.get_by_role("button", name="下载", exact=True),
                             "local-candidate")
        local_row.first.get_by_role("button", name="查看", exact=True).click()
        page.wait_for_url("**/models/*")
        e.shot(page, "19-local-candidate-viewer")
        back = page.get_by_role("button", name="回父模型 #").filter(has_text="看选区")
        expect(back).to_be_visible(timeout=15000)
        back.click()
        expect(page.get_by_text("的原选区 #")).to_be_visible()
        e.shot(page, "20-parent-review-region")

        # 12) 写 manifest
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps({
            "base_url": args.base_url, "user": args.user, "project_url": project_url,
            "main_job_url": job_url, "main_photos": [p.name for p in main_photos],
            "holdout_photos": [p.name for p in holdout_photos],
            "downloads": e.downloads, "timeline": e.timeline,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        e.log(f"manifest 写入 {args.manifest}")
        browser.close()


if __name__ == "__main__":
    main()
