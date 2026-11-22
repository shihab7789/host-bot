import asyncio
import html
import logging
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup
)

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID must be your numeric Telegram user ID.")
OWNER_ID = int(OWNER_ID_RAW)

BASE_DIR = Path(os.getenv("PROJECTS_DIR", "data/projects")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "600"))
MAX_LOG_CHARS = 3000
BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("host-manager")

router = Router()
# uid -> project -> process metadata
active_sessions: dict[int, dict[str, dict]] = {}
process_locks: dict[tuple[int, str], asyncio.Lock] = {}


# ---------------- FSM ----------------
class HostFlow(StatesGroup):
    project_name = State()
    script = State()
    requirements = State()


class PipFlow(StatesGroup):
    package = State()


class ReplaceFlow(StatesGroup):
    script = State()


# ---------------- UI ----------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 My Scripts"), KeyboardButton(text="☁️ Host New")],
            [KeyboardButton(text="🛑 Stop Scripts"), KeyboardButton(text="🗑 Delete Folder")],
            [KeyboardButton(text="📦 Pip Install")],
        ],
        resize_keyboard=True,
    )


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def project_path(uid: int, project: str) -> Path:
    # Only allow a single safe folder component; never trust callback data.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", project):
        raise ValueError("Invalid project name.")
    path = (BASE_DIR / str(uid) / project).resolve()
    user_root = (BASE_DIR / str(uid)).resolve()
    if path.parent != user_root:
        raise ValueError("Invalid project path.")
    return path


def safe_filename(name: str) -> str:
    if not name or Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]{1,30}", name):
        raise ValueError("Invalid file name.")
    if not name.lower().endswith(".py"):
        raise ValueError("Only .py files are accepted.")
    return name


def is_owner(user_id: Optional[int]) -> bool:
    return user_id == OWNER_ID


async def owner_only(message: Message) -> bool:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("🚫 Access denied.")
        return False
    return True


async def callback_owner(callback: CallbackQuery) -> bool:
    if not callback.from_user or not is_owner(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return False
    return True


def project_names(uid: int):
    root = BASE_DIR / str(uid)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]{1,20}", p.name))


def session_is_running(session: Optional[dict]) -> bool:
    proc = session.get("proc") if session else None
    return bool(proc and proc.returncode is None)


def read_logs(path: Path, lines: int = 30) -> str:
    log_path = path / "bot.log"
    if not log_path.exists():
        return "No logs generated yet."
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip() or "Log is empty."
    except OSError as exc:
        return f"Unable to read logs: {exc}"


def project_lock(uid: int, project: str):
    return process_locks.setdefault((uid, project), asyncio.Lock())


async def terminate_process(proc: asyncio.subprocess.Process):
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    except ProcessLookupError:
        pass


async def stop_project(uid: int, project: str) -> bool:
    sessions = active_sessions.get(uid, {})
    entry = sessions.get(project)
    if not entry:
        return False
    proc = entry.get("proc")
    if proc:
        await terminate_process(proc)
    handle = entry.get("log_handle")
    if handle and not handle.closed:
        handle.close()
    sessions.pop(project, None)
    if not sessions:
        active_sessions.pop(uid, None)
    return True


async def monitor_exit(uid: int, project: str, proc, log_handle):
    code = await proc.wait()
    if log_handle and not log_handle.closed:
        log_handle.close()
    current = active_sessions.get(uid, {}).get(project)
    # Ignore an old monitor after a restart/replacement.
    if not current or current.get("proc") is not proc:
        return
    active_sessions[uid].pop(project, None)
    if not active_sessions[uid]:
        active_sessions.pop(uid, None)
    log.info("Project %s exited with code %s", project, code)
    if code != 0:
        details = read_logs(project_path(uid, project), 18)[-2500:]
        try:
            await bot_instance.send_message(
                uid,
                f"💥 <b>Process crashed</b>\nProject: <code>{html.escape(project)}</code>\nExit: <code>{code}</code>\n\n<pre>{html.escape(details)}</pre>",
            )
        except Exception:
            log.exception("Could not send crash notification")


