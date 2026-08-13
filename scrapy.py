import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# Configuration
# ============================================================

TARGET_FILE = "target.txt"

SAVE_ROOT = Path("save")
DEBUG_ROOT = Path("debug")

# 同时打开多少个 session 页面
MAX_SESSION_CONCURRENCY = 4

# 同时下载多少个 PDF
DOWNLOAD_WORKERS = 10

# 页面超时
PAGE_TIMEOUT = 60000

# 页面额外等待 JS 加载时间
PAGE_EXTRA_WAIT = 2500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}


# ============================================================
# target.txt
# ============================================================

def load_targets(filename=TARGET_FILE):
    """
    支持：

    US-26

    或：

    US-26
    ASIA-26

    也支持注释：

    # Black Hat targets
    US-26
    ASIA-26
    """

    path = Path(filename)

    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {filename}\n"
            f"请创建 {filename}，例如：\n"
            f"US-26"
        )

    targets = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().upper()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if not re.fullmatch(r"(US|ASIA)-\d{2}", line):
            raise ValueError(
                f"target.txt 中存在无效目标：{line}\n"
                f"支持格式：US-26 / ASIA-26"
            )

        targets.append(line)

    targets = list(dict.fromkeys(targets))

    if not targets:
        raise ValueError(
            "target.txt 没有有效目标"
        )

    return targets


# ============================================================
# URL helpers
# ============================================================

def get_schedule_urls(target):
    """
    返回多个候选 schedule URL。

    Black Hat 不同年份/地区的页面实现可能稍有区别，
    所以同时尝试几个入口。
    """

    slug = target.lower()

    base = (
        f"https://www.blackhat.com/"
        f"{slug}/briefings/schedule/"
    )

    index = (
        f"https://www.blackhat.com/"
        f"{slug}/briefings/schedule/index.html"
    )

    return [
        base,
        index,

        # USA 一般 Wednesday / Thursday
        f"{index}?day=wednesday",
        f"{index}?day=thursday",

        # Asia 一般 Thursday / Friday
        f"{index}?day=friday",
    ]


def is_blackhat_host(url):
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()

        return (
            host == "blackhat.com"
            or host.endswith(".blackhat.com")
        )
    except Exception:
        return False


def normalize_url(url, base_url):
    """
    //i.blackhat.com/xxx
    /xxx
    relative/path
    全部转成绝对 URL。
    """

    if not url:
        return None

    url = url.strip()

    if url.startswith("//"):
        url = "https:" + url

    return urljoin(base_url, url)


def normalize_pdf_url(url, base_url):
    url = normalize_url(url, base_url)

    if not url:
        return None

    if not is_blackhat_host(url):
        return None

    parsed = urlparse(url)

    if not parsed.path.lower().endswith(".pdf"):
        return None

    return url


# ============================================================
# Browser helpers
# ============================================================

async def load_page(page, url):
    """
    打开动态页面并等待 JavaScript。
    """

    print(f"[PAGE] {url}")

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        if response:
            print(
                f"       HTTP {response.status} -> {page.url}"
            )

    except PlaywrightTimeoutError:
        print(
            f"[WARN] 页面加载超时，继续尝试读取 DOM: {url}"
        )

    except Exception as e:
        print(
            f"[WARN] 页面打开失败: {url}\n"
            f"       {e}"
        )
        return False

    # networkidle 某些网站可能永远达不到，所以单独 try
    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=15000,
        )
    except Exception:
        pass

    await page.wait_for_timeout(PAGE_EXTRA_WAIT)

    # 向下滚动，触发可能存在的 lazy loading
    try:
        for _ in range(5):
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )

            await page.wait_for_timeout(500)

        await page.evaluate(
            "window.scrollTo(0, 0)"
        )

    except Exception:
        pass

    return True


# ============================================================
# PDF extraction
# ============================================================

