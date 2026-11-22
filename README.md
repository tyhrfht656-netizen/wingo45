# WINGO 45S GPT Telegram Bot

## What it does
- Polls the Win Go 45s API.
- Reads only `statisticsInfo.statisticDataList.bigSmall`.
- Sends history to the OpenAI Responses API when a new target issue is detected.
- Sends the GPT analysis to Telegram.
- Checks saved predictions against actual API history and reports hit/miss.
- Commands: `/start`, `/stop`, `/du_doan`, `/thongke`, `/id`, `/help`.

## Important
This uses the OpenAI API, not the ChatGPT website session. API usage is billed separately.
The model's estimates are not guaranteed outcomes. This bot does not place bets.

## Install (Ubuntu/VPS)
```bash
sudo apt update
sudo apt install -y python3 python3-venv
mkdir -p ~/wingo45bot
cd ~/wingo45bot
# Upload bot.py, requirements.txt and .env.example here
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

## Run in background with systemd
Create `/etc/systemd/system/wingo45bot.service`:
```ini
[Unit]
Description=WINGO 45S GPT Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/wingo45bot
ExecStart=/root/wingo45bot/.venv/bin/python /root/wingo45bot/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
Change `/root/wingo45bot` to your actual directory, then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wingo45bot
sudo systemctl status wingo45bot
journalctl -u wingo45bot -f
```

## Termux
```bash
pkg update -y
pkg install python nano -y
mkdir -p ~/wingo45bot
cd ~/wingo45bot
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

## Telegram commands
- `/start`: enable monitoring for this chat and perform an immediate check.
- `/stop`: disable monitoring for this chat.
- `/du_doan`: fetch current API data and ask GPT to analyze immediately.
- `/thongke`: show settled prediction hit/miss stats.
- `/id`: show Telegram chat ID.
- `/help`: show help.

## Data interpretation
The bot uses the newest item in `bigSmall` history as the newest settled result.
It targets `data.issue` only when it is newer than that history item;
otherwise it uses `next_issue_info.issue`. Verify this mapping against the
game provider's actual settlement behavior before relying on notifications.

## Security
- Never share `.env`, Telegram token, or OpenAI API key.
- Set `TELEGRAM_ALLOWED_CHAT_IDS` after getting your chat ID with `/id`.
- The supplied third-party API may change its schema or block automated requests.
