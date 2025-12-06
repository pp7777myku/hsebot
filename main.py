import os
import csv
import io
import re
import logging

import chardet
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.error import TimedOut

# ---------------- 日志配置 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------- 环境变量 ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    logger.error("Missing Gemini API Key. Please set GEMINI_API_KEY environment variable.")
    raise Exception("Missing GEMINI_API_KEY environment variable.")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    logger.error("Missing TELEGRAM_BOT_TOKEN environment variable.")
    raise Exception("Missing TELEGRAM_BOT_TOKEN environment variable.")

# Cloud Run 部署后，在环境变量中设置服务的基础 URL，例如：
# https://your-service-name-xxxxx-uc.a.run.app
# 本地开发如果没设置，就用 http://localhost:8080 作为占位，避免直接崩溃
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    WEBHOOK_URL = "http://localhost:8080"
    logger.warning(
        "WEBHOOK_URL not set, using default http://localhost:8080 (local dev mode). "
        "In Cloud Run you MUST set WEBHOOK_URL to the real service URL."
    )

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "You are an AI legal assistant for IT procurement contracts inside a company. "
        "You help internal business users choose an appropriate contract type "
        "(IT equipment supply, software license, technical support services, mixed contract, or other), "
        "and you can generate an initial Russian-language draft contract based on the contract subject "
        "and a short description from the user. "
        "The contract should be written in clear legal Russian in plain text (no markdown), "
        "have a typical structure (title, parties, subject, rights and obligations, term, payments, "
        "liability, termination, other terms, details of the parties), and be suitable as a draft "
        "for further review by in-house lawyers."
    ),
)

# 配置 Gemini（仍用于简短评论等功能）
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Failed to configure Gemini API: {e}")
    raise

# ---------------- 知识库读取函数（保留） ----------------
CASES_CSV_PATH = r"D:\PhD\paper\paper_indai2025\code_indai2025\cases.csv"


def load_and_serialize_cases() -> str:
    """
    原来的工业诊断知识库读取函数，按你的要求保留。
    当前 bot 不使用结果，只是预留。
    """
    cases = []
    logger.info(f"Attempting to load knowledge base from local file: {CASES_CSV_PATH}")

    try:
        if not os.path.exists(CASES_CSV_PATH):
            logger.error(f"Local CSV file not found: {CASES_CSV_PATH}")
            return "KB_LOAD_FAILED"

        with open(CASES_CSV_PATH, 'rb') as f:
            raw = f.read()
            detected = chardet.detect(raw)
            encoding = detected['encoding'] or 'utf-8'
        logger.info(f"Detected encoding: {encoding}")

        csvfile = io.StringIO(raw.decode(encoding))
        reader = csv.DictReader(csvfile)

        if not reader.fieldnames:
            logger.warning("CSV DictReader could not detect any field names. CSV might be empty or malformed.")
            return "KB_PROCESS_FAILED"

        for i, row in enumerate(reader):
            cases.append(str(row))

        logger.info(f"Loaded {len(cases)} rows from CSV (for potential future use).")
        return "\n".join(cases)
    except Exception as e:
        logger.error(f"Error reading local CSV: {e}", exc_info=True)
        return "KB_PROCESS_FAILED"


# ---------------- Email 校验 ----------------
def is_valid_email(email: str) -> bool:
    """
    简单邮箱格式校验：不能包含空格，必须有 @ 和 点号。
    """
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return re.match(pattern, email) is not None


# ---------------- Gemini 调用 ----------------
ERROR_MSG_GEMINI_FAILED = "Ошибка: Не удалось автоматически сформировать проект договора. Попробуйте позже."
ERROR_MSG_AI_ANSWER_FAILED = (
    "К сожалению, не удалось получить автоматический ответ от AI. "
    "Попробуйте позже или обратитесь к юристам напрямую."
)


