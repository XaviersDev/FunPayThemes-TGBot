import asyncio
import logging
import os
import sqlite3
import json
import hashlib
import secrets
import io
import base64

import requests
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
    InputMediaPhoto,
    PreCheckoutQuery, LabeledPrice, SuccessfulPayment
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.deep_linking import create_start_link

import config

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.API_TOKEN)
dp = Dispatcher()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
THEMES_DIR = os.path.join(SCRIPT_DIR, config.THEMES_FOLDER_NAME)
DB_PATH = os.path.join(SCRIPT_DIR, config.DB_NAME)

class Database:
    def __init__(self, db_path):
        self.connection = sqlite3.connect(db_path)
        self.cursor = self.connection.cursor()
        self.setup()

    def setup(self):
        self.cursor.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, theme_slots INTEGER DEFAULT 10, is_banned INTEGER DEFAULT 0)')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS themes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, unique_id TEXT NOT NULL UNIQUE, owner_id INTEGER NOT NULL,
                name TEXT NOT NULL, description TEXT, is_public INTEGER DEFAULT 1, file_id TEXT NOT NULL,
                file_hash TEXT NOT NULL UNIQUE, preview_file_id TEXT,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (owner_id) REFERENCES users (id)
            )''')
        self.connection.commit()

    def add_user(self, user_id, username):
        self.cursor.execute("INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", (user_id, username))
        self.connection.commit()

    def get_user(self, user_id):
        self.cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return self.cursor.fetchone()

    def is_banned(self, user_id):
        user = self.get_user(user_id)
        return user and user[3] == 1

    def set_ban_status(self, user_id, status):
        self.cursor.execute("UPDATE users SET is_banned = ? WHERE id = ?", (1 if status else 0, user_id))
        self.connection.commit()
    
    def get_all_users(self):
        self.cursor.execute("SELECT id FROM users WHERE is_banned = 0")
        return self.cursor.fetchall()

    def get_user_theme_count(self, user_id):
        self.cursor.execute("SELECT COUNT(*) FROM themes WHERE owner_id = ?", (user_id,))
        return self.cursor.fetchone()[0]

    def has_duplicate_hash(self, file_hash):
        self.cursor.execute("SELECT 1 FROM themes WHERE file_hash = ?", (file_hash,))
        return self.cursor.fetchone() is not None

    def add_theme(self, owner_id, name, description, is_public, file_id, file_hash, preview_file_id):
        unique_id = secrets.token_urlsafe(16)
        self.cursor.execute('''
            INSERT INTO themes (unique_id, owner_id, name, description, is_public, file_id, file_hash, preview_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (unique_id, owner_id, name, description, is_public, file_id, file_hash, preview_file_id))
        self.connection.commit()
        return self.cursor.lastrowid
    
    def get_user_themes(self, user_id):
        self.cursor.execute("SELECT id, name, is_public FROM themes WHERE owner_id = ? ORDER BY upload_date DESC", (user_id,))
        return self.cursor.fetchall()
        
    def get_theme_by_id(self, theme_id):
        self.cursor.execute("SELECT * FROM themes WHERE id = ?", (theme_id,))
        return self.cursor.fetchone()

    def get_theme_by_unique_id(self, unique_id):
        self.cursor.execute("SELECT * FROM themes WHERE unique_id = ?", (unique_id,))
        return self.cursor.fetchone()
        
    def delete_theme(self, theme_id, user_id):
        self.cursor.execute("DELETE FROM themes WHERE id = ? AND owner_id = ?", (theme_id, user_id))
        self.connection.commit()
        return self.cursor.rowcount > 0

    def admin_delete_theme(self, theme_id):
        self.cursor.execute("DELETE FROM themes WHERE id = ?", (theme_id,))
        self.connection.commit()
        return self.cursor.rowcount > 0

    def set_theme_privacy(self, theme_id, user_id, is_public):
        self.cursor.execute("UPDATE themes SET is_public = ? WHERE id = ? AND owner_id = ?", (is_public, theme_id, user_id))
        self.connection.commit()
        return self.cursor.rowcount > 0
    
    def get_public_themes(self):
        self.cursor.execute('''
            SELECT t.id, t.name, t.description, u.username, t.preview_file_id
            FROM themes t JOIN users u ON t.owner_id = u.id
            WHERE t.is_public = 1 ORDER BY t.upload_date DESC
        ''')
        return self.cursor.fetchall()

    def count_public_themes(self):
        self.cursor.execute("SELECT COUNT(*) FROM themes WHERE is_public = 1")
        return self.cursor.fetchone()[0]

    def add_theme_slots(self, user_id, slots_to_add):
        self.cursor.execute("UPDATE users SET theme_slots = theme_slots + ? WHERE id = ?", (slots_to_add, user_id))
        self.connection.commit()

