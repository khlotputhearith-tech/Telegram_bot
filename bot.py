"""Telegram bot service for teacher attendance monitoring."""

from __future__ import annotations

from datetime import time
import logging
from typing import Iterable
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.db import Database

LOGGER = logging.getLogger(__name__)

REG_FULL_NAME, REG_CLASS = range(2)
REP_SCHEDULE, REP_STATUS, REP_NOTE, REP_SUBSTITUTE_NAME = range(10, 14)

WEEKDAYS = ["ចន្ទ", "អង្គារ", "ពុធ", "ព្រហស្បតិ៍", "សុក្រ", "សៅរ៍", "អាទិត្យ"]
STATUS_LABELS = {
    "present": "មកបង្រៀន",
    "late": "មកយឺត",
    "absent": "អវត្តមាន",
    "substitute": "គ្រូជំនួស",
}
STATUS_BADGES = {
    "present": "មក",
    "late": "យឺត",
    "absent": "អវត្ត",
    "substitute": "ជំនួស",
}
REPORTER_MENU = {
    "report": "📝 រាយការណ៍វត្តមាន",
    "today": "📋 របាយការណ៍ថ្ងៃនេះ",
    "schedules": "📚 កាលវិភាគ",
    "classes": "🏫 បញ្ជីថ្នាក់",
    "me": "👤 ព័ត៌មានខ្ញុំ",
    "status": "📡 ស្ថានភាពប្រព័ន្ធ",
    "help": "ℹ️ ជំនួយ",
}
ADMIN_MENU = {
    "summary": "📊 សរុបប្រចាំថ្ងៃ",
    "pending": "⏳ របាយការណ៍ខ្វះ",
    "reporters": "👥 អ្នករាយការណ៍",
    "admins": "👑 អ្នកគ្រប់គ្រង",
    "schedules": "📚 កាលវិភាគ",
    "classes": "🏫 បញ្ជីថ្នាក់",
    "me": "👤 ព័ត៌មានខ្ញុំ",
    "status": "📡 ស្ថានភាពប្រព័ន្ធ",
    "help": "ℹ️ ជំនួយ",
}