async def extract_pdf_urls(page):
    """
    从当前页面寻找所有官方 Black Hat PDF。

    不依赖固定 class。
    """

    pdfs = set()

    base_url = page.url

    # --------------------------------------------------------
    # 方法 1：直接找 a[href]
    # --------------------------------------------------------

    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            """
            elements => elements.map(
                element => element.getAttribute('href')
            )
            """
        )

        for href in hrefs:
            pdf = normalize_pdf_url(
                href,
                base_url,
            )

            if pdf:
                pdfs.add(pdf)

    except Exception:
        pass

    # --------------------------------------------------------
    # 方法 2：搜索整个 HTML
    #
    # 有些链接可能藏在 JS / JSON 数据中，
    # 没真正变成 <a>。
    # --------------------------------------------------------

    try:
        html = await page.content()

        # 处理 JSON 中的 https:\/\/
        html2 = html.replace("\\/", "/")

        absolute_urls = re.findall(
            r'https?://[^"\'<>\s]+?\.pdf(?:\?[^"\'<>\s]*)?',
            html2,
            flags=re.IGNORECASE,
        )

        for url in absolute_urls:
            pdf = normalize_pdf_url(
                url,
                base_url,
            )

            if pdf:
                pdfs.add(pdf)

        relative_urls = re.findall(
            r'["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']',
            html2,
            flags=re.IGNORECASE,
        )

        for url in relative_urls:
            pdf = normalize_pdf_url(
                url,
                base_url,
            )

            if pdf:
                pdfs.add(pdf)

    except Exception:
        pass

    return pdfs


# ============================================================
# Session extraction
# ============================================================

async def extract_session_urls(page, target):
    """
    从已经渲染后的 schedule 页面获取 session URL。

    同时兼容：
        #session-name-12345

    和：

        /briefings/schedule/session-name-12345
    """

    sessions = set()

    slug = target.lower()

    schedule_prefix = (
        f"/{slug}/briefings/schedule/"
    )

    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            """
            elements => elements.map(element => ({
                raw: element.getAttribute('href'),
                absolute: element.href
            }))
            """
        )

    except Exception:
        return sessions

    for item in hrefs:
        raw = item.get("raw") or ""
        absolute = item.get("absolute") or ""

        if not absolute:
            continue

        if not is_blackhat_host(absolute):
            continue

        parsed = urlparse(absolute)

        path = parsed.path.lower()

        if schedule_prefix not in path:
            continue

        # PDF 肯定不是 session
        if parsed.path.lower().endswith(".pdf"):
            continue

        # speakers / filters 排除
        if "speaker" in path:
            continue

        # -----------------------------------------------
        # 类型 1：
        # index.html#session-name-12345
        # -----------------------------------------------

        if parsed.fragment:
            fragment = parsed.fragment.lower()

            if (
                "speaker" not in fragment
                and len(fragment) > 3
            ):
                sessions.add(absolute)

                continue

        # -----------------------------------------------
        # 类型 2：
        # /schedule/session-name-12345
        # -----------------------------------------------

        clean_path = parsed.path.rstrip("/")

        generic_paths = {
            schedule_prefix.rstrip("/"),
            (
                schedule_prefix
                + "index.html"
            ).rstrip("/"),
        }

        if clean_path.lower() in generic_paths:
            continue

        # query 参数通常只是 day/filter
        # 不把纯过滤页面当成 session
        if (
            parsed.query
            and not parsed.fragment
            and clean_path.lower().endswith(
                "index.html"
            )
        ):
            continue

        # session 通常有数字 ID
        if re.search(
            r"-\d+$",
            clean_path
        ):
            sessions.add(absolute)

        # 部分年份可能不是数字结尾，
        # 非 index 页面也保留。
        elif not clean_path.lower().endswith(
            "index.html"
        ):
            sessions.add(absolute)

    return sessions


# ============================================================
# Schedule discovery
# ============================================================

