import os
import re
import time
import shutil
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import telebot
from telebot import types
from flask import Flask, request

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

BASE_SITE = "https://dozigame.ir/"

# Temporary files will be stored here.
DOWNLOAD_ROOT = os.path.join("/tmp", "manga_downloads")

# Maximum concurrent image downloads.
IMAGE_WORKERS = 30

# Maximum concurrent chapters.
CHAPTER_WORKERS = 5

# Maximum image number safety limit.
MAX_IMAGES_PER_CHAPTER = 20000

# Global lock so two jobs don't accidentally collide.
job_lock = threading.Lock()

# Keep track of users currently downloading.
active_users = set()

# User states.
user_states = {}


# ============================================================
# USER STATE
# ============================================================

def get_state(chat_id):
    if chat_id not in user_states:
        user_states[chat_id] = {
            "manga_name": None,
            "slug": None,
            "hd": False,
            "start_chapter": None,
            "end_chapter": None,
        }

    return user_states[chat_id]


def reset_state(chat_id):
    user_states[chat_id] = {
        "manga_name": None,
        "slug": None,
        "hd": False,
        "start_chapter": None,
        "end_chapter": None,
    }


# ============================================================
# NAME / URL
# ============================================================

def make_slug(name):
    """
    Convert:

        Solo Leveling

    into:

        Solo_Leveling

    Also removes unsafe characters.
    """

    name = name.strip()

    # Replace all whitespace with _
    name = re.sub(r"\s+", "_", name)

    # Remove characters that aren't useful in the URL.
    name = re.sub(r"[^A-Za-z0-9_\-]", "", name)

    # Avoid multiple underscores.
    name = re.sub(r"_+", "_", name)

    return name.strip("_")


def build_base_url(slug):
    return f"{BASE_SITE}{slug}/"


# ============================================================
# IMAGE URL
# ============================================================

def build_image_url(base_url, chapter, img_num, hd=False):
    if hd:
        return f"{base_url}{chapter}/HD/{img_num}.webp"

    return f"{base_url}{chapter}/{img_num}.webp"


# ============================================================
# HTTP SESSION
# ============================================================

def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/130.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Connection": "keep-alive",
    })

    return session


# ============================================================
# CHECK IMAGE
# ============================================================

def image_exists(session, base_url, chapter, img_num, hd):
    url = build_image_url(
        base_url,
        chapter,
        img_num,
        hd
    )

    try:
        response = session.head(
            url,
            timeout=8,
            allow_redirects=True
        )

        if response.status_code == 200:
            return True

        # Some servers don't properly support HEAD.
        if response.status_code in (403, 405):
            response = session.get(
                url,
                stream=True,
                timeout=8
            )

            ok = response.status_code == 200
            response.close()

            return ok

        return False

    except requests.RequestException:
        return False


# ============================================================
# FIND IMAGE COUNT
# ============================================================

def get_total_images(base_url, chapter, hd):
    session = create_session()

    try:

        if not image_exists(
            session,
            base_url,
            chapter,
            1,
            hd
        ):
            return 0

        # Find an upper bound.
        upper = 1

        while image_exists(
            session,
            base_url,
            chapter,
            upper,
            hd
        ):

            upper *= 2

            if upper > MAX_IMAGES_PER_CHAPTER:
                upper = MAX_IMAGES_PER_CHAPTER
                break

        lower = upper // 2

        # Binary search.
        while lower < upper:

            middle = (lower + upper + 1) // 2

            if image_exists(
                session,
                base_url,
                chapter,
                middle,
                hd
            ):
                lower = middle
            else:
                upper = middle - 1

        return lower

    finally:
        session.close()


# ============================================================
# DOWNLOAD ONE IMAGE
# ============================================================

