import asyncio
import os
import re
import subprocess
from urllib.parse import urljoin, urlsplit, urlunsplit, quote

from pyppeteer import launch
from bs4 import BeautifulSoup
from multiprocessing.pool import ThreadPool


# ============================================================
# Configuration
# ============================================================

TARGET_FILE = "target.txt"

PAGE_TIMEOUT = 60000

# 页面打开后等待 JS 更新 hash/modal
PAGE_WAIT = 1.2

# 下载 PDF 并发数
DOWNLOAD_THREADS = 10


# ============================================================
# Read target.txt
# ============================================================

def load_targets(filename=TARGET_FILE):
    """
    target.txt 示例：

    US-26
    US-25
    ASIA-26
    ASIA-25

    支持空行以及 # 注释。
    """

    if not os.path.exists(filename):
        raise FileNotFoundError(
            f"找不到 {filename}"
        )

    targets = []

    with open(
        filename,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            line = line.strip().upper()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if not re.fullmatch(
                r"(US|ASIA)-\d{2}",
                line
            ):
                print(
                    f"[WARN] Invalid target: {line}"
                )
                continue

            targets.append(line)

    # 去重并保持顺序
    return list(dict.fromkeys(targets))


# ============================================================
# Find Chrome
# ============================================================

def find_chrome():

    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]

    for path in candidates:

        if os.path.exists(path):

            print(
                f"[INFO] Using Chrome: {path}"
            )

            return path

    print(
        "[INFO] System Chrome not found, "
        "using Pyppeteer Chromium"
    )

    return None


# ============================================================
# Browser
# ============================================================

async def create_browser():

    chrome_path = find_chrome()

    kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-default-apps",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1920,1080",
        ],

        # 不把 Chrome DBus 日志刷到 GitHub Actions
        "dumpio": False,

        "autoClose": True,

        "ignoreHTTPSErrors": True,
    }

    if chrome_path:
        kwargs["executablePath"] = chrome_path

    browser = await launch(
        **kwargs
    )

    return browser


# ============================================================
# Configure page
# ============================================================

async def setup_page(browser):

    page = await browser.newPage()

    # 使用系统 Chrome 自己的 UA
    try:

        user_agent = await browser.userAgent()

        user_agent = user_agent.replace(
            "HeadlessChrome",
            "Chrome"
        )

        await page.setUserAgent(
            user_agent
        )

        print(
            f"[INFO] UA: {user_agent}"
        )

    except Exception as e:

        print(
            f"[WARN] Unable to set UA: {e}"
        )

    await page.setViewport({
        "width": 1920,
        "height": 1080,
        "deviceScaleFactor": 1,
    })

    await page.setExtraHTTPHeaders({
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
    })

    try:

        await page.evaluateOnNewDocument(
            """
            () => {
                Object.defineProperty(
                    navigator,
                    'webdriver',
                    {
                        get: () => undefined
                    }
                );
            }
            """
        )

    except Exception:
        pass

    return page


# ============================================================
# Warm up
# ============================================================

async def warm_up(page):

    print(
        "[INFO] Warming up blackhat.com..."
    )

    try:

        response = await page.goto(
            "https://blackhat.com/",
            {
                "waitUntil": "domcontentloaded",
                "timeout": PAGE_TIMEOUT,
            }
        )

        if response:

            print(
                f"[INFO] Warm-up HTTP: "
                f"{response.status}"
            )

        await asyncio.sleep(2)

    except Exception as e:

        print(
            f"[WARN] Warm-up failed: {e}"
        )


# ============================================================
# Open page
# ============================================================

async def goto_page(
    page,
    url,
    retry=2
):

    for attempt in range(
        1,
        retry + 1
    ):

        try:

            response = await page.goto(
                url,
                {
                    "waitUntil": "domcontentloaded",
                    "timeout": PAGE_TIMEOUT,
                }
            )

            # ==================================================
            # 对于同一个 index.html 不同 #fragment，
            # Chromium 有时认为是 same-document navigation，
            # response 会返回 None。
            #
            # 这是正常现象，不能判断为失败。
            # ==================================================

            if response is None:

                print(
                    f"[HTTP] same-document -> {page.url}"
                )

                await asyncio.sleep(
                    PAGE_WAIT
                )

                return True

            status = response.status

            print(
                f"[HTTP] {status} -> {page.url}"
            )

            if status < 400:

                await asyncio.sleep(
                    PAGE_WAIT
                )

                return True

            if status == 403:

                print(
                    f"[WARN] HTTP 403 "
                    f"(attempt {attempt}/{retry})"
                )

                await asyncio.sleep(
                    3 * attempt
                )

                continue

            print(
                f"[WARN] HTTP {status}"
            )

        except Exception as e:

            print(
                f"[WARN] page.goto failed: {e}"
            )

            await asyncio.sleep(
                2 * attempt
            )

    return False


