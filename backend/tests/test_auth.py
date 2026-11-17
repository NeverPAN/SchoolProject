"""认证与安全相关测试。"""
from __future__ import annotations

from app.core.security import decode_access_token, hash_password, verify_password


class TestPasswordHashing:
    def test_hash_and_verify(self):
        h = hash_password("s3cret-pass")
        assert h != "s3cret-pass"  # 不存明文
        assert verify_password("s3cret-pass", h)
        assert not verify_password("wrong", h)

    def test_hash_is_salted(self):
        assert hash_password("abc123") != hash_password("abc123")

    def test_verify_garbage_hash(self):
        assert not verify_password("x", "not-a-hash")


class TestJWT:
    def test_roundtrip(self):
        token = decode_access_token.__globals__["create_access_token"](1, "alice", "user")
        payload = decode_access_token(token)
        assert payload["sub"] == "1"
        assert payload["username"] == "alice"

    def test_invalid_token(self):
        assert decode_access_token("garbage.token.here") is None

    def test_tampered_token(self):
        from app.core.security import create_access_token

        token = create_access_token(1, "alice", "user")
        assert decode_access_token(token + "x") is None


class TestAuthAPI:
    def test_register_login_me(self, client):
        r = client.post("/api/v1/auth/register", json={"username": "carol", "password": "pass1234"})
        assert r.status_code == 201
        assert r.json()["role"] == "user"
        r = client.post("/api/v1/auth/login", json={"username": "carol", "password": "pass1234"})
        assert r.status_code == 200
        token = r.json()["access_token"]
        r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == "carol"

    def test_duplicate_username_rejected(self, client):
        client.post("/api/v1/auth/register", json={"username": "dave", "password": "pass1234"})
        r = client.post("/api/v1/auth/register", json={"username": "dave", "password": "other123"})
        assert r.status_code == 409

    def test_short_password_rejected(self, client):
        r = client.post("/api/v1/auth/register", json={"username": "eve", "password": "123"})
        assert r.status_code == 422

    def test_login_wrong_password(self, client):
        client.post("/api/v1/auth/register", json={"username": "frank", "password": "pass1234"})
        r = client.post("/api/v1/auth/login", json={"username": "frank", "password": "wrong!!!"})
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHORIZED"

    def test_me_requires_auth(self, client):
        assert client.get("/api/v1/me").status_code == 401

    def test_me_bad_token(self, client):
        r = client.get("/api/v1/me", headers={"Authorization": "Bearer bogus"})
        assert r.status_code == 401
