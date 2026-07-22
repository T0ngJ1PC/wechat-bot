import os
import uuid
import json
import hashlib
import hmac
import time
import requests
from flask import Flask, jsonify, request, make_response, send_file, send_from_directory
from wechatpy import parse_message, create_reply
from wechatpy.crypto import WeChatCrypto

app = Flask(__name__)

TOKEN = os.getenv('WX_TOKEN', '')
APP_ID = os.getenv('WX_APPID', '')
AES_KEY = os.getenv('WX_AES_KEY', '')
BASE_URL = os.getenv('BASE_URL', '').rstrip('/')
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')

STICKER_DIR = os.getenv('STICKER_DIR', '/opt/wechat-bot/stickers')
PENDING_FILE = os.getenv('PENDING_FILE', '/opt/wechat-bot/pending_unsupported.jsonl')
MP_QR_FILE = os.getenv('MP_QR_FILE', '/opt/wechat-bot/mp_login_qr.png')
MP_STATUS_FILE = os.getenv('MP_STATUS_FILE', '/opt/wechat-bot/mp_scraper_status.json')
os.makedirs(STICKER_DIR, exist_ok=True)

crypto = WeChatCrypto(TOKEN, AES_KEY, APP_ID) if AES_KEY and APP_ID else None

def check_sig(signature, timestamp, nonce):
    if not signature or not timestamp or not nonce:
        return False
    sort_list = sorted([TOKEN, timestamp, nonce])
    sha1 = hashlib.sha1(''.join(sort_list).encode()).hexdigest()
    return hmac.compare_digest(sha1, signature)

def text_response(text, status=200):
    resp = make_response(text, status)
    resp.content_type = 'text/plain; charset=utf-8'
    return resp

def admin_allowed():
    token = request.args.get('token', '') or request.headers.get('X-Admin-Token', '')
    return bool(ADMIN_TOKEN) and hmac.compare_digest(token, ADMIN_TOKEN)

def xml_response(xml, status=200):
    resp = make_response(xml, status)
    resp.content_type = 'application/xml; charset=utf-8'
    return resp

def is_encrypted_request():
    return request.args.get('encrypt_type', '').lower() == 'aes'

def sticker_base_url():
    if BASE_URL:
        return BASE_URL
    return request.host_url.rstrip('/') + '/stickers'

def xml_field(raw_xml, name):
    start = b'<' + name.encode() + b'>'
    end = b'</' + name.encode() + b'>'
    if start not in raw_xml or end not in raw_xml:
        return ''
    value = raw_xml.split(start, 1)[1].split(end, 1)[0]
    if value.startswith(b'<![CDATA[') and value.endswith(b']]>'):
        value = value[9:-3]
    return value.decode('utf-8', errors='replace')

def append_pending_unsupported(raw_xml, content):
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    item = {
        'created_at': int(time.time()),
        'from_user': xml_field(raw_xml, 'FromUserName'),
        'to_user': xml_field(raw_xml, 'ToUserName'),
        'msg_id': xml_field(raw_xml, 'MsgId'),
        'content': content,
    }
    with open(PENDING_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(item, ensure_ascii=False) + '\n')
    print('Pending unsupported message:', item, flush=True)

