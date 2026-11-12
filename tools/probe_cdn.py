# -*- coding: utf-8 -*-
"""runner 侧探针 v2：a10b 各节点轮换 / App UA / proxy 参数。"""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from wxyunpan import WxYunPan

ACCS = json.loads(os.environ.get('WXYP_ACCOUNTS', '[]'))
c = WxYunPan(ACCS[0]['account'], ACCS[0]['password'])
print('login:', c.login())

t = None
for d in c.list_dir('0'):
    if str(d.get('type')) == '2':
        for f in c.list_dir(d.get('id')):
            if 'IPX-219' in (f.get('name') or ''):
                t = f
u, _ = c.prepare_play(t.get('fileId'), t.get('shareId') or '')
print('原节点:', urlparse(u).netloc)


def try_get(u2, hdrs, label):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u2, headers=hdrs), timeout=25)
        print('  %-42s -> %s %s' % (label, r.status, r.headers.get('Content-Range')))
        return True
    except urllib.error.HTTPError as e:
        print('  %-42s -> HTTP %s' % (label, e.code))
    except Exception as e:
        print('  %-42s -> EXC %s' % (label, str(e)[:40]))
    return False


pu = urlparse(u)
print('\n=== A) a10b 节点轮换（同签名） ===')
ok_hosts = []
for n in range(1055, 1070):
    h = 'dl-a10b-%04d.mypikpak.com' % n
    u2 = urlunparse((pu.scheme, h, pu.path, pu.params, pu.query, pu.fragment))
    if try_get(u2, {'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-63'}, h):
        ok_hosts.append(h)
print('可用节点:', ok_hosts)

print('\n=== B) 换 UA / 加头（原节点） ===')
for label, hdrs in [
    ('okhttp UA', {'User-Agent': 'okhttp/3.12.13', 'Range': 'bytes=0-63'}),
    ('Dart UA', {'User-Agent': 'Dart/3.5 (dart:io)', 'Range': 'bytes=0-63'}),
    ('+Referer', {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://mypikpak.com/', 'Range': 'bytes=0-63'}),
    ('+Origin', {'User-Agent': 'Mozilla/5.0', 'Origin': 'https://mypikpak.com', 'Range': 'bytes=0-63'}),
    ('bytes=0- (开区间)', {'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-'}),
]:
    try_get(u, hdrs, label)

print('\n=== C) preparePlay 带 proxy/line 等参数 ===')
for extra in [{'proxy': True}, {'proxy': 1}, {'multiThread': True}, {'threadCount': 3}, {'autoSpeedTest': True}]:
    body = {'fileId': t.get('fileId'), 'shareId': t.get('shareId') or ''}
    body.update(extra)
    r = c._req('/magnet/preparePlay', 'POST', body)
    dd = r.get('data') or {}
    url2 = dd.get('webContentLink') or dd.get('defaultPlayUrl')
    print('  %-22s code=%s host=%s proxy=%s threadCount=%s' % (
        json.dumps(extra), r.get('code'), (urlparse(url2).netloc if url2 else None), dd.get('proxy'), dd.get('threadCount')))
