import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(os.getenv('BOT_DIR', '/opt/wechat-bot'))
STICKER_DIR = Path(os.getenv('STICKER_DIR', str(BASE_DIR / 'stickers')))
BASE_URL = os.getenv('BASE_URL', '').rstrip('/')
STATE_FILE = Path(os.getenv('MP_STATE_FILE', str(BASE_DIR / 'mp_state.json')))
QR_FILE = Path(os.getenv('MP_QR_FILE', str(BASE_DIR / 'mp_login_qr.png')))
STATUS_FILE = Path(os.getenv('MP_STATUS_FILE', str(BASE_DIR / 'mp_scraper_status.json')))
PENDING_FILE = Path(os.getenv('PENDING_FILE', str(BASE_DIR / 'pending_unsupported.jsonl')))
SCRAPER_STATE_FILE = Path(os.getenv('MP_SCRAPER_STATE_FILE', str(BASE_DIR / 'mp_scraper_state.json')))
POLL_SECONDS = int(os.getenv('MP_SCRAPER_POLL_SECONDS', '20'))
CLEANUP_INTERVAL_SECONDS = int(os.getenv('MP_CLEANUP_INTERVAL_SECONDS', '600'))
DELIVERED_PENDING_KEEP_SECONDS = int(os.getenv('MP_DELIVERED_PENDING_KEEP_SECONDS', '600'))
STATE_HISTORY_LIMIT = int(os.getenv('MP_STATE_HISTORY_LIMIT', '500'))


def now():
    return int(time.time())


def write_status(**kwargs):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    data.update(kwargs)
    data['updated_at'] = now()
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def source_key_from_url(url):
    if url.startswith('//'):
        url = 'https:' + url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.netloc == 'mp.weixin.qq.com' and parsed.path == '/cgi-bin/getimgdata':
        parts = ['getimgdata']
        for key in ['msgid', 'fileId', 'ow', 'source', 'mode']:
            value = query.get(key, [''])[0]
            if value:
                parts.append(f'{key}={value}')
        return '|'.join(parts)

    query_parts = []
    for key in sorted(query):
        if key in {'token', 'lang'}:
            continue
        for value in query[key]:
            query_parts.append(f'{key}={value}')
    base = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
    return base + ('?' + '&'.join(query_parts) if query_parts else '')


def launch_context(pw, headless=True, state=True):
    browser = pw.chromium.launch(
        headless=headless,
        args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    )
    kwargs = {
        'viewport': {'width': 1440, 'height': 980},
        'locale': 'zh-CN',
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        ),
    }
    if state and STATE_FILE.exists():
        kwargs['storage_state'] = str(STATE_FILE)
    context = browser.new_context(**kwargs)
    return browser, context


def is_logged_in(page):
    url = page.url
    if 'mp.weixin.qq.com/cgi-bin/' in url and 'token=' in url and 'login' not in url:
        return True
    try:
        if page.get_by_text('首页', exact=True).count() > 0 and 'login' not in url:
            return True
    except Exception:
        return False
    return False


def login(timeout_seconds):
    QR_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser, context = launch_context(pw, headless=True, state=False)
        page = context.new_page()
        page.goto('https://mp.weixin.qq.com/', wait_until='domcontentloaded', timeout=60000)
        deadline = time.time() + timeout_seconds
        write_status(login='waiting_for_scan', qr_file=str(QR_FILE))
        while time.time() < deadline:
            page.screenshot(path=str(QR_FILE), full_page=True)
            if is_logged_in(page):
                context.storage_state(path=str(STATE_FILE))
                write_status(login='ok', state_file=str(STATE_FILE), current_url=page.url)
                browser.close()
                return True
            page.wait_for_timeout(3000)
        write_status(login='timeout', current_url=page.url)
        browser.close()
        return False


def get_mp_token(page):
    parsed = urlparse(page.url)
    token = parse_qs(parsed.query).get('token', [''])[0]
    if token:
        return token
    match = re.search(r'token=(\d+)', page.content())
    return match.group(1) if match else ''


