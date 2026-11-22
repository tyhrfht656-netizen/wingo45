# -*- coding: utf-8 -*-
"""
WINGO 45S GPT TELEGRAM BOT
- Polls the supplied Win Go API.
- Detects new target issues and settled results.
- Sends the real history to OpenAI Responses API for LỚN/NHỎ analysis.
- Telegram commands: /start, /stop, /du_doan, /thongke, /id
- No betting, stake sizing, martingale, or guaranteed-win claims.
"""

import os
import re
import json
import time
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5").strip()

API_URL = os.getenv(
    "WINGO_API_URL",
    "https://vip1.luck2025.com/server/lottery/getCurrentLotteryInfo"
).strip()
LOTTERY_ID = int(os.getenv("LOTTERY_ID", "73"))

POLL_SECONDS = max(3, int(os.getenv("POLL_SECONDS", "5")))
HISTORY_LIMIT = max(30, min(200, int(os.getenv("HISTORY_LIMIT", "100"))))
DATABASE_PATH = os.getenv("DATABASE_PATH", "wingo45.sqlite3")

# Optional allow-list, comma-separated Telegram numeric chat/user IDs.
# Leave empty to allow anyone who can find the bot to use it.
ALLOWED_CHAT_IDS_RAW = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in ALLOWED_CHAT_IDS_RAW.split(",")
    if x.strip().lstrip("-").isdigit()
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wingo45")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong file .env")
if not OPENAI_API_KEY:
    raise RuntimeError("Thiếu OPENAI_API_KEY trong file .env")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
http_client: Optional[httpx.AsyncClient] = None


# ---------------- DATABASE ----------------
def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                last_target TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                target_issue TEXT NOT NULL,
                prediction TEXT NOT NULL,
                analysis TEXT NOT NULL,
                created_at TEXT NOT NULL,
                actual TEXT,
                correct INTEGER,
                UNIQUE(chat_id, target_issue)
            )
        """)
        conn.commit()


def chat_is_active(chat_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT active FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row and row["active"])


def set_chat_active(chat_id: int, active: bool):
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO chats(chat_id, active, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                active=excluded.active,
                updated_at=excluded.updated_at
        """, (chat_id, int(active), now))
        conn.commit()


def get_active_chats() -> List[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM chats WHERE active=1"
        ).fetchall()
    return [int(r["chat_id"]) for r in rows]


def get_prediction(chat_id: int, issue: str):
    with db_connect() as conn:
        return conn.execute("""
            SELECT * FROM predictions
            WHERE chat_id=? AND target_issue=?
        """, (chat_id, issue)).fetchone()


def save_prediction(chat_id: int, issue: str, prediction: str, analysis: str):
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO predictions
            (chat_id, target_issue, prediction, analysis, created_at)
            VALUES(?,?,?,?,?)
        """, (chat_id, issue, prediction, analysis, now))
        conn.commit()


def settle_predictions(chat_id: int, history: List[Dict[str, str]]):
    """Match pending prediction issue IDs to actual API history."""
    settled = []
    history_map = {x["issue"]: x["side"] for x in history if x.get("issue")}
    with db_connect() as conn:
        pending = conn.execute("""
            SELECT * FROM predictions
            WHERE chat_id=? AND actual IS NULL
        """, (chat_id,)).fetchall()

        for row in pending:
            actual = history_map.get(row["target_issue"])
            if actual not in ("LỚN", "NHỎ"):
                continue
            correct = int(row["prediction"] == actual)
            conn.execute("""
                UPDATE predictions
                SET actual=?, correct=?
                WHERE id=?
            """, (actual, correct, row["id"]))
            settled.append({
                "issue": row["target_issue"],
                "prediction": row["prediction"],
                "actual": actual,
                "correct": correct,
            })
        conn.commit()
    return settled


def stats_for_chat(chat_id: int) -> Dict[str, int]:
    with db_connect() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN correct=0 THEN 1 ELSE 0 END) AS losses
            FROM predictions
            WHERE chat_id=? AND actual IS NOT NULL
        """, (chat_id,)).fetchone()
    total = int(row["total"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 2) if total else 0.0,
    }


