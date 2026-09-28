#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dzen-publish — автоматическая публикация статьи на Яндекс Дзен через CDP.

Использование:
    python publish_dzen.py article.txt
    python publish_dzen.py article.txt --media image.jpg image2.png
    python publish_dzen.py article.txt --schedule 2026-09-24T14:00
    python publish_dzen.py                    # использует TITLE/BODY из этого файла напрямую

Требования:
    pip install playwright
    playwright install chromium
    Chrome запущен с: --remote-debugging-port=9222 --user-data-dir=<path> --remote-allow-origins=*

Настройки:
    CHANNEL_URL — полный адрес редактора вашего канала Дзен
    CDP_URL     — адрес Chrome DevTools Protocol
"""
import argparse, asyncio, json, subprocess, tempfile, os, re, sys
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────
CHANNEL_URL = "https://dzen.ru/profile/editor/ekonomikadliavseh"
CDP_URL    = "http://127.0.0.1:9222"
CDP_CONNECT_TIMEOUT_SECONDS = 15
CDP_CONNECT_RETRIES = 2

# ─── КОНТЕНТ (можно заменить чтением из файла) ────────────────────────────────
TITLE = "Кальянный лаунж в центре Петербурга: почему хороший вечер — это не дорого и не случайно"

BODY = """Есть такая петербургская привычка — долго сидеть. Не потому что нечем заняться, а потому что когда место правильное, уходить просто не хочется. Хороший разговор, тарелка, которая радует глаз ещё до первой вилки, кальян с вкусом, который угадан точно под настроение. Вот это — вечер.

Только вот попасть в такое место с первого раза в Петербурге — задача со звёздочкой. Город большой, заведений много, а мест, где всё это совпадает одновременно, — на самом деле не так много, как кажется."""


# ─── УТИЛИТЫ ─────────────────────────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def load_article(path):
    """Читает статью из файла: первая строка = заголовок, остальное = тело."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    title = lines[0].strip()
    body  = "\n".join(lines[2:]).strip()   # пустая строка-разделитель после заголовка
    return title, body


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def split_markdown_links(body):
    """Возвращает текст для редактора и ссылки, которые нужно сделать активными."""
    links = [(match.group(1), match.group(2)) for match in MARKDOWN_LINK_RE.finditer(body)]
    return MARKDOWN_LINK_RE.sub(r"\1", body), links


def parse_args():
    parser = argparse.ArgumentParser(description="Публикация статьи в Дзен через авторизованный Chrome CDP.")
    parser.add_argument("article", nargs="?", help="текстовый файл: первая строка — заголовок, остальное — тело")
    parser.add_argument("--media", nargs="+", default=[], help="локальные изображения для вставки в статью")
    parser.add_argument(
        "--sequence",
        help=(
            "JSON с упорядоченными блоками DOCX: текст и изображения вставляются "
            "на исходных позициях, а не одним списком"
        ),
    )
    parser.add_argument(
        "--reuse-first-image",
        action="store_true",
        help="восстановление прерванной ревизии: использовать единственный уже проверенный первый image-блок",
    )
    parser.add_argument(
        "--paste-docx",
        help="DOCX, уже скопированный tools/copy_docx_body_to_clipboard.ps1 в rich clipboard",
    )
    parser.add_argument(
        "--expected-pasted-images", type=int, default=0,
        help="ожидаемое число изображений при --paste-docx; проверяется до отправки",
    )
    parser.add_argument(
        "--footer-file",
        help="файл обязательного финального блока; для --paste-docx добавляется после вставки DOCX",
    )
    parser.add_argument("--schedule", help="отложенная публикация в формате YYYY-MM-DDTHH:MM (Europe/Moscow)")
    parser.add_argument(
        "--draft-url",
        help="продолжить существующий черновик после сверки; новый черновик не создаётся",
    )
    parser.add_argument(
        "--finalize-draft",
        action="store_true",
        help="только назначить время и отправить уже проверенный черновик; требует --draft-url и --schedule",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="только проверить подключение к авторизованному Chrome CDP; редактор не открывается",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="заполнить и проверить черновик без публикации или назначения времени",
    )
    return parser.parse_args()


def validate_media(paths):
    allowed = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
    result = []
    for raw_path in paths:
        path = os.path.abspath(raw_path)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Медиафайл не найден: {raw_path}")
        if os.path.splitext(path)[1].lower() not in allowed:
            raise ValueError(f"Неподдерживаемый формат изображения: {raw_path}")
        result.append(path)
    return result