async def run_project(uid: int, project: str, filename: str, notice: Message):
    filename = safe_filename(filename)
    path = project_path(uid, project)
    script_path = path / filename
    if not script_path.is_file():
        await notice.answer("❌ Script file not found.")
        return

    async with project_lock(uid, project):
        existing = active_sessions.get(uid, {}).get(project)
        if session_is_running(existing):
            await notice.answer("⚠️ This project is already running.")
            return

        await notice.answer(f"🚀 Starting <code>{html.escape(filename)}</code>...")
        log_handle = None
        try:
            log_handle = (path / "bot.log").open("w", encoding="utf-8", buffering=1)
            kwargs = {}
            if os.name != "nt":
                kwargs["start_new_session"] = True
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", filename,
                cwd=str(path),
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                **kwargs,
            )
            active_sessions.setdefault(uid, {})[project] = {
                "proc": proc, "file": filename, "log_handle": log_handle
            }
            await asyncio.sleep(1.0)
            if proc.returncode is not None:
                if not log_handle.closed:
                    log_handle.close()
                active_sessions.get(uid, {}).pop(project, None)
                output = read_logs(path, 20)[-2500:]
                await notice.answer(f"💥 Startup failed.\n<pre>{html.escape(output)}</pre>")
                return

            await notice.answer(
                f"✅ <b>{html.escape(project)}</b> started.\nPID: <code>{proc.pid}</code>"
            )
            asyncio.create_task(monitor_exit(uid, project, proc, log_handle))
        except Exception as exc:
            if log_handle and not log_handle.closed:
                log_handle.close()
            log.exception("Failed to start project")
            await notice.answer(f"❌ Could not start: <code>{html.escape(str(exc))}</code>")


async def render_project_list(message: Message, prefix: str):
    projects = project_names(message.from_user.id)
    if not projects:
        await message.answer("📂 No projects yet.")
        return
    rows = []
    for project in projects:
        running = session_is_running(active_sessions.get(message.from_user.id, {}).get(project))
        icon = "🟢" if running else "🔴"
        rows.append([InlineKeyboardButton(text=f"{icon} {project}", callback_data=f"{prefix}:{project}")])
    rows.append([InlineKeyboardButton(text="✖ Close", callback_data="close")])
    await message.answer("Select a project:", reply_markup=inline(rows))


async def show_project(callback: CallbackQuery, project: str):
    uid = callback.from_user.id
    path = project_path(uid, project)
    if not path.is_dir():
        await callback.answer("Project not found.", show_alert=True)
        return
    session = active_sessions.get(uid, {}).get(project)
    running = session_is_running(session)
    scripts = sorted(p.name for p in path.glob("*.py") if p.is_file())
    rows = [[InlineKeyboardButton(text=f"📄 {name}", callback_data=f"file:{project}:{name}")] for name in scripts]
    if running:
        rows.append([
            InlineKeyboardButton(text="🔄 Restart", callback_data=f"restart:{project}"),
            InlineKeyboardButton(text="🛑 Stop", callback_data=f"stop:{project}"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="▶ Start", callback_data=f"start:{project}")])
    rows += [
        [InlineKeyboardButton(text="📋 Read Logs", callback_data=f"logs:{project}")],
        [InlineKeyboardButton(text="🔄 Replace File", callback_data=f"replace:{project}")],
        [InlineKeyboardButton(text="🗑 Delete Project", callback_data=f"delete:{project}")],
        [InlineKeyboardButton(text="⬅ Back", callback_data="list")],
    ]
    text = f"<b>📂 {html.escape(project)}</b>\nStatus: {'🟢 Running' if running else '🔴 Stopped'}"
    await callback.message.edit_text(text, reply_markup=inline(rows))


