import asyncio
import re
import signal
import psutil
import os
import subprocess
from pathlib import Path
from urllib.parse import urljoin
from pyppeteer import launch
from bs4 import BeautifulSoup
from multiprocessing.pool import ThreadPool


# ============================================================
# 配置
# ============================================================

TARGET_FILE = "target.txt"

# 页面加载超时
PAGE_TIMEOUT = 60000

# 页面加载后额外等待
PAGE_WAIT = 5


# ============================================================
# 读取 target.txt
# ============================================================

def load_targets():
    """
    target.txt 可以写：

    US-26

    或者：

    US-26
    US-25
    ASIA-26
    ASIA-25
    """

    if not os.path.exists(TARGET_FILE):
        raise FileNotFoundError(
            f"找不到 {TARGET_FILE}"
        )

    targets = []

    with open(
        TARGET_FILE,
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
                    f"[WARN] 无效 target: {line}"
                )

                continue

            targets.append(line)

    return list(dict.fromkeys(targets))


# ============================================================
# 查找系统 Chrome
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
# 页面访问
# ============================================================

async def goto_page(
    page,
    url,
    retry=3
):

    for attempt in range(
        1,
        retry + 1
    ):

        print(
            f"[PAGE] {url} "
            f"(attempt {attempt}/{retry})"
        )

        try:

            response = await page.goto(
                url,
                {
                    "waitUntil": "networkidle2",
                    "timeout": PAGE_TIMEOUT,
                }
            )

            status = (
                response.status
                if response
                else None
            )

            print(
                f"[HTTP] {status} -> {page.url}"
            )

            # ----------------------------
            # 成功
            # ----------------------------

            if status and status < 400:

                await asyncio.sleep(
                    PAGE_WAIT
                )

                # 页面滚动，触发懒加载
                try:

                    await page.evaluate(
                        """
                        () => {
                            window.scrollTo(
                                0,
                                document.body.scrollHeight
                            );
                        }
                        """
                    )

                    await asyncio.sleep(2)

                    await page.evaluate(
                        """
                        () => {
                            window.scrollTo(0, 0);
                        }
                        """
                    )

                except:
                    pass

                return True

            # ----------------------------
            # 403
            # ----------------------------

            if status == 403:

                print(
                    "[WARN] HTTP 403, "
                    "retrying..."
                )

                await asyncio.sleep(
                    3 * attempt
                )

                continue

            print(
                f"[WARN] HTTP status: {status}"
            )

        except Exception as e:

            print(
                f"[WARN] page.goto failed: {e}"
            )

            await asyncio.sleep(
                3 * attempt
            )

    return False


# ============================================================
# 原来的 main()
# ============================================================

async def main(target):

    _return = []

    chrome_path = find_chrome()

    launch_args = {

        "headless": True,

        "options": {

            "args": [

                "--no-sandbox",

                "--disable-setuid-sandbox",

                "--disable-dev-shm-usage",

                "--disable-gpu",

                "--disable-blink-features=AutomationControlled",

                "--window-size=1920,1080",

            ],

            "dumpio": True,

            "autoClose": False,
        }
    }

    # 优先使用 GitHub runner 已安装的 Chrome
    if chrome_path:

        launch_args[
            "executablePath"
        ] = chrome_path

    browser = await launch(
        **launch_args
    )

    page = await browser.newPage()

    # ========================================================
    # 使用浏览器自己的 UA
    # 只去掉 HeadlessChrome
    # ========================================================

    try:

        user_agent = (
            await browser.userAgent()
        )

        user_agent = (
            user_agent.replace(
                "HeadlessChrome",
                "Chrome"
            )
        )

        await page.setUserAgent(
            user_agent
        )

        print(
            f"[INFO] UA: {user_agent}"
        )

    except Exception as e:

        print(
            f"[WARN] UA setup failed: {e}"
        )

    # ========================================================
    # 浏览器环境
    # ========================================================

    await page.setViewport({
        "width": 1920,
        "height": 1080,
        "deviceScaleFactor": 1,
    })

    await page.setExtraHTTPHeaders({

        "Accept-Language":
            "en-US,en;q=0.9",

        "DNT":
            "1",
    })

    # 避免 webdriver 标识
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

    # ========================================================
    # 先访问主页
    #
    # 原爬取逻辑没有改变。
    # 这里主要是先建立正常 Cookie / 浏览器会话。
    # ========================================================

    try:

        print(
            "[INFO] Warming up blackhat.com..."
        )

        await page.goto(
            "https://blackhat.com/",
            {
                "waitUntil":
                    "domcontentloaded",

                "timeout":
                    PAGE_TIMEOUT,
            }
        )

        await asyncio.sleep(3)

    except Exception as e:

        print(
            f"[WARN] Warm-up failed: {e}"
        )

    # ========================================================
    # 原来的遍历 target URL
    # ========================================================

    for url in target:

        success = await goto_page(
            page,
            url
        )

        if not success:

            print(
                f"[ERROR] Unable to load: {url}"
            )

            _return.append("")

            continue

        content = await page.content()

        _return.append(
            content
        )

    await browser.close()

    return _return


# ============================================================
# 原来的 kill_child_processes()
# ============================================================

def kill_child_processes(
    parent_pid,
    sig=signal.SIGTERM
):

    try:

        parent = psutil.Process(
            parent_pid
        )

    except psutil.NoSuchProcess:

        return

    children = parent.children(
        recursive=True
    )

    for process in children:

        try:

            process.send_signal(sig)

        except:

            pass


# ============================================================
# 运行 async main
# ============================================================

def browser_get(url):

    return asyncio.run(
        main([url])
    )


