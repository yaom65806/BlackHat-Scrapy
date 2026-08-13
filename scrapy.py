import asyncio, re, signal, psutil, os, subprocess
from sys import argv
from pyppeteer import launch
from bs4 import BeautifulSoup
from multiprocessing.pool import ThreadPool

async def main(target):
    _return = []
    browser = await launch(options={'args': ['--no-sandbox'] , 'dumpio':True, 'autoClose':False } )
    page = await browser.newPage()
    for url in target:
        await page.goto(url)
        #Need to wait
        await asyncio.sleep(1)
        content = await page.content()
        _return.append(content)
    await browser.close()
    return _return

#Kill parent process with chromium
def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for process in children:
        process.send_signal(sig)


#Get all the blackhat speech sessions
def get_All_Sessions(Area_With_Date):
    TopicURL = []

    url = f"https://www.blackhat.com/{Area_With_Date}/briefings/schedule/index.html"

    response = asyncio.get_event_loop().run_until_complete(main([url]))
    soup = BeautifulSoup(response[0], "lxml")

    # 不再依赖旧版的 ul#cal_content_Day
    links = soup.find_all("a", href=True)

    for link in links:
        href = link.get("href", "").strip()

        # Black Hat session 使用 #session-name-12345 形式
        if (
            href.startswith("#")
            and href != "#"
            and "speakers" not in href.lower()
            and re.search(r"-\d+$", href)
        ):
            TopicURL.append(url + href)

    TopicURL = list(set(TopicURL))

    print(f"Found {len(TopicURL)} sessions")

    return TopicURL
        

#Sort all the pdf file link
def sort_PDF():
    TopicURL = get_All_Sessions(Area_With_Date="US-25")
    All_PDF=[]
    for url in TopicURL:
        kill_child_processes(os.getpid())
        response = asyncio.get_event_loop().run_until_complete(main([url]))
        soup = BeautifulSoup(response[0],'lxml')
        print(url)
        div = soup.find('div', class_="bhpresentation")
       
        if not div:
            continue
        main_div = div.find_all('a')
        try:
            All_PDF.append(main_div[0]['href'].strip())
        except:
            pass
    return list(set(All_PDF))
    
#Download pdf file
def download_PDF(PDF):
    currentDir = os.getcwd()
    subprocess.call(['wget', '--no-check-certificate', '-t 1', '-T 10' ,'-P', currentDir + '/save', PDF], cwd=currentDir)

tp = ThreadPool(30)
All_pdf = sort_PDF()
_return = tp.map(download_PDF, (All_pdf))