def _build_typical_contract_template(contract_type: str, subject_clause: str) -> str:
    """
    固定的 типовая форма договора 模板。
    contract_type  用于标题里的“ДОГОВОР ...”,
    subject_clause 用于 раздел «Предмет договора».
    """
    return f"""
ДОГОВОР {contract_type}
№ ______

г. __________________      «___» __________ 20___ г.

__________________________, именуемое в дальнейшем «Заказчик», в лице ___________________________,
действующего на основании ___________________, с одной стороны, и

__________________________, именуемое в дальнейшем «Исполнитель», в лице ________________________,
действующего на основании ___________________, с другой стороны,

вместе именуемые «Стороны», заключили настоящий Договор о нижеследующем.

1. ПРЕДМЕТ ДОГОВОРА

1.1. {subject_clause}

1.2. Подробное описание предмета Договора, спецификация оборудования / программного обеспечения /
услуг, а также иные технические параметры указываются в Приложении № 1 к Договору,
являющемся его неотъемлемой частью.

2. ПРАВА И ОБЯЗАННОСТИ СТОРОН

2.1. Заказчик обязуется:
2.1.1. Предоставить Исполнителю всю необходимую информацию и документы, связанные с исполнением Договора.
2.1.2. Принять результат работ (товар, услуги) в порядке и сроки, установленные Договором.
2.1.3. Своевременно оплатить Исполнителю стоимость по Договору.

2.2. Заказчик вправе:
2.2.1. Требовать от Исполнителя надлежащего исполнения обязательств и устранения недостатков。
2.2.2. Получать информацию о ходе исполнения Договора.

2.3. Исполнитель обязуется:
2.3.1. Надлежащим образом исполнить обязательства по Договору.
2.3.2. Поставить оборудование / передать права / оказать услуги в объеме и в сроки,
установленные Договором и Приложениями к нему.
2.3.3. Уведомлять Заказчика о обстоятельствах, препятствующих исполнению обязательств.

2.4. Исполнитель вправе:
2.4.1. Получить оплату в порядке и сроки, установленные Договором.
2.4.2. Запрашивать у Заказчика информацию и документы, необходимые для исполнения обязательств.

3. СРОК ДЕЙСТВИЯ ДОГОВОРА

3.1. Договор вступает в силу с момента его подписания Сторонами и действует до «___» __________ 20___ г.
3.2. В части расчетов и ответственности Сторон Договор действует до полного исполнения обязательств.

4. ПОРЯДОК РАСЧЕТОВ

4.1. Общая цена Договора составляет ________ (__________________________) рублей, в том числе НДС (при наличии) ________ рублей.
4.2. Расчеты осуществляются в безналичной форме путем перечисления денежных средств
на расчетный счет Исполнителя на основании счетов и/или актов выполненных работ / накладных.
4.3. Конкретный порядок и сроки оплаты (аванс, поэтапная оплата и пр.) указываются в Приложении № 2.

5. ОТВЕТСТВЕННОСТЬ СТОРОН

5.1. За неисполнение или ненадлежащее исполнение обязательств по Договору Стороны несут ответственность
в соответствии с действующим законодательством Российской Федерации и условиями Договора.
5.2. Неустойка (штраф, пени) за нарушение сроков исполнения обязательств указывается в разделе 5.3. и, при необходимости, в Приложении № 3.
5.3. Стороны освобождаются от ответственности за частичное или полное неисполнение обязательств,
если оно явилось следствием обстоятельств непреодолимой силы (форс-мажор), подтвержденных в установленном порядке.

6. ПОРЯДОК ИЗМЕНЕНИЯ И РАСТОРЖЕНИЯ ДОГОВОРА

6.1. Все изменения и дополнения к настоящему Договору действительны при условии,
что они совершены в письменной форме и подписаны уполномоченными представителями Сторон.
6.2. Договор может быть расторгнут по соглашению Сторон, а также в иных случаях, предусмотренных законодательством РФ и настоящим Договором.
6.3. Сторона, инициирующая расторжение Договора, направляет другой Стороне письменное уведомление не позднее чем за ______ календарных дней.

7. ПРОЧИЕ УСЛОВИЯ

7.1. Во всем остальном, что не урегулировано настоящим Договором, Стороны руководствуются действующим законодательством Российской Федерации.
7.2. Переписка и документы, направленные Сторонами по официальным адресам, считаются надлежащим образом полученными.
7.3. Приложения к Договору являются его неотъемлемой частью:
Приложение № 1 – Спецификация / техническое задание;
Приложение № 2 – Порядок расчетов;
Приложение № 3 – Иные условия (при необходимости).

8. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН

ЗАКАЗЧИК:
Полное наименование: __________________________________________
Юридический адрес: ___________________________________________
Почтовый адрес: _______________________________________________
ИНН/КПП: ______________________________________________________
Р/с: __________________________________________________________
в банке: ______________________________________________________
БИК: _________________________________________________________
Корр./счет: ___________________________________________________
Телефон / e-mail: _____________________________________________
Подпись: ___________________    М.П.

ИСПОЛНИТЕЛЬ:
Полное наименование: __________________________________________
Юридический адрес: ___________________________________________
Почтовый адрес: _______________________________________________
ИНН/КПП: ______________________________________________________
Р/с: __________________________________________________________
в банке: ______________________________________________________
БИК: _________________________________________________________
Корр./счет: ___________________________________________________
Телефон / e-mail: _____________________________________________
Подпись: ___________________    М.П.
"""  # 这里的下划线和空白处就是“填空”的位置