def load_sequence(path):
    """Читает подготовленную из DOCX последовательность текстовых и image-блоков."""
    with open(path, encoding="utf-8") as source:
        payload = json.load(source)
    title = str(payload.get("title", "")).strip()
    raw_blocks = payload.get("blocks")
    if not title or not isinstance(raw_blocks, list) or not raw_blocks:
        raise ValueError("Файл --sequence должен содержать непустые title и blocks.")

    blocks = []
    for index, raw_block in enumerate(raw_blocks, start=1):
        if not isinstance(raw_block, dict):
            raise ValueError(f"Блок {index} в --sequence должен быть объектом.")
        block_type = raw_block.get("type")
        if block_type == "text":
            text = str(raw_block.get("text", "")).strip()
            if text:
                blocks.append({"type": "text", "text": text})
        elif block_type == "image":
            image_path = raw_block.get("path")
            if not isinstance(image_path, str):
                raise ValueError(f"У image-блока {index} отсутствует path.")
            blocks.append({"type": "image", "path": validate_media([image_path])[0]})
        else:
            raise ValueError(f"Неизвестный тип блока {index}: {block_type!r}.")
    if not blocks:
        raise ValueError("В --sequence не осталось содержательных блоков.")
    return title, blocks


def public_footer(path):
    """Возвращает только публичный блок, исключая служебную преамбулу reference-файла."""
    content = open(path, encoding="utf-8").read()
    marker = content.find("📌")
    footer = (content[marker:] if marker >= 0 else content).strip()
    if not footer:
        raise ValueError("Файл финального блока пуст.")
    return footer


def parse_schedule(value):
    """Проверяет локальное время отложенной публикации."""
    if not value:
        return None
    try:
        scheduled_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Время отложки: YYYY-MM-DDTHH:MM, например 2026-09-24T14:00.") from exc
    scheduled_at = scheduled_at.replace(second=0, microsecond=0)
    now = datetime.now(scheduled_at.tzinfo) if scheduled_at.tzinfo else datetime.now()
    if (scheduled_at - now).total_seconds() < 60:
        raise ValueError("До времени отложенной публикации должно оставаться не меньше минуты.")
    return scheduled_at


