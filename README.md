# 拓元售票監控腳本

自動監控 tixcraft.com 售票頁面，有票況變化時透過 Telegram Bot 發送通知。

## 安裝

### 1. 確認 Python 版本

需要 Python 3.8 以上。

```bash
python3 --version
```

### 2. 安裝套件

```bash
pip3 install -r requirements.txt
```

### 3. 確認 .env 設定

`.env` 已預先填好，確認內容正確：

```
TELEGRAM_BOT_TOKEN=你的 Bot Token
TELEGRAM_CHAT_ID=你的 Chat ID
TARGET_URL=要監控的售票頁面網址
```

### 4. （選用）安裝 ChromeDriver

腳本在 `requests` 被擋住時會自動切換 `selenium + headless Chrome`。
若要使用此功能，需要安裝 ChromeDriver：

```bash
# Mac（使用 Homebrew）
brew install --cask chromedriver

# 或透過 pip 自動管理（推薦）
pip3 install webdriver-manager
```

> 若不安裝 ChromeDriver，腳本仍可執行，但被反爬蟲擋住時無法自動切換。

## 啟動

```bash
python3 monitor.py
```

腳本會持續執行，每 3 分鐘檢查一次，Ctrl+C 可停止。

## 通知格式

```
🎫 拓元票務通知
狀態變更：尚未開賣 → 可購票（偵測到「立即購票」）
頁面：https://tixcraft.com/ticket/area/...
時間：2026-03-26 15:00:00
```

## 常見問題

**Q：Telegram 收不到通知？**
先確認 Bot Token 與 Chat ID 正確，並確保已對 Bot 發過一則訊息（讓 Bot 可以傳送給你）。

**Q：一直顯示「抓取失敗」？**
安裝 ChromeDriver 後重試，或確認網路是否正常。
