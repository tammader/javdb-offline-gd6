# -*- coding: utf-8 -*-
"""在 GitHub runner 上实测 CDN 取链与下载（判断 403 是否 runner/IP 侧问题）。"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from wxyunpan import WxYunPan

ACCS = json.loads(os.environ.get('WXYP_ACCOUNTS', '[]'))
c = WxYunPan(ACCS[0]['account'], ACCS[0]['password'])
print('login:', c.login())

targets = []
for d in c.list_dir('0'):
    if str(d.get('type')) == '2':
        for f in c.list_dir(d.get('id')):
            nm = f.get('name') or ''
            if 'IPX-219' in nm or 'ABW-183' in nm or 'APNS-121' in nm:
                targets.append(f)

for f in targets:
    nm = f.get('name')
    u, err = c.prepare_play(f.get('fileId'), f.get('shareId') or '')
    print('\n===== %s | preparePlay err=%s' % (nm, err))
    if not u:
        continue
    print('    host=%s' % urlparse(u).netloc)
    for label, hdrs in [('裸', {}), ('UA', {'User-Agent': 'Mozilla/5.0'})]:
        try:
            rr = urllib.request.urlopen(urllib.request.Request(u, method='HEAD', headers=hdrs), timeout=30)
            print('   urllib HEAD %-4s -> %s CL=%s' % (label, rr.status, rr.headers.get('Content-Length')))
        except urllib.error.HTTPError as e:
            print('   urllib HEAD %-4s -> HTTP %s' % (label, e.code))
        except Exception as e:
            print('   urllib HEAD %-4s -> EXC %s' % (label, str(e)[:70]))
    for label, rng in [('0-63', '0-63'), ('mid', '4000000000-4000000063')]:
        try:
            rr = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=%s' % rng}), timeout=30)
            print('   urllib GET %-5s -> %s CR=%s' % (label, rr.status, rr.headers.get('Content-Range')))
        except urllib.error.HTTPError as e:
            print('   urllib GET %-5s -> HTTP %s' % (label, e.code))
    # curl 独立验证
    for label, args in [('curl HEAD', ['curl', '-sI', '-o', '/dev/null', '-w', '%{http_code}', u]),
                        ('curl range', ['curl', '-s', '-r', '0-63', '-o', '/dev/null', '-w', '%{http_code}', u])]:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=60)
            print('   %-11s -> %s' % (label, (r.stdout or '').strip()[:20]))
        except Exception as e:
            print('   %-11s -> EXC %s' % (label, str(e)[:60]))
