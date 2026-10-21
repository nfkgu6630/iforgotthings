"""把 11 支 API 從頭到尾跑一遍，確認後端與資料庫都正常。

執行方式：python smoke_test.py
注意：會清空資料庫並重灌測試資料。
"""

import io
import sys
from pathlib import Path

from app import app, get_db
from seed import TEST_PASSWORD, USERS, seed

BOB = USERS[1][1]  # 測試帳號的學號

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

results = []


def check(name, response, status):
    ok = response.status_code == status
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}: {response.status_code} (expected {status})")
    if not ok:
        print("       ", response.get_data(as_text=True)[:300])
    return response.get_json(silent=True)


with app.app_context():
    db = get_db()
    db.execute((Path(app.root_path) / "schema.sql").read_text(encoding="utf-8"))
    db.commit()
    seed(db)

owner = app.test_client()
stranger = app.test_client()
guest = app.test_client()

# 帳號：註冊時的學號驗證
def register(client, username, password="secret1"):
    return client.post("/api/auth/register", json={
        "username": username, "password": password, "display_name": "x", "contact": "x"})


check("me before login -> 401", guest.get("/api/auth/me"), 401)
for label, bad_id in [
    ("letters", "aaa"), ("8 digits", "11542304"), ("10 digits", "1154230470"),
    ("year too old", "099423047"), ("year in future", "999423047"),
    ("college 0", "115023047"), ("college 9", "115923047"), ("program 1", "115413047"),
]:
    check(f"register {label} ({bad_id}) -> 400", register(guest, bad_id), 400)
check("register short password -> 400", register(guest, "115423099", "12345"), 400)
check("register transfer student, 5th digit 3 -> 201", register(app.test_client(), "113431007"), 201)
check("register duplicate -> 409", register(guest, BOB), 409)
me = check("register -> 201", owner.post("/api/auth/register", json={
    "username": "115423099", "password": "secret1", "display_name": "測試者", "contact": "Line: tester"}), 201)
check("me after register -> 200", owner.get("/api/auth/me"), 200)
check("login wrong password -> 401", stranger.post("/api/auth/login", json={
    "username": BOB, "password": "wrong"}), 401)
check("login -> 200", stranger.post("/api/auth/login", json={
    "username": BOB, "password": TEST_PASSWORD}), 200)

# 帳號管理：修改自己的資料
check("update me as guest -> 401", guest.put("/api/auth/me", json={"display_name": "x"}), 401)
check("update me empty -> 400", owner.put("/api/auth/me", json={}), 400)
updated_me = check("update name & contact -> 200", owner.put("/api/auth/me", json={
    "display_name": "測試者二號", "contact": "IG: tester2", "username": "ignored"}), 200)
assert updated_me["display_name"] == "測試者二號" and updated_me["contact"] == "IG: tester2"
assert updated_me["username"] == "115423099", "username must not change"
check("change password, wrong current -> 400", owner.put("/api/auth/me", json={
    "current_password": "nope", "new_password": "newpass1"}), 400)
check("change password, too short -> 400", owner.put("/api/auth/me", json={
    "current_password": "secret1", "new_password": "123"}), 400)
check("change password -> 200", owner.put("/api/auth/me", json={
    "current_password": "secret1", "new_password": "newpass1"}), 200)
check("login with old password -> 401", guest.post("/api/auth/login", json={
    "username": "115423099", "password": "secret1"}), 401)
check("login with new password -> 200", app.test_client().post("/api/auth/login", json={
    "username": "115423099", "password": "newpass1"}), 200)

# 貼文列表
listing = check("list posts (default open) -> 200", guest.get("/api/posts"), 200)
print("        open posts:", listing["total"])
assert "contact" not in listing["posts"][0], "list must not expose contact"
everything = check("list status=all -> 200", guest.get("/api/posts?status=all"), 200)
search = check("search q=水壺 -> 200", guest.get("/api/posts?q=水壺"), 200)
print("        search hits:", [p["title"] for p in search["posts"]])
check("filter category=key -> 200", guest.get("/api/posts?category=key"), 200)
check("bad category -> 400", guest.get("/api/posts?category=car"), 400)
check("mine=1 as guest -> 401", guest.get("/api/posts?mine=1"), 401)