# ============================================================
# Get one/multiple pages with ONE Chrome
# ============================================================

async def fetch_pages_async(urls):

    browser = await create_browser()

    page = await setup_page(
        browser
    )

    results = []

    try:

        # 一整个批次只 warm-up 一次
        await warm_up(page)

        total = len(urls)

        for index, url in enumerate(
            urls,
            start=1
        ):

            print(
                f"[PAGE {index}/{total}] {url}"
            )

            success = await goto_page(
                page,
                url
            )

            if not success:

                print(
                    f"[ERROR] Unable to load: {url}"
                )

                results.append("")

                continue

            try:

                content = await page.content()

                results.append(
                    content
                )

            except Exception as e:

                print(
                    f"[ERROR] Unable to get HTML: {e}"
                )

                results.append("")

    finally:

        await browser.close()

    return results


def browser_get(urls):

    if isinstance(
        urls,
        str
    ):
        urls = [urls]

    return asyncio.run(
        fetch_pages_async(
            urls
        )
    )


# ============================================================
# URL encode
# ============================================================

def encode_url(url):
    """
    PDF 文件名可能有空格，例如：

    Akash BlackHat Presentation 2026 - Final.pdf

    wget 对裸空格 URL 可能有问题，
    所以把 path 正确编码。
    """

    try:

        parts = urlsplit(url)

        encoded_path = quote(
            parts.path,
            safe="/%:@"
        )

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                encoded_path,
                parts.query,
                parts.fragment,
            )
        )

    except Exception:

        return url


# ============================================================
# Get all Black Hat sessions
#
# 保留原来的逻辑：
#
# schedule
#   ↓
# ul#cal_content_Day
#   ↓
# #session-name-ID
# ============================================================

def get_All_Sessions(
    Area_With_Date
):

    TopicURL = []

    url = (
        f"https://blackhat.com/"
        f"{Area_With_Date.lower()}/"
        f"briefings/schedule/index.html"
    )

    print()
    print(
        "=" * 70
    )

    print(
        f"[TARGET] {Area_With_Date}"
    )

    print(
        f"[SCHEDULE] {url}"
    )

    print(
        "=" * 70
    )

    response = browser_get(
        url
    )

    if (
        not response
        or not response[0]
    ):

        print(
            f"[ERROR] Unable to get schedule: "
            f"{Area_With_Date}"
        )

        return []

    html = response[0]

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    # ========================================================
    # 方法 1
    #
    # 完全使用原来的：
    # ul#cal_content_Day
    # ========================================================

    schedule_ul = soup.find(
        "ul",
        id="cal_content_Day"
    )

    if schedule_ul:

        print(
            "[INFO] Found old schedule DOM: "
            "ul#cal_content_Day"
        )

        main_li = schedule_ul.find_all(
            "li"
        )

        for li in main_li:

            anchors = li.find_all(
                "a",
                href=True
            )

            for a in anchors:

                href = (
                    a.get(
                        "href",
                        ""
                    )
                    .strip()
                )

                if not href:
                    continue

                if not href.startswith("#"):
                    continue

                if "speaker" in href.lower():
                    continue

                # Black Hat session 基本都有数字 ID
                if not re.search(
                    r"-\d+$",
                    href
                ):
                    continue

                TopicURL.append(
                    url + href
                )

    # ========================================================
    # 方法 2
    #
    # ul 名字以后改变时：
    # 仍然按照 #session-name-ID 原理找
    # ========================================================

    if not TopicURL:

        print(
            "[INFO] ul#cal_content_Day "
            "not found, scanning anchors..."
        )

        anchors = soup.find_all(
            "a",
            href=True
        )

        for a in anchors:

            href = (
                a.get(
                    "href",
                    ""
                )
                .strip()
            )

            if not href:
                continue

            if not href.startswith("#"):
                continue

            if "speaker" in href.lower():
                continue

            if not re.search(
                r"-\d+$",
                href
            ):
                continue

            TopicURL.append(
                url + href
            )

    # ========================================================
    # 方法 3
    #
    # 再 fallback 到 raw HTML
    # ========================================================

    if not TopicURL:

        print(
            "[INFO] Searching raw HTML "
            "for session fragments..."
        )

        fragments = re.findall(
            r'#[A-Za-z0-9_%\-]+-\d+',
            html
        )

        for fragment in fragments:

            if (
                "speaker"
                in fragment.lower()
            ):
                continue

            TopicURL.append(
                url + fragment
            )

    # 去重保持顺序
    TopicURL = list(
        dict.fromkeys(
            TopicURL
        )
    )

    print(
        f"[INFO] Found "
        f"{len(TopicURL)} sessions"
    )

    return TopicURL