async def discover_schedule(browser, target):
    """
    尝试多个 schedule 入口，
    汇总 session + 直接 PDF。
    """

    print()
    print("=" * 70)
    print(f"[TARGET] {target}")
    print("=" * 70)

    all_sessions = set()
    all_pdfs = set()

    best_html = ""
    best_score = -1

    for schedule_url in get_schedule_urls(target):

        page = await browser.new_page(
            user_agent=HEADERS["User-Agent"]
        )

        try:
            ok = await load_page(
                page,
                schedule_url,
            )

            if not ok:
                continue

            pdfs = await extract_pdf_urls(page)

            sessions = await extract_session_urls(
                page,
                target,
            )

            all_pdfs.update(pdfs)
            all_sessions.update(sessions)

            html = await page.content()

            score = (
                len(pdfs) * 100
                + len(sessions)
                + len(html) / 100000
            )

            if score > best_score:
                best_score = score
                best_html = html

            print(
                f"[INFO] schedule result: "
                f"{len(sessions)} sessions, "
                f"{len(pdfs)} direct PDFs"
            )

        finally:
            await page.close()

    print()
    print(
        f"[INFO] {target}: "
        f"{len(all_sessions)} unique sessions"
    )

    print(
        f"[INFO] {target}: "
        f"{len(all_pdfs)} PDFs directly from schedule"
    )

    # 保留一份 schedule 调试 HTML
    if best_html:
        DEBUG_ROOT.mkdir(
            parents=True,
            exist_ok=True,
        )

        debug_file = (
            DEBUG_ROOT
            / f"{target}-schedule.html"
        )

        debug_file.write_text(
            best_html,
            encoding="utf-8",
        )

    return all_sessions, all_pdfs


# ============================================================
# Visit individual sessions
# ============================================================

async def inspect_session(
    browser,
    semaphore,
    target,
    session_url,
):
    async with semaphore:

        page = await browser.new_page(
            user_agent=HEADERS["User-Agent"]
        )

        try:
            ok = await load_page(
                page,
                session_url,
            )

            if not ok:
                return set()

            pdfs = await extract_pdf_urls(
                page
            )

            if pdfs:
                print(
                    f"[PDF] {len(pdfs):2d} -> "
                    f"{session_url}"
                )

            return pdfs

        except Exception as e:
            print(
                f"[WARN] session 处理失败:\n"
                f"       {session_url}\n"
                f"       {e}"
            )

            return set()

        finally:
            await page.close()


async def discover_pdfs(browser, target):
    sessions, pdfs = await discover_schedule(
        browser,
        target,
    )

    all_pdfs = set(pdfs)

    if not sessions:

        print()
        print(
            f"[WARN] {target} 没找到 session URL。"
        )

        print(
            "[WARN] 已保存 schedule HTML 到 debug/，"
            "便于检查网站是否再次改版。"
        )

        return all_pdfs

    semaphore = asyncio.Semaphore(
        MAX_SESSION_CONCURRENCY
    )

    tasks = []

    for session_url in sorted(sessions):
        tasks.append(
            inspect_session(
                browser,
                semaphore,
                target,
                session_url,
            )
        )

    print()
    print(
        f"[INFO] 开始检查 "
        f"{len(tasks)} 个 session..."
    )

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    for result in results:

        if isinstance(result, set):
            all_pdfs.update(result)

    print()
    print(
        f"[RESULT] {target}: "
        f"共找到 {len(all_pdfs)} 个 PDF"
    )

    return all_pdfs


# ============================================================
# Downloads
# ============================================================

def safe_filename(url):
    parsed = urlparse(url)

    filename = os.path.basename(
        parsed.path
    )

    filename = unquote(filename)

    if not filename:
        filename = "unknown.pdf"

    # 防止奇怪路径字符
    filename = re.sub(
        r'[\\/:*?"<>|]',
        "_",
        filename,
    )

    return filename


