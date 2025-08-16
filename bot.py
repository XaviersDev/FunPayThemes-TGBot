
import asyncio
import logging
import os
import sqlite3
import json
import hashlib
import secrets
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    FSInputFile,
    PreCheckoutQuery, LabeledPrice, SuccessfulPayment
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.deep_linking import create_start_link
from playwright.async_api import async_playwright

import config


logging.basicConfig(level=logging.INFO)


bot = Bot(token=config.API_TOKEN)
dp = Dispatcher()


class Database:
    def __init__(self, db_file):
        self.connection = sqlite3.connect(db_file)
        self.cursor = self.connection.cursor()
        self.setup()

    def setup(self):
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                theme_slots INTEGER DEFAULT 10,
                is_banned INTEGER DEFAULT 0
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS themes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_id TEXT NOT NULL UNIQUE,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                is_public INTEGER DEFAULT 1,
                file_id TEXT NOT NULL,
                file_hash TEXT NOT NULL UNIQUE,
                preview_file_id TEXT,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_id) REFERENCES users (id)
            )
        ''')
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
    
    def get_public_themes(self, offset=0, limit=5):
        self.cursor.execute('''
            SELECT t.id, t.name, t.description, u.username 
            FROM themes t JOIN users u ON t.owner_id = u.id
            WHERE t.is_public = 1 ORDER BY t.upload_date DESC LIMIT ? OFFSET ?
        ''', (limit, offset))
        return self.cursor.fetchall()

    def count_public_themes(self):
        self.cursor.execute("SELECT COUNT(*) FROM themes WHERE is_public = 1")
        return self.cursor.fetchone()[0]

    def add_theme_slots(self, user_id, slots_to_add):
        self.cursor.execute("UPDATE users SET theme_slots = theme_slots + ? WHERE id = ?", (slots_to_add, user_id))
        self.connection.commit()


db = Database(config.DB_NAME)


class UploadTheme(StatesGroup):
    waiting_for_file = State()
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_privacy = State()


class AccessMiddleware:
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        if db.is_banned(user_id):
            return 
        
        try:
            member = await bot.get_chat_member(chat_id=config.CHANNEL_ID, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                await event.answer(
                    "Для использования бота необходимо подписаться на наш канал.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="➡️ Подписаться", url=f"https://t.me/{config.CHANNEL_ID.replace('@', '')}")],
                        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")]
                    ]),
                    show_alert=True if isinstance(event, CallbackQuery) else False
                )
                return
        except Exception:
            if isinstance(event, Message):
                await event.answer("Не удалось проверить подписку. Убедитесь, что вы подписаны на канал.")
            return

        return await handler(event, data)

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())


async def generate_preview(theme_data: dict, temp_file_path: str):
    try:
        with open('preview_template.html', 'r', encoding='utf-8') as f:
            template = f.read()

        
        template = template.replace('/*BG_COLOR_1*/', theme_data.get('bgColor1', '#000000'))
        template = template.replace('/*BG_COLOR_2*/', theme_data.get('bgColor2', '#000000'))
        template = template.replace('/*CONTAINER_BG_COLOR*/', theme_data.get('containerBgColor', 'rgba(0,0,0,0.5)'))
        template = template.replace('/*TEXT_COLOR*/', theme_data.get('textColor', '#ffffff'))
        template = template.replace('/*LINK_COLOR*/', theme_data.get('linkColor', '#0099ff'))
        template = template.replace('/*FONT_FAMILY*/', theme_data.get('font', 'Roboto'))
        template = template.replace('/*BORDER_RADIUS*/', str(theme_data.get('borderRadius', 8)))
        template = template.replace('/*BG_BLUR*/', str(theme_data.get('bgBlur', 0)))
        template = template.replace('/*BG_BRIGHTNESS*/', str(theme_data.get('bgBrightness', 100)))
        
        bg_image_url = theme_data.get('bgImage', '')
        
        if bg_image_url and bg_image_url.startswith('data:image'):
            
            bg_image_url = 'https://i.ibb.co/ZpS0d56/PH6-UEvp-Kn-KI.jpg' 
        template = template.replace('/*BG_IMAGE*/', bg_image_url)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(template)
            await page.locator('#capture-area').screenshot(path=temp_file_path, type='jpeg', quality=85)
            await browser.close()
        return temp_file_path
    except Exception as e:
        logging.error(f"Error generating preview: {e}")
        return None


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
            
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=preview_file_id,
                caption=caption,
                parse_mode="Markdown"
            )
            await bot.send_document(chat_id=message.chat.id, document=file_id)
            return

    await message.answer("👋 Добро пожаловать в FP Themes Bot!\n\nЗдесь вы можете загружать, скачивать и делиться темами для расширения FunPay Tools.",
                         reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("👋 Добро пожаловать в FP Themes Bot!\n\nЗдесь вы можете загружать, скачивать и делиться темами для расширения FunPay Tools.",
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
        f"Отлично! Отправьте мне файл темы в формате `.fptheme`.\n\n"
        f"⚠️ **Требования:**\n"
        f"- Только формат `.fptheme`\n"
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
    
    
    if not os.path.exists(config.THEMES_DIR):
        os.makedirs(config.THEMES_DIR)
        
    temp_path = os.path.join(config.THEMES_DIR, f"temp_{message.from_user.id}_{document.file_unique_id}.fptheme")
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
        if os.path.exists(temp_path):
            os.remove(temp_path) 

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
                             [
                                 InlineKeyboardButton(text="🌍 Публичная", callback_data="set_privacy_public"),
                                 InlineKeyboardButton(text="🔒 Приватная", callback_data="set_privacy_private")
                             ],
                             [InlineKeyboardButton(text="❌ Отмена", callback_data="start")]
                         ]))

@dp.callback_query(UploadTheme.waiting_for_privacy, F.data.startswith("set_privacy_"))
async def process_theme_privacy(callback: CallbackQuery, state: FSMContext):
    is_public = 1 if callback.data == "set_privacy_public" else 0
    await state.update_data(is_public=is_public)
    
    await callback.message.edit_text("⏳ Генерирую превью... Это может занять до 30 секунд.")
    
    user_data = await state.get_data()
    theme_data = user_data['theme_data']
    
    preview_path = os.path.join(config.THEMES_DIR, f"preview_{callback.from_user.id}_{secrets.token_hex(8)}.jpg")
    
    generated_path = await generate_preview(theme_data, preview_path)
    
    if not generated_path:
        await callback.message.edit_text("❌ Произошла ошибка при создании превью. Попробуйте загрузить тему еще раз.")
        await state.clear()
        return

    try:
        
        preview_msg = await bot.send_photo(chat_id=callback.from_user.id, photo=FSInputFile(generated_path))
        preview_file_id = preview_msg.photo[-1].file_id

        
        db.add_theme(
            owner_id=callback.from_user.id,
            name=user_data['name'],
            description=user_data['description'],
            is_public=user_data['is_public'],
            file_id=user_data['file_id'],
            file_hash=user_data['file_hash'],
            preview_file_id=preview_file_id
        )

        final_caption = f"✅ Тема **{user_data['name']}** успешно загружена!\n\nВы можете управлять ей во вкладке 'Мои темы'."

        if not is_public:
            theme_raw = db.get_theme_by_unique_id(user_data['file_hash']) 
            if theme_raw:
                 link = await create_start_link(bot, theme_raw[1], encode=True)
                 final_caption += f"\n\n🔗 Ваша приватная ссылка: {link}"
        
        await callback.message.edit_text(final_caption, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error in final upload stage: {e}")
        await callback.message.edit_text("❌ Произошла серьезная ошибка при сохранении темы. Пожалуйста, сообщите администратору.")
    finally:
        if os.path.exists(generated_path):
            os.remove(generated_path)
        await state.clear()
        await callback.message.answer("Возвращаю в главное меню...", reply_markup=main_menu_keyboard())





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
    
    await callback.message.edit_text("Ваши темы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("manage_theme_"))
async def manage_theme_handler(callback: CallbackQuery):
    theme_id = int(callback.data.split("_")[2])
    theme = db.get_theme_by_id(theme_id)
    
    if not theme or theme[2] != callback.from_user.id:
        await callback.answer("Тема не найдена или у вас нет прав.", show_alert=True)
        return
        
    _, unique_id, _, name, desc, is_public, file_id, _, preview_file_id, _ = theme
    status_text = "Публичная" if is_public else "Приватная"
    
    caption = f"🎨 **{name}**\n\n📝 *{desc}*\n\nСтатус: **{status_text}**"
    
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
        chat_id=callback.message.chat.id,
        photo=preview_file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
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
        await my_themes_handler(callback)
    else:
        await callback.answer("Ошибка при удалении.", show_alert=True)
        

@dp.callback_query(F.data.startswith("store_"))
async def store_handler(callback: CallbackQuery):
    page = int(callback.data.split("_")[1])
    limit = 5
    offset = page * limit
    
    themes = db.get_public_themes(offset, limit)
    total_themes = db.count_public_themes()
    
    if not themes:
        await callback.message.edit_text("В магазине пока нет публичных тем.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="start")]]))
        return
        
    for theme_id, name, desc, username in themes:
        theme = db.get_theme_by_id(theme_id)
        caption = f"🎨 **{name}**\n📝 *{desc}*\n👤 Автор: @{username}"
        await bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=theme[8], 
            caption=caption,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📥 Скачать", callback_data=f"download_{theme_id}")]
            ])
        )
    
    
    has_next = (page + 1) * limit < total_themes
    has_prev = page > 0
    nav_buttons = []
    if has_prev:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"store_{page-1}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"store_{page+1}"))
        
    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="start")])
    
    await callback.message.answer(f"Страница {page + 1}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass 

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
        provider_token="", 
        currency="XTR",
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





async def main():
    if not os.path.exists(config.THEMES_DIR):
        os.makedirs(config.THEMES_DIR)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
