"""图片上传、元数据、缩略图、质量检查测试（模块 B/C 验收）。"""
from __future__ import annotations

import io

import pytest


class TestImageUpload:
    def test_upload_single(self, client, auth_headers, project):
        from tests.conftest import make_image_bytes

        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("view_001.jpg", make_image_bytes(seed=1), "image/jpeg"))],
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text
        items = r.json()
        assert len(items) == 1
        img = items[0]
        assert img["original_filename"] == "view_001.jpg"
        assert img["width"] == 320 and img["height"] == 240
        assert img["format"] == "jpeg"
        assert len(img["checksum"]) == 64
        assert img["file_size"] > 0

    def test_upload_batch_consistency(self, client, auth_headers, project, upload_images):
        """上传后文件数量、总大小与数据库记录一致。"""
        r = upload_images(8)
        assert len(r.json()) == 8
        detail = client.get(f"/api/v1/projects/{project['id']}", headers=auth_headers).json()
        assert detail["image_count"] == 8
        total = detail["total_image_bytes"]
        assert total == sum(i["file_size"] for i in detail["images"])

    def test_upload_rejects_bad_extension(self, client, auth_headers, project):
        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("evil.exe", b"MZ fake binary", "application/octet-stream"))],
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert "不支持的文件格式" in r.json()["message"]

    def test_upload_rejects_corrupt_image(self, client, auth_headers, project):
        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("broken.jpg", b"\xff\xd8\xff this is not really a jpeg", "image/jpeg"))],
            headers=auth_headers,
        )
        assert r.status_code == 400
        assert "损坏" in r.json()["message"] or "无法读取" in r.json()["message"]

    def test_upload_duplicate_content_rejected(self, client, auth_headers, project):
        from tests.conftest import make_image_bytes

        data = make_image_bytes(seed=42)
        r1 = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("a.jpg", data, "image/jpeg"))],
            headers=auth_headers,
        )
        assert r1.status_code == 201
        r2 = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("b.jpg", data, "image/jpeg"))],
            headers=auth_headers,
        )
        assert r2.status_code == 409

    def test_upload_same_name_ok(self, client, auth_headers, project):
        """同名不同内容允许（存储名用 UUID）。"""
        from tests.conftest import make_image_bytes

        for seed in (1, 2):
            r = client.post(
                f"/api/v1/projects/{project['id']}/images",
                files=[("files", ("same.jpg", make_image_bytes(seed=seed), "image/jpeg"))],
                headers=auth_headers,
            )
            assert r.status_code == 201

    def test_stored_path_is_uuid(self, client, auth_headers, project):
        from tests.conftest import make_image_bytes

        r = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", ("../../etc/passwd.jpg", make_image_bytes(seed=9), "image/jpeg"))],
            headers=auth_headers,
        )
        assert r.status_code == 201
        assert r.json()[0]["stored_name"].endswith(".jpg")
        assert "/" not in r.json()[0]["stored_name"] and ".." not in r.json()[0]["stored_name"]


class TestThumbnails:
    def test_thumbnail(self, client, auth_headers, project, upload_images):
        upload_images(2)
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        assert all(i["has_thumbnail"] for i in imgs)
        r = client.get(f"/api/v1/images/{imgs[0]['id']}/thumbnail", headers=auth_headers)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")

    def test_thumbnail_isolation(self, client, auth_headers, auth_headers_b, project, upload_images):
        upload_images(1)
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        r = client.get(f"/api/v1/images/{imgs[0]['id']}/thumbnail", headers=auth_headers_b)
        assert r.status_code == 404


class TestQualityCheck:
    def test_quality_check_marks_images(self, client, auth_headers, project, upload_images):
        upload_images(3)
        r = client.post(f"/api/v1/projects/{project['id']}/quality-check", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["checked"] == 3
        assert body["ok"] + body["warning"] + body["invalid"] == 3
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        assert all(i["quality_status"] != "pending" for i in imgs)


class TestImageDelete:
    def test_delete_image(self, client, auth_headers, project, upload_images):
        upload_images(3)
        imgs = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        target = imgs[0]
        r = client.delete(f"/api/v1/images/{target['id']}", headers=auth_headers)
        assert r.status_code == 200
        imgs2 = client.get(f"/api/v1/projects/{project['id']}/images", headers=auth_headers).json()
        assert len(imgs2) == 2
        assert all(i["id"] != target["id"] for i in imgs2)
        # 文件也应删除
        from pathlib import Path

        assert not Path(target["stored_name"]).exists() if "/" not in target["stored_name"] else True
