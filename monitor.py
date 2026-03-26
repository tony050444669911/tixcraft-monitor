import os
import re
import sys
import time
import signal
import logging
import subprocess
import requests
import psutil
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL")

CHECK_INTERVAL_SEC = 15  # 每幾秒檢查一次
NO_TICKET_ALERT = 3600  # 連續幾秒沒票就發憐憫通知（1 小時）

# 身障/輪椅區關鍵字 → 忽略這些區域
DISABILITY_KEYWORDS = ["身障", "輪椅", "殘障", "身心障礙"]

# 電量警告門檻
BATTERY_WARN_30 = 30
BATTERY_WARN_15 = 15
BATTERY_CRITICAL_10 = 10
USER_IDLE_THRESHOLD = 300  # 超過幾秒沒操作視為「沒人在用」（5 分鐘）

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
        log.error(f"selenium 抓取失敗（session 可能已壞）：{e}")
        return None


def restart_driver(old_driver):
    """Chrome session 壞掉時重建"""
    try:
        old_driver.quit()
    except Exception:
        pass
    log.info("重新啟動 Chrome...")
    try:
        return create_driver()
    except Exception as e:
        log.error(f"Chrome 重啟失敗：{e}")
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
    lines = ["🚨🚨🚨 <b>有票！快搶！快搶！快搶！</b> 🚨🚨🚨", f"時間：{ts}\n", "<b>目前有票的區域：</b>"]
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


def wait_next():
    log.info(f"等待 {CHECK_INTERVAL_SEC} 秒後執行下一次檢查...")
    time.sleep(CHECK_INTERVAL_SEC)


def check_once(driver=None):
    html = fetch_with_requests(TARGET_URL)
    if html is None:
        html = fetch_with_selenium(TARGET_URL, driver)
    if html is None:
        return "抓取失敗", []
    return parse_status(html)


FAIL_STATUS = "抓取失敗"


LOCK_FILE = "/tmp/tixcraft_monitor.lock"


def acquire_lock():
    """確保只有一個執行實例，防止重複通知"""
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            old_pid = f.read().strip()
        # 檢查舊的 PID 是否還活著
        if old_pid and os.path.exists(f"/proc/{old_pid}"):
            log.error(f"已有另一個執行中的實例（PID {old_pid}），退出")
            sys.exit(0)
        # macOS 用 kill -0 檢查 PID
        try:
            os.kill(int(old_pid), 0)
            log.error(f"已有另一個執行中的實例（PID {old_pid}），退出")
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass  # 舊 PID 已不存在，繼續
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    log.info(f"取得執行鎖（PID {os.getpid()}）")


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def get_idle_seconds() -> float:
    """取得 macOS 系統閒置秒數（上次鍵盤/滑鼠動作距今）"""
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000  # 奈秒 → 秒
    except Exception:
        pass
    return 0


def is_user_active() -> bool:
    return get_idle_seconds() < USER_IDLE_THRESHOLD


def check_battery(notified: set) -> set:
    """
    檢查電量，依狀況發通知或讓筆電睡眠。
    notified：已發過通知的門檻集合（避免重複），充電後清空。
    回傳更新後的 notified。
    """
    try:
        batt = psutil.sensors_battery()
        if batt is None:
            return notified  # 非筆電環境，略過

        pct = int(batt.percent)
        plugged = batt.power_plugged

        # 充電中 → 重置所有門檻
        if plugged:
            if notified:
                log.info("已充電，重置電量警告門檻")
            return set()

        ts = now_str()
        log.info(f"電量：{pct}%")

        if pct <= BATTERY_CRITICAL_10 and BATTERY_CRITICAL_10 not in notified:
            if is_user_active():
                send_telegram(
                    f"🪫 <b>電量剩 {pct}%！請立刻充電！</b>\n"
                    f"偵測到有人正在使用，腳本繼續運行，但請趕快插電！\n時間：{ts}"
                )
            else:
                send_telegram(
                    f"🪫 <b>電量剩 {pct}%，筆電即將進入睡眠</b>\n"
                    f"沒有偵測到使用者操作，腳本停止並讓筆電睡眠以保護電池。\n"
                    f"充電後記得重新啟動腳本！\n時間：{ts}"
                )
                log.warning("電量過低且無人使用，進入睡眠")
                release_lock()
                time.sleep(3)
                subprocess.run(["pmset", "sleepnow"])
                sys.exit(0)
            notified.add(BATTERY_CRITICAL_10)

        elif pct <= BATTERY_WARN_15 and BATTERY_WARN_15 not in notified:
            send_telegram(
                f"🔋 <b>電量剩 {pct}%，快去充電！</b>\n"
                f"剩下不多了，再不充電腳本會自動在 {BATTERY_CRITICAL_10}% 時停止。\n時間：{ts}"
            )
            notified.add(BATTERY_WARN_15)

        elif pct <= BATTERY_WARN_30 and BATTERY_WARN_30 not in notified:
            send_telegram(
                f"🔋 <b>電量剩 {pct}%，記得充電喔！</b>\n"
                f"刷票機器人還在努力幫你守著，幫它插個電吧～\n時間：{ts}"
            )
            notified.add(BATTERY_WARN_30)

    except Exception as e:
        log.warning(f"電量檢查失敗：{e}")

    return notified