def generate_contract_draft(contract_subject: str, extra_info: str = "") -> str:
    """
    现在不再调用 Gemini，而是根据合同主题返回一份固定的 типовая форма договора，
    文本中有大量下划线，供用户/公司自己填空。
    """
    subject_lower = contract_subject.lower()

    if "поставка" in subject_lower:
        contract_type = "ПОСТАВКИ ИТ-ОБОРУДОВАНИЯ"
        subject_clause = (
            "Исполнитель обязуется поставить Заказчику ИТ-оборудование, а Заказчик обязуется принять "
            "и оплатить оборудование в соответствии с условиями настоящего Договора и Спецификацией "
            "(Приложение № 1)."
        )
    elif "лиценз" in subject_lower:
        contract_type = "ЛИЦЕНЗИОННОГО ДОГОВОРА НА ПРОГРАММНОЕ ОБЕСПЕЧЕНИЕ"
        subject_clause = (
            "Исполнитель (Правообладатель / Лицензиар) предоставляет Заказчику (Лицензиату) "
            "простую (неисключительную) лицензию на использование программного обеспечения "
            "в объеме и на условиях, указанных в настоящем Договоре и Приложении № 1."
        )
    elif "технической поддержке" in subject_lower or "поддержк" in subject_lower:
        contract_type = "ОКАЗАНИЯ УСЛУГ ПО ТЕХНИЧЕСКОЙ ПОДДЕРЖКЕ"
        subject_clause = (
            "Исполнитель обязуется оказывать Заказчику услуги по технической поддержке ИТ-систем "
            "и программного обеспечения Заказчика, а Заказчик обязуется оплачивать такие услуги "
            "в соответствии с условиями настоящего Договора."
        )
    elif "смешанный" in subject_lower or "смешан" in subject_lower:
        contract_type = "СМЕШАННОГО ДОГОВОРА (ПОСТАВКА ИТ-ОБОРУДОВАНИЯ И/ИЛИ ПРЕДОСТАВЛЕНИЕ ПРАВ И/ИЛИ УСЛУГИ)"
        subject_clause = (
            "Исполнитель обязуется осуществить поставку ИТ-оборудования и/или предоставить права "
            "на использование программного обеспечения, и/или оказать услуги по технической поддержке, "
            "а Заказчик обязуется принять результат и оплатить его в соответствии с условиями Договора."
        )
    else:
        contract_type = "В СФЕРЕ ИНФОРМАЦИОННЫХ ТЕХНОЛОГИЙ"
        subject_clause = (
            "Исполнитель обязуется оказать услуги / выполнить работы / передать права "
            "в сфере информационных технологий, а Заказчик обязуется принять и оплатить результат "
            "в соответствии с условиями настоящего Договора."
        )

    # 如果用户额外写了一些说明，可以轻微补充到 предмете
    if extra_info:
        subject_clause += f" Дополнительные существенные условия по предмету: {extra_info}"

    return _build_typical_contract_template(contract_type, subject_clause)


async def generate_short_answer(question: str) -> str:
    """
    使用 Gemini 对“自由问题 / 其他 предмет / 对方合同”给一个简短俄文回答（3–5 句）。
    """
    prompt_text = (
        SYSTEM_PROMPT
        + "\n\n"
        "Сейчас пользователь задаст вопрос или опишет ситуацию по ИТ-закупкам или договорам.\n"
        "Дай, пожалуйста, краткий ориентировочный ответ на РУССКОМ языке (3–5 предложений), "
        "сразу по сути вопроса/описания. НЕ пиши, кто ты такой и чем занимаешься, "
        "НЕ описывай свои функции. В конце одной фразой напомни, что это не юридическое "
        "заключение, а лишь предварительный комментарий AI.\n\n"
        f"Вопрос или описание пользователя: {question}"
    )

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt_text)
        text = getattr(response, "text", "").strip()
        if not text:
            return ERROR_MSG_AI_ANSWER_FAILED
        return text
    except Exception as e:
        logger.error(f"Gemini error in generate_short_answer: {e}")
        return ERROR_MSG_AI_ANSWER_FAILED