def download_pdf(target, url):
    target_dir = SAVE_ROOT / target

    target_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = safe_filename(url)

    output_file = (
        target_dir
        / filename
    )

    temp_file = (
        target_dir
        / (filename + ".part")
    )

    if output_file.exists():

        if output_file.stat().st_size > 0:
            return (
                True,
                f"[SKIP] {target}/{filename}"
            )

    headers = dict(HEADERS)

    headers["Referer"] = (
        f"https://www.blackhat.com/"
        f"{target.lower()}/"
    )

    try:
        with requests.get(
            url,
            headers=headers,
            timeout=90,
            stream=True,
            allow_redirects=True,
        ) as response:

            response.raise_for_status()

            with open(
                temp_file,
                "wb"
            ) as f:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):

                    if chunk:
                        f.write(chunk)

        if (
            not temp_file.exists()
            or temp_file.stat().st_size == 0
        ):
            raise RuntimeError(
                "下载完成但文件为空"
            )

        os.replace(
            temp_file,
            output_file,
        )

        size_mb = (
            output_file.stat().st_size
            / 1024
            / 1024
        )

        return (
            True,
            (
                f"[OK] {target}/{filename} "
                f"({size_mb:.2f} MB)"
            )
        )

    except Exception as e:

        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass

        return (
            False,
            (
                f"[FAIL] {url}\n"
                f"       {e}"
            )
        )


def download_target_pdfs(
    target,
    pdfs,
):
    pdfs = sorted(set(pdfs))

    target_dir = SAVE_ROOT / target

    target_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 保存 URL 清单
    urls_file = (
        target_dir
        / "pdf_urls.txt"
    )

    urls_file.write_text(
        "\n".join(pdfs) + "\n",
        encoding="utf-8",
    )

    if not pdfs:
        print(
            f"[WARN] {target}: 没有 PDF 可下载"
        )

        return 0, 0

    print()
    print("=" * 70)
    print(
        f"[DOWNLOAD] {target}: "
        f"{len(pdfs)} PDFs"
    )
    print("=" * 70)

    success = 0
    failed = 0

    with ThreadPoolExecutor(
        max_workers=DOWNLOAD_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                download_pdf,
                target,
                url,
            )
            for url in pdfs
        ]

        for future in as_completed(
            futures
        ):

            ok, message = future.result()

            print(message)

            if ok:
                success += 1
            else:
                failed += 1

    return success, failed


# ============================================================
# Main browser task
# ============================================================

async def scrape_targets(targets):

    results = {}

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        try:
            for target in targets:

                pdfs = await discover_pdfs(
                    browser,
                    target,
                )

                results[target] = pdfs

        finally:
            await browser.close()

    return results


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print("Black Hat PDF Downloader")
    print("=" * 70)

    try:
        targets = load_targets()

    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    print(
        "[INFO] Targets: "
        + ", ".join(targets)
    )

    SAVE_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    DEBUG_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        results = asyncio.run(
            scrape_targets(
                targets
            )
        )

    except Exception as e:

        print()
        print(
            f"[FATAL] 浏览器抓取失败: {e}"
        )

        sys.exit(1)

    total_success = 0
    total_failed = 0

    empty_targets = []

    for target, pdfs in results.items():

        if not pdfs:
            empty_targets.append(
                target
            )

            continue

        success, failed = (
            download_target_pdfs(
                target,
                pdfs,
            )
        )

        total_success += success
        total_failed += failed

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)

    for target in targets:

        count = len(
            results.get(
                target,
                set()
            )
        )

        print(
            f"{target}: {count} PDF URLs"
        )

    print(
        f"Downloaded/Existing: "
        f"{total_success}"
    )

    print(
        f"Download failures: "
        f"{total_failed}"
    )

    if empty_targets:

        print()
        print(
            "[WARN] 以下目标没有找到 PDF："
        )

        for target in empty_targets:
            print(
                f"       - {target}"
            )

        print()
        print(
            "请检查 debug/ 下生成的 HTML。"
        )

        # GitHub Actions 中让 build 失败，
        # 避免表面成功但实际什么都没抓。
        sys.exit(1)

    if total_failed > 0:

        print()
        print(
            "[WARN] 部分 PDF 下载失败"
        )

        sys.exit(1)

    print()
    print("[DONE] 全部完成")


if __name__ == "__main__":
    main()