# ============================================================
# Extract PDF from one session HTML
#
# 保留原来的：
#
# div.bhpresentation
#        ↓
#     <a href>
#        ↓
#      .pdf
# ============================================================

def extract_session_pdfs(
    html,
    session_url
):

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    session_pdfs = []

    # ========================================================
    # 第一优先：
    # 原来的 bhpresentation
    # ========================================================

    div = soup.find(
        "div",
        class_="bhpresentation"
    )

    if div:

        links = div.find_all(
            "a",
            href=True
        )

        for a in links:

            href = (
                a.get(
                    "href",
                    ""
                )
                .strip()
            )

            if not href:
                continue

            if ".pdf" not in href.lower():
                continue

            full_url = urljoin(
                session_url,
                href
            )

            session_pdfs.append(
                encode_url(
                    full_url
                )
            )

    # ========================================================
    # fallback
    #
    # 新页面如果 bhpresentation class 改名，
    # 从当前 session HTML 里找 Presentation PDF。
    #
    # 注意：
    # 必须带 /Presentations/
    #
    # 防止误抓：
    # /Trainings/BHUSA26_Trainings...
    # ========================================================

    if not session_pdfs:

        pdf_links = soup.find_all(
            "a",
            href=True
        )

        for a in pdf_links:

            href = (
                a.get(
                    "href",
                    ""
                )
                .strip()
            )

            if not href:
                continue

            lower_href = href.lower()

            clean_href = (
                lower_href
                .split("?")[0]
            )

            if not clean_href.endswith(
                ".pdf"
            ):
                continue

            full_url = urljoin(
                session_url,
                href
            )

            # fallback 只取 Presentation
            if (
                "/presentations/"
                not in full_url.lower()
            ):
                continue

            session_pdfs.append(
                encode_url(
                    full_url
                )
            )

    return list(
        dict.fromkeys(
            session_pdfs
        )
    )


# ============================================================
# Process ALL sessions using ONE Chrome
# ============================================================

async def process_sessions_async(
    TopicURL
):

    All_PDF = []

    if not TopicURL:
        return []

    browser = await create_browser()

    page = await setup_page(
        browser
    )

    try:

        # 整个 124 sessions 只 warm-up 一次
        await warm_up(page)

        total = len(TopicURL)

        print()
        print(
            f"[INFO] Processing "
            f"{total} sessions"
        )

        for index, url in enumerate(
            TopicURL,
            start=1
        ):

            print()
            print(
                f"[SESSION {index}/{total}]"
            )

            print(url)

            success = await goto_page(
                page,
                url
            )

            if not success:

                print(
                    "[WARN] Session page failed"
                )

                continue

            try:

                html = await page.content()

            except Exception as e:

                print(
                    f"[WARN] Unable to get HTML: "
                    f"{e}"
                )

                continue

            session_pdfs = (
                extract_session_pdfs(
                    html,
                    url
                )
            )

            print(
                f"[INFO] PDF found: "
                f"{len(session_pdfs)}"
            )

            for pdf in session_pdfs:

                print(
                    f"       {pdf}"
                )

                All_PDF.append(
                    pdf
                )

    finally:

        await browser.close()

    return list(
        dict.fromkeys(
            All_PDF
        )
    )


# ============================================================
# Sort PDFs
# ============================================================