# ---------------- 工具：分段发送长文本 ----------------
MAX_TG_MESSAGE_LEN = 4000  # Telegram 限制 4096，这里稍微留点余量


async def send_long_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """
    把很长的文本按 4000 字一段发，避免一次性太大。
    """
    if not text:
        return

    for start in range(0, len(text), MAX_TG_MESSAGE_LEN):
        chunk = text[start:start + MAX_TG_MESSAGE_LEN]
        logger.info(f"DEBUG: sending chunk [{start}:{start+len(chunk)}]")
        await context.bot.send_message(chat_id=chat_id, text=chunk)


# ---------------- Telegram 机器人逻辑 ----------------

(
    CHOOSING_PATH,
    CHOOSING_SUBJECT,
    WAITING_SUBJECT_OTHER,
    WAITING_TEMPLATE_DECISION,
    WAITING_NONSTANDARD_CONTRACT,
    WAITING_FREE_QUESTION,
    WAITING_EMAIL,
) = range(7)

MAIN_OPTION_PROCUREMENT = "Планирую ИТ-закупку"
MAIN_OPTION_QUESTION = "У меня есть свой вопрос"

SUBJECT_41 = "Поставка ИТ-оборудования"
SUBJECT_42 = "Лицензионный договор на ПО"
SUBJECT_43 = "Услуги по технической поддержке"
SUBJECT_44 = "Смешанный договор (оборудование + ПО/услуги)"
SUBJECT_45 = "Другой предмет договора (указать свой)"

TEMPLATE_OK = "Типовая форма подходит"
TEMPLATE_NOT_OK = "Типовая форма не подходит"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    keyboard = [[MAIN_OPTION_PROCUREMENT, MAIN_OPTION_QUESTION]]
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    text = (
        "Здравствуйте! Планируете закупку ИТ-оборудования или лицензий, "
        "но не знаете, как правильно оформить договорные отношения с контрагентом? "
        "Я помогу :) Готовы ответить на несколько вопросов?"
    )
    await update.message.reply_text(text, reply_markup=reply_markup)
    return CHOOSING_PATH