def download_image(
    base_url,
    chapter,
    img_num,
    chapter_folder,
    hd
):

    url = build_image_url(
        base_url,
        chapter,
        img_num,
        hd
    )

    path = os.path.join(
        chapter_folder,
        f"{img_num}.webp"
    )

    if os.path.exists(path):
        return True

    for attempt in range(3):

        try:

            response = requests.get(
                url,
                stream=True,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            if response.status_code == 200:

                with open(path, "wb") as f:

                    for chunk in response.iter_content(
                        chunk_size=65536
                    ):
                        if chunk:
                            f.write(chunk)

                response.close()

                return True

            response.close()

            if response.status_code == 404:
                return False

        except requests.RequestException:

            pass

        time.sleep(0.5 * (attempt + 1))

    return False


# ============================================================
# DOWNLOAD CHAPTER
# ============================================================

def download_chapter(
    base_url,
    chapter,
    manga_folder,
    hd,
    progress_callback=None
):

    chapter_folder = os.path.join(
        manga_folder,
        f"Chapter_{chapter}"
    )

    os.makedirs(
        chapter_folder,
        exist_ok=True
    )

    total_images = get_total_images(
        base_url,
        chapter,
        hd
    )

    if total_images == 0:
        return {
            "chapter": chapter,
            "success": False,
            "images": 0,
            "error": "No images found"
        }

    tasks = []

    for image_number in range(
        1,
        total_images + 1
    ):
        tasks.append(image_number)

    completed = 0

    with ThreadPoolExecutor(
        max_workers=IMAGE_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                download_image,
                base_url,
                chapter,
                image_number,
                chapter_folder,
                hd
            ): image_number

            for image_number in tasks
        }

        for future in as_completed(futures):

            success = future.result()

            if success:
                completed += 1

            if progress_callback:
                progress_callback(
                    chapter,
                    completed,
                    total_images
                )

    # Verify everything exists.
    for image_number in range(
        1,
        total_images + 1
    ):

        image_path = os.path.join(
            chapter_folder,
            f"{image_number}.webp"
        )

        if not os.path.exists(image_path):

            return {
                "chapter": chapter,
                "success": False,
                "images": completed,
                "error": (
                    f"Missing image {image_number}"
                )
            }

    return {
        "chapter": chapter,
        "success": True,
        "images": total_images,
        "error": None
    }


# ============================================================
# CREATE FINAL ZIP
# ============================================================

def create_zip(
    manga_folder,
    manga_slug,
    start_chapter,
    end_chapter
):

    zip_name = (
        f"{manga_slug}_"
        f"{start_chapter}-{end_chapter}.zip"
    )

    zip_path = os.path.join(
        DOWNLOAD_ROOT,
        zip_name
    )

    with zipfile.ZipFile(
        zip_path,
        "w",
        zipfile.ZIP_DEFLATED
    ) as zipf:

        for root, dirs, files in os.walk(
            manga_folder
        ):

            for filename in files:

                file_path = os.path.join(
                    root,
                    filename
                )

                arcname = os.path.relpath(
                    file_path,
                    manga_folder
                )

                zipf.write(
                    file_path,
                    arcname
                )

    return zip_path


# ============================================================
# CLEANUP
# ============================================================

def cleanup(path):

    try:

        if os.path.isdir(path):
            shutil.rmtree(path)

        elif os.path.isfile(path):
            os.remove(path)

    except Exception as e:

        print(
            f"Cleanup error: {e}"
        )


# ============================================================
# TELEGRAM BUTTONS
# ============================================================

def mode_keyboard():

    keyboard = types.InlineKeyboardMarkup()

    normal = types.InlineKeyboardButton(
        "🟢 Normal",
        callback_data="mode_normal"
    )

    hd = types.InlineKeyboardButton(
        "🔵 HD",
        callback_data="mode_hd"
    )

    keyboard.row(
        normal,
        hd
    )

    return keyboard