# ---------------- API DATA ----------------
def normalize_side(item: Dict[str, Any]) -> Optional[str]:
    raw = str(item.get("result", "")).strip().lower()
    name = str(item.get("name", "")).strip().lower()

    if raw in ("big", "large") or "lớn" in name or "lon" == name:
        return "LỚN"
    if raw in ("small",) or "nhỏ" in name or "nho" == name:
        return "NHỎ"

    # Win Go convention: 0-4 = Nhỏ, 5-9 = Lớn.
    value = str(item.get("value", "")).strip()
    if value.isdigit():
        n = int(value)
        if 0 <= n <= 4:
            return "NHỎ"
        if 5 <= n <= 9:
            return "LỚN"
    return None


def issue_sort_key(issue: str):
    digits = re.sub(r"\D", "", str(issue))
    return int(digits) if digits else 0


async def fetch_game_data() -> Dict[str, Any]:
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "Mozilla/5.0 Wingo45GPTBot/1.0"},
            follow_redirects=True,
        )

    response = await http_client.get(
        API_URL,
        params={"lottery_id": LOTTERY_ID},
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict) or payload.get("code") != 1:
        raise ValueError(f"API trả dữ liệu không hợp lệ: {str(payload)[:500]}")

    data = payload.get("data") or {}
    stats = data.get("statisticsInfo") or {}
    data_list = (stats.get("statisticDataList") or {}).get("bigSmall") or []

    history = []
    seen = set()
    for item in data_list:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue", "")).strip()
        side = normalize_side(item)
        if not issue or not side or issue in seen:
            continue
        seen.add(issue)
        history.append({
            "issue": issue,
            "side": side,
            "value": str(item.get("value", "")),
        })

    history.sort(key=lambda x: issue_sort_key(x["issue"]))
    history = history[-HISTORY_LIMIT:]

    if not history:
        raise ValueError("API không có lịch sử bigSmall hợp lệ.")

    # The newest settled record in statisticDataList is the source of truth.
    latest = history[-1]
    api_last_issue = str(data.get("last_issue", "")).strip()
    current_issue = str(data.get("issue", "")).strip()

    # Prefer issue as the currently open/target issue only if it is newer
    # than the newest settled history record. Otherwise use next_issue_info.
    if current_issue and issue_sort_key(current_issue) > issue_sort_key(latest["issue"]):
        target_issue = current_issue
    else:
        next_info = data.get("next_issue_info") or {}
        target_issue = str(next_info.get("issue", "")).strip()

    return {
        "game_name": data.get("name", "Win Go 45 giây"),
        "latest_history_issue": latest["issue"],
        "latest_history_side": latest["side"],
        "api_last_issue": api_last_issue,
        "current_issue": current_issue,
        "target_issue": target_issue,
        "seconds": data.get("second"),
        "is_open": data.get("is_open"),
        "history": history,
        "raw_time": payload.get("time"),
    }


# ---------------- GPT ANALYSIS ----------------
GPT_INSTRUCTIONS = """
Bạn là chuyên gia phân tích dữ liệu chuỗi Win Go 45 giây.
Chỉ phân tích hai nhãn LỚN và NHỎ. Tuyệt đối bỏ qua màu sắc.
Chỉ dùng lịch sử được cung cấp; không bịa dữ liệu, mã phiên hay kết quả.
Phân tích mô tả: tần suất 20/50/100 phiên, run-length/bệt,
luân phiên 1-1, nhịp 2-2/3-3, các mẫu gần giống, chuyển trạng thái
Markov bậc 1/2/3 nếu đủ mẫu, độ cân bằng và entropy.
Đánh giá độ tin cậy của từng mẫu theo số lần xuất hiện; không kết luận
một mẫu hiếm là tín hiệu mạnh. Nếu có thể, mô tả kiểm định walk-forward
một cách thận trọng; không được bịa con số backtest.
Đưa ra ước tính LỚN/NHỎ cho phiên mục tiêu, hoặc CHƯA RÕ nếu dữ liệu
không đủ bằng chứng. Tổng hai tỷ lệ phải bằng 100%.
Không tuyên bố chắc thắng, không khuyến khích đặt cược.
Trả lời tiếng Việt, ngắn gọn, đúng cấu trúc:
PHIÊN MỤC TIÊU:
DỰ ĐOÁN: LỚN | NHỎ | CHƯA RÕ
TỶ LỆ ƯỚC TÍNH: LỚN xx% - NHỎ yy%
MỨC TIN CẬY: THẤP | TRUNG BÌNH | CAO
PHÂN TÍCH CẦU:
- ...
LÝ DO:
- ...
CẢNH BÁO:
- Đây là ước tính thống kê, không bảo đảm kết quả.
"""