async def choosing_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_choice = (update.message.text or "").strip()
    logger.info(f"DEBUG: choosing_path, user_choice='{user_choice}'")

    if user_choice == MAIN_OPTION_PROCUREMENT:
        text_3 = (
            "Отлично. Давайте определимся с предметом будущего договора.\n"
            "Выберите, пожалуйста, из предложенных вариантов или напишите свой."
        )

        subject_keyboard = [
            [SUBJECT_41],
            [SUBJECT_42],
            [SUBJECT_43],
            [SUBJECT_44],
            [SUBJECT_45],
        ]
        reply_markup = ReplyKeyboardMarkup(
            subject_keyboard,
            resize_keyboard=True,
            one_time_keyboard=True,
        )

        await update.message.reply_text(text_3, reply_markup=reply_markup)
        return CHOOSING_SUBJECT

    else:
        await update.message.reply_text(
            "Напишите, пожалуйста, Ваш вопрос. Я дам краткий комментарий, "
            "а затем ваш запрос будет передан юристам для более детального анализа.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return WAITING_FREE_QUESTION


async def handle_free_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    question = (update.message.text or "").strip()
    logger.info(f"Свободный вопрос пользователя: {question}")

    await update.message.reply_text("Пожалуйста, подождите...")

    ai_answer = await generate_short_answer(question)
    await update.message.reply_text(f"Краткий комментарий AI:\n\n{ai_answer}")

    context.user_data["email_purpose"] = "free_question"

    await update.message.reply_text(
        "Если вы хотите получить более детальное заключение от юристов компании по этому вопросу, "
        "укажите, пожалуйста, вашу корпоративную почту (e-mail).\n\n"
        "Если хотите начать новую консультацию без указания почты, просто отправьте команду /start."
    )
    return WAITING_EMAIL


async def choosing_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = (update.message.text or "").strip()
    context.user_data["subject"] = choice
    logger.info(f"DEBUG: choosing_subject, choice='{choice}'")

    if choice in {SUBJECT_41, SUBJECT_42, SUBJECT_43, SUBJECT_44}:
        return await send_template_offer(update, context)

    await update.message.reply_text(
        "Опишите, пожалуйста, предмет вашего будущего договора более подробно. "
        "Я дам краткий автоматический комментарий, а затем передам запрос юристам.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_SUBJECT_OTHER


async def handle_subject_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    subject_text = (update.message.text or "").strip()
    logger.info(f"Пользовательский предмет договора (другой): {subject_text}")
    context.user_data["subject_other_detail"] = subject_text

    await update.message.reply_text("Пожалуйста, подождите...")

    ai_answer = await generate_short_answer(
        f"Пользователь описывает предмет будущего договора так: {subject_text}"
    )
    await update.message.reply_text(
        f"Краткий комментарий AI по вашему описанию предмета договора:\n\n{ai_answer}"
    )

    context.user_data["email_purpose"] = "subject_other"
    await update.message.reply_text(
        "Чтобы юристы могли подготовить для вас более детальное заключение или предложить форму договора, "
        "укажите, пожалуйста, вашу корпоративную почту (e-mail).\n\n"
        "Если вы передумали и хотите начать новую консультацию, просто отправьте команду /start."
    )
    return WAITING_EMAIL


async def send_template_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text_6 = (
        "Поздравляем! Для выбранного предмета договора в нашей компании разработана и утверждена "
        "типовая форма договора.\n\n"
        "Это означает, что если контрагент готов подписать договор без внесения изменений в его текст, "
        "такой договор может быть заключён без дополнительного согласования с юристами компании.\n\n"
        "Ниже представлена утверждённая типовая форма договора с полями для заполнения. "
        "Пожалуйста, ознакомьтесь."
    )

    keyboard = [[TEMPLATE_OK, TEMPLATE_NOT_OK]]
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(text_6, reply_markup=reply_markup)
    return WAITING_TEMPLATE_DECISION


async def handle_template_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    decision = (update.message.text or "").strip()
    logger.info(f"DEBUG: handle_template_decision, decision='{decision}'")

    if decision == TEMPLATE_OK:
        subject = context.user_data.get("subject", "ИТ-договор")

        await update.message.reply_text(
            "Готовлю типовую форму договора. Пожалуйста, подождите...",
            reply_markup=ReplyKeyboardRemove(),
        )

        # 这里已经是固定模板，不再调用 Gemini，只是根据 subject 选择对应的 типовая форма
        draft_text = generate_contract_draft(subject)

        full_text = (
            "Вот типовая форма договора с полями для заполнения:\n\n"
            f"{draft_text}\n\n"
            "Вы можете скопировать этот текст, заполнить недостающие данные "
            "и передать проект юристам компании для согласования."
        )

        logger.info("DEBUG: sending fixed template contract to user (may be split into chunks)")
        await send_long_text(context, update.effective_chat.id, full_text)

        context.user_data["email_purpose"] = "template_ok"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "Если вы хотите получить комментарии юристов по этому проекту и, при необходимости, "
                "согласованную финальную версию договора, укажите, пожалуйста, вашу корпоративную почту (e-mail).\n\n"
                "Если почта не нужна, вы в любой момент можете начать новую консультацию командой /start."
            ),
        )
        return WAITING_EMAIL

    elif decision == TEMPLATE_NOT_OK:
        text_72 = (
            "К сожалению, предложенная типовая форма не подходит для этой сделки.\n"
            "Контрагент предлагает свою форму договора."
        )
        await update.message.reply_text(text_72, reply_markup=ReplyKeyboardRemove())

        text_8_prompt = (
            "Пожалуйста, приложите форму договора контрагента (файлом или текстом).\n"
            "Я дам краткий общий комментарий, а затем передам материалы юристам."
        )
        await update.message.reply_text(text_8_prompt)
        return WAITING_NONSTANDARD_CONTRACT

    else:
        await update.message.reply_text(
            f"Пожалуйста, выберите один из вариантов: «{TEMPLATE_OK}» или «{TEMPLATE_NOT_OK}»."
        )
        return WAITING_TEMPLATE_DECISION