class AttendanceBot:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        defaults = Defaults(tzinfo=ZoneInfo(settings.timezone))
        self.application = Application.builder().token(settings.bot_token).defaults(defaults).build()
        self._register_handlers()
        self._register_jobs()

    def _register_handlers(self) -> None:
        registration_conversation = ConversationHandler(
            entry_points=[CommandHandler("start", self.start)],
            states={
                REG_FULL_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.register_capture_full_name)
                ],
                REG_CLASS: [CallbackQueryHandler(self.register_choose_class, pattern=r"^reg_class:")],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            allow_reentry=True,
        )

        reporting_conversation = ConversationHandler(
            entry_points=[CommandHandler("report", self.report_start), CommandHandler("r", self.report_start)],
            states={
                REP_SCHEDULE: [CallbackQueryHandler(self.report_choose_schedule, pattern=r"^rep_sched:")],
                REP_STATUS: [CallbackQueryHandler(self.report_choose_status, pattern=r"^rep_status:")],
                REP_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.report_receive_note)],
                REP_SUBSTITUTE_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.report_receive_substitute_name)
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            allow_reentry=True,
        )

        self.application.add_handler(registration_conversation)
        self.application.add_handler(reporting_conversation)

        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler(["today", "d"], self.today_command))
        self.application.add_handler(CommandHandler(["summary", "sum"], self.summary_command))
        self.application.add_handler(CommandHandler(["pending", "pen"], self.pending_command))
        self.application.add_handler(CommandHandler("reporters", self.reporters_command))
        self.application.add_handler(CommandHandler("admins", self.admins_command))
        self.application.add_handler(CommandHandler("me", self.me_command))
        self.application.add_handler(CommandHandler(["classes", "cls"], self.classes_command))
        self.application.add_handler(CommandHandler(["schedules", "sch"], self.schedules_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("cancel", self.cancel))
        self.application.add_handler(CallbackQueryHandler(self.ignore_unknown_callback))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.menu_text_router),
            group=2,
        )

        self.application.add_error_handler(self.error_handler)

    def _register_jobs(self) -> None:
        if self.application.job_queue is None:
            LOGGER.warning("JobQueue unavailable; reminder job is disabled")
            return

        reminder_time = time(
            hour=self.settings.reminder_hour,
            minute=self.settings.reminder_minute,
            tzinfo=ZoneInfo(self.settings.timezone),
        )
        self.application.job_queue.run_daily(
            self.daily_reminder_job,
            time=reminder_time,
            days=(0, 1, 2, 3, 4, 5, 6),
            name="daily-attendance-reminder",
        )
        LOGGER.info(
            "Registered daily reminder at %02d:%02d (%s)",
            self.settings.reminder_hour,
            self.settings.reminder_minute,
            self.settings.timezone,
        )

    def run(self) -> None:
        LOGGER.info("Starting Telegram bot in polling mode")
        self.application.run_polling(drop_pending_updates=True)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        tg_user = update.effective_user
        if tg_user is None or update.effective_chat is None:
            return ConversationHandler.END

        user = self.db.get_user(tg_user.id)
        if user:
            self.db.log_action(tg_user.id, "start_existing")
            await update.effective_chat.send_message(
                self._role_welcome_text(user["role"], user["full_name"], user["class_name"]),
                reply_markup=self._menu_keyboard(user["role"]),
            )
            return ConversationHandler.END

        if tg_user.id in self.settings.admin_ids:
            full_name = tg_user.full_name or tg_user.username or f"Admin {tg_user.id}"
            self.db.create_or_update_user(
                telegram_id=tg_user.id,
                full_name=full_name,
                role="admin",
                class_name=None,
                is_active=1,
            )
            self.db.log_action(tg_user.id, "auto_register_admin")
            await update.effective_chat.send_message(
                self._role_welcome_text("admin", full_name, None),
                reply_markup=self._menu_keyboard("admin"),
            )
            return ConversationHandler.END

        await update.effective_chat.send_message(
            "សួស្តី! ប្រព័ន្ធតាមដានវត្តមានគ្រូ។\nសូមផ្ញើឈ្មោះពេញរបស់អ្នក។"
        )
        return REG_FULL_NAME

    async def register_capture_full_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.message is None or update.effective_user is None:
            return ConversationHandler.END

        full_name = (update.message.text or "").strip()
        if not full_name:
            await update.message.reply_text("សូមបញ្ចូលឈ្មោះឱ្យបានត្រឹមត្រូវ។")
            return REG_FULL_NAME

        classes = self.db.list_classes()
        if not classes:
            await update.message.reply_text("មិនមានថ្នាក់នៅក្នុងប្រព័ន្ធទេ។ សូមទាក់ទងអ្នកគ្រប់គ្រង។")
            return ConversationHandler.END

        context.user_data["register_full_name"] = full_name

        keyboard: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for class_name in classes:
            row.append(InlineKeyboardButton(class_name, callback_data=f"reg_class:{class_name}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        await update.message.reply_text(
            "សូមជ្រើសថ្នាក់របស់អ្នក:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return REG_CLASS

    async def register_choose_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query is None or update.effective_user is None:
            return ConversationHandler.END

        query = update.callback_query
        await query.answer()

        class_name = query.data.split(":", maxsplit=1)[1]
        full_name = context.user_data.get("register_full_name") or (
            update.effective_user.full_name or f"Reporter {update.effective_user.id}"
        )

        self.db.create_or_update_user(
            telegram_id=update.effective_user.id,
            full_name=full_name,
            role="reporter",
            class_name=class_name,
            is_active=1,
        )
        self.db.log_action(update.effective_user.id, f"register_reporter:{class_name}")
        context.user_data.pop("register_full_name", None)

        await query.edit_message_text(
            self._role_welcome_text("reporter", full_name, class_name)
        )
        await self._send_message(
            update,
            "មីនុយរួចរាល់។ អ្នកអាចចុចប៊ូតុងខាងក្រោមបាន។",
            reply_markup=self._menu_keyboard("reporter"),
        )
        return ConversationHandler.END

    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return
        await self._send_message(
            update,
            "នេះជាមីនុយរហ័ស។ ចុចប៊ូតុងដើម្បីដំណើរការ។",
            reply_markup=self._menu_keyboard(user["role"]),
        )

    async def menu_text_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return

        if any(k in context.user_data for k in ("pending_status", "schedule_id", "register_full_name")):
            return

        user = self._get_user(update)
        if not user:
            return

        text = (update.message.text or "").strip()
        action_map = REPORTER_MENU if user["role"] == "reporter" else ADMIN_MENU
        reverse_map = {v: k for k, v in action_map.items()}
        action = reverse_map.get(text)
        if not action:
            return

        dispatch = {
            "report": self.report_start,
            "today": self.today_command,
            "schedules": self.schedules_command,
            "classes": self.classes_command,
            "me": self.me_command,
            "status": self.status_command,
            "help": self.help_command,
            "summary": self.summary_command,
            "pending": self.pending_command,
            "reporters": self.reporters_command,
            "admins": self.admins_command,
        }
        handler = dispatch.get(action)
        if handler:
            await handler(update, context)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return

        text = (
            "ជំនួយការប្រើប្រាស់\n"
            "- ចុច /menu ដើម្បីបើកមីនុយប៊ូតុង\n"
            "- Shortcut លឿន: /r /d /sum /pen /sch /cls\n"
            f"- ពាក្យបញ្ជា: {self._command_list_for_role(user['role'])}"
        )
        await self._send_message(update, text, reply_markup=self._menu_keyboard(user["role"]))

    async def me_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return

        active = "សកម្ម" if int(user["is_active"]) == 1 else "បិទ"
        role_kh = "អ្នកគ្រប់គ្រង" if user["role"] == "admin" else "អ្នករាយការណ៍"
        class_name = user["class_name"] or "-"
        text = (
            f"ឈ្មោះ: {user['full_name']}\n"
            f"Telegram ID: {user['telegram_id']}\n"
            f"តួនាទី: {role_kh}\n"
            f"ថ្នាក់: {class_name}\n"
            f"ស្ថានភាព: {active}"
        )
        await self._send_message(update, text)

    async def classes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        classes = self.db.list_classes()
        if not classes:
            await self._send_message(update, "មិនមានទិន្នន័យថ្នាក់ទេ។")
            return
        await self._send_message(update, "បញ្ជីថ្នាក់:\n" + "\n".join(f"- {c}" for c in classes))

    async def schedules_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return

        if user["role"] == "reporter":
            rows = self.db.get_schedules(class_name=user["class_name"])
            text = self._format_schedules(rows, include_class=False)
            await self._send_long_message(update, text or "មិនមានកាលវិភាគសម្រាប់ថ្នាក់របស់អ្នកទេ។")
            return

        rows = self.db.get_schedules()
        text = self._format_schedules(rows, include_class=True)
        await self._send_long_message(update, text or "មិនមានកាលវិភាគទេ។")

    async def today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return

        if user["role"] != "reporter":
            await self._send_message(update, "សម្រាប់អ្នកគ្រប់គ្រង សូមប្រើ /summary និង /pending")
            return

        report_date = self.db.today_str()
        rows = self.db.get_reports_for_class_and_date(user["class_name"], report_date)
        if not rows:
            await self._send_message(update, "មិនទាន់មានរបាយការណ៍ថ្ងៃនេះទេ។")
            return

        lines = [f"របាយការណ៍ថ្ងៃនេះ - {user['class_name']} ({report_date})"]
        for row in rows:
            badge = STATUS_BADGES.get(row["status"], "?")
            line = f"[{badge}] {row['period_name']} | {row['subject_name']} | {row['teacher_name']}"
            if row["status"] in {"late", "absent"} and row["note"]:
                line += f" | មូលហេតុ: {row['note']}"
            if row["status"] == "substitute" and row["substitute_teacher_name"]:
                line += f" | គ្រូជំនួស: {row['substitute_teacher_name']}"
            lines.append(line)

        await self._send_long_message(update, "\n".join(lines))

    async def summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not self._is_admin(user):
            await self._send_message(update, "មានតែអ្នកគ្រប់គ្រងប៉ុណ្ណោះអាចប្រើ /summary")
            return

        report_date = self.db.today_str()
        summary = self.db.get_summary(report_date=report_date)
        text = (
            f"សរុបប្រចាំថ្ងៃ ({report_date})\n"
            f"សរុបរបាយការណ៍: {summary.total}\n"
            f"មកបង្រៀន: {summary.present}\n"
            f"មកយឺត: {summary.late}\n"
            f"អវត្តមាន: {summary.absent}\n"
            f"គ្រូជំនួស: {summary.substitute}"
        )
        await self._send_message(update, text)

    async def pending_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not self._is_admin(user):
            await self._send_message(update, "មានតែអ្នកគ្រប់គ្រងប៉ុណ្ណោះអាចប្រើ /pending")
            return

        report_date = self.db.today_str()
        rows = self.db.get_pending_reports(report_date)
        if not rows:
            await self._send_message(update, f"មិនមានរបាយការណ៍ខ្វះសម្រាប់ {report_date} ទេ។")
            return

        lines = [f"បញ្ជីរបាយការណ៍ខ្វះ ({report_date}): {len(rows)}"]
        for row in rows[:80]:
            lines.append(
                f"- {row['class_name']} | {row['period_name']} | {row['subject_name']} | {row['teacher_name']}"
            )
        if len(rows) > 80:
            lines.append(f"... និង {len(rows) - 80} វគ្គទៀត")
        await self._send_long_message(update, "\n".join(lines))

    async def reporters_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not self._is_admin(user):
            await self._send_message(update, "មានតែអ្នកគ្រប់គ្រងប៉ុណ្ណោះអាចប្រើ /reporters")
            return

        rows = self.db.list_users_by_role("reporter")
        if not rows:
            await self._send_message(update, "មិនទាន់មានអ្នករាយការណ៍ទេ។")
            return

        lines = ["បញ្ជីអ្នករាយការណ៍:"]
        for row in rows:
            active = "សកម្ម" if int(row["is_active"]) == 1 else "បិទ"
            lines.append(f"- {row['full_name']} | {row['class_name']} | {row['telegram_id']} | {active}")
        await self._send_long_message(update, "\n".join(lines))

    async def admins_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        if not self._is_admin(user):
            await self._send_message(update, "មានតែអ្នកគ្រប់គ្រងប៉ុណ្ណោះអាចប្រើ /admins")
            return

        rows = self.db.list_users_by_role("admin")
        if not rows:
            await self._send_message(update, "មិនទាន់មានអ្នកគ្រប់គ្រងទេ។")
            return

        lines = ["បញ្ជីអ្នកគ្រប់គ្រង:"]
        for row in rows:
            lines.append(f"- {row['full_name']} | {row['telegram_id']}")
        await self._send_message(update, "\n".join(lines))

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_user(update)
        role = user["role"] if user else "unknown"
        role_kh = "អ្នកគ្រប់គ្រង" if role == "admin" else "អ្នករាយការណ៍" if role == "reporter" else "មិនស្គាល់"
        now = self.db.now()
        text = (
            "ស្ថានភាពប្រព័ន្ធ: កំពុងដំណើរការ\n"
            f"ថ្ងៃខែ: {now.date().isoformat()}\n"
            f"ម៉ោង: {now.strftime('%H:%M:%S')}\n"
            f"តំបន់ពេលវេលា: {self.settings.timezone}\n"
            f"តួនាទី: {role_kh}"
        )
        await self._send_message(update, text)

    async def report_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = self._get_user(update)
        if not user:
            await self._send_message(update, "សូមប្រើ /start ជាមុនសិន។")
            return ConversationHandler.END

        if user["role"] != "reporter":
            await self._send_message(update, "មានតែអ្នករាយការណ៍ប៉ុណ្ណោះអាចប្រើ /report")
            return ConversationHandler.END

        report_date = self.db.today_str()
        weekday = self.db.weekday_for_date(report_date)
        schedules = self.db.get_schedules_for_class_and_weekday(user["class_name"], weekday)

        if not schedules:
            await self._send_message(update, "ថ្ងៃនេះមិនមានកាលវិភាគទេ។")
            return ConversationHandler.END

        existing = {
            row["schedule_id"]: row["status"]
            for row in self.db.get_reports_for_class_and_date(user["class_name"], report_date)
        }

        keyboard: list[list[InlineKeyboardButton]] = []
        for row in schedules:
            badge = STATUS_BADGES.get(existing.get(row["id"], ""), "")
            prefix = f"[{badge}] " if badge else ""
            label = f"{prefix}{row['period_name']} | {row['subject_name']} | {row['teacher_name']}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"rep_sched:{row['id']}")])

        context.user_data["report_date"] = report_date

        await self._send_message(
            update,
            f"{user['class_name']} - {WEEKDAYS[weekday]}\nសូមជ្រើសម៉ោងសិក្សាដើម្បីរាយការណ៍:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return REP_SCHEDULE

    async def report_choose_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query is None:
            return ConversationHandler.END
        query = update.callback_query
        await query.answer()

        user = self._get_user(update)
        if not user or user["role"] != "reporter":
            await query.edit_message_text("មានតែអ្នករាយការណ៍ប៉ុណ្ណោះអាចរាយការណ៍បាន។")
            return ConversationHandler.END

        schedule_id = int(query.data.split(":", maxsplit=1)[1])
        schedule = self.db.get_schedule_by_id(schedule_id)
        if not schedule or schedule["class_name"] != user["class_name"]:
            await query.edit_message_text("កាលវិភាគមិនត្រឹមត្រូវ។")
            return ConversationHandler.END

        context.user_data["schedule_id"] = schedule_id

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("មកបង្រៀន", callback_data="rep_status:present"),
                    InlineKeyboardButton("មកយឺត", callback_data="rep_status:late"),
                ],
                [
                    InlineKeyboardButton("អវត្តមាន", callback_data="rep_status:absent"),
                    InlineKeyboardButton("គ្រូជំនួស", callback_data="rep_status:substitute"),
                ],
            ]
        )

        await query.edit_message_text(
            (
                f"{schedule['period_name']} | {schedule['subject_name']}\n"
                f"គ្រូ: {schedule['teacher_name']}\n"
                "សូមជ្រើសស្ថានភាព:"
            ),
            reply_markup=keyboard,
        )
        return REP_STATUS

    async def report_choose_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.callback_query is None:
            return ConversationHandler.END

        query = update.callback_query
        await query.answer()
        status = query.data.split(":", maxsplit=1)[1]

        if status == "present":
            await self._persist_report(update, context, status=status, note=None, substitute_name=None)
            await query.edit_message_text("រក្សាទុករួចរាល់: មកបង្រៀន")
            return ConversationHandler.END

        if status in {"late", "absent"}:
            context.user_data["pending_status"] = status
            await query.edit_message_text("សូមវាយមូលហេតុ:")
            return REP_NOTE

        if status == "substitute":
            context.user_data["pending_status"] = status
            await query.edit_message_text("សូមបញ្ចូលឈ្មោះគ្រូជំនួស:")
            return REP_SUBSTITUTE_NAME

        await query.edit_message_text("ស្ថានភាពមិនត្រឹមត្រូវ។")
        return ConversationHandler.END

    async def report_receive_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if update.message is None:
            return ConversationHandler.END

        status = context.user_data.get("pending_status")
        note = (update.message.text or "").strip()

        if status not in {"late", "absent"}:
            await update.message.reply_text("សម័យបានផុតកំណត់។ សូមប្រើ /report ម្តងទៀត។")
            return ConversationHandler.END

        if not note:
            await update.message.reply_text("សូមវាយមូលហេតុខ្លីមួយ។")
            return REP_NOTE

        await self._persist_report(update, context, status=status, note=note, substitute_name=None)
        await update.message.reply_text(f"រក្សាទុករួចរាល់: {STATUS_LABELS[status]}")
        return ConversationHandler.END

    async def report_receive_substitute_name(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        if update.message is None:
            return ConversationHandler.END

        name = (update.message.text or "").strip()
        if not name:
            await update.message.reply_text("សូមបញ្ចូលឈ្មោះគ្រូជំនួស។")
            return REP_SUBSTITUTE_NAME

        await self._persist_report(
            update,
            context,
            status="substitute",
            note=None,
            substitute_name=name,
        )
        await update.message.reply_text("រក្សាទុករួចរាល់: គ្រូជំនួស")
        return ConversationHandler.END

    async def _persist_report(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        status: str,
        note: str | None,
        substitute_name: str | None,
    ) -> None:
        user = self._get_user(update)
        if not user:
            return

        report_date = context.user_data.get("report_date") or self.db.today_str()
        schedule_id = context.user_data.get("schedule_id")
        if schedule_id is None:
            await self._send_message(update, "សម័យបានផុតកំណត់។ សូមប្រើ /report ម្តងទៀត។")
            return

        schedule = self.db.get_schedule_by_id(int(schedule_id))
        if not schedule:
            await self._send_message(update, "រកមិនឃើញកាលវិភាគទេ។")
            return

        existed = self.db.get_report(report_date, user["class_name"], int(schedule_id)) is not None

        self.db.upsert_attendance_report(
            report_date=report_date,
            class_name=user["class_name"],
            schedule_id=int(schedule_id),
            teacher_name=schedule["teacher_name"],
            subject_name=schedule["subject_name"],
            period_name=schedule["period_name"],
            time_range=schedule["time_range"],
            status=status,
            note=note,
            substitute_teacher_name=substitute_name,
            reporter_telegram_id=int(user["telegram_id"]),
            reporter_name=user["full_name"],
        )

        action = "update_report" if existed else "create_report"
        self.db.log_action(int(user["telegram_id"]), f"{action}:{schedule_id}:{status}")

        await self._notify_admins_on_report(
            report_date=report_date,
            class_name=user["class_name"],
            schedule=schedule,
            status=status,
            note=note,
            substitute_name=substitute_name,
            reporter_name=user["full_name"],
            reporter_telegram_id=int(user["telegram_id"]),
            updated=existed,
        )

        context.user_data.pop("pending_status", None)
        context.user_data.pop("schedule_id", None)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("pending_status", None)
        context.user_data.pop("schedule_id", None)
        context.user_data.pop("register_full_name", None)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("បានបោះបង់។")
        else:
            await self._send_message(update, "បានបោះបង់។")
        return ConversationHandler.END

    async def ignore_unknown_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query:
            await update.callback_query.answer("សំណើផុតកំណត់។ សូមសាកល្បងម្ដងទៀត។", show_alert=False)

    async def daily_reminder_job(self, context: CallbackContext) -> None:
        today = self.db.today_str()
        reporters = self.db.list_users_by_role("reporter")

        for reporter in reporters:
            if int(reporter["is_active"]) != 1:
                continue

            pending_count = len(self.db.get_pending_reports(today, class_name=reporter["class_name"]))
            if pending_count == 0:
                continue

            text = (
                "ការរំលឹក: សូមរាយការណ៍វត្តមានគ្រូថ្ងៃនេះ\n"
                f"ថ្នាក់: {reporter['class_name']}\n"
                f"ចំនួនម៉ោងនៅសល់: {pending_count}\n"
                "សូមប្រើ /report"
            )
            try:
                await context.bot.send_message(chat_id=int(reporter["telegram_id"]), text=text)
            except Exception as exc:
                LOGGER.warning("Failed to send reminder to %s: %s", reporter["telegram_id"], exc)

        await self._notify_admins_daily_overview(context, today)

    async def _get_admin_chat_ids(self) -> list[int]:
        chat_ids: set[int] = set(self.settings.admin_ids)
        for row in self.db.list_users_by_role("admin"):
            if int(row["is_active"]) != 1:
                continue
            if row["telegram_id"] is None:
                continue
            chat_ids.add(int(row["telegram_id"]))
        return sorted(chat_ids)

    async def _notify_admins_on_report(
        self,
        report_date: str,
        class_name: str,
        schedule: object,
        status: str,
        note: str | None,
        substitute_name: str | None,
        reporter_name: str,
        reporter_telegram_id: int,
        updated: bool,
    ) -> None:
        admin_chat_ids = await self._get_admin_chat_ids()
        if not admin_chat_ids:
            return

        status_kh = STATUS_LABELS.get(status, status)
        change_type = "កែប្រែ" if updated else "ថ្មី"
        text = (
            f"ជូនដំណឹងទៅនាយកសាលា / ICT ({change_type})\n"
            f"ថ្ងៃ: {report_date}\n"
            f"ថ្នាក់: {class_name}\n"
            f"ម៉ោង: {schedule['period_name']} ({schedule['time_range']})\n"
            f"មុខវិជ្ជា: {schedule['subject_name']}\n"
            f"គ្រូ: {schedule['teacher_name']}\n"
            f"ស្ថានភាព: {status_kh}\n"
            f"អ្នករាយការណ៍: {reporter_name} ({reporter_telegram_id})"
        )

        if note:
            text += f"\nមូលហេតុ: {note}"
        if substitute_name:
            text += f"\nគ្រូជំនួស: {substitute_name}"

        for chat_id in admin_chat_ids:
            try:
                await self.application.bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:
                LOGGER.warning("Failed to send admin report notification to %s: %s", chat_id, exc)

    async def _notify_admins_daily_overview(self, context: CallbackContext, report_date: str) -> None:
        admin_chat_ids = await self._get_admin_chat_ids()
        if not admin_chat_ids:
            return

        summary = self.db.get_summary(report_date=report_date)
        pending_total = len(self.db.get_pending_reports(report_date))
        text = (
            "ជូនដំណឹងប្រចាំថ្ងៃទៅនាយកសាលា / ICT\n"
            f"ថ្ងៃ: {report_date}\n"
            f"សរុបរបាយការណ៍: {summary.total}\n"
            f"មកបង្រៀន: {summary.present}\n"
            f"មកយឺត: {summary.late}\n"
            f"អវត្តមាន: {summary.absent}\n"
            f"គ្រូជំនួស: {summary.substitute}\n"
            f"របាយការណ៍នៅសល់: {pending_total}"
        )

        for chat_id in admin_chat_ids:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:
                LOGGER.warning("Failed to send daily admin notification to %s: %s", chat_id, exc)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.exception("Unhandled exception during update processing", exc_info=context.error)
        if isinstance(update, Update):
            await self._send_message(update, "ប្រព័ន្ធកំពុងមានបញ្ហាបន្តិច។ សូមសាកល្បងម្ដងទៀត។")

    def _get_user(self, update: Update) -> object | None:
        tg_user = update.effective_user
        if tg_user is None:
            return None
        return self.db.get_user(tg_user.id)

    def _is_admin(self, user: object | None) -> bool:
        return bool(user and user["role"] == "admin")

    def _menu_keyboard(self, role: str) -> ReplyKeyboardMarkup:
        if role == "admin":
            keyboard = [
                [KeyboardButton(ADMIN_MENU["summary"]), KeyboardButton(ADMIN_MENU["pending"])],
                [KeyboardButton(ADMIN_MENU["reporters"]), KeyboardButton(ADMIN_MENU["admins"])],
                [KeyboardButton(ADMIN_MENU["schedules"]), KeyboardButton(ADMIN_MENU["classes"])],
                [KeyboardButton(ADMIN_MENU["me"]), KeyboardButton(ADMIN_MENU["status"])],
                [KeyboardButton(ADMIN_MENU["help"])],
            ]
        else:
            keyboard = [
                [KeyboardButton(REPORTER_MENU["report"]), KeyboardButton(REPORTER_MENU["today"])],
                [KeyboardButton(REPORTER_MENU["schedules"]), KeyboardButton(REPORTER_MENU["classes"])],
                [KeyboardButton(REPORTER_MENU["me"]), KeyboardButton(REPORTER_MENU["status"])],
                [KeyboardButton(REPORTER_MENU["help"])],
            ]

        return ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="សូមជ្រើសមុខងារ...",
        )

    async def _send_message(
        self,
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    ) -> None:
        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup)
            return
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
            return
        if update.effective_chat:
            await update.effective_chat.send_message(text, reply_markup=reply_markup)

    async def _send_long_message(self, update: Update, text: str, limit: int = 3800) -> None:
        chunks = self._chunk_lines(text, limit=limit)
        for chunk in chunks:
            await self._send_message(update, chunk)

    def _chunk_lines(self, text: str, limit: int = 3800) -> list[str]:
        lines = text.splitlines() or [text]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1
            if current_len + line_len > limit and current:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _role_welcome_text(self, role: str, full_name: str, class_name: str | None) -> str:
        if role == "admin":
            return (
                f"សូមស្វាគមន៍ {full_name} (អ្នកគ្រប់គ្រង)\n"
                "អាចប្រើ /menu ដើម្បីចុចប៊ូតុងងាយស្រួល។\n"
                f"ពាក្យបញ្ជា: {self._command_list_for_role('admin')}"
            )

        return (
            f"សួស្តី {full_name}! ការចុះឈ្មោះជាអ្នករាយការណ៍បានជោគជ័យ។\n"
            f"ថ្នាក់: {class_name}\n"
            "អាចប្រើ /menu ដើម្បីចុចប៊ូតុងងាយស្រួល។\n"
            f"ពាក្យបញ្ជា: {self._command_list_for_role('reporter')}"
        )

    def _command_list_for_role(self, role: str) -> str:
        if role == "admin":
            return "/menu /help /summary(/sum) /pending(/pen) /reporters /admins /me /classes(/cls) /schedules(/sch) /status /cancel"
        return "/menu /help /report(/r) /today(/d) /me /classes(/cls) /schedules(/sch) /status /cancel"

    def _format_schedules(self, rows: Iterable[object], include_class: bool) -> str:
        lines: list[str] = []
        for row in rows:
            prefix = f"{row['class_name']} | " if include_class else ""
            lines.append(
                f"{prefix}{WEEKDAYS[row['weekday']]} | {row['period_name']} | {row['time_range']} | "
                f"{row['subject_name']} | {row['teacher_name']}"
            )
        return "\n".join(lines)