# ============================================================
# 原逻辑：
# Get all the blackhat speech sessions
# ============================================================

def get_All_Sessions(
    Area_With_Date
):

    TopicURL = []

    # --------------------------------------------
    # 原来：
    #
    # https://www.blackhat.com/US-25/...
    #
    # 现在统一小写 + blackhat.com
    #
    # US-25 如果归档，
    # 浏览器会自动跟随 redirect。
    # --------------------------------------------

    url = (
        f"https://blackhat.com/"
        f"{Area_With_Date.lower()}/"
        f"briefings/schedule/index.html"
    )

    print()
    print(
        "=" * 60
    )

    print(
        f"[TARGET] {Area_With_Date}"
    )

    print(
        f"[SCHEDULE] {url}"
    )

    print(
        "=" * 60
    )

    response = browser_get(
        url
    )

    if (
        not response
        or not response[0]
    ):

        print(
            f"[ERROR] Unable to get schedule "
            f"for {Area_With_Date}"
        )

        return []

    html = response[0]

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    # ========================================================
    # 第一种：
    #
    # 完全保留你原来的 DOM
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

        for i in main_li:

            anchors = i.find_all(
                "a",
                href=True
            )

            for x in anchors:

                href = (
                    x.get(
                        "href",
                        ""
                    )
                    .strip()
                )

                if not href:

                    continue

                if (
                    href.startswith("#")
                    and
                    "speakers"
                    not in href.lower()
                ):

                    TopicURL.append(
                        url + href
                    )

    # ========================================================
    # 第二种：
    #
    # DOM class/id 变化时，
    # 仍然按照原来的 "#session-xxxxx-ID"
    # 原理找 session。
    # ========================================================

    if not TopicURL:

        print(
            "[INFO] Old schedule DOM not found, "
            "searching session anchors..."
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

            # ----------------------------------------
            # 原 Black Hat session 格式：
            #
            # #apple-storm-xxxxx-12345
            # ----------------------------------------

            if (
                href.startswith("#")
                and
                "speaker"
                not in href.lower()
                and
                re.search(
                    r"-\d+$",
                    href
                )
            ):

                TopicURL.append(
                    url + href
                )

    # ========================================================
    # 第三种：
    #
    # 有些 session 链接藏在 HTML / JS 内容里面，
    # 但还是原来的 #xxxx-ID 结构。
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
                not in fragment.lower()
            ):

                TopicURL.append(
                    url + fragment
                )

    # 去重
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
# 原逻辑：
# Sort all the pdf file link
# ============================================================

def sort_PDF(
    Area_With_Date
):

    TopicURL = get_All_Sessions(
        Area_With_Date
    )

    All_PDF = []

    print(
        f"[INFO] Processing "
        f"{len(TopicURL)} sessions"
    )

    for index, url in enumerate(
        TopicURL,
        start=1
    ):

        print()
        print(
            f"[SESSION "
            f"{index}/"
            f"{len(TopicURL)}]"
        )

        print(url)

        # 原来的 cleanup 保留
        kill_child_processes(
            os.getpid()
        )

        response = browser_get(
            url
        )

        if (
            not response
            or not response[0]
        ):

            print(
                "[WARN] Session page empty"
            )

            continue

        soup = BeautifulSoup(
            response[0],
            "lxml"
        )

        # ====================================================
        # 第一优先：
        # 完全保留你原来的 bhpresentation
        # ====================================================

        div = soup.find(
            "div",
            class_="bhpresentation"
        )

        session_pdfs = []

        if div:

            main_div = div.find_all(
                "a",
                href=True
            )

            for a in main_div:

                href = (
                    a.get(
                        "href",
                        ""
                    )
                    .strip()
                )

                if (
                    ".pdf"
                    in href.lower()
                ):

                    href = urljoin(
                        url,
                        href
                    )

                    session_pdfs.append(
                        href
                    )

        # ====================================================
        # bhpresentation 名字变化时
        #
        # 仍然是在当前 session 页面找 PDF，
        # 爬虫流程没有改变。
        # ====================================================

        if not session_pdfs:

            pdf_links = soup.find_all(
                "a",
                href=re.compile(
                    r"\.pdf(?:\?|$)",
                    re.I
                )
            )

            for a in pdf_links:

                href = (
                    a.get(
                        "href",
                        ""
                    )
                    .strip()
                )

                if href:

                    href = urljoin(
                        url,
                        href
                    )

                    session_pdfs.append(
                        href
                    )

        session_pdfs = list(
            dict.fromkeys(
                session_pdfs
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

    return list(
        dict.fromkeys(
            All_PDF
        )
    )


# ============================================================
# 原逻辑：
# wget 下载
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

    subprocess.call(
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

    print(
        "[INFO] Targets: "
        + ", ".join(Targets)
    )

    for Area_With_Date in Targets:

        print()
        print(
            "#" * 70
        )

        print(
            f"# START: {Area_With_Date}"
        )

        print(
            "#" * 70
        )

        All_pdf = sort_PDF(
            Area_With_Date
        )

        print()
        print(
            f"[RESULT] {Area_With_Date}: "
            f"{len(All_pdf)} PDF files"
        )

        if not All_pdf:

            print(
                f"[WARN] No PDFs found "
                f"for {Area_With_Date}"
            )

            continue

        tp = ThreadPool(30)

        jobs = [
            (
                Area_With_Date,
                pdf
            )
            for pdf in All_pdf
        ]

        tp.map(
            download_PDF,
            jobs
        )

        tp.close()

        tp.join()

    print()
    print(
        "=" * 70
    )

    print(
        "DONE"
    )

    print(
        "=" * 70
    )
