from quart import render_template, Blueprint, websocket, current_app, request, redirect, url_for
import aiomysql
from pymongo import MongoClient
import asyncio
import datetime
import os
import dotenv
import random
import json

game_bp = Blueprint("game", __name__, url_prefix="/game")
dotenv.load_dotenv()

card_types = ["C", "D", "H", "S"]
card_values = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

client = MongoClient(os.getenv("MONGODB_URI"))
db = client['blackjack_db']
collection = db['game_logs']

def log_move(player_id, action, card_info):
    data = {
        "p_id": player_id,
        "act": action,
        "card": card_info,
        "ts": datetime.datetime.now()
    }
    collection.insert_one(data)

async def get_user_from_cookie():
    """從 cookie 的 sid 查詢使用者資訊（僅在 request context 中使用）"""
    try:
        sid = request.cookies.get('sid')
    except RuntimeError:
        # 不在 request context 中（例如 WebSocket）
        return None
    
    if not sid:
        return None
    
    pool = current_app.mysql_pool
    async with pool.acquire() as conn:
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

@game_bp.route("/blackjack")
async def blackjack():
    user = await get_user_from_cookie()
    if not user:
        return redirect(url_for('login'))
    return await render_template("blackjack.html", user=user)

@game_bp.websocket("/blackjack/ws")
async def blackjack_ws():
    # WebSocket 從 headers 取得 cookie
    cookie_header = websocket.headers.get('Cookie', '')
    sid = None
    for cookie in cookie_header.split(';'):
        cookie = cookie.strip()
        if cookie.startswith('sid='):
            sid = cookie[4:]  # 移除 'sid='
            break
    
    if not sid:
        await websocket.close(code=1008, reason="未登入")
        return
    
    pool = current_app.mysql_pool
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM cookie WHERE value = %s", (sid,))
            row = await cur.fetchone()
            if not row:
                await websocket.close(code=1008, reason="無效的 cookie")
                return
            user_id = row[0]
    
    try:
        while True:
            data = await websocket.receive_json()
            # 使用 cookie 查到的 user_id，不信任前端傳的 player_id
            player_id = user_id
            action = data.get("action")

            if action == "join":
                log_move(player_id, action, None)
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO hands (id, score, stat) VALUES (%s, 0, 'waiting') ON DUPLICATE KEY UPDATE stat='waiting', score=0", 
                            (player_id,)
                        )
                        await conn.commit()
                await websocket.send_json({"status": "success"})

            elif action == "hit":
                c_type = random.choice(card_types)
                c_val = random.choice(card_values)
                new_card = f"{c_type}{c_val}"
                
                log_move(player_id, action, new_card)
                print(player_id if player_id else "None")
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO cards(card_id, id, card) VALUES (%s, %s, %s)", 
                            (random.randint(111111111111111111111111111111111111, 999999999999999999999999999999999999), player_id, new_card)
                        )
                        # 修正 fetchone 用法
                        await cur.execute("SELECT score FROM hands WHERE id = %s", (player_id,))
                        row = await cur.fetchone()
                        score = int(row[0]) if row and row[0] is not None else 0

                        if c_val == "A":
                            score += 1 if score + 11 > 21 else 11
                        elif c_val in ["J", "Q", "K"]:
                            score += 10
                        else:
                            score += int(c_val)

                        await cur.execute(
                            "UPDATE hands SET score = %s WHERE id = %s",
                            (score, player_id)
                        )
                        await conn.commit()
                
                await websocket.send_json({"status": "success", "card": new_card, "score": score})
                
            elif action == "stand":
                log_move(player_id, action, None)
                await websocket.send_json({"status": "success"})
            else:
                await websocket.send_json({"status": "error", "message": "未知的 action"})
    except asyncio.CancelledError:
        raise  # 客戶端斷線，正常結束
    except Exception as e:
        print(f"WS Error: {e}")
        try:
            await websocket.send_json({"status": "error", "message": str(e)})
        except Exception:
            pass
        # 不 return，繼續 while 迴圈，保持連線
        