def goto_private_messages(page):
    page.goto('https://mp.weixin.qq.com/', wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(3000)
    if not is_logged_in(page):
        page.screenshot(path=str(QR_FILE), full_page=True)
        raise RuntimeError('mp.weixin.qq.com login is required')

    token = get_mp_token(page)
    candidates = []
    if token:
        candidates.extend([
            f'https://mp.weixin.qq.com/cgi-bin/message?t=message/list&count=20&day=7&token={token}&lang=zh_CN',
            f'https://mp.weixin.qq.com/cgi-bin/message?action=list&t=message/list&token={token}&lang=zh_CN',
        ])
    candidates.append('https://mp.weixin.qq.com/')

    for url in candidates:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(5000)
        if 'message' in page.url or has_private_message_ui(page):
            return

    click_texts = ['互动管理', '私信', '近期私信']
    for text in click_texts:
        try:
            locator = page.get_by_text(text, exact=True)
            if locator.count() > 0:
                locator.first.click(timeout=5000)
                page.wait_for_timeout(5000)
        except Exception:
            pass


def has_private_message_ui(page):
    for text in ['近期私信', '已收藏私信', '私信设置', '回复私信']:
        try:
            if page.get_by_text(text, exact=True).count() > 0:
                return True
        except Exception:
            pass
    return False


def is_candidate_image(item):
    url = item.get('url', '')
    if not url.startswith(('http://', 'https://', '//')):
        return False
    if url.startswith('//'):
        url = 'https:' + url
        item['url'] = url
    if 'mp.weixin.qq.com/cgi-bin/getimgdata?' in url and 'msgid=' in url:
        width = int(item.get('naturalWidth') or item.get('width') or 0)
        height = int(item.get('naturalHeight') or item.get('height') or 0)
        return width >= 40 and height >= 40
    blocked = ['res.wx.qq.com', 'wx.qlogo.cn', 'mmbiz.qpic.cn/mmbiz_png/Vjdp4', 'emoji.qpic.cn']
    if any(part in url for part in blocked):
        return False
    width = int(item.get('naturalWidth') or item.get('width') or 0)
    height = int(item.get('naturalHeight') or item.get('height') or 0)
    if width < 60 or height < 60:
        return False
    return any(part in url for part in ['mmbiz.qpic.cn', 'mmbiz.qlogo.cn', 'qpic.cn', 'wx_fmt='])


def collect_dom_images(page):
    script = """
    () => Array.from(document.images).map(img => ({
      url: img.currentSrc || img.src,
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      width: img.width,
      height: img.height,
      top: img.getBoundingClientRect().top,
      bottom: img.getBoundingClientRect().bottom,
      left: img.getBoundingClientRect().left,
      className: String(img.className || ''),
      alt: img.alt || ''
    }))
    """
    try:
        items = page.evaluate(script)
    except Exception:
        return []
    return [item for item in items if is_candidate_image(item)]


def candidate_sort_key(item):
    bottom = item.get('bottom')
    top = item.get('top')
    left = item.get('left')
    try:
        y = float(bottom if bottom is not None else top if top is not None else -1)
    except (TypeError, ValueError):
        y = -1
    try:
        x = float(left if left is not None else 0)
    except (TypeError, ValueError):
        x = 0
    return y, x


def source_sequence(item):
    source_key = item.get('source_key', '')
    match = re.search(r'msgid=(\d+)', source_key)
    if match:
        return int(match.group(1)), candidate_sort_key(item)
    return 0, candidate_sort_key(item)


def extension_from_content_type(content_type, url, body=b''):
    if body.startswith(b'GIF87a') or body.startswith(b'GIF89a'):
        return '.gif'
    if body.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if body.startswith(b'RIFF') and body[8:12] == b'WEBP':
        return '.webp'
    if body.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    content_type = (content_type or '').lower()
    if 'gif' in content_type:
        return '.gif'
    if 'png' in content_type:
        return '.png'
    if 'webp' in content_type:
        return '.webp'
    if 'jpeg' in content_type or 'jpg' in content_type:
        return '.jpg'
    if 'gif' in url:
        return '.gif'
    if 'png' in url:
        return '.png'
    if 'webp' in url:
        return '.webp'
    return '.jpg'


def build_requests_session(context):
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://mp.weixin.qq.com/',
    })
    for cookie in context.cookies():
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain'), path=cookie.get('path', '/'))
    return session