db = Database(DB_PATH)

class UploadTheme(StatesGroup):
    waiting_for_file = State()
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_privacy = State()

class AdminStates(StatesGroup):
    broadcast_message = State()
    delete_theme_id = State()
    ban_user_id = State()
    unban_user_id = State()

class AccessMiddleware:
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        if db.is_banned(user_id): return 
        try:
            member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                await event.answer(
                    "Для использования бота необходимо подписаться на наш канал.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="➡️ Подписаться", url=f"https://t.me/{config.CHANNEL_ID.replace('@', '')}")],
                        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")]
                    ]), show_alert=True if isinstance(event, CallbackQuery) else False
                )
                return
        except Exception:
            if isinstance(event, Message): await event.answer("Не удалось проверить подписку. Убедитесь, что вы подписаны на канал.")
            return
        return await handler(event, data)

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def get_font(size, style='Regular'):
    font_map = {'Regular': 'Roboto-Regular.ttf', 'Bold': 'Roboto-Bold.ttf'}
    font_filename = font_map.get(style, 'Roboto-Regular.ttf')
    font_path = os.path.join(SCRIPT_DIR, font_filename)
    try:
        return ImageFont.truetype(font_path, size)
    except IOError:
        return ImageFont.load_default(size)

async def generate_preview(theme_data: dict, temp_file_path: str):
    try:
        W, H = 1280, 720
        img = Image.new('RGB', (W, H), color=hex_to_rgb(theme_data.get('bgColor1', '#121212')))
        
        bg_image_url = theme_data.get('bgImage')
        if bg_image_url:
            try:
                bg_img_data = None
                if bg_image_url.startswith('data:image'):
                    header, encoded = bg_image_url.split(',', 1)
                    img_data = base64.b64decode(encoded)
                    bg_img_data = io.BytesIO(img_data)
                else:
                    response = requests.get(bg_image_url, timeout=10)
                    response.raise_for_status()
                    bg_img_data = io.BytesIO(response.content)
                
                bg = Image.open(bg_img_data).convert("RGBA")
                bg_w, bg_h = bg.size
                scale = max(W / bg_w, H / bg_h)
                new_w, new_h = int(bg_w * scale), int(bg_h * scale)
                bg = bg.resize((new_w, new_h), Image.Resampling.LANCZOS)
                
                left, top = (new_w - W) / 2, (new_h - H) / 2
                bg = bg.crop((left, top, left + W, top + H))
                
                blur_radius = int(theme_data.get('bgBlur', 0))
                if blur_radius > 0:
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                
                brightness = float(theme_data.get('bgBrightness', 100)) / 100
                enhancer = ImageEnhance.Brightness(bg)
                bg = enhancer.enhance(brightness)
                
                img.paste(bg, (0, 0), bg if bg.mode == 'RGBA' else None)
            except Exception as e:
                logging.warning(f"Could not load background image: {e}")

        draw = ImageDraw.Draw(img, 'RGBA')
        
        bar_h = 80; bar_y = H - bar_h - 40; bar_x = 40; bar_w = W - 80
        container_color = list(hex_to_rgb(theme_data.get('containerBgColor', '#1e1e1e')))
        container_color.append(int(float(theme_data.get('containerBgOpacity', 0.85)) * 255))
        
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=int(theme_data.get('borderRadius', 12)),
            fill=tuple(container_color)
        )
        
        colors = [
            theme_data.get('bgColor1', '#000000'), theme_data.get('bgColor2', '#000000'),
            theme_data.get('containerBgColor', '#1e1e1e'), theme_data.get('textColor', '#ffffff'),
            theme_data.get('linkColor', '#0099ff'),
        ]
        
        color_w = (bar_w - 40) / len(colors)
        current_x = bar_x + 20
        for color_hex in colors:
            draw.rectangle(
                (current_x, bar_y + 20, current_x + color_w - 20, bar_y + bar_h - 20),
                fill=color_hex, outline="#fff", width=1
            )
            current_x += color_w
            
        img.save(temp_file_path, "JPEG", quality=90)
        return temp_file_path
    except Exception as e:
        logging.error(f"Fatal error in Pillow preview generation: {e}", exc_info=True)
        return None