def new_download_keyboard():

    keyboard = types.InlineKeyboardMarkup()

    keyboard.add(
        types.InlineKeyboardButton(
            "📥 Download another",
            callback_data="new_download"
        )
    )

    return keyboard


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    chat_id = message.chat.id

    reset_state(chat_id)

    bot.send_message(
        chat_id,
        "📚 <b>Manga / Manhwa Downloader</b>\n\n"
        "Send me the manga/manhwa name.\n\n"
        "Example:\n"
        "<code>Solo Leveling</code>\n\n"
        "Spaces will automatically become "
        "<code>_</code>.",
        parse_mode="HTML"
    )


# ============================================================
# /CANCEL
# ============================================================

@bot.message_handler(commands=["cancel"])
def cancel_command(message):

    reset_state(
        message.chat.id
    )

    bot.send_message(
        message.chat.id,
        "❌ Cancelled.\n\n"
        "Send another manga/manhwa name."
    )


# ============================================================
# MANGA NAME
# ============================================================

@bot.message_handler(
    func=lambda message: True
)
def receive_name(message):

    chat_id = message.chat.id

    # Ignore commands.
    if message.text.startswith("/"):
        return

    state = get_state(chat_id)

    # If we're expecting chapter numbers,
    # don't treat them as a manga name.
    if (
        state["manga_name"] is not None
        and state["start_chapter"] is None
    ):
        # handled below through chapter input
        pass

    name = message.text.strip()

    if not name:
        bot.send_message(
            chat_id,
            "❌ Please send a manga/manhwa name."
        )
        return

    slug = make_slug(name)

    if not slug:
        bot.send_message(
            chat_id,
            "❌ Invalid manga/manhwa name."
        )
        return

    state["manga_name"] = name
    state["slug"] = slug

    url = build_base_url(slug)

    bot.send_message(
        chat_id,
        "🔎 <b>Found name:</b>\n"
        f"<code>{slug}</code>\n\n"
        "🌐 <b>URL:</b>\n"
        f"<code>{url}</code>\n\n"
        "Choose download quality:",
        parse_mode="HTML",
        reply_markup=mode_keyboard()
    )


# ============================================================
# MODE CALLBACK
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data in (
        "mode_normal",
        "mode_hd"
    )
)
def mode_callback(call):

    chat_id = call.message.chat.id

    state = get_state(chat_id)

    if not state["slug"]:

        bot.answer_callback_query(
            call.id,
            "Start again with /start"
        )

        return

    state["hd"] = (
        call.data == "mode_hd"
    )

    quality = (
        "🔵 HD"
        if state["hd"]
        else "🟢 Normal"
    )

    bot.answer_callback_query(
        call.id,
        f"{quality} selected"
    )

    bot.edit_message_text(
        "✅ <b>Quality:</b> "
        f"{quality}\n\n"
        "Now send the <b>starting chapter</b>.\n\n"
        "Example:\n"
        "<code>1</code>",
        chat_id,
        call.message.message_id,
        parse_mode="HTML"
    )

    state["waiting_for_start"] = True


# ============================================================
# NEW DOWNLOAD
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data == "new_download"
)
def new_download(call):

    chat_id = call.message.chat.id

    reset_state(chat_id)

    bot.answer_callback_query(
        call.id
    )

    bot.send_message(
        chat_id,
        "📚 Send the next manga/manhwa name."
    )


# ============================================================
# CHAPTER INPUT
# ============================================================

@bot.message_handler(
    func=lambda message: (
        message.chat.id in user_states
        and user_states[message.chat.id].get(
            "waiting_for_start",
            False
        )
    )
)
def receive_start_chapter(message):

    chat_id = message.chat.id
    state = get_state(chat_id)

    try:
        chapter = int(
            message.text.strip()
        )

        if chapter < 0:
            raise ValueError

    except ValueError:

        bot.send_message(
            chat_id,
            "❌ Send a valid chapter number.\n"
            "Example: <code>1</code>",
            parse_mode="HTML"
        )

        return

    state["start_chapter"] = chapter
    state["waiting_for_start"] = False
    state["waiting_for_end"] = True

    bot.send_message(
        chat_id,
        "📖 Now send the <b>ending chapter</b>.\n\n"
        "Example:\n"
        "<code>10</code>",
        parse_mode="HTML"
    )


