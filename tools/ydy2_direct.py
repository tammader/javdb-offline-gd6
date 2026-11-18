# -*- coding: utf-8 -*-
"""139 移动云盘 个人云(personal_new) 直传：EOS 分片 PUT，纯标准库（无 alist）。

协议照 alistx drivers/139（Go）实现：
  1. POST https://user-njs.yun.139.com/user/route/qryRoutePolicy    -> personal https 主机
  2. POST {host}/file/list  {"parentFileId":"/"}                    -> 目标目录 fileId
  3. POST {host}/file/create {contentHash,size,parentFileId,name,partInfos[:100]}
  4. PUT  {uploadUrl}   (Content-Type / Origin / Referer)
  5. >100 片：POST {host}/file/getUploadUrl 每 100 片一批
  6. POST {host}/file/complete {contentHash,fileId,uploadId}

实测要点：**EOS 拒绝乱序分片** —— 乱序 PUT 返回
  400 InvalidPartOrder "fail to get previous part hash context"
所以分片必须按 partNumber 升序串行；--workers 只作记录。

用法:
  python ydy2_direct.py --file PATH --dir 老 [--part-mb 100] [--name N] [--auth-file F]
  python ydy2_direct.py --gen-mb 512 --dir 老          # 造随机数据上传（测速用）
  python ydy2_direct.py --list /
"""
import argparse
import base64
import hashlib
import json
import os
import random
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROUTE_URL = 'https://user-njs.yun.139.com/user/route/qryRoutePolicy'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(m):
    print('%s %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def md5hex(s):
    return hashlib.md5(s.encode()).hexdigest()


def encode_uri_component(s):
    r = urllib.parse.quote(s, safe='')
    for a, b in (('%21', '!'), ('%27', "'"), ('%28', '('), ('%29', ')'), ('%2A', '*')):
        r = r.replace(a, b)
    return r


def cal_sign(body, ts, rand):
    b = encode_uri_component(body)
    b = ''.join(sorted(b))
    b = base64.b64encode(b.encode()).decode()
    return md5hex(md5hex(b) + md5hex(ts + ':' + rand)).upper()


def rand_str(n=16):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class Yun139(object):
    def __init__(self, authorization):
        self.auth = (authorization or '').strip()
        self.host = None
        try:
            acct = base64.b64decode(self.auth).decode('utf-8', 'replace').split(':')
            self.account = acct[1] if len(acct) > 1 else ''
        except Exception:
            self.account = ''

    def _headers(self, body):
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        rnd = rand_str()
        return {
            'Accept': 'application/json, text/plain, */*',
            'Authorization': 'Basic ' + self.auth,
            'Caller': 'web',
            'Cms-Device': 'default',
            'Content-Type': 'application/json',
            'Mcloud-Channel': '1000101',
            'Mcloud-Client': '10701',
            'Mcloud-Route': '001',
            'Mcloud-Sign': '%s,%s,%s' % (ts, rnd, cal_sign(body, ts, rnd)),
            'Mcloud-Version': '7.14.0',
            'User-Agent': UA,
            'x-DeviceInfo': '||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||',
            'x-huawei-channelSrc': '10000034',
            'x-inner-ntwk': '2',
            'x-m4c-caller': 'PC',
            'x-m4c-src': '10002',
            'x-SvcType': '1',
            'X-Yun-Api-Version': 'v1',
            'X-Yun-App-Channel': '10000034',
            'X-Yun-Channel-Source': '10000034',
            'X-Yun-Client-Info': '||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||',
            'X-Yun-Module-Type': '100',
            'X-Yun-Svc-Type': '1',
        }

    def post(self, url, payload, timeout=180):
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        req = urllib.request.Request(url, data=body.encode('utf-8'),
                                     headers=self._headers(body), method='POST')
        try:
            with OP.open(req, timeout=timeout) as r:
                j = json.loads(r.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            raise RuntimeError('HTTP %s %s' % (e.code, str(e.read()[:200])))
        if not j.get('success', True):
            raise RuntimeError('139 API: %s' % str(j.get('message'))[:150])
        return j

    def route(self):
        j = self.post(ROUTE_URL, {'userInfo': {'userType': 1, 'accountType': 1,
                                               'accountName': self.account},
                                  'modAddrType': 1})
        for p in (j.get('data') or {}).get('routePolicyList') or []:
            if p.get('modName') == 'personal':
                self.host = p.get('httpsUrl')
                break
        if not self.host:
            raise RuntimeError('没拿到 personal 主机（授权可能过期）')
        return self.host

    def list_dir(self, parent='/', page_size=100):
        out, cursor = [], ''
        for _ in range(200):
            j = self.post(self.host + '/file/list', {
                'imageThumbnailStyleList': ['Small', 'Large'],
                'orderBy': 'updated_at', 'orderDirection': 'DESC',
                'pageInfo': {'pageCursor': cursor, 'pageSize': page_size},
                'parentFileId': parent})
            d = j.get('data') or {}
            out += d.get('items') or []
            cursor = d.get('nextPageCursor') or ''
            if not cursor:
                break
        return out

    def resolve(self, path):
        segs = [s for s in (path or '').replace('\\', '/').split('/') if s]
        cur, fid = '/', None
        for s in segs:
            hit = [i for i in self.list_dir(cur)
                   if i.get('name') == s and i.get('type') == 'folder']
            if not hit:
                raise RuntimeError('目录不存在: %s（不在 %s 下）' % (s, cur))
            fid = hit[0]['fileId']
            cur = fid
        return fid or '/'

    def create(self, name, size, sha, parent_id, part_infos):
        return self.post(self.host + '/file/create', {
            'contentHash': sha, 'contentHashAlgorithm': 'SHA256',
            'contentType': 'application/octet-stream', 'parallelUpload': False,
            'partInfos': part_infos, 'size': size, 'parentFileId': parent_id,
            'name': name, 'type': 'file', 'fileRenameMode': 'auto_rename'})

    def more_urls(self, file_id, upload_id, part_infos):
        return self.post(self.host + '/file/getUploadUrl', {
            'fileId': file_id, 'uploadId': upload_id, 'partInfos': part_infos,
            'commonAccountInfo': {'account': self.account, 'accountType': 1}})

    def complete(self, sha, file_id, upload_id):
        return self.post(self.host + '/file/complete',
                         {'contentHash': sha, 'contentHashAlgorithm': 'SHA256',
                          'fileId': file_id, 'uploadId': upload_id})


def put_part(url, path, offset, length, tries=3):
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read(length)
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method='PUT', headers={
            'Content-Type': 'application/octet-stream',
            'Origin': 'https://yun.139.com',
            'Referer': 'https://yun.139.com/',
            'User-Agent': UA})
        t0 = time.time()
        try:
            with OP.open(req, timeout=1800) as r:
                if r.status == 200:
                    return time.time() - t0
                raise RuntimeError('part HTTP %s' % r.status)
        except urllib.error.HTTPError as e:
            body = ''
            try:
                body = e.read().decode('utf-8', 'replace')[:160]
            except Exception:
                pass
            if attempt == tries - 1:
                raise RuntimeError('HTTP %s %s' % (e.code, body))
            log('    片重试(%d/%d) HTTP %s' % (attempt + 1, tries, e.code))
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            if attempt == tries - 1:
                raise
            log('    片重试(%d/%d) %s' % (attempt + 1, tries, str(e)[:70]))
            time.sleep(3 * (attempt + 1))


def upload_file(c, local_path, name=None, dirname='老', part_mb=0, quiet=False):
    """上传一个本地文件到 139 个人云目录；返回 dict(bytes, seconds, mibps, name)。"""
    size = os.path.getsize(local_path)
    name = name or os.path.basename(local_path)
    part_mb = part_mb or (512 if size > 30 * 1024 ** 3 else 100)
    part = part_mb * 1024 * 1024
    nparts = max(1, (size + part - 1) // part)
    log('上传 %s  %.1f MiB  分片 %dMB x %d' % (name, size / 1048576.0, part_mb, nparts))

    t_hash = time.time()
    sha = sha256_file(local_path)
    log('SHA256 %s… (%.1fs)' % (sha[:20], time.time() - t_hash))

    parent = c.resolve(dirname)
    log('目标 %r fileId=%s' % (dirname, parent))

    infos = [{'partNumber': i + 1, 'partSize': min(part, size - i * part),
              'parallelHashCtx': {'partOffset': i * part}} for i in range(nparts)]
    r = c.create(name, size, sha, parent, infos[:100])
    d = r.get('data') or {}
    if d.get('exist'):
        log('云端已有同名同哈希文件（秒传），跳过上传')
        return {'bytes': 0, 'seconds': 0, 'mibps': 0, 'name': name, 'rapid': True}
    file_id, upload_id = d.get('fileId'), d.get('uploadId')
    urls = d.get('partInfos') or []
    log('create ok fileId=%s 首批 url=%d' % (file_id, len(urls)))
    for i in range(100, nparts, 100):
        more = c.more_urls(file_id, upload_id, infos[i:i + 100])
        urls += (more.get('data') or {}).get('partInfos') or []

    jobs = sorted((int(u['partNumber']), u['uploadUrl']) for u in urls)
    t0 = time.time()
    done_bytes = 0
    for no, url in jobs:
        ln = infos[no - 1]['partSize']
        dt = put_part(url, local_path, (no - 1) * part, ln)
        done_bytes += ln
        if not quiet:
            log('   片 %d/%d  %.1f MiB  %.1fs  %.2f MiB/s  | 累计 %.2f MiB/s'
                % (no, nparts, ln / 1048576.0, dt, ln / 1048576.0 / max(dt, 1e-6),
                   done_bytes / 1048576.0 / max(time.time() - t0, 1e-6)))
    el = time.time() - t0
    c.complete(sha, file_id, upload_id)
    mibps = done_bytes / 1048576.0 / max(el, 1e-6)
    log('complete ok: %s  %.1f MiB / %.1fs = %.2f MiB/s' % (name, done_bytes / 1048576.0, el, mibps))

    items = c.list_dir(parent)
    hit = [i for i in items if i.get('name') == name]
    log('回读: %s' % ('size=%s' % hit[0].get('size') if hit else '未在列表中找到'))
    return {'bytes': done_bytes, 'seconds': el, 'mibps': mibps, 'name': name}


def make_random_file(mb, tag='gen'):
    p = os.path.join(tempfile.gettempdir(), '_ydy2_%s_%d.bin' % (tag, mb))
    if os.path.exists(p) and os.path.getsize(p) == mb * 1024 * 1024:
        return p
    blk = os.urandom(4 * 1024 * 1024)
    with open(p, 'wb') as f:
        left = mb * 1024 * 1024
        while left > 0:
            f.write(blk[:min(len(blk), left)])
            left -= min(len(blk), left)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file')
    ap.add_argument('--gen-mb', type=int, default=0)
    ap.add_argument('--dir', default='老')
    ap.add_argument('--part-mb', type=int, default=0)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--name')
    ap.add_argument('--auth-env', default='YDY_AUTHORIZATION')
    ap.add_argument('--auth-file')
    ap.add_argument('--list')
    a = ap.parse_args()

    auth = os.environ.get(a.auth_env, '').strip()
    if not auth and a.auth_file and os.path.exists(a.auth_file):
        auth = open(a.auth_file, encoding='utf-8').read().strip()
    if not auth:
        raise SystemExit('缺 139 授权：--auth-file 或环境变量 %s' % a.auth_env)

    c = Yun139(auth)
    log('personal 主机: %s' % c.route())

    if a.list is not None:
        fid = c.resolve(a.list) if a.list.strip('/') else '/'
        items = c.list_dir(fid)
        print('目录 %r (id=%s) %d 项' % (a.list, fid, len(items)))
        for i in items[:20]:
            print('   %-40s type=%-7s size=%s' % ((i.get('name') or '')[:40], i.get('type'), i.get('size')))
        return

    if a.gen_mb:
        path = make_random_file(a.gen_mb, 'gen%d' % a.gen_mb)
        name = a.name or '_ydy2_gen_%dMB_%s.bin' % (a.gen_mb, time.strftime('%m%d_%H%M'))
    elif a.file:
        path, name = a.file, a.name
    else:
        raise SystemExit('要么 --file/--gen-mb，要么 --list')

    res = upload_file(c, path, name, a.dir, a.part_mb)
    print('RESULT %s' % json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