# 上傳與發文
check("upload as guest -> 401", guest.post("/api/upload", data={
    "image": (io.BytesIO(PNG_1PX), "a.png")}), 401)
check("upload fake png -> 400", owner.post("/api/upload", data={
    "image": (io.BytesIO(b"<html>not an image</html>"), "a.png")}), 400)
uploaded = check("upload png -> 201", owner.post("/api/upload", data={
    "image": (io.BytesIO(PNG_1PX), "photo.png")}), 201)
check("fetch uploaded image -> 200", guest.get(uploaded["url"]), 200)

check("create post as guest -> 401", guest.post("/api/posts", json={}), 401)
check("create post missing fields -> 400", owner.post("/api/posts", json={"title": "x"}), 400)
check("create post bad date -> 400", owner.post("/api/posts", json={
    "title": "x", "category": "other", "found_location": "x", "found_at": "yesterday"}), 400)
post = check("create post -> 201", owner.post("/api/posts", json={
    "title": "紅色筆袋", "category": "stationery", "description": "裡面有三支筆",
    "found_location": "工程二館", "found_at": "2026-10-13T09:30", "image_url": uploaded["url"]}), 201)
assert post["contact"] == "IG: tester2", "contact should fall back to user's default"
assert post["is_owner"] is True
assert post["returned_at"] is None, "a new post has no return time"

detail = check("detail as guest -> 200", guest.get(f"/api/posts/{post['id']}"), 200)
assert detail["contact"] == "IG: tester2" and detail["is_owner"] is False
assert detail["author_name"] == "測試者二號（115423099）", "author shows display name + student ID"
listed_mine = guest.get("/api/posts?q=紅色筆袋").get_json()["posts"][0]
assert listed_mine["author_name"] == "測試者二號（115423099）", "list shows display name + student ID"
check("detail missing -> 404", guest.get("/api/posts/99999"), 404)

# 修改與刪除
check("update by stranger -> 403", stranger.put(f"/api/posts/{post['id']}", json={"status": "returned"}), 403)
check("update bad status -> 400", owner.put(f"/api/posts/{post['id']}", json={"status": "lost"}), 400)
check("update empty body -> 400", owner.put(f"/api/posts/{post['id']}", json={}), 400)
updated = check("mark returned -> 200", owner.put(f"/api/posts/{post['id']}", json={"status": "returned"}), 200)
assert updated["status"] == "returned" and updated["returned_at"], "return time must be recorded"
print("        returned_at:", updated["returned_at"])
again = check("save returned again -> 200", owner.put(f"/api/posts/{post['id']}", json={
    "status": "returned", "storage_location": "學務處"}), 200)
assert again["returned_at"] == updated["returned_at"], "re-saving must not move the return time"
listed = check("list returned -> 200", guest.get("/api/posts?status=returned"), 200)
assert any(p["id"] == post["id"] and p["returned_at"] for p in listed["posts"]), "list shows return time"
reopened = check("reopen -> 200", owner.put(f"/api/posts/{post['id']}", json={"status": "open"}), 200)
assert reopened["returned_at"] is None, "reopening clears the return time"
check("mark returned again -> 200", owner.put(f"/api/posts/{post['id']}", json={"status": "returned"}), 200)
mine = check("mine=1 -> 200", owner.get("/api/posts?mine=1&status=all"), 200)
assert mine["total"] == 1
check("delete by stranger -> 403", stranger.delete(f"/api/posts/{post['id']}"), 403)
check("delete -> 200", owner.delete(f"/api/posts/{post['id']}"), 200)
check("uploaded image removed -> 404", guest.get(uploaded["url"]), 404)
check("logout -> 200", owner.post("/api/auth/logout"), 200)
check("me after logout -> 401", owner.get("/api/auth/me"), 401)

# 還原成乾淨的測試資料
with app.app_context():
    seed(get_db())

passed = sum(results)
print(f"\n{passed}/{len(results)} checks passed")
sys.exit(0 if passed == len(results) else 1)
