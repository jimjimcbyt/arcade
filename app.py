from quart import Quart, render_template, request, redirect, url_for, make_response
from blueprint.game import game_bp
from mangum import Mangum
import aiomysql
import os
import secrets
import dotenv
import httpx
from datetime import datetime
import json

dotenv.load_dotenv()

app = Quart(__name__)
app.register_blueprint(game_bp)

# Google OAuth 基本參數
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = "https://arcade.1010819.xyz/auth"

# MongoDB 連線（for log），失敗時不擋啟動
from pymongo import MongoClient

collection = None
try:
    MONGODB_URI = os.getenv("MONGODB_URI")
    if MONGODB_URI:
        mongo_client = MongoClient(MONGODB_URI)
        db = mongo_client['blackjack_db']
        collection = db['log_main']
except Exception:
    pass  # 無 MONGODB_URI 或連線失敗時 collection 維持 None

def write_log(action, data):
    if collection is not None:
        log_doc = {
            "action": action,
            "data": data,
            "timestamp": datetime.now()
        }
        try:
            collection.insert_one(log_doc)
        except Exception:
            pass # 若 log 寫入失敗就忽略

async def get_user_from_cookie():
    """從 cookie 的 sid 查詢使用者資訊"""
    sid = request.cookies.get('sid')
    if not sid:
        return None
    
    async with app.mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM cookie WHERE value = %s", (sid,))
            row = await cur.fetchone()
            if not row:
                return None
            user_id = row[0]
            
            await cur.execute("SELECT id, email FROM users WHERE id = %s", (user_id,))
            user_row = await cur.fetchone()
            if not user_row:
                return None
            
            return {
                "sub": user_row[0],
                "email": user_row[1]
            }

@app.before_serving
async def create_db_pool():
    app.mysql_pool = await aiomysql.create_pool(
        host="mysql-jimtechtw.i.aivencloud.com",
        port=26025,
        user="avnadmin",
        password=os.getenv("MYSQL_PASSWORD"),
        db="arcade",
        minsize=1,
        maxsize=10
    )
    write_log("app_startup", {"msg": "app 啟動，MySQL pool 建立"})

@app.route("/login")
async def login():
    # 檢查是否已登入（有有效的 sid cookie）
    user = await get_user_from_cookie()
    if user:
        return redirect(url_for('index'))
    
    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        "&response_type=code"
        "&scope=openid%20email%20profile"
    )
    write_log("login_start", {"ip": request.headers.get("X-Forwarded-For", request.remote_addr)})
    return redirect(google_auth_url)

@app.route("/auth")
async def auth():
    code = request.args.get('code')
    if not code:
        write_log("auth_failed", {"reason": "no_code_in_url"})
        return "授權失敗", 400

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            }
        )
        token_data = token_res.json()
        access_token = token_data.get('access_token')

        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        userinfo = user_res.json()

    user_id = userinfo.get('sub')
    if not user_id:
        write_log("auth_failed", {"reason": "no_sub", "userinfo": userinfo})
        return "無法取得使用者識別 (缺少 sub)", 400

    # 每次登入都產生新的隨機 16 字元，寫入 DB 並設成 cookie
    value = secrets.token_hex(8)

    # 偵測/新增用戶註冊（重要）
    async with app.mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            result = await cur.fetchone()
            if not result:
                await cur.execute(
                    "INSERT INTO users (id, email) VALUES (%s, %s)",
                    (user_id, userinfo.get("email"))
                )
                await conn.commit()
                write_log("auto_register", {"user_id": user_id, "email": userinfo.get("email")})
            else:
                write_log("user_exists", {"user_id": user_id, "email": userinfo.get("email")})
    
    # 記錄 log：登入
    login_log = {
        "user_id": user_id,
        "email": userinfo.get("email"),
        "action": "login",
        "timestamp": datetime.now(),
    }
    if collection is not None:
        try:
            collection.insert_one(login_log)
        except Exception:
            pass

    # cookie 寫入（重要操作）
    async with app.mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO cookie (id, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value=%s",
                (user_id, value, value)
            )
            await conn.commit()
            write_log("cookie_update", {"user_id": user_id, "cookie_val": value})

    response = redirect(url_for('index'))
    # 用 sid 存隨機值（全部以這個 cookie 為主）
    response.set_cookie(
        "sid",
        value,
        path="/",
        httponly=True,
        samesite="Lax",
        max_age=86400 * 30,  # 30 天
    )

    write_log("login_success", {"user_id": user_id, "email": userinfo.get("email"), "ip": request.headers.get("X-Forwarded-For", request.remote_addr)})
    return response

@app.route("/logout")
async def logout():
    user = await get_user_from_cookie()
    if user:
        write_log("logout", {"user_id": user.get("sub"), "email": user.get("email"), "ip": request.headers.get("X-Forwarded-For", request.remote_addr)})
    
    response = redirect(url_for('index'))
    response.delete_cookie("sid", path="/")
    return response

@app.route("/")
async def index():
    write_log("index_page_view", {"ip": request.headers.get("X-Forwarded-For", request.remote_addr)})
    user = await get_user_from_cookie()
    return await render_template("index.html", user=user)

handler = Mangum(app)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
