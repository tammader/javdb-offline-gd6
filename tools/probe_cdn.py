# -*- coding: utf-8 -*-
"""runner 侧探针 v3：扫描 dl-a10b-* 节点，找没被封的可用节点（同签名）。"""
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
pu = urlparse(u)
print('原节点:', pu.netloc)


def probe(host, timeout=12):
    u2 = urlunparse((pu.scheme, host, pu.path, pu.params, pu.query, pu.fragment))
    try:
        r = urllib.request.urlopen(urllib.request.Request(u2, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-63'}), timeout=timeout)
        return r.status
    except urllib.error.HTTPError as e:
        return 'HTTP%s' % e.code
    except Exception as e:
        return 'EXC'


good = []
counts = {}
for n in list(range(840, 891)):
    h = 'dl-a10b-%04d.mypikpak.com' % n
    st = probe(h)
    counts[st] = counts.get(st, 0) + 1
    if st in (200, 206):
        good.append(h)
        print('  ✅ %s -> %s' % (h, st))
print('节点扫描结果统计:', counts)
print('可用节点:', good)

# 再测一次原节点（看是否为瞬时）
print('原节点复测:', probe(pu.netloc))
