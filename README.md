# Telegram Bot Hosting Manager (Render)

A private Telegram bot for managing Python projects on a Render background worker.

## Features

- Owner-only access
- Create project folders and upload `.py` scripts
- Optional `requirements.txt` installation
- Start, stop, restart and replace scripts
- Read recent project logs
- Install trusted Python packages through Pip Install
- Per-project process tracking and graceful process-tree termination
- Persistent project storage when deployed with the Render disk in `render.yaml`

## Important security warning

The Bot API token pasted in the original source code has been exposed. **Revoke it in @BotFather and create a new token before deploying.** Do not reuse the exposed token. Never commit `.env` or secrets to GitHub.

Uploaded Python scripts execute with the same operating-system permissions as this manager. Only upload code you trust. This is a single-owner personal tool, not a safe multi-tenant hosting platform.

## Deploy on Render

1. Revoke the previously exposed token in @BotFather and create a new one.
2. Put this project in a GitHub repository. Do not upload `.env`.
3. In Render, choose **New + → Blueprint** and connect the repository.
4. Render reads `render.yaml` and creates a **Background Worker**.
5. In the Render environment prompt, set:
   - `BOT_TOKEN`: the newly generated token
   - `OWNER_ID`: your numeric Telegram user ID
6. Deploy. Open the worker logs and confirm `Bot Hosting Manager started`.
7. Open your Telegram bot and send `/start`.

### Render plan and persistent storage

The blueprint requests a Starter background worker and a 1 GB persistent disk. Render plan and disk charges may apply. A persistent disk is important because the uploaded project files and logs otherwise may be lost when an instance is replaced or redeployed. Confirm current pricing and availability in your Render dashboard before creating the service.

## Manual Render setup

If you do not use Blueprint:

- Service type: **Background Worker**
- Runtime: **Python**
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Add environment variables `BOT_TOKEN`, `OWNER_ID`, `PROJECTS_DIR=/opt/render/project/src/data/projects`
- Add a persistent disk mounted at `/opt/render/project/src/data`

## Local test

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="YOUR_NEW_TOKEN"
export OWNER_ID="YOUR_TELEGRAM_NUMERIC_ID"
python main.py
```

Windows PowerShell:

```powershell
$env:BOT_TOKEN="YOUR_NEW_TOKEN"
$env:OWNER_ID="YOUR_TELEGRAM_NUMERIC_ID"
python main.py
```

## Operational notes and limitations

- This uses long polling. Do not run multiple replicas with the same bot token.
- Render background workers do not provide an interactive shell to hosted scripts; use Telegram logs.
- Hosted scripts share the manager's Python environment. Installing packages can affect every hosted project.
- Package installation and arbitrary uploaded Python code are powerful operations. This tool validates filenames and restricts package-name syntax, but it is not a sandbox.
- `active_sessions` is in memory. If the manager restarts, child processes are cleaned up during normal shutdown; after a hard instance termination, Render stops the instance processes. The bot does not automatically relaunch scripts after a redeploy.
- The original malloc_trim logic was removed: forcing allocator trimming after every action does not guarantee lower total RSS and can reduce performance. `gc.collect()` is not a substitute for memory limits.
- Do not expose this bot to other users without redesigning authentication, isolation, quotas, auditing and process sandboxing.