def get_theme_features_text(theme_data):
    features = []
    if theme_data.get('bgImage'): features.append("🖼️ Фон")
    if theme_data.get('enableCircleCustomization'): features.append("💿 Круги")
    if theme_data.get('enableImprovedSeparators'): features.append("✨ Разделители")
    if theme_data.get('enableGlassmorphism'): features.append("💎 Стекло")
    if theme_data.get('enableCustomScrollbar'): features.append("📜 Скроллбар")
    return " | ".join(features) if features else "Стандартный набор"

def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Загрузить тему", callback_data="upload_theme")],
        [InlineKeyboardButton(text="🎨 Мои темы", callback_data="my_themes")],
        [InlineKeyboardButton(text="🏪 Магазин тем", callback_data="store_0")],
        [InlineKeyboardButton(text="⭐ Купить слоты", callback_data="buy_slots")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🗑️ Удалить тему", callback_data="admin_delete_theme")],
        [InlineKeyboardButton(text="🔨 Забанить", callback_data="admin_ban")],
        [InlineKeyboardButton(text="🕊️ Разбанить", callback_data="admin_unban")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="start")]
    ])

@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext):
    await state.clear()
    db.add_user(message.from_user.id, message.from_user.username)
    
    payload = message.text.split()
    if len(payload) > 1:
        unique_id = payload[1]
        theme_raw = db.get_theme_by_unique_id(unique_id)
        if theme_raw:
            theme_id, _, owner_id, name, desc, is_public, file_id, _, preview_file_id, _ = theme_raw
            owner = db.get_user(owner_id)
            owner_username = f"@{owner[1]}" if owner[1] else f"User ID: {owner_id}"
            caption = f"🎨 **{name}**\n\n📝 *{desc}*\n\n👤 **Автор:** {owner_username}"
            
            await bot.send_photo(chat_id=message.chat.id, photo=preview_file_id, caption=caption, parse_mode="Markdown")
            await bot.send_document(chat_id=message.chat.id, document=file_id)
            return

    await message.answer("👋 Добро пожаловать в FunPay Themes Bot!\n\nЗдесь вы можете загружать, скачивать и делиться темами для расширения FunPay Tools.",
                         reply_markup=main_menu_keyboard())

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id == config.ADMIN_ID:
        await message.answer("Добро пожаловать в админ-панель!", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("👋 Добро пожаловать в FunPay Themes Bot!\n\nЗдесь вы можете загружать, скачивать и делиться темами для расширения FunPay Tools.",
                                     reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "check_subscription")
async def check_sub_callback(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
        if member.status in ['member', 'administrator', 'creator']:
            await callback.message.delete()
            await callback.answer("Спасибо за подписку!", show_alert=True)
            await command_start_handler(callback.message, state)
        else:
            await callback.answer("Вы все еще не подписаны на канал.", show_alert=True)
    except Exception:
        await callback.answer("Произошла ошибка при проверке. Попробуйте позже.", show_alert=True)

@dp.callback_query(F.data == "upload_theme")
async def upload_theme_start(callback: CallbackQuery, state: FSMContext):
    user_info = db.get_user(callback.from_user.id)
    user_slots = user_info[2] if user_info else 10
    current_themes = db.get_user_theme_count(callback.from_user.id)
    
    if current_themes >= user_slots:
        await callback.answer(f"❌ У вас закончились слоты для тем ({current_themes}/{user_slots}). Удалите старые или купите новые.", show_alert=True)
        return

    await state.set_state(UploadTheme.waiting_for_file)
    await callback.message.edit_text(
        f"Отлично! Отправьте мне файл темы в формате .fptheme\n\n"
        f"⚠️ ТРЕБОВАНИЯ:\n"
        f"- Только формат .fptheme\n"
        f"- Размер до {config.MAX_FILE_SIZE_MB} МБ\n"
        f"- Тема не должна быть дубликатом уже загруженной\n\n"
        f"У вас осталось слотов: {user_slots - current_themes}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="start")]])
    )

@dp.message(UploadTheme.waiting_for_file, F.document)
async def process_theme_file(message: Message, state: FSMContext):
    document = message.document
    if not document.file_name.endswith('.fptheme'):
        await message.reply("Неверный формат файла. Пожалуйста, отправьте файл с расширением `.fptheme`.")
        return

    if document.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.reply(f"Файл слишком большой. Максимальный размер - {config.MAX_FILE_SIZE_MB} МБ.")
        return
    
    temp_path = os.path.join(THEMES_DIR, f"temp_{message.from_user.id}_{document.file_unique_id}.fptheme")
    await bot.download(document, destination=temp_path)

    try:
        with open(temp_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        if db.has_duplicate_hash(file_hash):
            await message.reply("Такая тема уже была загружена в бота.")
            return

        with open(temp_path, 'r', encoding='utf-8') as f:
            theme_data = json.load(f)
        if not all(k in theme_data for k in ['bgColor1', 'font', 'bgImage']):
            raise ValueError("Invalid theme file structure")
            
    except Exception:
        await message.reply("Не удалось прочитать файл темы. Убедитесь, что это корректный JSON файл.")
        return
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

    await state.update_data(file_id=document.file_id, file_hash=file_hash, theme_data=theme_data)
    await state.set_state(UploadTheme.waiting_for_name)
    await message.answer("Файл принят! Теперь введите название для вашей темы (например, 'Cyberpunk Neon').")

@dp.message(UploadTheme.waiting_for_name)
async def process_theme_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(UploadTheme.waiting_for_description)
    await message.answer("Отличное название! Теперь введите краткое описание темы.")

@dp.message(UploadTheme.waiting_for_description)
async def process_theme_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(UploadTheme.waiting_for_privacy)
    await message.answer("Описание добавлено. Сделать тему публичной или приватной?",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="🌍 Публичная", callback_data="set_privacy_public"),
                              InlineKeyboardButton(text="🔒 Приватная", callback_data="set_privacy_private")],
                             [InlineKeyboardButton(text="❌ Отмена", callback_data="start")]
                         ]))