def download_image(session, url, seen_hashes):
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    body = resp.content
    if len(body) < 2048:
        return None
    digest = hashlib.sha1(body).hexdigest()
    ext = extension_from_content_type(resp.headers.get('Content-Type'), url, body)
    filename = digest[:32] + ext
    path = STICKER_DIR / filename
    if digest in seen_hashes:
        existing = sorted(STICKER_DIR.glob(digest[:32] + '.*'))
        if existing:
            filename = existing[0].name
            return {
                'sha1': digest,
                'filename': filename,
                'path': str(existing[0]),
                'url': BASE_URL + '/' + filename,
                'source_url': url,
                'size': existing[0].stat().st_size,
                'created_at': now(),
                'duplicate': True,
            }
    path.write_bytes(body)
    return {
        'sha1': digest,
        'filename': filename,
        'path': str(path),
        'url': BASE_URL + '/' + filename,
        'source_url': url,
        'size': len(body),
        'created_at': now(),
        'duplicate': False,
    }


def load_pending():
    if not PENDING_FILE.exists():
        return []
    items = []
    for line in PENDING_FILE.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            pass
    return items


def undelivered_pending(state):
    delivered = set(state.get('delivered_msg_ids', []))
    return [item for item in load_pending() if item.get('msg_id') not in delivered and item.get('from_user')]


def trim_list(values, limit):
    if limit <= 0 or len(values) <= limit:
        return values
    return values[-limit:]


def cleanup_processed(now_ts=None):
    now_ts = now() if now_ts is None else now_ts
    state = load_json(SCRAPER_STATE_FILE, {'seen_hashes': [], 'delivered_msg_ids': [], 'seen_sources': []})
    delivered = set(state.get('delivered_msg_ids', []))
    pending_items = load_pending()
    keep_items = []
    removed_ids = set()

    for item in pending_items:
        msg_id = item.get('msg_id')
        created_at = int(item.get('created_at') or 0)
        if msg_id in delivered and created_at and now_ts - created_at >= DELIVERED_PENDING_KEEP_SECONDS:
            removed_ids.add(msg_id)
            continue
        keep_items.append(item)

    if removed_ids:
        tmp_file = PENDING_FILE.with_suffix(PENDING_FILE.suffix + '.tmp')
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        with tmp_file.open('w', encoding='utf-8') as f:
            for item in keep_items:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
        tmp_file.replace(PENDING_FILE)

    live_pending_ids = {item.get('msg_id') for item in keep_items if item.get('msg_id')}
    state['delivered_msg_ids'] = trim_list(
        [msg_id for msg_id in state.get('delivered_msg_ids', []) if msg_id in live_pending_ids or msg_id not in removed_ids],
        STATE_HISTORY_LIMIT,
    )
    state['seen_hashes'] = trim_list(state.get('seen_hashes', []), STATE_HISTORY_LIMIT)
    state['seen_sources'] = trim_list(state.get('seen_sources', []), STATE_HISTORY_LIMIT)
    state['last_cleanup_at'] = now_ts
    save_json(SCRAPER_STATE_FILE, state)

    result = {
        'removed_pending': len(removed_ids),
        'pending_before': len(pending_items),
        'pending_after': len(keep_items),
        'last_cleanup_at': now_ts,
    }
    write_status(cleanup=result)
    return result