@bot.message_handler(
    func=lambda message: (
        message.chat.id in user_states
        and user_states[message.chat.id].get(
            "waiting_for_end",
            False
        )
    )
)
def receive_end_chapter(message):

    chat_id = message.chat.id
    state = get_state(chat_id)

    try:
        chapter = int(
            message.text.strip()
        )

        if chapter < 0:
            raise ValueError

    except ValueError:

        bot.send_message(
            chat_id,
            "❌ Send a valid chapter number."
        )

        return

    if chapter < state["start_chapter"]:

        bot.send_message(
            chat_id,
            "❌ Ending chapter can't be "
            "smaller than starting chapter."
        )

        return

    state["end_chapter"] = chapter
    state["waiting_for_end"] = False

    # Start download in background.
    threading.Thread(
        target=start_download_job,
        args=(chat_id,),
        daemon=True
    ).start()


# ============================================================
# DOWNLOAD JOB
# ============================================================

def start_download_job(chat_id):

    # Prevent multiple downloads from same user.
    with job_lock:

        if chat_id in active_users:

            bot.send_message(
                chat_id,
                "⏳ You already have a download running."
            )

            return

        active_users.add(chat_id)

    try:

        state = get_state(chat_id)

        manga_name = state["manga_name"]
        slug = state["slug"]
        hd = state["hd"]
        start_chapter = state["start_chapter"]
        end_chapter = state["end_chapter"]

        base_url = build_base_url(slug)

        manga_folder = os.path.join(
            DOWNLOAD_ROOT,
            f"{chat_id}_{slug}"
        )

        os.makedirs(
            DOWNLOAD_ROOT,
            exist_ok=True
        )

        os.makedirs(
            manga_folder,
            exist_ok=True
        )

        quality = (
            "HD"
            if hd
            else "Normal"
        )

        total_chapters = (
            end_chapter - start_chapter + 1
        )

        bot.send_message(
            chat_id,
            "🚀 <b>Download started!</b>\n\n"
            f"📚 Manga: <code>{slug}</code>\n"
            f"🎞 Quality: <b>{quality}</b>\n"
            f"📖 Chapters: "
            f"<b>{start_chapter} → {end_chapter}</b>\n\n"
            "⏳ Checking chapters...",
            parse_mode="HTML"
        )

        # ----------------------------------------------------
        # Count chapters
        # ----------------------------------------------------

        chapters = []

        with ThreadPoolExecutor(
            max_workers=8
        ) as executor:

            futures = {
                executor.submit(
                    get_total_images,
                    base_url,
                    chapter,
                    hd
                ): chapter

                for chapter in range(
                    start_chapter,
                    end_chapter + 1
                )
            }

            for future in as_completed(futures):

                chapter = futures[future]

                try:
                    count = future.result()

                    if count > 0:
                        chapters.append(
                            chapter
                        )

                except Exception as e:

                    print(
                        f"Chapter check error: {e}"
                    )

        chapters.sort()

        if not chapters:

            bot.send_message(
                chat_id,
                "❌ No chapters were found.\n\n"
                "Check the manga name and chapter range."
            )

            cleanup(manga_folder)

            return

        bot.send_message(
            chat_id,
            "📚 <b>Chapters found:</b> "
            f"{len(chapters)}\n\n"
            "⬇️ Starting image downloads...",
            parse_mode="HTML"
        )

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

        progress_lock = threading.Lock()

        progress = {
            "done": 0,
            "total": 0,
            "last_update": 0
        }

        # First calculate total image count.
        # This is mostly informational.
        for chapter in chapters:

            try:

                count = get_total_images(
                    base_url,
                    chapter,
                    hd
                )

                progress["total"] += count

            except:
                pass

        def progress_callback(
            chapter,
            done,
            total
        ):

            with progress_lock:

                progress["done"] += 1

                now = time.time()

                # Don't spam Telegram.
                if now - progress["last_update"] < 5:
                    return

                progress["last_update"] = now

                bot.send_message(
                    chat_id,
                    "⬇️ <b>Downloading...</b>\n\n"
                    f"📖 Chapter: <b>{chapter}</b>\n"
                    f"🖼 Images: "
                    f"<b>{done}/{total}</b>",
                    parse_mode="HTML"
                )

        # ----------------------------------------------------
        # Download chapters
        # ----------------------------------------------------

        results = []

        with ThreadPoolExecutor(
            max_workers=CHAPTER_WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    download_chapter,
                    base_url,
                    chapter,
                    manga_folder,
                    hd,
                    progress_callback
                ): chapter

                for chapter in chapters
            }

            for future in as_completed(futures):

                result = future.result()

                results.append(result)

        failed = [
            result
            for result in results
            if not result["success"]
        ]

        if failed:

            failed_text = ", ".join(
                str(x["chapter"])
                for x in failed
            )

            bot.send_message(
                chat_id,
                "⚠️ Some chapters failed:\n\n"
                f"<code>{failed_text}</code>\n\n"
                "The ZIP will not be created.",
                parse_mode="HTML"
            )

            cleanup(manga_folder)

            return

        # ----------------------------------------------------
        # ZIP
        # ----------------------------------------------------

        bot.send_message(
            chat_id,
            "📦 <b>All chapters downloaded.</b>\n\n"
            "Creating ZIP file...",
            parse_mode="HTML"
        )

        zip_path = create_zip(
            manga_folder,
            slug,
            start_chapter,
            end_chapter
        )

        # ----------------------------------------------------
        # Send ZIP
        # ----------------------------------------------------

        file_size_mb = (
            os.path.getsize(zip_path)
            / 1024
            / 1024
        )

        bot.send_message(
            chat_id,
            "📤 Uploading ZIP...\n\n"
            f"📦 Size: <b>{file_size_mb:.1f} MB</b>",
            parse_mode="HTML"
        )

        with open(
            zip_path,
            "rb"
        ) as document:

            bot.send_document(
                chat_id,
                document,
                caption=(
                    f"📚 {slug}\n"
                    f"📖 Chapters "
                    f"{start_chapter}-{end_chapter}\n"
                    f"🎞 {quality}"
                ),
                reply_markup=new_download_keyboard()
            )

        # ----------------------------------------------------
        # CLEAN EVERYTHING
        # ----------------------------------------------------

        cleanup(manga_folder)
        cleanup(zip_path)

        bot.send_message(
            chat_id,
            "✅ <b>Finished!</b>\n\n"
            "🗑 Temporary files deleted.",
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"DOWNLOAD ERROR: {repr(e)}"
        )

        bot.send_message(
            chat_id,
            "❌ <b>Download failed.</b>\n\n"
            f"<code>{str(e)[:1000]}</code>",
            parse_mode="HTML"
        )

    finally:

        with job_lock:
            active_users.discard(chat_id)


# ============================================================
# WASMER WEBHOOK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "status": "online",
        "service": "Manga Downloader Bot"
    }


@app.route(
    "/telegram/webhook",
    methods=["POST"]
)
def telegram_webhook():

    try:

        json_string = request.get_data().decode(
            "utf-8"
        )

        update = telebot.types.Update.de_json(
            json_string
        )

        bot.process_new_updates(
            [update]
        )

        return "OK", 200

    except Exception as e:

        print(
            f"Webhook error: {repr(e)}"
        )

        return "ERROR", 500


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    os.makedirs(
        DOWNLOAD_ROOT,
        exist_ok=True
    )

    # For local testing.
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                8080
            )
        )
    )