def make_gpt_input(game: Dict[str, Any], manual: bool = False) -> str:
    history = game["history"]
    sequence = " ".join(x["side"] for x in history)
    rows = "\n".join(
        f'{x["issue"]}: {x["side"]} (số {x["value"]})'
        for x in history[-HISTORY_LIMIT:]
    )
    return f"""
Yêu cầu: {"Phân tích ngay theo lệnh người dùng." if manual else "Phân tích khi API xuất hiện phiên mục tiêu mới."}

Tên game: {game["game_name"]}
Phiên có kết quả gần nhất trong danh sách lịch sử: {game["latest_history_issue"]}
Kết quả gần nhất: {game["latest_history_side"]}
Phiên API hiện tại: {game["current_issue"]}
Phiên mục tiêu cần dự đoán: {game["target_issue"]}
Thời gian đếm ngược API (nếu có): {game["seconds"]}

Lịch sử theo thứ tự cũ đến mới:
{rows}

Chuỗi LỚN/NHỎ:
{sequence}

Hãy phân tích độc lập dựa trên dữ liệu thực ở trên.
Không dùng màu sắc. Không tự suy ra kết quả của phiên mục tiêu.
"""


def extract_prediction(text: str) -> str:
    upper = text.upper()
    # Parse only the explicit DỰ ĐOÁN line to avoid matching words in analysis.
    match = re.search(r"DỰ ĐOÁN\s*:\s*(LỚN|NHỎ|CHƯA RÕ)", upper)
    return match.group(1) if match else "CHƯA RÕ"


async def ask_gpt(game: Dict[str, Any], manual: bool = False) -> str:
    prompt = make_gpt_input(game, manual=manual)
    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        instructions=GPT_INSTRUCTIONS,
        input=prompt,
        max_output_tokens=1100,
    )
    output = (response.output_text or "").strip()
    if not output:
        raise RuntimeError("GPT không trả nội dung phân tích.")
    return output


# ---------------- TELEGRAM HELPERS ----------------
def allowed(update: Update) -> bool:
    if not update.effective_chat:
        return False
    if ALLOWED_CHAT_IDS and update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return False
    return True


async def reply(update: Update, text: str):
    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            disable_web_page_preview=True,
        )