def send_kefu_text(openid, content):
    appid = os.getenv('WX_APPID', '')
    secret = os.getenv('WX_APPSECRET', '')
    if not appid or not secret:
        return {'ok': False, 'reason': 'missing WX_APPID or WX_APPSECRET'}

    cache_file = BASE_DIR / 'access_token_cache.json'
    cache = load_json(cache_file, {})
    token = cache.get('access_token')
    if not token or cache.get('expires_at', 0) < now() + 120:
        resp = requests.get(
            'https://api.weixin.qq.com/cgi-bin/token',
            params={'grant_type': 'client_credential', 'appid': appid, 'secret': secret},
            timeout=15,
        )
        data = resp.json()
        if 'access_token' not in data:
            return {'ok': False, 'reason': 'access_token_failed', 'response': data}
        token = data['access_token']
        save_json(cache_file, {'access_token': token, 'expires_at': now() + int(data.get('expires_in', 7200))})

    payload = {
        'touser': openid,
        'msgtype': 'text',
        'text': {'content': content},
    }
    resp = requests.post(
        'https://api.weixin.qq.com/cgi-bin/message/custom/send',
        params={'access_token': token},
        json=payload,
        timeout=15,
    )
    data = resp.json()
    return {'ok': data.get('errcode') == 0, 'response': data}


def send_mp_private_text(page, content):
    try:
        page.locator('.edit_area[contenteditable="true"]').last.click(timeout=5000)
        page.evaluate(
            """(content) => {
                const el = document.querySelector('.edit_area[contenteditable="true"]');
                if (!el) throw new Error('private message editor not found');
                el.focus();
                el.textContent = content;
                for (const name of ['beforeinput', 'input', 'keyup', 'change']) {
                    el.dispatchEvent(new Event(name, {bubbles: true, cancelable: true}));
                }
            }""",
            content,
        )
        page.wait_for_timeout(800)
        buttons = page.locator('button')
        clicked = False
        button_debug = []
        for index in range(buttons.count() - 1, -1, -1):
            button = buttons.nth(index)
            text = (button.inner_text(timeout=1000) or '').strip()
            if text != '发送':
                continue
            box = button.bounding_box()
            class_name = button.get_attribute('class') or ''
            button_debug.append({'index': index, 'text': text, 'box': box, 'class': class_name})
            if not box or box.get('width', 0) <= 0 or box.get('height', 0) <= 0:
                continue
            if 'disabled' in class_name:
                continue
            button.click(timeout=5000)
            clicked = True
            break
        if not clicked:
            return {'ok': False, 'method': 'mp_private_page', 'reason': 'send button not clickable', 'buttons': button_debug}
        page.wait_for_timeout(1500)
        return {'ok': True, 'method': 'mp_private_page'}
    except Exception as exc:
        return {'ok': False, 'method': 'mp_private_page', 'reason': str(exc)}


def deliver_if_pending(saved, page=None, pending_item=None):
    state = load_json(SCRAPER_STATE_FILE, {'seen_hashes': [], 'delivered_msg_ids': []})
    pending = undelivered_pending(state)
    if not pending:
        return None
    delivered = set(state.get('delivered_msg_ids', []))
    item = pending_item or pending[-1]
    text = '表情包已保存！\n下载链接：' + saved['url'] + '\n长按链接可复制'
    result = send_mp_private_text(page, text) if page is not None else {'ok': False, 'reason': 'missing page'}
    if not result.get('ok'):
        result = {'ok': False, 'ui_result': result, 'kefu_result': send_kefu_text(item['from_user'], text)}
    delivered_count = 0
    if result.get('ok'):
        delivered.add(item.get('msg_id'))
        delivered_count = 1
        state['delivered_msg_ids'] = sorted(delivered)
        save_json(SCRAPER_STATE_FILE, state)
    return {'pending': item, 'delivered_count': delivered_count, 'send_result': result}