def download_and_save(url):
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').lower()
        if 'gif' in content_type:
            ext = '.gif'
        elif 'png' in content_type:
            ext = '.png'
        elif 'webp' in content_type:
            ext = '.webp'
        else:
            ext = '.jpg'
        filename = uuid.uuid4().hex + ext
        filepath = os.path.join(STICKER_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(resp.content)
        return sticker_base_url() + '/' + filename
    except Exception as e:
        print('Download error:', e)
        return None

@app.route('/', methods=['GET'])
def index():
    return text_response('wechat-bot ok')

@app.route('/api/count', methods=['POST'])
def api_count():
    return {'ok': True}

@app.route('/admin/mp-qr', methods=['GET'])
def mp_login_qr():
    if not admin_allowed():
        return text_response('', 404)
    if not os.path.exists(MP_QR_FILE):
        return text_response('QR screenshot is not ready', 404)
    return send_file(MP_QR_FILE, mimetype='image/png')

@app.route('/admin/mp-status', methods=['GET'])
def mp_scraper_status():
    if not admin_allowed():
        return text_response('', 404)
    data = {'ok': True, 'qr_exists': os.path.exists(MP_QR_FILE)}
    if os.path.exists(MP_STATUS_FILE):
        with open(MP_STATUS_FILE, 'r', encoding='utf-8') as f:
            data.update(json.load(f))
    return jsonify(data)

@app.route('/stickers/<path:filename>', methods=['GET'])
def sticker_file(filename):
    return send_from_directory(STICKER_DIR, filename)

@app.route('/wechat', methods=['GET'])
def verify():
    signature = request.args.get('signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    echostr = request.args.get('echostr', '')
    print(
        f'VERIFY: path={request.path} sig={signature} ts={timestamp} '
        f'nonce={nonce} echo={echostr} ip={request.headers.get("X-Real-IP", request.remote_addr)}',
        flush=True,
    )
    if check_sig(signature, timestamp, nonce):
        print('VERIFY OK', flush=True)
        return text_response(echostr)
    print('VERIFY FAIL', flush=True)
    return text_response('', 403)

@app.route('/', methods=['POST'])
@app.route('/wechat', methods=['POST'])
def handle_message():
    signature = request.args.get('signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    encrypt_type = request.args.get('encrypt_type', '').lower()
    raw_msg = request.data

    print(
        f'POST: path={request.path} encrypt_type={encrypt_type} sig={signature} '
        f'msg_sig={request.args.get("msg_signature", "")} ts={timestamp} nonce={nonce}',
        flush=True,
    )

    if is_encrypted_request():
        if crypto is None:
            print('DECRYPT FAIL: missing WX_AES_KEY or WX_APPID', flush=True)
            return text_response('', 500)
        try:
            raw_msg = crypto.decrypt_message(
                raw_msg,
                request.args.get('msg_signature', ''),
                timestamp,
                nonce,
            )
            print('DECRYPT OK', flush=True)
        except Exception as e:
            print('DECRYPT FAIL:', e, flush=True)
            return text_response('', 403)
    elif not check_sig(signature, timestamp, nonce):
        print('POST VERIFY FAIL', flush=True)
        return text_response('', 403)

    print('=== Raw XML ===', flush=True)
    print(raw_msg, flush=True)
    parsed = parse_message(raw_msg)
    print('=== MsgType ===', parsed.type, flush=True)

    if parsed.type in ('image', 'emoji'):
        pic_url = getattr(parsed, 'image', None) or getattr(parsed, 'picurl', None)
        print(f'PicUrl: {pic_url}', flush=True)
        if pic_url:
            link = download_and_save(pic_url)
            if link:
                reply_text = '表情包已保存！\n下载链接：' + link + '\n长按链接可复制'
            else:
                reply_text = '保存失败，请稍后重试~'
        else:
            reply_text = '未能获取表情包，请重新发送~'
    else:
        content = getattr(parsed, 'content', '') or ''
        print('TextContent:', content, flush=True)
        if '收到不支持的消息类型' in content:
            append_pending_unsupported(raw_msg, content)
            reply_text = (
                '收到收藏表情，后台正在尝试从公众号私信页面抓取原图。\n'
                '如果抓取成功，会再发送下载链接。'
            )
        else:
            reply_text = (
                '请把微信表情包直接发给我，我会帮你保存。\n'
                '如果是收藏的表情，系统会尝试自动抓取并返回下载链接。'
            )

    reply = create_reply(reply_text, parsed)
    reply_xml = reply.render()
    if is_encrypted_request():
        reply_xml = crypto.encrypt_message(reply_xml, nonce, timestamp)
        print('ENCRYPT REPLY OK', flush=True)
    return xml_response(reply_xml)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