def send_startup_notification():
    ts = now_str()
    msg = (
        "🎉🎊 <b>嗨大家！刷票機器人已上線！</b> 🎊🎉\n\n"
        "從現在開始我會幫你們緊緊盯著拓元售票頁面，\n"
        "有票的話我會馬上大聲告訴你們！🚨\n\n"
        "所以請放心去過你們美好的生活吧 💪\n"
        "認真上班、好好吃飯、開心玩耍——\n"
        "搶票的事交給我就好！\n\n"
        "愛你們每一個人 🥰\n"
        f"監控開始時間：{ts}"
    )
    send_telegram(msg)


def setup_shutdown_handler():
    """收到系統關機/終止信號時，先發 Telegram 通知再結束"""
    def handler(signum, frame):
        ts = now_str()
        msg = (
            f"⚠️ <b>監控腳本已停止！</b>\n"
            f"電腦可能關機或腳本被中斷，請記得重新啟動！\n\n"
            f'🔗 <a href="{TARGET_URL}">售票頁面</a>\n'
            f"時間：{ts}"
        )
        log.warning("收到終止信號，發送停止通知...")
        send_telegram(msg)
        release_lock()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handler)


def main():
    global last_status, last_ticket_time, last_pity_time

    if not all([BOT_TOKEN, CHAT_ID, TARGET_URL]):
        log.error("請確認 .env 已設定 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID、TARGET_URL")
        return

    acquire_lock()
    setup_shutdown_handler()
    log.info(f"監控開始，目標：{TARGET_URL}")
    log.info(f"每 {CHECK_INTERVAL_SEC} 秒執行一次檢查，身障/輪椅區不通知")
    send_startup_notification()

    driver = None
    if fetch_with_requests(TARGET_URL) is None:
        log.info("requests 被擋，啟動 Chrome（保持開著以節省資源）")
        try:
            driver = create_driver()
        except Exception as e:
            log.error(f"Chrome 啟動失敗：{e}")

    start_time = time.time()
    last_pity_time = start_time

    consecutive_fails = 0
    battery_notified = set()
    battery_check_counter = 0

    try:
        while True:
            wait_next()

            # 每 4 次檢查（約 1 分鐘）確認一次電量
            battery_check_counter += 1
            if battery_check_counter >= 4:
                battery_notified = check_battery(battery_notified)
                battery_check_counter = 0

            try:
                status, areas = check_once(driver)
                ts = now_str()
                now_ts = time.time()

                # Chrome 連續失敗 3 次 → 自動重建，不發通知
                if status == FAIL_STATUS:
                    consecutive_fails += 1
                    log.warning(f"連續抓取失敗 {consecutive_fails} 次")
                    if consecutive_fails >= 3 and driver is not None:
                        log.info("Chrome 疑似崩潰，自動重建中...")
                        driver = restart_driver(driver)
                        consecutive_fails = 0
                    continue

                consecutive_fails = 0

                area_summary = " | ".join(
                    a["name"] + (f" 剩{a['remaining']}" if a["remaining"] else "")
                    for a in areas[:3]
                ) if areas else ""
                log.info(f"狀態：{status}" + (f"（{area_summary}）" if area_summary else ""))

                if status == "有票":
                    last_ticket_time = now_ts
                    last_pity_time = now_ts
                    msg = format_ticket_notification(areas, ts)
                    send_telegram(msg)
                    last_status = status
                else:
                    if last_status is None:
                        last_status = status
                        log.info(f"初始狀態：{status}")
                    elif status != last_status:
                        # 只有「尚未開賣」這種重要狀態才通知，其他沒票的變化不打擾
                        if status == "尚未開賣":
                            send_telegram(
                                f"📢 售票狀態：{status}\n\n"
                                f'🔗 <a href="{TARGET_URL}">售票頁面</a>\n時間：{ts}'
                            )
                        log.info(f"狀態變更：{last_status} → {status}（不通知）")
                        last_status = status
                    else:
                        log.info("狀態無變化")

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
        release_lock()
        if driver:
            driver.quit()
            log.info("Chrome 已關閉")


if __name__ == "__main__":
    main()