def scan_once():
    state = load_json(SCRAPER_STATE_FILE, {'seen_hashes': [], 'delivered_msg_ids': [], 'seen_sources': []})
    seen_hashes = set(state.get('seen_hashes', []))
    seen_sources = set(state.get('seen_sources', []))
    response_images = []

    with sync_playwright() as pw:
        browser, context = launch_context(pw, headless=True, state=True)
        page = context.new_page()

        def on_response(response):
            try:
                if response.request.resource_type == 'image':
                    response_images.append({'url': response.url, 'naturalWidth': 100, 'naturalHeight': 100})
            except Exception:
                pass

        page.on('response', on_response)
        try:
            goto_private_messages(page)
            page.screenshot(path=str(BASE_DIR / 'mp_private_last.png'), full_page=True)
            dom_images = collect_dom_images(page)
            candidates = []
            seen_urls = set()
            for item in dom_images + response_images:
                if not is_candidate_image(item):
                    continue
                url = item['url']
                if url not in seen_urls:
                    seen_urls.add(url)
                    candidates.append(item)
            candidates.sort(key=candidate_sort_key)

            session = build_requests_session(context)
            saved_items = []
            candidate_results = []
            for item in candidates:
                try:
                    source_key = source_key_from_url(item['url'])
                    saved = download_image(session, item['url'], seen_hashes)
                    if saved:
                        saved['source_key'] = source_key
                        saved['new_source'] = source_key not in seen_sources
                        candidate_results.append(saved)
                        if saved.get('duplicate'):
                            continue
                        seen_hashes.add(saved['sha1'])
                        saved_items.append(saved)
                except Exception as exc:
                    print('download failed:', item.get('url'), exc, flush=True)

            pending = undelivered_pending(state)
            delivery = None
            deliveries = []
            if pending and candidate_results:
                fresh_results = sorted(
                    [item for item in candidate_results if item.get('new_source')],
                    key=source_sequence,
                )
                if fresh_results:
                    for pending_item, selected in zip(pending, fresh_results):
                        delivery = deliver_if_pending(selected, page=page, pending_item=pending_item)
                        deliveries.append(delivery)
                        if delivery and delivery.get('send_result', {}).get('ok'):
                            seen_sources.add(selected['source_key'])
                        else:
                            break
                else:
                    delivery = {
                        'pending': pending[-1],
                        'delivered_count': 0,
                        'send_result': {
                            'ok': False,
                            'method': 'mp_private_page',
                            'reason': 'waiting_for_new_private_image',
                        },
                    }
            elif not pending:
                for item in candidate_results:
                    if item.get('source_key'):
                        seen_sources.add(item['source_key'])

            state = load_json(SCRAPER_STATE_FILE, {'seen_hashes': [], 'delivered_msg_ids': [], 'seen_sources': []})
            state['seen_hashes'] = sorted(seen_hashes)
            state['seen_sources'] = sorted(seen_sources)
            state['last_scan_at'] = now()
            save_json(SCRAPER_STATE_FILE, state)
            write_status(
                login='ok',
                private_url=page.url,
                candidate_count=len(candidates),
                saved_count=len(saved_items),
                candidate_result_count=len(candidate_results),
                saved_items=saved_items[-5:],
                delivery=delivery,
                deliveries=deliveries,
            )
            browser.close()
            return saved_items
        except Exception as exc:
            page.screenshot(path=str(QR_FILE), full_page=True)
            write_status(login='error', error=str(exc), current_url=page.url)
            browser.close()
            raise


def watch():
    while True:
        try:
            saved = scan_once()
            print('scan saved:', len(saved), flush=True)
            state = load_json(SCRAPER_STATE_FILE, {})
            if now() - int(state.get('last_cleanup_at') or 0) >= CLEANUP_INTERVAL_SECONDS:
                cleanup = cleanup_processed()
                print('cleanup:', cleanup, flush=True)
        except Exception as exc:
            print('scan failed:', exc, flush=True)
        time.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    login_parser = sub.add_parser('login')
    login_parser.add_argument('--timeout', type=int, default=600)
    sub.add_parser('scan-once')
    sub.add_parser('cleanup')
    sub.add_parser('watch')
    args = parser.parse_args()

    if args.cmd == 'login':
        ok = login(args.timeout)
        raise SystemExit(0 if ok else 1)
    if args.cmd == 'scan-once':
        scan_once()
        return
    if args.cmd == 'cleanup':
        print(json.dumps(cleanup_processed(), ensure_ascii=False, indent=2))
        return
    if args.cmd == 'watch':
        watch()


if __name__ == '__main__':
    main()