@dp.callback_query(UploadTheme.waiting_for_privacy, F.data.startswith("set_privacy_"))
async def process_theme_privacy(callback: CallbackQuery, state: FSMContext):
    is_public = 1 if callback.data == "set_privacy_public" else 0
    await state.update_data(is_public=is_public)
    
    await callback.message.edit_text("⏳ Генерирую превью... Это может занять несколько секунд.")
    
    user_data = await state.get_data()
    theme_data_from_file = user_data['theme_data']

    preview_path = os.path.join(THEMES_DIR, f"preview_{callback.from_user.id}_{secrets.token_hex(8)}.jpg")
    
    generated_path = await generate_preview(theme_data_from_file, preview_path)
    
    if not generated_path:
        await callback.message.edit_text("❌ Произошла ошибка при создании превью.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    try:
        preview_msg = await bot.send_photo(chat_id=callback.from_user.id, photo=FSInputFile(generated_path))
        preview_file_id = preview_msg.photo[-1].file_id

        db.add_theme(
            owner_id=callback.from_user.id, name=user_data['name'], description=user_data['description'],
            is_public=user_data['is_public'], file_id=user_data['file_id'], file_hash=user_data['file_hash'],
            preview_file_id=preview_file_id
        )

        final_caption = f"✅ Тема **{user_data['name']}** успешно загружена!\n\nВы можете управлять ей во вкладке 'Мои темы'."

        if not is_public:
            last_theme = db.get_user_themes(callback.from_user.id)[0]
            theme_info = db.get_theme_by_id(last_theme[0])
            if theme_info:
                 link = await create_start_link(bot, theme_info[1], encode=True)
                 final_caption += f"\n\n🔗 Ваша приватная ссылка: {link}"
        
        await callback.message.edit_text(final_caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В главное меню", callback_data="start")]]))

    except Exception as e:
        logging.error(f"Error in final upload stage: {e}")
        await callback.message.edit_text("❌ Произошла серьезная ошибка при сохранении темы.", reply_markup=main_menu_keyboard())
    finally:
        if os.path.exists(generated_path): os.remove(generated_path)
        await state.clear()

@dp.callback_query(F.data == "my_themes")
async def my_themes_handler(callback: CallbackQuery):
    themes = db.get_user_themes(callback.from_user.id)
    if not themes:
        await callback.answer("У вас пока нет загруженных тем.", show_alert=True)
        return
    
    keyboard = []
    for theme_id, name, is_public in themes:
        status = "🌍" if is_public else "🔒"
        keyboard.append([InlineKeyboardButton(text=f"{status} {name}", callback_data=f"manage_theme_{theme_id}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="start")])
    
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer("Ваши темы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await callback.message.edit_text("Ваши темы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("manage_theme_"))
async def manage_theme_handler(callback: CallbackQuery):
    theme_id = int(callback.data.split("_")[2])
    theme = db.get_theme_by_id(theme_id)
    
    if not theme or theme[2] != callback.from_user.id:
        await callback.answer("Тема не найдена или у вас нет прав.", show_alert=True)
        return
        
    _, unique_id, _, name, desc, is_public, file_id, file_hash, preview_file_id, _ = theme
    status_text = "Публичная" if is_public else "Приватная"

    temp_path = os.path.join(THEMES_DIR, f"temp_{callback.from_user.id}_{file_hash}.fptheme")
    await bot.download(file_id, destination=temp_path)
    with open(temp_path, 'r', encoding='utf-8') as f:
        theme_data = json.load(f)
    os.remove(temp_path)
    features = get_theme_features_text(theme_data)
    
    caption = f"🎨 **{name}**\n\n📝 *{desc}*\n\n⚙️ Включено: `{features}`\n\nСтатус: **{status_text}**"
    
    privacy_btn_text = "🔒 Сделать приватной" if is_public else "🌍 Сделать публичной"
    privacy_callback = f"privacy_theme_{theme_id}_{0 if is_public else 1}"
    
    keyboard = [
        [InlineKeyboardButton(text=privacy_btn_text, callback_data=privacy_callback)],
        [InlineKeyboardButton(text="🗑️ Удалить тему", callback_data=f"delete_theme_{theme_id}")],
    ]
    if not is_public:
         link = await create_start_link(bot, unique_id, encode=True)
         keyboard.append([InlineKeyboardButton(text="🔗 Получить ссылку", url=link)])
    
    keyboard.append([InlineKeyboardButton(text="🔙 К списку тем", callback_data="my_themes")])
    
    await bot.send_photo(
        chat_id=callback.message.chat.id, photo=preview_file_id, caption=caption,
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.message.delete()

@dp.callback_query(F.data.startswith("privacy_theme_"))
async def change_privacy_handler(callback: CallbackQuery):
    _, _, theme_id, new_status = callback.data.split("_")
    if db.set_theme_privacy(int(theme_id), callback.from_user.id, int(new_status)):
        await callback.answer("Статус приватности изменен!", show_alert=True)
        await manage_theme_handler(callback)
    else:
        await callback.answer("Ошибка!", show_alert=True)

@dp.callback_query(F.data.startswith("delete_theme_"))
async def delete_theme_handler(callback: CallbackQuery):
    theme_id = int(callback.data.split("_")[2])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, УДАЛИТЬ", callback_data=f"confirm_delete_{theme_id}")],
        [InlineKeyboardButton(text="🚫 Нет, отмена", callback_data=f"manage_theme_{theme_id}")]
    ]))

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_handler(callback: CallbackQuery):
    theme_id = int(callback.data.split("_")[2])
    if db.delete_theme(theme_id, callback.from_user.id):
        await callback.answer("Тема удалена.", show_alert=True)
        await callback.message.delete()
        await callback.message.answer("Возврат к списку тем...")
        await my_themes_handler(callback)
    else:
        await callback.answer("Ошибка при удалении.", show_alert=True)

async def show_store_page(callback_or_message, page: int):
    themes = db.get_public_themes()
    total_themes = len(themes)

    message_instance = callback_or_message if isinstance(callback_or_message, Message) else callback_or_message.message

    if not themes:
        await message_instance.edit_text("В магазине пока нет публичных тем.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="start")]]))
        return

    page = max(0, min(page, total_themes - 1))
    
    theme_id, name, desc, username, preview_file_id = themes[page]
    
    caption = f"🎨 **{name}**\n\n📝 *{desc}*\n\n👤 Автор: @{username}"
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"store_{page-1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text=" ", callback_data="noop"))

    nav_buttons.append(InlineKeyboardButton(text="📥 Скачать", callback_data=f"download_{theme_id}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1} / {total_themes}", callback_data="noop"))

    if page < total_themes - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"store_{page+1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text=" ", callback_data="noop"))
        
    keyboard = [nav_buttons, [InlineKeyboardButton(text="🔙 В главное меню", callback_data="start")]]
    
    media = InputMediaPhoto(media=preview_file_id, caption=caption, parse_mode="Markdown")
    
    if isinstance(callback_or_message, CallbackQuery):
        try:
            await callback_or_message.message.edit_media(media=media, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        except TelegramBadRequest as e:
            if "message is not modified" in e.message:
                await callback_or_message.answer()
            else:
                logging.error(f"Error editing media: {e}")
    else:
        await message_instance.answer_photo(photo=preview_file_id, caption=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("store_"))
async def store_handler(callback: CallbackQuery):
    page = int(callback.data.split("_")[1])
    
    
    if callback.message.text and "Добро пожаловать" in callback.message.text:
        await callback.message.delete()
        await show_store_page(callback.message, page)
    else: 
        await show_store_page(callback, page)

@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data.startswith("download_"))
async def download_theme_handler(callback: CallbackQuery):
    theme_id = int(callback.data.split("_")[1])
    theme = db.get_theme_by_id(theme_id)
    if theme:
        await bot.send_document(chat_id=callback.from_user.id, document=theme[6])
    else:
        await callback.answer("Тема не найдена.", show_alert=True)

@dp.callback_query(F.data == "buy_slots")
async def buy_slots_handler(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"{config.PAID_THEME_SLOTS} дополнительных слотов для тем",
        description=f"Купите {config.PAID_THEME_SLOTS} слотов, чтобы загружать больше тем!",
        payload=f"buy_slots_{callback.from_user.id}_{config.PAID_THEME_SLOTS}",
        provider_token="", currency="XTR",
        prices=[LabeledPrice(label=f"{config.PAID_THEME_SLOTS} слотов", amount=config.STARS_PRICE)]
    )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payload_parts = message.successful_payment.invoice_payload.split("_")
    if payload_parts[0] == "buy_slots":
        user_id = int(payload_parts[1])
        slots_bought = int(payload_parts[2])
        db.add_theme_slots(user_id, slots_bought)
        user_info = db.get_user(user_id)
        await bot.send_message(
            chat_id=user_id,
            text=f"✅ Оплата прошла успешно! Вам добавлено {slots_bought} слотов. "
                 f"Теперь у вас {user_info[2]} слотов."
        )

@dp.callback_query(F.data.startswith("admin_"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]
    if callback.from_user.id != config.ADMIN_ID: return
    
    if action == "broadcast":
        await state.set_state(AdminStates.broadcast_message)
        await callback.message.edit_text("Введите сообщение для рассылки всем пользователям:")
    elif action == "deletetheme":
        await state.set_state(AdminStates.delete_theme_id)
        await callback.message.edit_text("Введите ID темы для удаления:")
    elif action == "ban":
        await state.set_state(AdminStates.ban_user_id)
        await callback.message.edit_text("Введите ID пользователя для бана:")
    elif action == "unban":
        await state.set_state(AdminStates.unban_user_id)
        await callback.message.edit_text("Введите ID пользователя для разбана:")

@dp.message(AdminStates.broadcast_message)
async def broadcast_message(message: Message, state: FSMContext):
    await state.clear()
    users = db.get_all_users()
    count = 0
    for user in users:
        try:
            await bot.send_message(user[0], message.text)
            count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.warning(f"Could not send message to user {user[0]}: {e}")
    await message.answer(f"Рассылка завершена. Отправлено {count} пользователям.", reply_markup=admin_keyboard())

@dp.message(AdminStates.delete_theme_id)
async def admin_delete_theme_by_id(message: Message, state: FSMContext):
    await state.clear()
    try:
        theme_id = int(message.text)
        if db.admin_delete_theme(theme_id):
            await message.answer(f"Тема с ID {theme_id} удалена.", reply_markup=admin_keyboard())
        else:
            await message.answer(f"Тема с ID {theme_id} не найдена.", reply_markup=admin_keyboard())
    except ValueError:
        await message.answer("Неверный ID. Введите число.", reply_markup=admin_keyboard())

@dp.message(AdminStates.ban_user_id)
async def admin_ban_user(message: Message, state: FSMContext):
    await state.clear()
    try:
        user_id = int(message.text)
        db.set_ban_status(user_id, True)
        await message.answer(f"Пользователь с ID {user_id} забанен.", reply_markup=admin_keyboard())
    except ValueError:
        await message.answer("Неверный ID. Введите число.", reply_markup=admin_keyboard())

@dp.message(AdminStates.unban_user_id)
async def admin_unban_user(message: Message, state: FSMContext):
    await state.clear()
    try:
        user_id = int(message.text)
        db.set_ban_status(user_id, False)
        await message.answer(f"Пользователь с ID {user_id} разбанен.", reply_markup=admin_keyboard())
    except ValueError:
        await message.answer("Неверный ID. Введите число.", reply_markup=admin_keyboard())

async def main():
    if not os.path.exists(THEMES_DIR):
        os.makedirs(THEMES_DIR)
    
    for style in ['Regular', 'Bold']:
        font_filename = f'Roboto-{style}.ttf'
        font_path = os.path.join(SCRIPT_DIR, font_filename)
        if not os.path.exists(font_path):
            try:
                logging.info(f"Downloading font: {font_filename}")
                url = f"https://github.com/google/fonts/raw/main/apache/roboto/{font_filename}"
                response = requests.get(url)
                response.raise_for_status()
                with open(font_path, 'wb') as f:
                    f.write(response.content)
                logging.info(f"Font {font_filename} downloaded successfully.")
            except Exception as e:
                logging.error(f"Could not download font {font_filename}: {e}")

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
