# Media Deal Radar — V1

A lightweight personal deal monitor for **r/MediaSwap** focused on:

- Arrow 4K Limited Editions
- Second Sight 4K Limited Editions
- Specific title + price thresholds that you define
- Immediate Telegram alerts
- Great / Interesting / Pass buttons on each alert so your preferences can be recorded

## How V1 works

About once per minute the app checks the MediaSwap **new-post RSS feed**.

Each unseen post is scored:

1. **Specific target title** rules are strongest.
2. If no specific title matches, it looks for Arrow / Second Sight + 4K + Limited Edition language.
3. Relevant posts are sent to Telegram.
4. Your Telegram button feedback is stored locally in `deal_radar.db`.

The current feedback is deliberately stored rather than automatically training an AI model on Reddit content. In a later version, we can use your feedback to tune your own thresholds/rules.

## Setup

### 1. Install Python

Use Python 3.11+.

### 2. Install dependencies

Open Command Prompt in this folder:

    pip install -r requirements.txt

### 3. Create your Telegram bot

In Telegram:

1. Message **@BotFather**
2. Run `/newbot`
3. Copy the bot token
4. Send any message to your new bot
5. Get your `chat_id` by opening:

   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`

6. Copy `.env.example` to a new file named `.env`
7. Paste your token and chat ID into `.env`

### 4. Add your real targets

Open `config.json`.

For each title, set:

- `title`
- `aliases`
- `label`
- `great_buy_max`
- `good_buy_max`

Example:

    {
      "title": "Drive",
      "aliases": ["drive second sight", "drive le"],
      "label": "Second Sight",
      "format": "4K",
      "edition": "Limited Edition",
      "great_buy_max": 80,
      "good_buy_max": 100
    }

### 5. Run it

    python deal_radar.py

On its **first launch**, it marks posts already in the feed as seen. That prevents a flood of old alerts.

After that, new matching posts will trigger Telegram alerts.

## Running 24/7

For true constant monitoring, run this on something that stays online:

- your always-on Windows PC
- a small Raspberry Pi
- a cheap VPS
- a cloud worker/container

The core app does not depend on any one hosting platform.

## Architecture for V2+

The source layer is intentionally replaceable. Future monitors can feed the same scoring engine:

- Reddit approved API / Developer Platform
- eBay search/API
- Orbit DVD
- DiabolikDVD
- Atomic Movie Store
- Odyssey Movies
- boutique retailer restocks
- other permitted sources

Facebook Groups are a separate question because access is much more restricted and should only be added through a permitted/authorized mechanism.

## Important

This is a personal alerting tool, not an auto-buy bot. It does not purchase items or message sellers automatically.


## V1.1 rate-limit fix

V1.1 changes Reddit polling from 20 seconds to 60 seconds, avoids an immediate second fetch after first-run bootstrap, and respects HTTP 429 rate-limit responses with a longer retry delay.