def set_clipboard(text):
    """Копирует текст в буфер обмена через PowerShell (Windows)."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", delete=False)
    tmp.write(text)
    tmp.close()
    ps = f'Get-Content -Path "{tmp.name}" -Encoding UTF8 -Raw | Set-Clipboard'
    r  = subprocess.run(["powershell", "-NonInteractive", "-Command", ps], capture_output=True)
    os.unlink(tmp.name)
    ok = r.returncode == 0
    log(f"  clipboard: {'OK' if ok else 'FAIL'} ({len(text)} chars)")
    return ok


async def wait_for_ces(page, min_count=2, timeout_ms=15000):
    """Ждёт появления ≥ min_count contenteditable элементов."""
    for _ in range(timeout_ms // 500):
        n = await page.evaluate("document.querySelectorAll('[contenteditable]').length")
        if n >= min_count:
            return n
        await page.wait_for_timeout(500)
    return await page.evaluate("document.querySelectorAll('[contenteditable]').length")


async def open_channel(page, channel_url=None):
    """Shared read-only navigation; no editor clicks or draft creation."""
    target = channel_url or CHANNEL_URL
    if page.url.rstrip('/') == target.rstrip('/'):
        return
    # Переходим на dzen.ru если нужно
    if not page.url.startswith("https://dzen.ru"):
        log("  goto dzen.ru...")
        await page.goto("https://dzen.ru", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)

    # Навигируемся на страницу канала (SPA)
    async with page.expect_navigation(wait_until='domcontentloaded', timeout=25000):
        await page.evaluate("url => { window.location.href = url; }", target)
    log(f"  channel page loaded, url={page.url[-50:]}")


async def open_new_article(page):
    """Open the channel and create a draft using the upstream editor controls."""
    await open_channel(page)

    # Кликаем кнопку «+» в хедере → появляется меню «Написать статью».
    # Селектор устойчивее к масштабу, а координаты оставлены для старого редактора.
    add_button = page.locator('[data-testid="add-publication-button"]:visible')
    if await add_button.count():
        await add_button.first.click()
        log("  clicked add-publication button")
    else:
        await page.mouse.click(883, 32)   # legacy editor, CSS px
        log("  clicked legacy add-publication position")
    await page.wait_for_timeout(2000)

    try:
        btn = await page.wait_for_selector("text=Написать статью", timeout=5000)
        await btn.click()
        log("  clicked 'Написать статью'")
    except:
        log("  'Написать статью' not found via text, menu may have not appeared")

    await page.wait_for_timeout(3000)
    log(f"  editor url: {page.url}")
    return page.url


async def handle_channel_modal(page):
    """
    Обрабатывает диалог «Настройка канала»: ставит галку и нажимает «Продолжить».
    Возвращает True если диалог был и обработан.
    """
    # В публикационных настройках есть видимый чекбокс «Опубликовать позже».
    # Он не относится к настройке канала и не должен включаться автоматически.
    continue_buttons = page.locator("button:visible").filter(has_text="Продолжить")
    if await continue_buttons.count() == 0:
        return False

    log("  channel modal found")

    checkboxes = page.locator("input[type='checkbox']:visible")
    checkbox = checkboxes.first
    if not await checkbox.is_checked():
        await checkbox.check(force=True)
        await page.wait_for_timeout(200)
        log("  checkbox accepted")

    # Нажимаем видимую кнопку «Продолжить» в диалоге.
    await continue_buttons.first.click()
    await page.wait_for_timeout(2000)
    log(f"  clicked Продолжить, url={page.url[-40:]}")
    return True


async def clear_article_body(page, body_editor, keep_first_image=False):
    """Очищает прежний текст и atomic-блоки перед заменой ревизии.

    ``fill('')`` очищает текстовый Draft-блок, но старые изображения в
    некоторых версиях редактора остаются отдельными atomic-блоками. Повторный
    Ctrl+A/Backspace нужен именно для редактирования уже запланированной статьи:
    иначе новая последовательность получила бы дубликаты медиа.
    """
    log("  clearing previous article body...")
    await body_editor.fill("")
    await page.wait_for_timeout(500)
    if keep_first_image:
        remaining = await page.locator('[data-testid="article-image-item"] img').count()
        if remaining != 1:
            raise RuntimeError(
                "--reuse-first-image требует ровно один уже существующий image-блок в черновике."
            )
        log("  preserving the existing first source image")
        return
    if await page.locator('[data-testid="article-image-item"] img').count():
        await body_editor.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(800)
    # An already saved image can survive Draft's bulk text deletion. The
    # editor exposes a native remove control inside each image block; use it
    # rather than leaving a stale block or manipulating the DOM directly.
    uploaded = page.locator('[data-testid="article-image-item"] img')
    while await uploaded.count():
        before = await uploaded.count()
        log(f"  removing stale image block ({before} remaining)...")
        image_block = uploaded.first.locator('xpath=ancestor::figure[@data-editor="ZenDraftEditor"][1]')
        image_box = await image_block.bounding_box()
        if image_box:
            # Draft selects an atomic figure when it receives the real focus,
            # then handles Backspace as a block removal. This is the most
            # reliable path for an image whose hover control is not clickable
            # after a scheduled draft reload.
            await page.mouse.click(
                image_box["x"] + image_box["width"] / 2,
                image_box["y"] + min(image_box["height"] / 2, 80),
            )
            await page.keyboard.press("Delete")
            for _ in range(5):
                await page.wait_for_timeout(250)
                if await uploaded.count() < before:
                    break
            if await uploaded.count() < before:
                continue
        remove = page.locator('div[class*="removeButton"]')
        if await remove.count() == 0:
            raise RuntimeError("В редакторе не найдена штатная кнопка удаления старого изображения.")
        await remove.first.scroll_into_view_if_needed(timeout=3000)
        box = await remove.first.bounding_box()
        if not box:
            raise RuntimeError("Не удалось получить позицию штатной кнопки удаления изображения.")
        # A real pointer click is needed here: the React image control ignores
        # a synthetic HTMLElement.click() when it is opacity-hidden.
        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(10):
            await page.wait_for_timeout(300)
            if await uploaded.count() < before:
                break
        else:
            raise RuntimeError("Редактор не удалил старый image-блок после штатного клика.")
    log("  previous article body cleared")
    remaining = await uploaded.count()
    if remaining:
        raise RuntimeError(
            f"Редактор оставил {remaining} старых изображений; остановлено, чтобы не создать дубликаты."
        )


async def focus_body_end(page, body_editor):
    """Ставит каретку в конец статьи штатными событиями клавиатуры."""
    await body_editor.click()
    await page.keyboard.press("Control+End")


async def attach_one_media(page, image_path, body_editor, expected_count):
    """Вставляет один image-блок в текущую позицию каретки и ждёт его превью."""
    insert_button = page.locator('button[data-tip="Вставить изображение"]')
    if await insert_button.count() == 0:
        raise RuntimeError("В редакторе не найдена кнопка «Вставить изображение».")
    await insert_button.first.evaluate("button => button.click()")
    file_input = page.locator('input[type="file"][accept*="image"]')
    if await file_input.count() == 0:
        raise RuntimeError("В редакторе не найдено поле загрузки изображения.")
    await file_input.first.set_input_files(image_path)

    uploaded = page.locator('[data-testid="article-image-item"] img')
    for _ in range(30):
        if await uploaded.count() >= expected_count:
            sizes = await uploaded.evaluate_all("els => els.map(e => [e.naturalWidth, e.naturalHeight])")
            if all(width > 0 and height > 0 for width, height in sizes):
                log(f"  image placed at source position: {os.path.basename(image_path)}")
                return
        await page.wait_for_timeout(500)
    raise RuntimeError(f"Дзен не подтвердил загрузку изображения: {image_path}")


async def fill_article_sequence(page, title, blocks, reuse_first_image=False):
    """Заменяет статью последовательностью из DOCX, сохраняя якоря изображений.

    Текст добавляется только до следующего изображения, затем изображение
    вставляется при текущей каретке. Поэтому медиа остаётся после собственного
    контекста, а не собирается в начале или в конце статьи.
    """
    n = await wait_for_ces(page, min_count=2, timeout_ms=15000)
    log(f"  CE count: {n}")
    body_editor = page.locator("[contenteditable]").nth(1)
    await clear_article_body(page, body_editor, keep_first_image=reuse_first_image)

    links = []
    image_count = 0
    started = False
    before_preserved_image = reuse_first_image
    text_length = 0
    for block in blocks:
        if block["type"] == "text":
            plain_text, block_links = split_markdown_links(block["text"])
            links.extend(block_links)
            text_length += len(plain_text)
            if before_preserved_image:
                if not started:
                    await body_editor.click()
                    await page.keyboard.press("Control+Home")
                    await page.keyboard.insert_text(plain_text)
                    started = True
                else:
                    await page.keyboard.press("Enter")
                    await page.keyboard.insert_text(plain_text)
                continue
            if not started:
                await body_editor.fill(plain_text)
                started = True
            else:
                await focus_body_end(page, body_editor)
                await page.keyboard.press("Enter")
                await page.keyboard.insert_text(plain_text)
        else:
            if before_preserved_image:
                # The interrupted run left the first already-uploaded source
                # image as an atomic block. Text above was typed before it;
                # now continue immediately after that same image.
                image_count = 1
                before_preserved_image = False
                started = True
                continue
            if not started:
                # The Dzen editor requires a text Draft block before an atomic
                # media block. A blank block is removed once the next text is
                # appended but preserves an image that starts a source document.
                await body_editor.fill(" ")
                started = True
            await focus_body_end(page, body_editor)
            image_count += 1
            await attach_one_media(page, block["path"], body_editor, image_count)

    if links:
        await apply_markdown_links(page, body_editor, links)

    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)
    title_editor = page.locator("[contenteditable]").first
    await title_editor.fill(title)
    title_check = len(await title_editor.inner_text())
    body_check = len(await body_editor.inner_text())
    uploaded = page.locator('[data-testid="article-image-item"] img')
    if await uploaded.count() != image_count:
        raise RuntimeError(
            f"Нарушена последовательность: в документе {image_count} изображений, "
            f"редактор показывает {await uploaded.count()}."
        )
    log(f"  ordered sequence verified: text={body_check}, images={image_count}, links={len(links)}")
    return title_check, body_check


async def fill_article_from_rich_clipboard(page, title, footer, expected_images):
    """Pastes one DOCX body from Word's rich clipboard into Dzen.

    Word provides a single RTF/HTML clipboard payload where drawings retain
    their positions between paragraphs. Dzen's native paste handler imports
    the text and image blocks together, avoiding a later bulk media append.
    """
    n = await wait_for_ces(page, min_count=2, timeout_ms=15000)
    log(f"  CE count: {n}")
    body_editor = page.locator("[contenteditable]").nth(1)
    await body_editor.fill("")
    await body_editor.click()
    await page.keyboard.press("Control+V")

    uploaded = page.locator('[data-testid="article-image-item"] img')
    for _ in range(40):
        count = await uploaded.count()
        if count >= expected_images:
            sizes = await uploaded.evaluate_all("els => els.map(e => [e.naturalWidth, e.naturalHeight])")
            if all(width > 0 and height > 0 for width, height in sizes):
                break
        await page.wait_for_timeout(500)
    else:
        raise RuntimeError(
            f"Dzen не импортировал {expected_images} изображений из единой вставки DOCX; "
            f"получено {await uploaded.count()}."
        )

    plain_footer, footer_links = split_markdown_links(footer)
    await focus_body_end(page, body_editor)
    await page.keyboard.press("Enter")
    await page.keyboard.insert_text(plain_footer)
    await apply_markdown_links(page, body_editor, footer_links)

    await page.evaluate("window.scrollTo(0, 0)")
    title_editor = page.locator("[contenteditable]").first
    await title_editor.fill(title)
    title_check = len(await title_editor.inner_text())
    body_check = len(await body_editor.inner_text())
    if await uploaded.count() != expected_images:
        raise RuntimeError(
            f"После добавления footer редактор показывает {await uploaded.count()} из "
            f"{expected_images} изображений DOCX."
        )
    log(f"  rich DOCX paste verified: text={body_check}, images={expected_images}, links={len(footer_links)}")
    return title_check, body_check


async def fill_article(page, title, body, media_paths=None, sequence_blocks=None, reuse_first_image=False):
    """
    Заполняет редактор: сначала тело (через clipboard), потом заголовок.
    Возвращает (title_len, body_len).
    """
    if sequence_blocks is not None:
        return await fill_article_sequence(page, title, sequence_blocks, reuse_first_image)

    # Ждём 2 CE (title + body)
    n = await wait_for_ces(page, min_count=2, timeout_ms=15000)
    log(f"  CE count: {n}")

    # Позиции CE
    ces = await page.evaluate("""
    (function() {
        return Array.from(document.querySelectorAll('[contenteditable]')).map((e, i) => {
            var r = e.getBoundingClientRect();
            return {i:i, cx:Math.round(r.left+r.width/2), cy:Math.round(r.top+r.height/2), top:Math.round(r.top)};
        });
    })()
    """)
    log(f"  CE positions: {ces}")

    # ── 1. Заполняем ТЕЛО первым (пока оба CE видны) ──
    # fill() надёжнее буфера обмена: в отдельных профилях Chrome вставка могла
    # успеть передать редактору только первый символ.
    body_editor = page.locator("[contenteditable]").nth(1)
    plain_body, markdown_links = split_markdown_links(body)
    await body_editor.fill(plain_body)
    await page.wait_for_timeout(1000)
    body_check = len(await body_editor.inner_text())
    log(f"  body pasted: {body_check} chars")
    if markdown_links:
        await apply_markdown_links(page, body_editor, markdown_links)

    # ── 2. Прокрутка к верху + заполняем ЗАГОЛОВОК ──
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(800)

    # Ждём title CE в видимой области
    for _ in range(8):
        t = await page.evaluate("""
        (function() {
            var c = document.querySelectorAll('[contenteditable]')[0];
            if (!c) return null;
            var r = c.getBoundingClientRect();
            return {cx:Math.round(r.left+r.width/2), cy:Math.round(r.top+r.height/2), top:Math.round(r.top)};
        })()
        """)
        if t and t["top"] > 0:
            break
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(400)

    title_editor = page.locator("[contenteditable]").first
    await title_editor.fill(title)
    await page.wait_for_timeout(800)
    title_check = len(await title_editor.inner_text())
    log(f"  title set: {title_check} chars")
    if media_paths:
        await attach_media(page, media_paths, body_editor)
    return title_check, body_check


async def apply_markdown_links(page, body_editor, links):
    """Создаёт ссылки штатной панелью Draft-редактора Дзена.

    ``document.execCommand`` меняет DOM, но не состояние Draft.js, поэтому
    ссылка исчезает при автосохранении. Здесь выделение передаётся редактору
    через событие ``select``, затем URL вводится в его собственное поле.
    """
    select_text = """
    (root, text) => {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const start = node.data.indexOf(text);
            if (start < 0) continue;
            const range = document.createRange();
            range.setStart(node, start);
            range.setEnd(node, start + text.length);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            root.focus();
            root.dispatchEvent(new Event('select', {bubbles: true}));
            return true;
        }
        return false;
    }
    """
    link_tool = page.locator('div[class*="editor-toolbar__linkTools"]:visible')
    # The toolbar marker class is on the input wrapper; the native input has
    # the editor-provided placeholder and a generic input-control class.
    link_input = page.locator('input[placeholder="Введите ссылку"]:visible')

    for text, url in links:
        selected = await body_editor.evaluate(select_text, text)
        if not selected:
            raise RuntimeError(f"В тексте не найдено название ссылки «{text}».")
        await page.wait_for_timeout(250)
        if await link_tool.count() == 0:
            raise RuntimeError(f"Редактор Дзена не открыл панель форматирования для «{text}».")
        await link_tool.first.click()
        if await link_input.count() == 0:
            raise RuntimeError(f"Редактор Дзена не открыл поле URL для «{text}».")
        await link_input.first.fill(url)
        await link_input.first.press("Enter")
        await page.wait_for_timeout(350)

    actual = await body_editor.locator("a[href]").evaluate_all(
        "anchors => anchors.map(anchor => ({text: anchor.textContent, href: anchor.href}))"
    )
    missing = [
        text for text, url in links
        if not any(item["text"] == text and item["href"] == url for item in actual)
    ]
    if missing:
        raise RuntimeError("Не подтверждены активные ссылки: " + ", ".join(missing))
    log(f"  active links verified: {len(links)}")


async def attach_media(page, media_paths, body_editor):
    """Вставляет изображения через штатный диалог редактора Дзена."""
    # Draft.js may keep the side toolbar button in the DOM but hidden after
    # programmatic text insertion. Its regular onClick still opens the native
    # image picker once the caret is placed in the body.
    insert_button = page.locator('button[data-tip="Вставить изображение"]')
    if await insert_button.count() == 0:
        raise RuntimeError("В редакторе не найдена кнопка «Вставить изображение».")

    # Ставим курсор после текста, чтобы изображение стало отдельным блоком в конце статьи.
    await focus_body_end(page, body_editor)
    await insert_button.first.evaluate("button => button.click()")

    file_input = page.locator('input[type="file"][accept*="image"]')
    if await file_input.count() == 0:
        raise RuntimeError("В редакторе не найдено поле загрузки изображения.")
    await file_input.first.set_input_files(media_paths)
    await page.wait_for_timeout(6000)

    uploaded = page.locator('[data-testid="article-image-item"] img')
    count = await uploaded.count()
    if count < len(media_paths):
        raise RuntimeError(f"Дзен загрузил {count} изображений из {len(media_paths)}.")
    sizes = await uploaded.evaluate_all("els => els.map(e => [e.naturalWidth, e.naturalHeight])")
    if any(width == 0 or height == 0 for width, height in sizes):
        raise RuntimeError("Превью изображения ещё не загрузилось в редакторе.")
    log(f"  images attached: {len(media_paths)}")


async def open_publish_settings(page):
    """Открывает настройки публикации и обрабатывает первичный диалог канала."""
    if await page.locator('[data-testid="publish-btn"]:visible').count():
        return
    header_publish = page.locator('[data-testid="article-publish-btn"]:visible')
    if await header_publish.count() == 0:
        header_publish = page.locator("button:visible").filter(has_text="Опубликовать")
    if await header_publish.count() == 0:
        raise RuntimeError("В редакторе не найдена кнопка «Опубликовать».")
    await header_publish.first.click()
    await page.wait_for_timeout(3000)

    # Первый клик открывает настройки публикации. При первом запуске аккаунта
    # перед ними может появиться отдельный диалог настройки канала.
    channel_modal = await handle_channel_modal(page)
    if channel_modal:
        header_publish = page.locator('[data-testid="article-publish-btn"]:visible')
        if await header_publish.count() == 0:
            raise RuntimeError("После настройки канала не найдена кнопка «Опубликовать».")
        await header_publish.first.click()
        await page.wait_for_timeout(3000)


async def enable_schedule(page, scheduled_at):
    """Включает отложку и заполняет дату и время штатными полями редактора."""
    schedule_label = page.locator("label").filter(has_text="Опубликовать позже")
    schedule_checkbox = schedule_label.locator("input[type='checkbox']")
    if await schedule_checkbox.count():
        if not await schedule_checkbox.first.is_checked():
            await schedule_checkbox.first.check(force=True)
    else:
        visible_text = page.get_by_text("Опубликовать позже", exact=True)
        if await visible_text.count() == 0:
            raise RuntimeError("В настройках Дзена не найден переключатель «Опубликовать позже».")
        await visible_text.first.click()
    await page.wait_for_timeout(500)

    time_value = scheduled_at.strftime("%H:%M")
    fields = page.locator("input:visible")
    metadata = await fields.evaluate_all("""
        els => els.map((e, i) => ({
            i,
            type: (e.type || '').toLowerCase(),
            name: (e.name || '').toLowerCase(),
            placeholder: (e.placeholder || '').toLowerCase(),
            aria: (e.getAttribute('aria-label') || '').toLowerCase(),
            value: e.value || ''
        }))
    """)

    date_fields = []
    time_fields = []
    text_fields = []
    for item in metadata:
        text = " ".join([item["name"], item["placeholder"], item["aria"]])
        if item["type"] == "date" or "дат" in text:
            date_fields.append(item["i"])
        elif item["type"] == "time" or "врем" in text:
            time_fields.append(item["i"])
        elif item["type"] not in {"checkbox", "radio", "hidden", "file", "button", "submit"}:
            text_fields.append(item["i"])

    if not date_fields and text_fields:
        date_fields.append(text_fields.pop(0))
    if not time_fields and text_fields:
        time_fields.append(text_fields.pop(0))
    if not date_fields or not time_fields:
        raise RuntimeError("Дзен не показал поля даты и времени после включения отложки.")

    date_field = fields.nth(date_fields[0])
    time_field = fields.nth(time_fields[0])
    if await date_field.get_attribute("readonly") is not None:
        calendar = page.locator('[data-testid="datepicker-calendar"]:visible')
        if await calendar.count() == 0:
            await date_field.click()
        day_cells = page.locator('[data-testid="datepicker-calendar-day"]:visible')
        matching_days = await day_cells.evaluate_all(
            "(cells, day) => cells.map((cell, index) => ({index, text: cell.textContent.trim(), "
            "available: cell.className.includes('isAvailable')})).filter(cell => "
            "cell.text === String(day) && cell.available).map(cell => cell.index)",
            scheduled_at.day,
        )
        if not matching_days:
            raise RuntimeError("В календаре Дзена не найден доступный день отложенной публикации.")
        await day_cells.nth(matching_days[0]).click()
    else:
        date_iso = scheduled_at.strftime("%Y-%m-%d")
        date_ru = scheduled_at.strftime("%d.%m.%Y")
        date_type = metadata[date_fields[0]]["type"]
        await date_field.fill(date_iso if date_type == "date" else date_ru)
    await time_field.fill(time_value)
    await page.wait_for_timeout(500)

    date_result = await date_field.input_value()
    time_result = await time_field.input_value()
    if str(scheduled_at.day) not in date_result or time_value not in time_result:
        raise RuntimeError("Дзен не сохранил указанную дату или время отложки.")
    log(f"  schedule set: {date_result} {time_result}")


async def submit_publish_settings(page, scheduled_at):
    """Отправляет настройки и подтверждает публикацию или отложку один раз."""
    submit = page.locator('[data-testid="publish-btn"]:visible')
    if await submit.count() == 0:
        raise RuntimeError("Не найдена кнопка подтверждения публикации в настройках Дзена.")
    await submit.first.click()
    await page.wait_for_timeout(3000)

    # Диалог подтверждения может называться «Опубликовать» или «Запланировать».
    for _ in range(6):
        confirm = await page.evaluate("""
        (scheduled) => {
            for (var b of document.querySelectorAll('button')) {
                var t = b.textContent.trim();
                var r = b.getBoundingClientRect();
                var matches = scheduled ? /[Зз]аплан|[Оо]публик/.test(t) : /[Оо]публик/.test(t);
                if (matches && r.top > 80 && r.width > 0)
                    return {t:t, x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)};
            }
            return null;
        }
        """, bool(scheduled_at))
        if confirm:
            log(f"  confirm dialog: {confirm}")
            await page.mouse.click(confirm["x"], confirm["y"])
            await page.wait_for_timeout(6000)
            break
        await page.wait_for_timeout(1000)


async def publish(page, scheduled_at=None):
    """Публикует сейчас или ставит подтверждённую статью в отложку."""
    await open_publish_settings(page)
    if scheduled_at:
        await enable_schedule(page, scheduled_at)
    await submit_publish_settings(page, scheduled_at)

    return page.url


async def connect_cdp(playwright):
    """Подключается к CDP с ограничением времени и одним безопасным повтором.

    Повтор выполняется только на этапе подключения, до открытия редактора и
    любых действий с публикацией. Это позволяет пережить зависший WebSocket,
    не создавая риск двойной отправки статьи.
    """
    last_error = None
    for attempt in range(1, CDP_CONNECT_RETRIES + 1):
        log(f"Connecting to Chrome CDP at {CDP_URL} (attempt {attempt}/{CDP_CONNECT_RETRIES})...")
        try:
            browser = await asyncio.wait_for(
                playwright.chromium.connect_over_cdp(CDP_URL),
                timeout=CDP_CONNECT_TIMEOUT_SECONDS,
            )
            if not browser.contexts:
                raise RuntimeError("CDP подключён, но Chrome не вернул ни одного контекста.")
            log(
                "CDP connected: "
                f"contexts={len(browser.contexts)}, pages={sum(len(c.pages) for c in browser.contexts)}"
            )
            return browser
        except Exception as exc:  # Playwright raises several transport-specific errors.
            last_error = exc
            log(f"  CDP attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt < CDP_CONNECT_RETRIES:
                await asyncio.sleep(1)

    raise RuntimeError(
        f"Chrome CDP не ответил за {CDP_CONNECT_TIMEOUT_SECONDS} секунд "
        f"({CDP_CONNECT_RETRIES} попытки): {last_error}"
    )


def select_page(context):
    """Выбирает уже открытый Dzen tab, не полагаясь на порядок вкладок."""
    pages = list(context.pages)
    if not pages:
        raise RuntimeError("В подключённом Chrome нет открытых вкладок.")
    dzen_pages = [page for page in pages if page.url.startswith("https://dzen.ru")]
    return dzen_pages[-1] if dzen_pages else pages[-1]


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    global TITLE, BODY

    args = parse_args()
    if args.paste_docx:
        if args.sequence or args.media or args.reuse_first_image:
            raise ValueError("--paste-docx нельзя сочетать с --sequence, --media или --reuse-first-image.")
        if not args.article or not os.path.isfile(args.article):
            raise ValueError("--paste-docx требует article-файл для заголовка.")
        if not os.path.isfile(args.paste_docx):
            raise FileNotFoundError(f"DOCX для rich paste не найден: {args.paste_docx}")
        if not args.footer_file or not os.path.isfile(args.footer_file):
            raise ValueError("--paste-docx требует существующий --footer-file.")
        if args.expected_pasted_images < 1:
            raise ValueError("--paste-docx требует --expected-pasted-images больше нуля.")
    if args.reuse_first_image and not args.sequence:
        raise ValueError("--reuse-first-image применяется только вместе с --sequence.")
    if args.sequence and args.media:
        raise ValueError("--sequence уже содержит изображения; не передавайте с ним --media.")
    if args.sequence and not args.article:
        # Последовательность — полноценный источник заголовка и текста.
        TITLE, sequence_blocks = load_sequence(args.sequence)
        BODY = ""
        log(f"Loaded ordered DOCX sequence from {args.sequence}: {len(sequence_blocks)} blocks")
    else:
        sequence_blocks = None
    media_paths = validate_media(args.media)
    scheduled_at = parse_schedule(args.schedule)
    if args.finalize_draft and (not args.draft_url or not scheduled_at):
        raise ValueError("--finalize-draft требует --draft-url и --schedule.")

    # Если передан файл — читаем из него
    if args.article and os.path.isfile(args.article):
        TITLE, BODY = load_article(args.article)
        log(f"Loaded article from {args.article}")
        log(f"Title: {TITLE[:60]}...")
        if args.sequence:
            sequence_title, sequence_blocks = load_sequence(args.sequence)
            if sequence_title != TITLE:
                raise ValueError("Заголовки article и --sequence не совпадают.")

    async with async_playwright() as p:
        browser = await connect_cdp(p)
        context = browser.contexts[0]
        if args.healthcheck:
            page = select_page(context)
            log(f"CDP HEALTHCHECK OK: page={page.url or '<blank>'}")
            # A health check must not close the user’s authorized Chrome.
            # Playwright disconnects when the async context exits.
            return None

        try:
            await asyncio.wait_for(
                context.grant_permissions(["clipboard-read", "clipboard-write"]),
                timeout=5,
            )
        except Exception as exc:
            # Clipboard is no longer needed for normal filling, so an older
            # Chrome context failing this optional permission must not block.
            log(f"  clipboard permissions skipped: {type(exc).__name__}: {exc}")
        if args.draft_url:
            pages = [page for page in context.pages if page.url.startswith(args.draft_url)]
            page = pages[-1] if pages else await context.new_page()
            if page.url != args.draft_url:
                await page.goto(args.draft_url, wait_until="domcontentloaded", timeout=25000)
            log(f"Resuming checked draft: {page.url}")
        else:
            page = select_page(context)
        await page.bring_to_front()
        log(f"Page: {page.url}")

        if args.finalize_draft:
            log("\n[1] Finalizing checked draft...")
            final_url = await publish(page, scheduled_at)
            log(f"\n{'='*60}")
            log(f"  SCHEDULED: {final_url}")
            log(f"{'='*60}")
            return final_url

        # 1. Открываем новый черновик
        if not args.draft_url:
            log("\n[1] Opening new article...")
            await open_new_article(page)
        else:
            log("\n[1] Reusing existing draft...")

        # 2. Ждём редактор
        log("\n[2] Waiting for editor...")
        await wait_for_ces(page, min_count=2, timeout_ms=15000)

        # 3. Заполняем заголовок и тело
        log("\n[3] Filling content...")
        if args.paste_docx:
            t_len, b_len = await fill_article_from_rich_clipboard(
                page, TITLE, public_footer(args.footer_file), args.expected_pasted_images
            )
        else:
            t_len, b_len = await fill_article(
                page, TITLE, BODY, media_paths, sequence_blocks=sequence_blocks,
                reuse_first_image=args.reuse_first_image,
            )
        log(f"  title={t_len} chars, body={b_len} chars")

        if args.prepare_only:
            log(f"  DRAFT PREPARED: {page.url}")
            return page.url

        # 4. Публикуем или ставим в отложку
        action = f"Scheduling for {scheduled_at.isoformat()}" if scheduled_at else "Publishing now"
        log(f"\n[4] {action}...")
        final_url = await publish(page, scheduled_at)

        log(f"\n{'='*60}")
        log(f"  {'SCHEDULED' if scheduled_at else 'PUBLISHED'}: {final_url}")
        log(f"{'='*60}")

        # Keep the dedicated authorized Chrome session alive for the next run.
        # Exiting async_playwright disconnects the CDP client without closing Chrome.
        return final_url


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Keep the CLI result machine-readable and bounded; callers can store
        # this concrete error without confusing a traceback with a submission.
        log(f"ERROR: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
