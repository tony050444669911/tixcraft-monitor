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

# 身障/輪椅區關鍵字 → 忽略這些區域
DISABILITY_KEYWORDS = ["身障", "輪椅", "殘障", "身心障礙"]

MAX_RETRY = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://tixcraft.com/",
}

last_status = None
last_ticket_time = None   # 上次偵測到一般區有票的時間
last_pity_time = None     # 上次發憐憫通知的時間


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
                log.warning(f"Telegram 回應異常（{chat_id}）：{resp.status_code} {resp.text}")
            except Exception as e:
                log.warning(f"Telegram 傳送失敗（{chat_id}，第 {attempt+1} 次）：{e}")
            time.sleep(3)
        else:
            log.error(f"Telegram 通知失敗（{chat_id}）")


def fetch_with_requests(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        html = resp.text.strip()
        if resp.status_code in (401, 403):
            log.warning(f"requests 被擋住（{resp.status_code}），切換 selenium")
            return None
        if len(html) < 500:
            log.warning(f"requests 內容過短（{len(html)} chars），切換 selenium")
            return None
        if html.startswith("{") and "identify" in html:
            log.warning("tixcraft 反爬蟲偵測（identify），切換 selenium")
            return None
        if "驗證" in html and "tixcraft" not in html.lower():
            log.warning("疑似驗證頁面，切換 selenium")
            return None
        return html
    except Exception as e:
        log.warning(f"requests 抓取失敗：{e}")
        return None


def create_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)
    return driver


def fetch_with_selenium(url: str, driver=None):
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By

        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(1)
        return driver.page_source
    except Exception as e:
        log.error(f"selenium 抓取失敗：{e}")
        return None


def is_disability_zone(text: str) -> bool:
    return any(kw in text for kw in DISABILITY_KEYWORDS)


def parse_available_areas(soup: BeautifulSoup) -> list[dict]:
    """
    掃描頁面上所有票區，回傳有票的一般區域（排除身障/輪椅區）。
    每筆格式：{"name": "...", "remaining": 2, "price": 5880}
    """
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
        # 排除身障/輪椅區
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
        return "頁面內容異常（可能被擋）", []

    if "請先登入" in full_text or "請登入後再繼續" in full_text:
        return "需要登入", []

    if any(kw in full_text for kw in ["尚未開賣", "未開賣", "即將開賣", "Coming Soon", "敬請期待"]):
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


def format_status_change(status: str, prev: str, ts: str) -> str:
    return (
        f"🎫 <b>拓元票務通知</b>\n"
        f"狀態變更：{prev} → {status}\n\n"
        f'🔗 <a href="{TARGET_URL}">點我立即搶票</a>\n\n'
        f"時間：{ts}"
    )


def format_pity(elapsed_hours: float, ts: str) -> str:
    return (
        f"😔 <b>拓元票務通知</b>\n"
        f"已連續監控 <b>{elapsed_hours:.0f} 小時</b>仍未搶到一般區票票\n"
        f"繼續加油，不要放棄！\n\n"
        f'🔗 <a href="{TARGET_URL}">售票頁面</a>\n\n'
        f"時間：{ts}"
    )


def wait_until_check_second():
    """等到下一個整分鐘的第 CHECK_SECOND 秒"""
    now = datetime.now()
    current_second = now.second
    if current_second < CHECK_SECOND:
        wait = CHECK_SECOND - current_second
    else:
        wait = 60 - current_second + CHECK_SECOND
    if wait > 0:
        log.info(f"等待 {wait} 秒後於每分鐘第 {CHECK_SECOND} 秒執行檢查...")
        time.sleep(wait)


def check_once(driver=None):
    html = fetch_with_requests(TARGET_URL)
    if html is None:
        html = fetch_with_selenium(TARGET_URL, driver)
    if html is None:
        return "抓取失敗", []
    return parse_status(html)


def main():
    global last_status, last_ticket_time, last_pity_time

    if not all([BOT_TOKEN, CHAT_ID, TARGET_URL]):
        log.error("請確認 .env 已設定 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID、TARGET_URL")
        return

    log.info(f"監控開始，目標：{TARGET_URL}")
    log.info(f"每分鐘第 {CHECK_SECOND} 秒執行檢查，身障/輪椅區不通知")

    driver = None
    if fetch_with_requests(TARGET_URL) is None:
        log.info("requests 被擋，啟動 Chrome（保持開著以節省資源）")
        try:
            driver = create_driver()
        except Exception as e:
            log.error(f"Chrome 啟動失敗：{e}")

    start_time = time.time()
    last_pity_time = start_time

    try:
        while True:
            wait_until_check_second()

            try:
                status, areas = check_once(driver)
                ts = now_str()
                now_ts = time.time()

                area_summary = " | ".join(
                    a["name"] + (f" 剩{a['remaining']}" if a["remaining"] else "")
                    for a in areas[:3]
                ) if areas else ""
                log.info(f"狀態：{status}" + (f"（{area_summary}）" if area_summary else ""))

                if status == "有票":
                    last_ticket_time = now_ts
                    last_pity_time = now_ts  # 有票就重置憐憫計時
                    msg = format_ticket_notification(areas, ts)
                    send_telegram(msg)
                    last_status = status
                else:
                    # 狀態變化通知
                    if last_status is None:
                        last_status = status
                        log.info(f"初始狀態：{status}")
                    elif status != last_status:
                        msg = format_status_change(status, last_status, ts)
                        send_telegram(msg)
                        log.info(f"狀態變更：{last_status} → {status}")
                        last_status = status
                    else:
                        log.info("狀態無變化")

                    # 連續 1 小時沒一般區票 → 憐憫通知
                    elapsed = now_ts - last_pity_time
                    if elapsed >= NO_TICKET_ALERT:
                        elapsed_hours = (now_ts - start_time) / 3600
                        msg = format_pity(elapsed_hours, ts)
                        send_telegram(msg)
                        log.info(f"已 {elapsed/60:.0f} 分鐘沒票，發送憐憫通知")
                        last_pity_time = now_ts

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error(f"未預期錯誤：{e}，下一輪繼續")
                continue

    except KeyboardInterrupt:
        log.info("使用者中斷，結束監控")
    finally:
        if driver:
            driver.quit()
            log.info("Chrome 已關閉")


if __name__ == "__main__":
    main()
