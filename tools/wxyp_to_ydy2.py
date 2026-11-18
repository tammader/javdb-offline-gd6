# -*- coding: utf-8 -*-
"""无限云盘(WxYunPan) -> 139 个人云 老/ ：先下载一段，再直传上去，两腿分别计时。

用法：
  python wxyp_to_ydy2.py --mb 512 --dir 老 [--part-mb 100]
依赖：同目录 wxyunpan.py（无限云盘客户端）、ydy2_direct.py（139 直传）
环境：WXYP_ACCOUNTS(JSON) / YDY_AUTHORIZATION
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wxyunpan import WxYunPan            # noqa: E402
import ydy2_direct as Y                  # noqa: E402

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cdn_size(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
            with OP.open(req, timeout=40) as r:
                if r.headers.get('Content-Length'):
                    return int(r.headers['Content-Length'])
        except Exception:
            pass
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-0'})
            with OP.open(req, timeout=40) as r:
                cr = r.headers.get('Content-Range') or ''
                if '/' in cr:
                    return int(cr.rsplit('/', 1)[1])
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def pick(clients, want=1024 * 1024 * 1024):
    """挑一个已离线完成(Saved)的文件，返回 (client, file, url, size)"""
    for tag, c in clients:
        try:
            tops = c.list_dir('0')
        except Exception as e:
            Y.log('列根目录失败 %s: %s' % (tag, str(e)[:60]))
            continue
        cand = []
        for t in tops:
            if str(t.get('type')) == '2' or t.get('isDir'):
                try:
                    cand += [k for k in c.list_dir(str(t.get('id')))
                             if (k.get('message') or '') == 'Saved']
                except Exception:
                    pass
            elif (t.get('message') or '') == 'Saved':
                cand.append(t)
        Y.log('[%s] 离线已完成 %d 个' % (tag, len(cand)))
        best = None
        for f in cand[:6]:
            u, err = c.prepare_play(f.get('fileId'), f.get('shareId') or '')
            if not u:
                continue
            s = cdn_size(u)
            if not s:
                continue
            Y.log('    候选 %.1f MiB  %s' % (s / 1048576.0, (f.get('name') or '')[:52]))
            if best is None or s > best[3]:
                best = (c, f, u, s)
            if s >= want:
                break
        if best:
            return best
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mb', type=int, default=512)
    ap.add_argument('--dir', default='老')
    ap.add_argument('--part-mb', type=int, default=0)
    a = ap.parse_args()

    raw = os.environ.get('WXYP_ACCOUNTS', '[]')
    accounts = json.loads(raw)
    if not accounts:
        raise SystemExit('缺 WXYP_ACCOUNTS')
    clients = []
    for acc in accounts:
        c = WxYunPan(acc['account'], acc['password'])
        if c.login():
            clients.append((acc['account'][:6] + '***', c))
            Y.log('无限云盘登录成功: %s' % (acc['account'][:6] + '***'))
        else:
            Y.log('无限云盘登录失败: %s' % (acc['account'][:6] + '***'))
    if not clients:
        raise SystemExit('无限云盘全部登录失败')

    got = pick(clients, want=a.mb * 1024 * 1024)
    if not got:
        raise SystemExit('没拿到可下载的直链')
    c_src, f, url, src_size = got
    span = min(src_size, a.mb * 1024 * 1024)
    Y.log('源 %s  抽 %s / 总 %s' % ((f.get('name') or '')[:60],
                                  '%.1f MiB' % (span / 1048576.0), '%.1f MiB' % (src_size / 1048576.0)))

    tmp = os.path.join(tempfile.gettempdir(), 'wxyp_slice_%d.bin' % span)
    got_bytes, t_dl = 0, 0.0
    try:
        t0 = time.time()
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                   'Range': 'bytes=0-%d' % (span - 1)})
        with OP.open(req, timeout=300) as r:
            Y.log('CDN HTTP %s content-range=%s' % (r.status, r.headers.get('Content-Range')))
            with open(tmp, 'wb') as fh:
                while got_bytes < span:
                    buf = r.read(min(8 * 1024 * 1024, span - got_bytes))
                    if not buf:
                        break
                    fh.write(buf)
                    got_bytes += len(buf)
        t_dl = time.time() - t0
    except urllib.error.HTTPError as e:
        raise SystemExit('CDN 下载被拒 HTTP %s' % e.code)
    if not got_bytes:
        raise SystemExit('CDN 一个字节都没下来')
    Y.log('leg1 下载: %.1f MiB / %.1fs = %.2f MiB/s'
          % (got_bytes / 1048576.0, t_dl, got_bytes / 1048576.0 / max(t_dl, 1e-6)))

    auth = os.environ.get('YDY_AUTHORIZATION', '').strip()
    if not auth:
        raise SystemExit('缺 YDY_AUTHORIZATION')
    client = Y.Yun139(auth)
    Y.log('139 personal 主机: %s' % client.route())
    name = '_wxyp_%s_%s.bin' % (str(int(time.time()))[-6:], os.path.basename(tmp))
    res = Y.upload_file(client, tmp, name, a.dir, a.part_mb)
    os.remove(tmp)

    total = t_dl + (res.get('seconds') or 0)
    Y.log('SUMMARY 下载 %.2f MiB/s | 上传 %.2f MiB/s | 端到端 %.2f MiB/s (%.1f MiB / %.1fs)'
          % (got_bytes / 1048576.0 / max(t_dl, 1e-6), res.get('mibps') or 0,
             got_bytes / 1048576.0 / max(total, 1e-6), got_bytes / 1048576.0, total))
    print('RESULT %s' % json.dumps({'dl_mibps': got_bytes / 1048576.0 / max(t_dl, 1e-6),
                                    'up_mibps': res.get('mibps') or 0,
                                    'bytes': got_bytes, 'dl_s': t_dl,
                                    'up_s': res.get('seconds') or 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
