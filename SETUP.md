# OloidBot — Setup Guide

AI-powered email assistant for Slack. Each team member connects their own Gmail
and gets summaries, search, and conversational access to their inbox.

---

## Step 1: Create the Slack App

1. Go to **https://api.slack.com/apps**
2. Click **"Create New App"** → **"From a manifest"**
3. Select your workspace
4. Switch to **JSON** tab and paste the contents of `manifest.json` from this repo
5. Click **Create**

### Get your tokens

After the app is created:

1. **Bot Token:**
   - Go to **OAuth & Permissions** in the sidebar
   - Click **"Install to Workspace"** and authorize
   - Copy the **Bot User OAuth Token** (`xoxb-...`)

2. **App-Level Token:**
   - Go to **Basic Information** → scroll to **"App-Level Tokens"**
   - Click **"Generate Token and Scopes"**
   - Name it anything (e.g., `socket-mode`)
   - Add the scope: **`connections:write`**
   - Click **Generate** and copy the token (`xapp-...`)

3. **Enable Socket Mode:**
   - Go to **Socket Mode** in the sidebar
   - Toggle **"Enable Socket Mode"** ON

4. **Enable Events:**
   - Go to **Event Subscriptions**
   - Toggle **"Enable Events"** ON
   - Under **"Subscribe to bot events"**, ensure these are listed:
     - `app_mention`
     - `message.im`

5. **Enable DMs:**
   - Go to **App Home** in the sidebar
   - Under **"Show Tabs"**, enable **"Messages Tab"**
   - Check **"Allow users to send Slash commands and messages from the messages tab"**

---

## Step 2: Set Up Google Cloud (Gmail API)

1. Go to **https://console.cloud.google.com/**
2. Create a new project (or use existing)
3. Go to **APIs & Services → Library**
4. Search for **"Gmail API"** and **Enable** it
5. Go to **APIs & Services → Credentials**
6. Click **"Create Credentials" → "OAuth client ID"**
7. If prompted, configure the **OAuth consent screen** first:
   - User type: **Internal** (for your org) or **External** (for testing)
   - App name: `OloidBot`
   - Add scope: `https://www.googleapis.com/auth/gmail.readonly`
   - Add your test users if External
8. Back in Credentials, create **OAuth client ID**:
   - Application type: **Web application**
   - Name: `OloidBot`
   - Authorized redirect URIs: **`http://localhost:8080/oauth/callback`**
     (change host/port if you deploy remotely)
9. Copy the **Client ID** and **Client Secret**

---

## Step 3: Configure Environment

```bash
cd OloidSummarizer
cp .env.example .env
```

Edit `.env` with your values:

```env
# Slack tokens (from Step 1)
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token

# Google OAuth (from Step 2)
GOOGLE_CLIENT_ID=123456789.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-secret

# Must match the redirect URI in Google Cloud Console
OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback
OAUTH_SERVER_PORT=8080

# Default LLM for all users (users can override via /set-llm)
LLM_PROVIDER=openai
LLM_API_KEY=sk-your-openai-key
LLM_MODEL=gpt-4o
```

---

## Step 4: Install & Run

```bash
pip install -r requirements.txt
python main.py
```

You should see:

```
OloidBot starting...
OAuth callback server running on port 8080
Slack bot starting in Socket Mode...
```

---

## Step 5: Use It in Slack

### For each team member:

1. **Connect Gmail:** Type `/connect-gmail` in any channel or DM with the bot
   - Click the authorization link
   - Sign in with your Google account
   - Allow access → return to Slack
   - You'll get a confirmation message

2. **Summarize emails:** `/summarize` or `/summarize 20`

3. **List emails with links:** `/emails` or `/emails 5`

4. **Chat with the bot:** DM the bot with natural language:
   - "What emails did I get today?"
   - "Summarize emails from john@example.com"
   - "Any urgent emails?"

5. **View settings:** `/mysettings`

6. **Change AI provider:** `/set-llm anthropic sk-ant-key claude-sonnet-4-20250514`
   - Or reset to default: `/set-llm default`

7. **Disconnect Gmail:** `/disconnect-gmail`

### Slash Commands Reference

| Command                             | Description                              |
| ----------------------------------- | ---------------------------------------- |
| `/connect-gmail`                    | Link your Gmail account                  |
| `/disconnect-gmail`                 | Remove your Gmail connection             |
| `/summarize [N]`                    | AI summary of last N emails (default 10) |
| `/emails [N]`                       | List last N emails with Gmail links      |
| `/set-llm <provider> <key> [model]` | Override AI provider for yourself        |
| `/mysettings`                       | View your current configuration          |

---

## Optional: Morning Digest

Set these in `.env` to auto-send email digests:

```env
DIGEST_ENABLED=true
DIGEST_CRON_HOUR=9
DIGEST_CRON_MINUTE=0
DIGEST_CHANNEL=C0123456789   # Channel ID, or leave empty to DM each user
```

---

## Supported LLM Providers

| Provider    | Example model              | Notes                    |
| ----------- | -------------------------- | ------------------------ |
| `openai`    | `gpt-4o`                   | Default                  |
| `anthropic` | `claude-sonnet-4-20250514` |                          |
| `gemini`    | `gemini-1.5-pro`           |                          |
| `groq`      | `llama-3.1-70b-versatile`  | Fast inference           |
| `mistral`   | `mistral-large-latest`     |                          |
| `cohere`    | `command-r-plus`           |                          |
| `ollama`    | `llama3.1`                 | Local, no API key needed |

---

## Deploying Remotely

If you deploy on a server instead of localhost:

1. Change `OAUTH_REDIRECT_URI` in `.env` to your server's public URL:
   ```
   OAUTH_REDIRECT_URI=https://your-server.com:8080/oauth/callback
   ```
2. Update the **Authorized redirect URI** in Google Cloud Console to match
3. Ensure port 8080 is accessible from the internet (or use a reverse proxy)

Socket Mode means the Slack bot itself does NOT need a public URL —
only the OAuth callback needs to be reachable by the user's browser.