# ---------------- Start / Help ----------------
@router.message(Command("start"))
async def start(message: Message):
    if not await owner_only(message):
        return
    (BASE_DIR / str(OWNER_ID)).mkdir(parents=True, exist_ok=True)
    await message.answer(
        "<b>⚡ Bot Hosting Manager Online</b>\nManage your Python bot projects from Telegram.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message):
    if await owner_only(message):
        await message.answer(
            "<b>Commands</b>\n/start — open manager\n/help — show help\n/cancel — cancel current action\n\n"
            "Upload only trusted Python scripts. Hosted scripts run with the same OS permissions as this manager.",
            reply_markup=main_keyboard(),
        )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Cancel")
async def cancel(message: Message, state: FSMContext):
    if not await owner_only(message):
        return
    await state.clear()
    await message.answer("❌ Cancelled.", reply_markup=main_keyboard())


# ---------------- Host new project ----------------
@router.message(F.text == "☁️ Host New")
async def host_new(message: Message, state: FSMContext):
    if not await owner_only(message):
        return
    await state.clear()
    await message.answer("Enter project name (letters, numbers, _ and - only):")
    await state.set_state(HostFlow.project_name)


@router.message(HostFlow.project_name)
async def receive_project_name(message: Message, state: FSMContext):
    if not await owner_only(message):
        return
    name = (message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", name):
        await message.answer("Invalid name. Use 1–20 letters, numbers, _ or -.")
        return
    path = project_path(OWNER_ID, name)
    if path.exists():
        await message.answer("⚠️ That project already exists. Choose another name.")
        return
    path.mkdir(parents=True)
    await state.update_data(project=name)
    await message.answer("✅ Project folder created. Now upload the main .py script.")
    await state.set_state(HostFlow.script)


@router.message(HostFlow.script, F.document)
async def receive_main_script(message: Message, state: FSMContext, bot: Bot):
    if not await owner_only(message):
        return
    doc = message.document
    try:
        filename = safe_filename(doc.file_name or "")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer(f"❌ File too large. Limit: {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
        return
    data = await state.get_data()
    path = project_path(OWNER_ID, data["project"])
    await bot.download(doc, destination=path / filename)
    await state.update_data(main_script=filename)
    await message.answer(
        "📄 Script saved. Upload <code>requirements.txt</code> or tap Skip.",
        reply_markup=inline([[InlineKeyboardButton(text="⏭ Skip requirements", callback_data="skipreq")]]),
    )
    await state.set_state(HostFlow.requirements)


@router.message(HostFlow.script)
async def wrong_script_type(message: Message):
    if await owner_only(message):
        await message.answer("Please upload a .py document.")


async def install_requirements(path: Path, message: Message):
    req = path / "requirements.txt"
    if not req.exists():
        return True
    await message.answer("📦 Installing requirements. This can take a few minutes...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--no-cache-dir", "-r", str(req),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await message.answer("❌ Dependency installation timed out.")
        return False
    result = output.decode("utf-8", errors="replace")[-2500:]
    if proc.returncode:
        await message.answer(f"❌ pip install failed:\n<pre>{html.escape(result)}</pre>")
        return False
    await message.answer("✅ Dependencies installed.")
    return True


async def finish_host(message: Message, state: FSMContext, with_requirements: bool):
    data = await state.get_data()
    await state.clear()
    path = project_path(OWNER_ID, data["project"])
    if with_requirements:
        if not await install_requirements(path, message):
            await message.answer("Project saved. Fix requirements and try again from the server.")
            return
    await message.answer(
        f"✅ Deployed <b>{html.escape(data['project'])}</b>.",
        reply_markup=inline([[InlineKeyboardButton(
            text="▶ Run now",
            callback_data=f"run:{data['project']}:{data['main_script']}",
        )]]),
    )


@router.message(HostFlow.requirements, F.document)
async def receive_requirements(message: Message, state: FSMContext, bot: Bot):
    if not await owner_only(message):
        return
    doc = message.document
    if doc.file_name != "requirements.txt":
        await message.answer("Please upload a file named exactly requirements.txt, or tap Skip.")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer("❌ requirements.txt is too large.")
        return
    data = await state.get_data()
    path = project_path(OWNER_ID, data["project"])
    await bot.download(doc, destination=path / "requirements.txt")
    await finish_host(message, state, True)


@router.callback_query(F.data == "skipreq")
async def skip_requirements(callback: CallbackQuery, state: FSMContext):
    if not await callback_owner(callback):
        return
    await callback.answer()
    await finish_host(callback.message, state, False)


# ---------------- Pip install ----------------
@router.message(F.text == "📦 Pip Install")
async def pip_start(message: Message, state: FSMContext):
    if not await owner_only(message):
        return
    await state.clear()
    await message.answer("Enter package specifier(s), e.g. <code>aiohttp==3.10.5</code>.\nOnly trusted packages.")
    await state.set_state(PipFlow.package)


@router.message(PipFlow.package)
async def pip_run(message: Message, state: FSMContext):
    if not await owner_only(message):
        return
    raw = (message.text or "").strip()
    await state.clear()
    packages = raw.split()
    if not packages or len(packages) > 10 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+(==[A-Za-z0-9_.+-]+)?", p) for p in packages):
        await message.answer("❌ Invalid package input. Use package names or package==version.")
        return
    status = await message.answer("⏳ Installing packages...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", *packages,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT)
        text = output.decode("utf-8", errors="replace")[-2500:]
        label = "✅ Success" if proc.returncode == 0 else "❌ Failed"
        await status.edit_text(f"{label}\n<pre>{html.escape(text)}</pre>")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await status.edit_text("❌ Installation timed out.")


# ---------------- Menus ----------------
@router.message(F.text == "📂 My Scripts")
async def my_scripts(message: Message):
    if await owner_only(message):
        await render_project_list(message, "view")


@router.message(F.text == "🗑 Delete Folder")
async def delete_menu(message: Message):
    if await owner_only(message):
        await render_project_list(message, "delete")


@router.message(F.text == "🛑 Stop Scripts")
async def stop_menu(message: Message):
    if not await owner_only(message):
        return
    running = active_sessions.get(OWNER_ID, {})
    rows = [
        [InlineKeyboardButton(text=f"🛑 {name}", callback_data=f"stop:{name}")]
        for name, item in running.items() if session_is_running(item)
    ]
    if not rows:
        await message.answer("❌ No active scripts.")
        return
    rows.append([InlineKeyboardButton(text="✖ Close", callback_data="close")])
    await message.answer("Choose a running project:", reply_markup=inline(rows))


@router.callback_query(F.data == "close")
async def close_menu(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    await callback.answer()
    await callback.message.delete()


@router.callback_query(F.data == "list")
async def back_to_list(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    await callback.answer()
    await callback.message.delete()
    await render_project_list(callback.message, "view")


@router.callback_query(F.data.startswith("view:"))
async def view_project(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    await callback.answer()
    await show_project(callback, callback.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("file:"))
async def file_details(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    _, project, filename = callback.data.split(":", 2)
    path = project_path(OWNER_ID, project)
    if not (path / safe_filename(filename)).is_file():
        await callback.answer("File not found.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"📄 <b>{html.escape(filename)}</b>\nProject: <code>{html.escape(project)}</code>",
        reply_markup=inline([[InlineKeyboardButton(text="⬅ Back", callback_data=f"view:{project}")]]),
    )


@router.callback_query(F.data.startswith("logs:"))
async def logs_view(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    logs = read_logs(project_path(OWNER_ID, project))[-MAX_LOG_CHARS:]
    await callback.answer()
    await callback.message.edit_text(
        f"📋 <b>Logs: {html.escape(project)}</b>\n\n<pre>{html.escape(logs)}</pre>",
        reply_markup=inline([[InlineKeyboardButton(text="⬅ Back", callback_data=f"view:{project}")]]),
    )


# ---------------- Process controls ----------------
@router.callback_query(F.data.startswith("run:"))
async def run_callback(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    _, project, filename = callback.data.split(":", 2)
    await callback.answer()
    await run_project(OWNER_ID, project, filename, callback.message)


@router.callback_query(F.data.startswith("start:"))
async def start_callback(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    path = project_path(OWNER_ID, project)
    scripts = sorted(p.name for p in path.glob("*.py") if p.is_file())
    if not scripts:
        await callback.answer("No Python scripts found.", show_alert=True)
        return
    target = "main.py" if "main.py" in scripts else scripts[0]
    await callback.answer()
    await run_project(OWNER_ID, project, target, callback.message)


@router.callback_query(F.data.startswith("stop:"))
async def stop_callback(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    await stop_project(OWNER_ID, project)
    await callback.answer("Stopped.")
    await show_project(callback, project)


@router.callback_query(F.data.startswith("restart:"))
async def restart_callback(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    session = active_sessions.get(OWNER_ID, {}).get(project)
    filename = session.get("file") if session else None
    if not filename:
        await callback.answer("Project is not running.", show_alert=True)
        return
    await stop_project(OWNER_ID, project)
    await callback.answer("Restarting...")
    await run_project(OWNER_ID, project, filename, callback.message)


@router.callback_query(F.data.startswith("delete:"))
async def delete_confirm(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.edit_text(
        f"⚠️ Permanently delete <b>{html.escape(project)}</b>?",
        reply_markup=inline([[
            InlineKeyboardButton(text="✅ Yes, delete", callback_data=f"delete_yes:{project}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"view:{project}"),
        ]]),
    )


@router.callback_query(F.data.startswith("delete_yes:"))
async def delete_confirmed(callback: CallbackQuery):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    path = project_path(OWNER_ID, project)
    await stop_project(OWNER_ID, project)
    shutil.rmtree(path, ignore_errors=True)
    await callback.answer("Deleted.")
    await callback.message.delete()
    await render_project_list(callback.message, "view")


# ---------------- Replace script ----------------
@router.callback_query(F.data.startswith("replace:"))
async def replace_start(callback: CallbackQuery, state: FSMContext):
    if not await callback_owner(callback):
        return
    project = callback.data.split(":", 1)[1]
    if not project_path(OWNER_ID, project).is_dir():
        await callback.answer("Project not found.", show_alert=True)
        return
    await state.clear()
    await state.update_data(replace_project=project)
    await callback.answer()
    await callback.message.answer(f"Upload replacement .py file for <b>{html.escape(project)}</b>.")
    await state.set_state(ReplaceFlow.script)


@router.message(ReplaceFlow.script, F.document)
async def replace_upload(message: Message, state: FSMContext, bot: Bot):
    if not await owner_only(message):
        return
    doc = message.document
    try:
        filename = safe_filename(doc.file_name or "")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer("❌ File too large.")
        return
    data = await state.get_data()
    project = data["replace_project"]
    path = project_path(OWNER_ID, project)
    if not path.is_dir():
        await state.clear()
        await message.answer("Project no longer exists.")
        return
    was_running = session_is_running(active_sessions.get(OWNER_ID, {}).get(project))
    old_session = active_sessions.get(OWNER_ID, {}).get(project)
    old_file = old_session.get("file") if old_session else None
    await stop_project(OWNER_ID, project)
    temp_path = path / f".upload-{filename}"
    await bot.download(doc, destination=temp_path)
    os.replace(temp_path, path / filename)
    await state.clear()
    await message.answer(f"✅ Replaced <code>{html.escape(filename)}</code>.")
    if was_running:
        await run_project(OWNER_ID, project, filename if old_file == filename else old_file or filename, message)


# ---------------- Shutdown / entry ----------------
bot_instance: Optional[Bot] = None


async def shutdown_cleanup():
    for uid in list(active_sessions):
        for project in list(active_sessions.get(uid, {})):
            await stop_project(uid, project)


async def main():
    global bot_instance
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot Hosting Manager started. Owner ID: %s", OWNER_ID)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        await shutdown_cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