async def handle_nonstandard_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    text_part = (msg.caption or msg.text or "").strip()
    subject = context.user_data.get("subject", "ИТ-договор")

    logger.info("Получена нетиповая форма договора (текст или файл).")
    logger.info(f"DEBUG nonstandard text: '{text_part[:200]}'...")

    description = (
        f"Контрагент предлагает свою форму договора по предмету: {subject}. "
        f"Пользователь также передал следующее описание/фрагмент: {text_part}"
    )

    await update.message.reply_text("Пожалуйста, подождите...")

    ai_answer = await generate_short_answer(description)
    await update.message.reply_text(
        f"Краткий общий комментарий AI по форме договора контрагента:\n\n{ai_answer}"
    )

    await update.message.reply_text(
        "Чтобы юристы компании могли изучить договор и направить вам своё заключение, "
        "укажите, пожалуйста, вашу корпоративную почту (e-mail).\n\n"
        "Если вы не хотите оставлять почту, можете в любой момент начать новую консультацию командой /start."
    )

    context.user_data["email_purpose"] = "nonstandard"
    return WAITING_EMAIL


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = (update.message.text or "").strip()
    logger.info(f"DEBUG: handle_email, got='{email}'")

    if not is_valid_email(email):
        await update.message.reply_text(
            "Пожалуйста, укажите вашу корпоративную почту в формате name@company.ru.\n"
            "Если вы передумали оставлять почту, можете в любой момент начать новую консультацию командой /start."
        )
        return WAITING_EMAIL

    context.user_data["user_email"] = email
    purpose = context.user_data.get("email_purpose", "generic")

    if purpose == "free_question":
        msg = (
            "Спасибо! Ваш вопрос и краткий комментарий AI будут переданы юристам компании.\n"
            f"Более детальное заключение вы получите на корпоративную почту {email} "
            "в течение 3-х дней.\n\n"
            "Если Вам потребуется новая консультация, отправьте команду /start."
        )
    elif purpose == "subject_other":
        msg = (
            "Спасибо! Ваш запрос по предмету договора и комментарий AI будут направлены юристам компании.\n"
            f"Ответ вы получите на корпоративную почту {email} в течение 3-х дней.\n\n"
            "Если Вам потребуется новая консультация, отправьте команду /start."
        )
    elif purpose == "template_ok":
        msg = (
            "Спасибо! Черновик договора, который вы получили, будет передан юристам вместе с вашим адресом.\n"
            f"При необходимости вы получите согласованную версию или комментарии на почту {email}.\n\n"
            "Если Вам потребуется новая консультация, отправьте команду /start."
        )
    elif purpose == "nonstandard":
        msg = (
            "Спасибо! Форма договора контрагента и краткий комментарий AI будут переданы юристам компании.\n"
            f"Их заключение вы получите на корпоративную почту {email} в течение 5-ти дней.\n\n"
            "Если Вам потребуется новая консультация, отправьте команду /start."
        )
    else:
        msg = (
            f"Спасибо! Мы сохранили ваш адрес {email}.\n\n"
            "Если Вам потребуется новая консультация, отправьте команду /start."
        )

    await update.message.reply_text(msg)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Диалог прерван. Чтобы начать заново, используйте команду /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, TimedOut):
        logger.error("DEBUG: Telegram request timed out (likely proxy/network issue).")


# ---------------- Cloud Run / Webhook 入口 ----------------
def main() -> None:
    """
    Cloud Run 使用的入口函数。
    通过 webhook 模式接收 Telegram 更新，而不是本地长轮询。
    """
    # Cloud Run 会通过 PORT 环境变量告诉要监听的端口
    port = int(os.environ.get("PORT", "8080"))

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CHOOSING_PATH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, choosing_path)
            ],
            CHOOSING_SUBJECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, choosing_subject)
            ],
            WAITING_SUBJECT_OTHER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_subject_other)
            ],
            WAITING_TEMPLATE_DECISION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_template_decision)
            ],
            WAITING_NONSTANDARD_CONTRACT: [
                MessageHandler(
                    (filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
                    handle_nonstandard_contract,
                )
            ],
            WAITING_FREE_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_question)
            ],
            WAITING_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("cancel", cmd_cancel),
        ],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Webhook URL：Cloud Run 服务的基础地址 + /<bot token>
    webhook_path = f"/{TELEGRAM_BOT_TOKEN}"
    webhook_url_full = WEBHOOK_URL.rstrip("/") + webhook_path

    logger.info(f"Starting webhook on 0.0.0.0:{port}, webhook_url={webhook_url_full}")

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=webhook_url_full,
    )


if __name__ == "__main__":
    main()