def format_header(game: Dict[str, Any]) -> str:
    return (
        "🎯 WINGO 45S – GPT ANALYZER\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Phiên có kết quả gần nhất: {game['latest_history_issue']}\n"
        f"📊 Kết quả gần nhất: {game['latest_history_side']}\n"
        f"🎯 Phiên mục tiêu: {game['target_issue'] or 'Chưa xác định'}\n"
        f"🕒 Countdown API: {game['seconds']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )


async def analyze_and_send(
    bot,
    chat_id: int,
    game: Dict[str, Any],
    manual: bool = False,
):
    target = game.get("target_issue")
    if not target:
        await bot.send_message(
            chat_id,
            "⚠️ API chưa cung cấp mã phiên mục tiêu hợp lệ. Không tự tạo mã phiên."
        )
        return

    existing = get_prediction(chat_id, target)
    if existing and not manual:
        return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    analysis = await ask_gpt(game, manual=manual)
    prediction = extract_prediction(analysis)

    # Keep only one saved prediction per chat + target issue.
    save_prediction(chat_id, target, prediction, analysis)

    title = "🧠 PHÂN TÍCH GPT" if manual else "🆕 PHÁT HIỆN DỮ LIỆU / PHIÊN MỚI"
    message = (
        f"{title}\n"
        f"{format_header(game)}"
        f"{analysis}\n\n"
        "ℹ️ Tỷ lệ là ước tính thống kê, không phải xác suất bảo đảm."
    )
    await bot.send_message(chat_id, message, disable_web_page_preview=True)


async def process_new_results(bot, chat_id: int, game: Dict[str, Any]):
    settled = settle_predictions(chat_id, game["history"])
    for item in settled:
        status = "✅ ĐÚNG" if item["correct"] else "❌ SAI"
        await bot.send_message(
            chat_id,
            "📋 ĐỐI CHIẾU KẾT QUẢ\n"
            f"Phiên: {item['issue']}\n"
            f"GPT dự đoán: {item['prediction']}\n"
            f"Kết quả API: {item['actual']}\n"
            f"Đánh giá: {status}"
        )


# ---------------- COMMANDS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    set_chat_active(chat_id, True)
    await reply(
        update,
        "🟢 ĐÃ BẬT THEO DÕI WINGO 45S\n"
        f"Bot kiểm tra API mỗi {POLL_SECONDS} giây.\n"
        "Khi phát hiện phiên mục tiêu mới, bot gửi dữ liệu sang GPT "
        "để phân tích LỚN/NHỎ rồi thông báo tại đây.\n\n"
        "Lệnh: /du_doan /thongke /stop /id"
    )
    try:
        game = await fetch_game_data()
        await process_new_results(context.bot, chat_id, game)
        await analyze_and_send(context.bot, chat_id, game, manual=False)
    except Exception as exc:
        log.exception("Start immediate check failed")
        await reply(update, f"⚠️ Chưa lấy/phân tích được API: {exc}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    set_chat_active(update.effective_chat.id, False)
    await reply(
        update,
        "🔴 ĐÃ DỪNG THEO DÕI.\n"
        "Bot không tiếp tục gọi GPT tự động cho cuộc trò chuyện này.\n"
        "Gửi /start để bật lại."
    )


async def cmd_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    try:
        game = await fetch_game_data()
        await process_new_results(context.bot, chat_id, game)
        await analyze_and_send(context.bot, chat_id, game, manual=True)
    except Exception as exc:
        log.exception("Manual prediction failed")
        await reply(update, f"⚠️ Lỗi khi dự đoán: {exc}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    s = stats_for_chat(update.effective_chat.id)
    await reply(
        update,
        "📈 THỐNG KÊ DỰ ĐOÁN ĐÃ ĐỐI CHIẾU\n"
        f"Tổng phiên: {s['total']}\n"
        f"Đúng: {s['wins']}\n"
        f"Sai: {s['losses']}\n"
        f"Tỷ lệ đúng: {s['win_rate']}%\n\n"
        "Chỉ tính các dự đoán đã có kết quả thật từ API."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await reply(
        update,
        f"Chat ID: {update.effective_chat.id}\n"
        "Dùng ID này trong TELEGRAM_ALLOWED_CHAT_IDS nếu muốn khóa bot."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await reply(
        update,
        "WINGO 45S GPT BOT\n\n"
        "/start - Bật theo dõi API và GPT\n"
        "/stop - Dừng theo dõi tự động\n"
        "/du_doan - Lấy dữ liệu và yêu cầu GPT phân tích ngay\n"
        "/thongke - Thống kê đúng/sai đã đối chiếu\n"
        "/id - Xem Chat ID\n"
        "/help - Hướng dẫn\n\n"
        "Chỉ phân tích LỚN/NHỎ, không phân tích màu."
    )


# ---------------- BACKGROUND MONITOR ----------------
async def monitor_loop(application: Application):
    log.info("Monitor loop started")
    while True:
        try:
            active_chats = get_active_chats()
            if active_chats:
                game = await fetch_game_data()
                for chat_id in active_chats:
                    try:
                        await process_new_results(application.bot, chat_id, game)

                        target = game.get("target_issue")
                        if target:
                            old = get_prediction(chat_id, target)
                            if not old:
                                await analyze_and_send(
                                    application.bot, chat_id, game, manual=False
                                )
                    except Exception:
                        log.exception("Error processing chat %s", chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Monitor API cycle failed")

        await asyncio.sleep(POLL_SECONDS)


async def post_init(application: Application):
    init_db()
    application.bot_data["monitor_task"] = asyncio.create_task(
        monitor_loop(application)
    )
    log.info("Bot initialized")


async def post_shutdown(application: Application):
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if http_client:
        await http_client.aclose()
    await openai_client.close()


def main():
    init_db()
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("du_doan", cmd_predict))
    app.add_handler(CommandHandler("thongke", cmd_stats))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("help", cmd_help))

    log.info("Starting WINGO 45S GPT Telegram bot...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