def sort_PDF(
    Area_With_Date
):

    TopicURL = get_All_Sessions(
        Area_With_Date
    )

    if not TopicURL:

        print(
            f"[WARN] No sessions found "
            f"for {Area_With_Date}"
        )

        return []

    # ========================================================
    # 关键修改
    #
    # 原来：
    #
    # for session:
    #     开一个 Chrome
    #
    # 现在：
    #
    # 开一个 Chrome
    #     ↓
    # session 1
    # session 2
    # session 3
    # ...
    # session 124
    #     ↓
    # 关闭 Chrome
    # ========================================================

    All_PDF = asyncio.run(
        process_sessions_async(
            TopicURL
        )
    )

    return All_PDF


# ============================================================
# Download PDF
#
# 保留原来的 wget 下载逻辑
# ============================================================

def download_PDF(args):

    Area_With_Date, PDF = args

    currentDir = os.getcwd()

    save_dir = os.path.join(
        currentDir,
        "save",
        Area_With_Date
    )

    os.makedirs(
        save_dir,
        exist_ok=True
    )

    print(
        f"[DOWNLOAD] {PDF}"
    )

    result = subprocess.call(
        [
            "wget",

            "--no-check-certificate",

            "-t",
            "3",

            "-T",
            "30",

            "--content-disposition",

            "-P",
            save_dir,

            PDF,
        ],

        cwd=currentDir
    )

    if result != 0:

        print(
            f"[WARN] wget failed: "
            f"{PDF}"
        )

    return result


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "=" * 70
    )

    print(
        "Black Hat PDF Downloader"
    )

    print(
        "=" * 70
    )

    Targets = load_targets()

    if not Targets:

        print(
            "[ERROR] No valid targets "
            "found in target.txt"
        )

        raise SystemExit(1)

    print(
        "[INFO] Targets: "
        + ", ".join(Targets)
    )

    os.makedirs(
        "save",
        exist_ok=True
    )

    total_pdf_count = 0

    # ========================================================
    # 每一个 target：
    #
    # 1. schedule
    # 2. sessions
    # 3. PDF
    # 4. wget
    # ========================================================

    for Area_With_Date in Targets:

        print()
        print(
            "#" * 70
        )

        print(
            f"# START: "
            f"{Area_With_Date}"
        )

        print(
            "#" * 70
        )

        try:

            All_pdf = sort_PDF(
                Area_With_Date
            )

        except Exception as e:

            print()
            print(
                f"[ERROR] "
                f"{Area_With_Date} failed:"
            )

            print(
                f"        {e}"
            )

            continue

        print()
        print(
            "=" * 70
        )

        print(
            f"[RESULT] "
            f"{Area_With_Date}: "
            f"{len(All_pdf)} "
            f"unique PDF files"
        )

        print(
            "=" * 70
        )

        if not All_pdf:

            print(
                f"[WARN] No PDFs found "
                f"for {Area_With_Date}"
            )

            continue

        # 保存 URL 清单
        target_dir = os.path.join(
            "save",
            Area_With_Date
        )

        os.makedirs(
            target_dir,
            exist_ok=True
        )

        url_file = os.path.join(
            target_dir,
            "pdf_urls.txt"
        )

        with open(
            url_file,
            "w",
            encoding="utf-8"
        ) as f:

            for pdf in All_pdf:

                f.write(
                    pdf + "\n"
                )

        total_pdf_count += len(
            All_pdf
        )

        # ====================================================
        # wget 并发下载
        # ====================================================

        jobs = [
            (
                Area_With_Date,
                pdf
            )

            for pdf in All_pdf
        ]

        tp = ThreadPool(
            DOWNLOAD_THREADS
        )

        try:

            tp.map(
                download_PDF,
                jobs
            )

        finally:

            tp.close()
            tp.join()

        print()
        print(
            f"[DONE] "
            f"{Area_With_Date}"
        )

    # ========================================================
    # FINAL
    # ========================================================

    print()
    print(
        "=" * 70
    )

    print(
        "ALL TARGETS FINISHED"
    )

    print(
        f"Total unique PDFs: "
        f"{total_pdf_count}"
    )

    print(
        "=" * 70
    )

    if total_pdf_count == 0:

        print(
            "[ERROR] No PDF found "
            "for any target"
        )

        raise SystemExit(1)
