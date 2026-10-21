"""校園失物招領系統後端（Flask + PostgreSQL）。

API 規格見組內的「校園失物招領系統 API 文件」。
"""

import os
import re
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import psycopg
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from psycopg.rows import dict_row
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CATEGORIES = {
    "id_card", "electronics", "wallet", "key",
    "clothing", "stationery", "bottle_umbrella", "other",
}
POST_STATUSES = {"open", "returned"}
MAX_UPLOAD_MB = 5
MIN_PASSWORD_LENGTH = 6

# 帳號 = 中央大學學號（9 碼）：入學學年度 3 碼 + 學院 1 碼 + 學制 1 碼 + 系所 1 碼 + 流水號 3 碼
STUDENT_ID_RE = re.compile(r"^[0-9]{9}$")
MIN_ENTRY_YEAR = 100  # 民國 100 學年度
STUDENT_ID_COLLEGES = "12345678"  # 文、理、工、管、資電、地科、客家、生醫理工
STUDENT_ID_PROGRAMS = "0235"  # 學士、碩士或轉大二、轉大三、博士／在職專班

# 副檔名 → 檔頭特徵，避免把非圖片檔改副檔名後上傳
IMAGE_SIGNATURES = {
    ".jpg": lambda h: h.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    ".png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    ".webp": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP",
}

# 只提供前端 build 後的 frontend/dist/（npm run build 產生），不提供 frontend/ 原始碼與設定檔；
# 前端改用獨立容器 + Nginx 部署時，dist/ 不存在，這裡就只提供 API
FRONTEND_DIR = Path(
    os.environ.get("FRONTEND_DIR", BASE_DIR.parent / "frontend" / "dist")
).resolve()

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    DATABASE_URL=os.environ.get(
        "DATABASE_URL", "postgresql://lostfound@localhost:5432/lostfound"
    ),
    APP_TIMEZONE=os.environ.get("APP_TIMEZONE", "Asia/Taipei"),
    UPLOAD_DIR=Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads")),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.config["UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = psycopg.connect(
            app.config["DATABASE_URL"],
            row_factory=dict_row,
            options=f"-c timezone={app.config['APP_TIMEZONE']}",
        )
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()  # 未 commit 的交易會被 rollback


# ---------------------------------------------------------------- errors

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(error=e.message), e.status


@app.errorhandler(HTTPException)
def handle_http_error(e):
    messages = {
        404: "not found",
        405: "method not allowed",
        413: f"file too large (max {MAX_UPLOAD_MB} MB)",
    }
    return jsonify(error=messages.get(e.code, e.name.lower())), e.code


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    app.logger.exception("Unhandled error: %s", e)
    return jsonify(error="internal server error"), 500


# ---------------------------------------------------------------- helpers

def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "request body must be a JSON object")
    return data


