import os

from playwright.sync_api import sync_playwright

STATE = os.getenv('MP_STATE_FILE', '/opt/wechat-bot/mp_state.json')
URL = os.getenv('MP_PRIVATE_MESSAGE_URL', '')
if not URL:
    token = os.getenv('MP_PRIVATE_TOKEN', '')
    if not token:
        raise SystemExit('Set MP_PRIVATE_MESSAGE_URL or MP_PRIVATE_TOKEN before running.')
    URL = (
        'https://mp.weixin.qq.com/cgi-bin/message'
        f'?t=message/list&count=20&day=7&token={token}&lang=zh_CN'
    )

SCREENSHOT = os.getenv('MP_PRIVATE_SCREENSHOT', '/opt/wechat-bot/mp_private_after_send.png')

with sync_playwright() as pw:
    b=pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
    ctx=b.new_context(storage_state=STATE, viewport={'width':1440,'height':980}, locale='zh-CN')
    p=ctx.new_page(); p.goto(URL, wait_until='domcontentloaded', timeout=60000); p.wait_for_timeout(5000)
    p.screenshot(path=SCREENSHOT, full_page=True)
    print(p.url)
    b.close()
