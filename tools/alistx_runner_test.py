# -*- coding: utf-8 -*-
"""在 GitHub runner 里把 alistx 跑起来，挂 /无限云盘(WxYunPan) + /ydy2(139 personal_new)，
然后量两条腿：alist->139 的 PUT 上传、WxYunPan CDN -> alist 的下载。

环境变量：
  WXYP_ACCOUNTS     [{"account":..,"password":..}]      # 无限云盘
  YDY_AUTHORIZATION 139 授权串
  ALIST_BIN         默认 ./alist
  ALIST_DATA        默认 /tmp/alistdata
  ALIST_PASS        默认随机固定串
  SRC_MB            本轮测试多少 MiB（默认 128）
"""
import base64
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BIN = os.environ.get('ALIST_BIN', './alist')
DATA = os.environ.get('ALIST_DATA', '/tmp/alistdata')
PASS = os.environ.get('ALIST_PASS', 'runnerpw123')
BASE = 'http://127.0.0.1:5244'
SRC_MB = int(os.environ.get('SRC_MB', '128'))
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(m):
    print('%s %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def wait_up(timeout=90):
    for _ in range(timeout):
        try:
            with OP.open(BASE + '/api/public/settings', timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def req(path, payload=None, method='POST', headers=None, data=None, timeout=600):
    h = {'Content-Type': 'application/json'}
    if headers:
        h.update(headers)
    body = data if data is not None else (json.dumps(payload).encode() if payload is not None else None)
    r = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    try:
        with OP.open(r, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return {'code': e.code, 'message': 'HTTP %s %s' % (e.code, e.read()[:200])}
    try:
        return json.loads(raw.decode())
    except Exception:
        return {'code': -1, 'message': raw[:200].decode('utf-8', 'replace')}


def main():
    os.makedirs(DATA, exist_ok=True)
    log('设置 admin 密码')
    r = subprocess.run([BIN, 'admin', 'set', PASS, '--data', DATA], capture_output=True, text=True, timeout=300)
    log('  admin set rc=%s %s' % (r.returncode, (r.stdout or r.stderr or '').strip()[:120]))

    log('启动 alistx')
    proc = subprocess.Popen([BIN, 'server', '--data', DATA],
                            stdout=open('/tmp/alist.log', 'w'), stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            log('!! alist 起不来，日志尾部：')
            print(open('/tmp/alist.log').read()[-1500:], flush=True)
            raise SystemExit(1)
        log('alistx 就绪 %s' % req('/api/public/settings', method='GET').get('data', {}).get('version', '?'))

        j = req('/api/auth/login', {'username': 'admin', 'password': PASS})
        tok = (j.get('data') or {}).get('token')
        if not tok:
            raise SystemExit('登录失败: %s' % str(j)[:200])
        AUTH = {'Authorization': tok}
        log('登录 ok token=%s…' % tok[:18])

        accounts = json.loads(os.environ.get('WXYP_ACCOUNTS', '[]'))
        ydy_auth = os.environ.get('YDY_AUTHORIZATION', '').strip()
        if not accounts or not ydy_auth:
            raise SystemExit('缺 WXYP_ACCOUNTS / YDY_AUTHORIZATION')

        storages = [
            {'mount_path': '/无限云盘', 'order': 0, 'driver': 'WxYunPan', 'cache_expiration': 0,
             'status': '', 'remark': 'runner test', 'disabled': False,
             'addition': json.dumps({'account': accounts[0]['account'], 'password': accounts[0]['password'],
                                     'session_key': '', 'root_folder': '0'}, ensure_ascii=False)},
            {'mount_path': '/ydy2', 'order': 1, 'driver': '139Yun', 'cache_expiration': 0,
             'status': '', 'remark': 'runner test', 'disabled': False,
             'addition': json.dumps({'authorization': ydy_auth, 'type': 'personal_new',
                                     'cloud_id': '', 'custom_upload_part_size': 0,
                                     'report_real_size': False, 'use_large_thumbnail': False,
                                     'root_folder_id': '/'}, ensure_ascii=False)},
        ]
        for s in storages:
            st = req('/api/admin/storage/create', s, headers=AUTH)
            log('建存储 %-8s -> code=%s %s' % (s['mount_path'], st.get('code'), str(st.get('message'))[:80]))

        for p in ('/无限云盘', '/ydy2', '/ydy2/老'):
            L = req('/api/fs/list', {'path': p, 'page': 1, 'per_page': 0, 'refresh': True}, headers=AUTH)
            items = ((L.get('data') or {}).get('content') or [])
            log('列 %-9s code=%s 项=%d %s' % (p, L.get('code'), len(items), str(L.get('message'))[:60]))
            for it in items[:3]:
                log('    %-40s dir=%s size=%s' % ((it.get('name') or '')[:40], it.get('is_dir'), it.get('size')))

        # ---------- leg A: runner -> alist -> 139 ----------
        mb = SRC_MB
        tmp = os.path.join(tempfile.gettempdir(), 'alist_up_%d.bin' % mb)
        blk = os.urandom(4 * 1024 * 1024)
        with open(tmp, 'wb') as f:
            left = mb * 1024 * 1024
            while left > 0:
                f.write(blk[:min(len(blk), left)])
                left -= min(len(blk), left)
        size = os.path.getsize(tmp)
        name = 'alist_runner_%s.bin' % ''.join(random.choice(string.digits) for _ in range(6))
        remote = '/ydy2/老/' + name
        log('leg A 上传 %d MiB -> %s' % (mb, remote))
        t0 = time.time()
        with open(tmp, 'rb') as body:
            st = req('/api/fs/put', data=body, method='PUT', timeout=5400,
                     headers={'Authorization': tok,
                              'File-Path': urllib.parse.quote(remote),
                              'Content-Type': 'application/octet-stream',
                              'Content-Length': str(size)})
        dt = time.time() - t0
        up = size / 1048576.0 / max(dt, 1e-6)
        log('leg A 结果: code=%s %s | %.1f MiB / %.1fs = %.2f MiB/s'
            % (st.get('code'), str(st.get('message'))[:60], size / 1048576.0, dt, up))

        # ---------- leg B: WxYunPan CDN -> runner ----------
        L = req('/api/fs/list', {'path': '/无限云盘/根目录', 'page': 1, 'per_page': 0, 'refresh': True}, headers=AUTH)
        items = [i for i in ((L.get('data') or {}).get('content') or []) if not i.get('is_dir')]
        items.sort(key=lambda x: -(x.get('size') or 0))
        dl = 0.0
        if items:
            pick = items[0]
            path = '/无限云盘/根目录/' + pick['name']
            g = req('/api/fs/get', {'path': path, 'refresh': True}, headers=AUTH)
            raw = (g.get('data') or {}).get('raw_url')
            log('leg B 源 %s (%s MiB)' % (pick['name'][:50], (pick.get('size') or 0) // 1048576))
            if raw:
                span = min(SRC_MB * 1024 * 1024, pick.get('size') or 0)
                t0 = time.time()
                got = 0
                try:
                    rq = urllib.request.Request(raw, headers={'User-Agent': 'Mozilla/5.0',
                                                             'Range': 'bytes=0-%d' % (span - 1)})
                    with OP.open(rq, timeout=600) as r:
                        while got < span:
                            b = r.read(min(4 * 1024 * 1024, span - got))
                            if not b:
                                break
                            got += len(b)
                    dt = time.time() - t0
                    dl = got / 1048576.0 / max(dt, 1e-6)
                    log('leg B 结果: %d MiB / %.1fs = %.2f MiB/s' % (got // 1048576, dt, dl))
                except urllib.error.HTTPError as e:
                    log('leg B HTTP %s' % e.code)
            else:
                log('leg B 拿不到 raw_url: %s' % str(g)[:150])
        else:
            log('leg B 跳过：/无限云盘/根目录 没列到文件')

        # 清理
        t = req('/api/fs/remove', {'dir': '/ydy2/老', 'names': [name]}, headers=AUTH)
        log('清理 %s -> code=%s' % (name, t.get('code')))
        os.remove(tmp)
        log('RESULT {"up_mibps": %.2f, "dl_mibps": %.2f, "mb": %d}' % (up, dl, mb))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            tail = open('/tmp/alist.log').read()[-800:]
            if tail:
                log('alist 日志尾部: %s' % tail.replace('\n', ' | ')[-600:])
        except Exception:
            pass


if __name__ == '__main__':
    main()
