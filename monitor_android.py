"""
monitor_android.py — 安卓平板專用版（不需要 Chrome/Selenium）
"""
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL")

CHECK_SECOND = 50        # 每分鐘第幾秒執行檢查
NO_TICKET_ALERT = 3600  # 連續幾秒沒票就發憐憫通知（1 小時）
DISABILITY_KEYWORDS = ["身障", "輪椅", "殘障", "身心障礙"]
MAX_RETRY = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# 模擬不同瀏覽器的 headers，輪流嘗試繞過反爬蟲
HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://tixcraft.com/",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://tixcraft.com/activity",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    },
]

last_status = None
last_pity_time = None
header_index = 0  # 輪流切換 headers


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_telegram(message: str):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    chat_ids = [cid.strip() for cid in CHAT_ID.split(",") if cid.strip()]
    for chat_id in chat_ids:
        for attempt in range(MAX_RETRY):
            try:
                resp = requests.post(
                    api_url,
                    json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=15,
                )
                if resp.ok:
                    log.info(f"Telegram 通知已送出（{chat_id}）")
                    break
                log.warning(f"Telegram 回應異常（{chat_id}）：{resp.status_code}")
            except Exception as e:
                log.warning(f"Telegram 傳送失敗（第 {attempt+1} 次）：{e}")
            time.sleep(3)
        else:
            log.error(f"Telegram 通知失敗（{chat_id}）")


def fetch_page(url: str) -> str | None:
    global header_index
    headers = HEADERS_LIST[header_index % len(HEADERS_LIST)]
    header_index += 1

    session = requests.Session()
    # 先造訪首頁取得 cookie，再訪問目標頁面
    try:
        session.get("https://tixcraft.com/", headers=headers, timeout=10)
    except Exception:
        pass

    try:
        resp = session.get(url, headers=headers, timeout=20)
        html = resp.text.strip()

        if resp.status_code in (401, 403):
            log.warning(f"被擋住（{resp.status_code}），下次換 headers 重試")
            return None
        if len(html) < 500 or (html.startswith("{") and "identify" in html):
            log.warning(f"內容異常（{len(html)} chars），下次換 headers 重試")
            return None
        return html
    except Exception as e:
        log.warning(f"抓取失敗：{e}")
        return None


def is_disability_zone(text: str) -> bool:
    return any(kw in text for kw in DISABILITY_KEYWORDS)


def parse_available_areas(soup: BeautifulSoup) -> list[dict]:
    available = []
    seen = set()

    for tag in soup.find_all(["a", "li"]):
        text = tag.get_text(separator=" ", strip=True)
        if not text or "區" not in text or len(text) > 80:
            continue
        if any(kw in text for kw in ["關於拓元", "服務條款", "隱私權", "常見問題", "訂單查詢", "選擇區域"]):
            continue
        if "已售完" in text or "售罄" in text:
            continue
        if is_disability_zone(text):
            continue
        if text in seen:
            continue
        seen.add(text)

        remaining = None
        m = re.search(r"剩餘\s*(\d+)", text)
        if m:
            remaining = int(m.group(1))

        price = None
        pm = re.search(r"\b(\d{3,5})\b", text)
        if pm:
            price = int(pm.group(1))

        available.append({"name": text, "remaining": remaining, "price": price})

    return available


def parse_status(html: str) -> tuple[str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator=" ", strip=True)

    if len(full_text.strip()) < 200:
        return "頁面內容異常", []
    if "請先登入" in full_text or "請登入後再繼續" in full_text:
        return "需要登入", []
    if any(kw in full_text for kw in ["尚未開賣", "未開賣", "即將開賣", "Coming Soon"]):
        return "尚未開賣", []

    available = parse_available_areas(soup)
    if available:
        return "有票", available

    if "已售完" in full_text or "售罄" in full_text:
        return "全部售完", []

    return "未開放購票（等待中）", []


def format_ticket_notification(areas: list[dict], ts: str) -> str:
    lines = ["🎫 <b>拓元票務通知 — 有票可搶！</b>", f"時間：{ts}\n", "<b>目前有票的區域：</b>"]
    for a in areas:
        parts = []
        if a["remaining"] is not None:
            parts.append(f"剩餘 <b>{a['remaining']} 張</b>")
        if a["price"] is not None:
            parts.append(f"NT${a['price']}")
        detail = "　" + "、".join(parts) if parts else ""
        lines.append(f"・{a['name']}{detail}")
    lines.append(f"\n🔗 <a href=\"{TARGET_URL}\">點我立即搶票</a>")
    return "\n".join(lines)


def format_pity(elapsed_hours: float, ts: str) -> str:
    return (
        f"😔 <b>拓元票務通知</b>\n"
        f"已連續監控 <b>{elapsed_hours:.0f} 小時</b>仍未搶到一般區票票\n"
        f"繼續加油，不要放棄！\n\n"
        f'🔗 <a href="{TARGET_URL}">售票頁面</a>\n\n'
        f"時間：{ts}"
    )


def wait_until_check_second():
    now = datetime.now()
    current_second = now.second
    wait = CHECK_SECOND - current_second if current_second < CHECK_SECOND else 60 - current_second + CHECK_SECOND
    if wait > 0:
        log.info(f"等待 {wait} 秒後於每分鐘第 {CHECK_SECOND} 秒執行檢查...")
        time.sleep(wait)


def main():
    global last_status, last_pity_time

    if not all([BOT_TOKEN, CHAT_ID, TARGET_URL]):
        log.error("請確認 .env 已設定 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID、TARGET_URL")
        return

    log.info(f"監控開始（安卓版），目標：{TARGET_URL}")
    log.info(f"每分鐘第 {CHECK_SECOND} 秒執行，身障/輪椅區不通知")

    start_time = time.time()
    last_pity_time = start_time

    while True:
        wait_until_check_second()

        try:
            html = fetch_page(TARGET_URL)
            ts = now_str()

            if html is None:
                log.warning("頁面抓取失敗，下一輪重試")
                continue

            status, areas = parse_status(html)
            now_ts = time.time()

            area_summary = " | ".join(
                a["name"] + (f" 剩{a['remaining']}" if a["remaining"] else "")
                for a in areas[:3]
            ) if areas else ""
            log.info(f"狀態：{status}" + (f"（{area_summary}）" if area_summary else ""))

            if status == "有票":
                last_pity_time = now_ts
                msg = format_ticket_notification(areas, ts)
                send_telegram(msg)
                last_status = status
            else:
                if last_status is None:
                    last_status = status
                    log.info(f"初始狀態：{status}")
                elif status != last_status:
                    send_telegram(
                        f"🎫 <b>拓元票務通知</b>\n狀態變更：{last_status} → {status}\n\n"
                        f'🔗 <a href="{TARGET_URL}">售票頁面</a>\n時間：{ts}'
                    )
                    last_status = status
                else:
                    log.info("狀態無變化")

                elapsed = now_ts - last_pity_time
                if elapsed >= NO_TICKET_ALERT:
                    elapsed_hours = (now_ts - start_time) / 3600
                    send_telegram(format_pity(elapsed_hours, ts))
                    log.info("發送憐憫通知")
                    last_pity_time = now_ts

        except KeyboardInterrupt:
            log.info("結束監控")
            break
        except Exception as e:
            log.error(f"未預期錯誤：{e}，下一輪繼續")


if __name__ == "__main__":
    main()