def text_field(data, name, *, required=False, max_len=None):
    value = data.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(400, f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{name} must be a string")
    value = value.strip()
    if max_len and len(value) > max_len:
        raise ApiError(400, f"{name} must be at most {max_len} characters")
    return value


def choice_field(data, name, choices):
    value = text_field(data, name, required=True)
    if value not in choices:
        raise ApiError(400, f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def datetime_field(data, name):
    raw = text_field(data, name, required=True)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ApiError(400, f"{name} must be an ISO 8601 datetime, e.g. 2026-10-13T14:30:00")
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(app.config["APP_TIMEZONE"])).replace(tzinfo=None)
    return value


def current_academic_year():
    """民國學年度，每年 8 月換新學年度。"""
    now = datetime.now(ZoneInfo(app.config["APP_TIMEZONE"]))
    return now.year - 1911 - (1 if now.month < 8 else 0)


def validate_student_id(username):
    if not STUDENT_ID_RE.fullmatch(username):
        raise ApiError(400, "帳號必須是 9 碼數字的中央大學學號，例如 115423047")
    year, college, program = int(username[:3]), username[3], username[4]
    latest = current_academic_year()
    if not MIN_ENTRY_YEAR <= year <= latest:
        raise ApiError(400, f"學號前 3 碼（入學學年度）必須介於 {MIN_ENTRY_YEAR} 到 {latest}")
    if college not in STUDENT_ID_COLLEGES:
        raise ApiError(400, "學號第 4 碼（學院代碼）必須是 1 到 8")
    if program not in STUDENT_ID_PROGRAMS:
        raise ApiError(400, f"學號第 5 碼（學制）必須是 {'、'.join(STUDENT_ID_PROGRAMS)} 其中之一")


def password_field(data, name):
    password = data.get(name)
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ApiError(400, f"{name} must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def iso(value):
    return value.isoformat(timespec="seconds") if value else None


def escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def remove_upload(url):
    if url and url.startswith("/uploads/"):
        path = app.config["UPLOAD_DIR"] / Path(url).name
        path.unlink(missing_ok=True)


def current_user_id():
    return session.get("user_id")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user_id() is None:
            raise ApiError(401, "login required")
        return view(*args, **kwargs)
    return wrapper


def user_out(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "contact": row["contact"],
        "created_at": iso(row["created_at"]),
    }


def post_summary_out(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "found_location": row["found_location"],
        "found_at": iso(row["found_at"]),
        "image_url": row["image_url"],
        "status": row["status"],
        "returned_at": iso(row["returned_at"]),
        "author_name": row["author_name"],
        "created_at": iso(row["created_at"]),
    }


def post_detail_out(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "description": row["description"],
        "found_location": row["found_location"],
        "found_at": iso(row["found_at"]),
        "storage_location": row["storage_location"],
        "contact": row["contact"] or row["author_contact"],
        "image_url": row["image_url"],
        "status": row["status"],
        "returned_at": iso(row["returned_at"]),
        "author_name": row["author_name"],
        "is_owner": row["user_id"] == current_user_id(),
        "created_at": iso(row["created_at"]),
        "updated_at": iso(row["updated_at"]),
    }


POST_SELECT = """
    SELECT p.*, u.display_name || '（' || u.username || '）' AS author_name,
           u.contact AS author_contact
    FROM posts p JOIN users u ON u.id = p.user_id
"""


def fetch_post(post_id):
    row = get_db().execute(POST_SELECT + " WHERE p.id = %s", (post_id,)).fetchone()
    if row is None:
        raise ApiError(404, "post not found")
    return row


def fetch_own_post(post_id):
    row = fetch_post(post_id)
    if row["user_id"] != current_user_id():
        raise ApiError(403, "you can only modify your own posts")
    return row


# ---------------------------------------------------------------- auth API

@app.post("/api/auth/register")
def register():
    data = json_body()
    username = text_field(data, "username", required=True)
    validate_student_id(username)
    password = password_field(data, "password")
    display_name = text_field(data, "display_name", required=True, max_len=50)
    contact = text_field(data, "contact", required=True, max_len=100)

    db = get_db()
    try:
        row = db.execute(
            """INSERT INTO users (username, password_hash, display_name, contact)
               VALUES (%s, %s, %s, %s) RETURNING *""",
            (username, generate_password_hash(password), display_name, contact),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "username already taken")
    db.commit()

    session.clear()
    session["user_id"] = row["id"]
    return jsonify(user_out(row)), 201


@app.post("/api/auth/login")
def login():
    data = json_body()
    username = text_field(data, "username", required=True)
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise ApiError(400, "password is required")

    row = get_db().execute(
        "SELECT * FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        raise ApiError(401, "invalid username or password")

    session.clear()
    session["user_id"] = row["id"]
    return jsonify(user_out(row))


@app.post("/api/auth/logout")
@login_required
def logout():
    session.clear()
    return jsonify(message="logged out")


@app.get("/api/auth/me")
def me():
    user_id = current_user_id()
    row = None
    if user_id is not None:
        row = get_db().execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        session.clear()
        raise ApiError(401, "not logged in")
    return jsonify(user_out(row))


@app.put("/api/auth/me")
@login_required
def update_me():
    """修改自己的顯示名稱、聯絡方式或密碼；帳號（學號）不能改，傳了也會忽略。"""
    data = json_body()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (current_user_id(),)).fetchone()

    updates = {}
    if "display_name" in data:
        updates["display_name"] = text_field(data, "display_name", required=True, max_len=50)
    if "contact" in data:
        updates["contact"] = text_field(data, "contact", required=True, max_len=100)
    if "new_password" in data:
        new_password = password_field(data, "new_password")
        current_password = data.get("current_password")
        if not isinstance(current_password, str) or not check_password_hash(
            user["password_hash"], current_password
        ):
            raise ApiError(400, "目前密碼不正確")
        updates["password_hash"] = generate_password_hash(new_password)
    if not updates:
        raise ApiError(400, "no editable fields given; allowed: display_name, contact, new_password")

    assignments = ", ".join(f"{name} = %({name})s" for name in updates)
    row = db.execute(
        f"UPDATE users SET {assignments} WHERE id = %(id)s RETURNING *",
        {**updates, "id": user["id"]},
    ).fetchone()
    db.commit()
    return jsonify(user_out(row))


# ---------------------------------------------------------------- posts API

@app.get("/api/posts")
def list_posts():
    conditions, params = [], []

    status = request.args.get("status", "open")
    if status != "all":
        if status not in POST_STATUSES:
            raise ApiError(400, "status must be one of: all, open, returned")
        conditions.append("p.status = %s")
        params.append(status)

    category = request.args.get("category")
    if category:
        if category not in CATEGORIES:
            raise ApiError(400, f"category must be one of: {', '.join(sorted(CATEGORIES))}")
        conditions.append("p.category = %s")
        params.append(category)

    q = request.args.get("q", "").strip()
    if q:
        conditions.append(
            "(p.title ILIKE %s OR p.description ILIKE %s OR p.found_location ILIKE %s)"
        )
        params += [f"%{escape_like(q)}%"] * 3

    if request.args.get("mine") == "1":
        if current_user_id() is None:
            raise ApiError(401, "login required")
        conditions.append("p.user_id = %s")
        params.append(current_user_id())

    sql = POST_SELECT
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY p.found_at DESC, p.id DESC"

    rows = get_db().execute(sql, params).fetchall()
    return jsonify(total=len(rows), posts=[post_summary_out(r) for r in rows])


@app.get("/api/posts/<int:post_id>")
def get_post(post_id):
    return jsonify(post_detail_out(fetch_post(post_id)))


# PUT 可修改的欄位與各自的檢查規則
EDITABLE_POST_FIELDS = {
    "title": lambda d: text_field(d, "title", required=True, max_len=100),
    "category": lambda d: choice_field(d, "category", CATEGORIES),
    "description": lambda d: text_field(d, "description", max_len=2000),
    "found_location": lambda d: text_field(d, "found_location", required=True, max_len=100),
    "found_at": lambda d: datetime_field(d, "found_at"),
    "storage_location": lambda d: text_field(d, "storage_location", max_len=100),
    "contact": lambda d: text_field(d, "contact", max_len=100),
    "image_url": lambda d: text_field(d, "image_url", max_len=500),
    "status": lambda d: choice_field(d, "status", POST_STATUSES),
}


@app.post("/api/posts")
@login_required
def create_post():
    data = json_body()
    values = {
        name: rule(data)
        for name, rule in EDITABLE_POST_FIELDS.items()
        if name != "status"
    }

    db = get_db()
    row = db.execute(
        """INSERT INTO posts (user_id, title, category, description, found_location,
                              found_at, storage_location, contact, image_url)
           VALUES (%(user_id)s, %(title)s, %(category)s, %(description)s, %(found_location)s,
                   %(found_at)s, %(storage_location)s, %(contact)s, %(image_url)s)
           RETURNING id""",
        {**values, "user_id": current_user_id()},
    ).fetchone()
    db.commit()
    return jsonify(post_detail_out(fetch_post(row["id"]))), 201


@app.put("/api/posts/<int:post_id>")
@login_required
def update_post(post_id):
    data = json_body()
    old = fetch_own_post(post_id)

    updates = {
        name: rule(data)
        for name, rule in EDITABLE_POST_FIELDS.items()
        if name in data
    }
    if not updates:
        raise ApiError(400, f"no editable fields given; allowed: {', '.join(EDITABLE_POST_FIELDS)}")

    assignments = ", ".join(f"{name} = %({name})s" for name in updates)
    # 歸還時間由後端記錄：標記已歸還的當下寫入，改回待認領就清空；已歸還再存一次不會覆蓋
    new_status = updates.get("status")
    if new_status == "returned" and old["status"] != "returned":
        assignments += ", returned_at = LOCALTIMESTAMP(0)"
    elif new_status == "open":
        assignments += ", returned_at = NULL"
    db = get_db()
    db.execute(
        f"UPDATE posts SET {assignments}, updated_at = LOCALTIMESTAMP(0) WHERE id = %(id)s",
        {**updates, "id": post_id},
    )
    db.commit()

    if "image_url" in updates and updates["image_url"] != old["image_url"]:
        remove_upload(old["image_url"])
    return jsonify(post_detail_out(fetch_post(post_id)))


@app.delete("/api/posts/<int:post_id>")
@login_required
def delete_post(post_id):
    old = fetch_own_post(post_id)
    db = get_db()
    db.execute("DELETE FROM posts WHERE id = %s", (post_id,))
    db.commit()
    remove_upload(old["image_url"])
    return jsonify(message="deleted")


# ---------------------------------------------------------------- uploads

@app.post("/api/upload")
@login_required
def upload_image():
    file = request.files.get("image")
    if file is None or not file.filename:
        raise ApiError(400, "image file is required (form field name: image)")

    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_SIGNATURES:
        raise ApiError(400, "image must be .jpg, .jpeg, .png or .webp")
    header = file.stream.read(12)
    file.stream.seek(0)
    if not IMAGE_SIGNATURES[ext](header):
        raise ApiError(400, "file content does not match its image type")

    filename = uuid.uuid4().hex + (".jpg" if ext == ".jpeg" else ext)
    file.save(app.config["UPLOAD_DIR"] / filename)
    return jsonify(url=f"/uploads/{filename}"), 201


@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(app.config["UPLOAD_DIR"], filename)


# ---------------------------------------------------------------- frontend

@app.get("/")
def index():
    if (FRONTEND_DIR / "index.html").exists():
        return app.send_static_file("index.html")
    return jsonify(message="Lost & Found API is running")


# ---------------------------------------------------------------- CLI

def ensure_utf8_database(db):
    """資料庫不是 UTF8 就存不了中文；Debian 容器沒設 locale 時預設會建成 SQL_ASCII。"""
    encoding = db.execute("SHOW server_encoding").fetchone()["server_encoding"]
    if encoding != "UTF8":
        name = db.info.dbname
        raise click.ClickException(
            f"資料庫 {name} 的編碼是 {encoding}，無法儲存中文，必須是 UTF8。\n"
            "請刪掉並用 UTF8 重建（裡面的資料會一併刪除）：\n"
            f'  runuser -u postgres -- psql -c "DROP DATABASE {name};"\n'
            f'  runuser -u postgres -- psql -c "CREATE DATABASE {name} OWNER {db.info.user} '
            "ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' TEMPLATE template0;\""
        )


@app.cli.command("init-db")
def init_db_command():
    """依 schema.sql 建立資料表（已存在的表不會被動到）。"""
    db = get_db()
    ensure_utf8_database(db)
    db.execute((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))
    db.commit()
    click.echo("Tables created.")


@app.cli.command("seed-db")
@click.option("--yes", is_flag=True, help="不詢問，直接清空並重灌測試資料")
def seed_db_command(yes):
    """清空 users、posts 並寫入展示用測試資料。"""
    if not yes:
        click.confirm("This deletes ALL users and posts. Continue?", abort=True)
    from seed import seed
    db = get_db()
    ensure_utf8_database(db)
    seed(db)
    click.echo("Seed data inserted.")


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
