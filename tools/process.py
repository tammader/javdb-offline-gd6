# -*- coding: utf-8 -*-
"""JavDB: ed2k/magnet 离线到无限云盘 -> 下载 -> 重命名 -> 上传 gd6/老 -> 源文件移到「已搬gd6」-> 记录。

纯 Python，跑在 GitHub Actions。凭据全部来自环境变量(Secrets)：
  WXYP_ACCOUNTS  JSON: [{"account":"..","password":".."},{"account":"..","password":".."}]
  GD_CLIENT_ID / GD_CLIENT_SECRET / GD_REFRESH_TOKEN   (gd6)
可选：
  MAX_OFFLINE_PER_ACCOUNT  每个账号本轮最多提交多少离线（默认20）
  MAX_TRANSFER             本轮最多下载+上传多少个（默认5，防6h超时）
  GDRIVE_FOLDER            目标文件夹名（默认「老」）
  ARCHIVE_FOLDER          归档文件夹名（默认「已搬gd6」）
  CUTOFF                   起始日期（默认 2026-09-01）——仅用于初次从清单筛选
  STATE_FILE               状态文件（默认 state.json）
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from wxyunpan import WxYunPan

# ---------- 配置 ----------
ACCOUNTS = json.loads(os.environ.get('WXYP_ACCOUNTS', '[]'))
GD_CID = os.environ.get('GD_CLIENT_ID', '')
GD_CSEC = os.environ.get('GD_CLIENT_SECRET', '')
GD_RT = os.environ.get('GD_REFRESH_TOKEN', '')
MAX_OFFLINE = int(os.environ.get('MAX_OFFLINE_PER_ACCOUNT', '20'))
MAX_TRANSFER = int(os.environ.get('MAX_TRANSFER', '5'))
GD_FOLDER = os.environ.get('GDRIVE_FOLDER', '老')
ARCHIVE = os.environ.get('ARCHIVE_FOLDER', '已搬gd6')
STATE_FILE = os.environ.get('STATE_FILE', 'state.json')
LINKS_FILE = os.environ.get('LINKS_FILE', 'links.json')   # 待离线清单 [{type,code,link}]

_CODE_RE = re.compile(r'[A-Za-z]{2,10}[-_ ]?\d{2,5}')


def clean_name(name):
    """找到第一个「字母-数字」编号，砍掉前面所有前缀。无编号则不动。"""
    stem, dot, ext = name.rpartition('.')
    if not dot:
        stem, ext = name, ''
    m = _CODE_RE.search(stem)
    if not m or m.start() == 0:
        return name
    return stem[m.start():] + ('.' + ext if ext else '')


def norm_code(name_or_code):
    m = _CODE_RE.search(name_or_code or '')
    if not m:
        return None
    s = m.group(0).upper().replace('_', '-').replace(' ', '-')
    mm = re.match(r'([A-Z]+)-?0*(\d+)', s.replace('-', ''))
    if mm:
        return '%s-%d' % (mm.group(1), int(mm.group(2)))
    return s


def mask_account(acc):
    """公开库日志脱敏：只保留前 2 个字符，其余用 *** 代替（连长度都不泄露）。"""
    s = (acc or '').strip()
    return (s[:2] + '***') if len(s) > 2 else '***'


def load_state():
    if os.path.exists(STATE_FILE):
        s = json.load(open(STATE_FILE, encoding='utf-8'))
    else:
        s = {}
    s.setdefault('offlined', {})    # code -> {account, ts}
    s.setdefault('done', [])        # codes 已搬完
    s.setdefault('failed', {})
    return s


def save_state(s):
    json.dump(s, open(STATE_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


# ---------- Google Drive ----------
class GDrive:
    def __init__(self, cid, csec, rt):
        self.cid, self.csec, self.rt = cid, csec, rt
        self.at, self.exp = None, 0

    def token(self):
        if self.at and time.time() < self.exp - 60:
            return self.at
        data = urllib.parse.urlencode({'client_id': self.cid, 'client_secret': self.csec,
                                       'refresh_token': self.rt, 'grant_type': 'refresh_token'}).encode()
        j = json.loads(urllib.request.urlopen(urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST'), timeout=30).read())
        self.at, self.exp = j['access_token'], time.time() + j.get('expires_in', 3600)
        return self.at

    def find_folder(self, name, parent='root'):
        q = urllib.parse.quote("name='%s' and mimeType='application/vnd.google-apps.folder' and '%s' in parents and trashed=false" % (name.replace("'", "\\'"), parent))
        req = urllib.request.Request('https://www.googleapis.com/drive/v3/files?q=' + q + '&fields=files(id,name)', headers={'Authorization': 'Bearer ' + self.token()})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return d['files'][0]['id'] if d.get('files') else None

    def make_folder(self, name, parent='root'):
        body = json.dumps({'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent]}).encode()
        req = urllib.request.Request('https://www.googleapis.com/drive/v3/files', data=body, method='POST',
                                     headers={'Authorization': 'Bearer ' + self.token(), 'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())['id']

    def find_or_make(self, name, parent='root'):
        return self.find_folder(name, parent) or self.make_folder(name, parent)

    def find_file(self, name, parent):
        """返回 gd6 中同名文件 {id,name,size} 或 None。"""
        q = urllib.parse.quote("name='%s' and '%s' in parents and trashed=false" % (name.replace("'", "\\'"), parent))
        req = urllib.request.Request('https://www.googleapis.com/drive/v3/files?q=' + q + '&fields=files(id,name,size)',
                                     headers={'Authorization': 'Bearer ' + self.token()})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return d['files'][0] if d.get('files') else None

    def delete_file(self, fid):
        req = urllib.request.Request('https://www.googleapis.com/drive/v3/files/' + fid, method='DELETE',
                                     headers={'Authorization': 'Bearer ' + self.token()})
        urllib.request.urlopen(req, timeout=60)
        return True


def cdn_size(url):
    """无限云盘 CDN 侧真实字节数（HEAD Content-Length）。元数据 fileList.size 会虚报，不能用。"""
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return int(r.headers.get('Content-Length') or 0) or None
    except Exception:
        return None


def download(url, dest, progress_tag=''):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        while True:
            buf = r.read(4 * 1024 * 1024)
            if not buf:
                break
            f.write(buf); got += len(buf)
            if total and got % (256 * 1024 * 1024) < 4 * 1024 * 1024:
                print('    下载 %s: %.1f/%.1f GB' % (progress_tag, got / 1024**3, total / 1024**3), flush=True)
    return os.path.getsize(dest)


def rclone_upload(local_path, remote_name, folder_id):
    """用 rclone 上传到 gd6 的「老」目录 —— 自带分块/续传/退避重试，稳定。
    替代自研 urllib 分块上传（大文件会被 Broken pipe / Connection reset 掐断）。
    关键：conf 里必须放【刚换出的有效 access_token + 未来 expiry】，
    否则 rclone 启动即判 token 过期并报 "no refresh token"（即使带了 refresh_token）。"""
    import datetime
    conf_dir = '/tmp/rclone'
    os.makedirs(conf_dir, exist_ok=True)
    conf_path = os.path.join(conf_dir, 'rclone.conf')
    # 用同样的 refresh_token 换一个新鲜 access_token（GDrive.token 与 alist 用的是同一套 OAuth）
    data = urllib.parse.urlencode({'client_id': GD_CID, 'client_secret': GD_CSEC,
                                   'refresh_token': GD_RT, 'grant_type': 'refresh_token'}).encode()
    tj = json.loads(urllib.request.urlopen(urllib.request.Request(
        'https://oauth2.googleapis.com/token', data=data, method='POST'), timeout=30).read())
    at = tj['access_token']
    exp = (datetime.datetime.now(datetime.timezone.utc) +
           datetime.timedelta(seconds=tj.get('expires_in', 3600) - 60)).strftime('%Y-%m-%dT%H:%M:%SZ')
    token_json = json.dumps({"access_token": at, "token_type": "Bearer",
                             "refresh_token": GD_RT, "expiry": exp})
    with open(conf_path, 'w') as f:
        f.write('[gd6]\ntype = drive\nclient_id = %s\nclient_secret = %s\ntoken = %s\n'
                % (GD_CID, GD_CSEC, token_json))
    env = os.environ.copy()
    env['RCLONE_CONFIG'] = conf_path
    dest = 'gd6:%s/%s' % (GD_FOLDER, remote_name)
    r = subprocess.run(['rclone', 'copyto', local_path, dest,
                        '--drive-chunk-size', '128M', '--retries', '5', '--low-level-retries', '10'],
                       env=env, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        raise RuntimeError('rclone failed: %s | %s' % ((r.stderr or '')[:250], (r.stdout or '')[:150]))
    return True


def main():
    if not ACCOUNTS:
        print('!! 缺 WXYP_ACCOUNTS'); sys.exit(2)
    if not (GD_CID and GD_RT):
        print('!! 缺 gd6 凭证'); sys.exit(2)
    # 清单来源：优先环境变量 WXYP_LINKS(JSON)，其次 links.json 文件
    links = []
    if os.environ.get('WXYP_LINKS'):
        try:
            links = json.loads(os.environ['WXYP_LINKS'])
        except Exception as e:
            print('!! WXYP_LINKS 解析失败', str(e)[:80])
    elif os.path.exists(LINKS_FILE):
        links = json.load(open(LINKS_FILE, encoding='utf-8-sig'))
    state = load_state()
    gd = GDrive(GD_CID, GD_CSEC, GD_RT)
    gd_parent = gd.find_or_make(GD_FOLDER)
    print('gd6 目标文件夹 %s id=%s' % (GD_FOLDER, gd_parent))

    # 自测模式：只验证 rclone 上传链路（不下载大文件）
    if os.environ.get('RCLONE_SELFTEST', '').lower() == 'true':
        p = '/tmp/_rclone_test.bin'
        with open(p, 'wb') as f:
            f.write(os.urandom(5 * 1024 * 1024))
        rclone_upload(p, '_rclone_test.bin', gd_parent)
        os.remove(p)
        print('SELFTEST OK: 5MB 已通过 rclone 传到 gd6/%s' % GD_FOLDER)
        return

    # 建两个账号客户端
    clients = []
    for i, acc in enumerate(ACCOUNTS):
        c = WxYunPan(acc['account'], acc['password'])
        tag = mask_account(acc['account'])
        if c.login():
            c.tag = tag
            print('登录成功: %s' % tag); clients.append(c)
        else:
            print('登录失败: %s' % tag)

    # ===== 阶段1：提交离线（未离线的链接，按账号额度均分）=====
    todo = [x for x in links if norm_code(x.get('code', '')) not in state['offlined'] and norm_code(x.get('code','')) not in state['done']]
    print('待离线 %d 条' % len(todo))
    per = {}
    for i, x in enumerate(todo):
        code = norm_code(x.get('code', ''))
        if not code:
            continue
        acct_idx = i % len(clients) if clients else 0
        if not clients:
            break
        per.setdefault(acct_idx, [])
        if len(per[acct_idx]) >= MAX_OFFLINE:
            continue
        per[acct_idx].append((code, x['link']))
    for idx, lst in per.items():
        c = clients[idx]
        tag = getattr(c, 'tag', '***')
        for code, link in lst:
            r = c.add_offline(link)
            ok = r.get('code') == 200
            print('  离线[%s] %s -> %s' % (tag, code, ok))
            if ok:
                # state 只存账号序号，绝不把账号写进会 commit 回公开仓库的文件
                state['offlined'][code] = {'account_idx': idx, 'ts': time.strftime('%Y-%m-%d %H:%M')}
    save_state(state)

    # ===== 阶段2：扫描已完成离线的文件，下载→传gd6→归档 =====
    print('\n=== 扫描离线完成的文件 ===')
    transferred = 0   # 成功数
    attempts = 0      # 尝试数（成功+失败都算，防止失败后无限下载白费流量）
    for c in clients:
        # 根目录 + 根目录/根目录(如有) 里找已完成的离线文件
        stack = [('0', '')]
        while stack and attempts < MAX_TRANSFER:
            pid, pname = stack.pop()
            try:
                files = c.list_dir(pid)
            except Exception as e:
                print('  列目录失败', pid, str(e)[:80]); continue
            for f in files:
                if attempts >= MAX_TRANSFER:
                    break
                name = f.get('name', '')
                is_dir = str(f.get('type')) == '2' or f.get('isDir')
                if is_dir:
                    # 深入一级（根目录）；跳过归档文件夹
                    if name != ARCHIVE:
                        stack.append((f.get('id'), name))
                    continue
                # 归档标记文件(已搬_前缀) = 已处理，跳过，绝不重复初始重命名
                if name.startswith(ARCHIVE + '_') or name.startswith('已搬_'):
                    continue
                # 只处理离线已完成(message=='Saved')的视频文件；未完成/失败跳过
                msg = (f.get('message') or '')
                if msg != 'Saved':
                    continue
                code = norm_code(name)
                new_name = clean_name(name)   # 初始重命名（番号开头），仅用于 gd6 文件名
                url, err = c.prepare_play(f.get('fileId'), f.get('shareId') or '')
                if not url:
                    print('  取直链失败 %s: %s' % (name, err)); continue
                src_size = cdn_size(url)      # CDN 真实字节数（无限云盘元数据 size 会虚报，不可信）
                existing = gd.find_file(new_name, gd_parent)
                # 防重复：gd6 里已有同名且字节数一致 → 真完成，跳过（顺手把源文件打标记）
                if existing and src_size and int(existing.get('size') or 0) == src_size:
                    if code and code not in state['done']:
                        state['done'].append(code); save_state(state)
                    print('  已完成(gd6 字节一致 %s)，跳过: %s' % (src_size, new_name))
                    _mark_source(c, f, name)
                    continue
                # gd6 里同名但字节不符（半截/旧文件）→ 删除重传
                if existing:
                    print('  gd6 同名文件字节不符(现 %s / 源 %s) → 删除重传' % (existing.get('size'), src_size))
                    gd.delete_file(existing['id'])
                attempts += 1
                local = os.path.join('/tmp', re.sub(r'[^\w.\-]+', '_', new_name))
                print('  ↓ %s (源 %s 字节)' % (new_name, src_size))
                try:
                    download(url, local, new_name)
                except Exception as e:
                    print('  下载失败', str(e)[:100])
                    if os.path.exists(local): os.remove(local)
                    continue
                got = os.path.getsize(local)
                if src_size and got != src_size:
                    print('  下载不完整(%s/%s) → 放弃本轮' % (got, src_size))
                    os.remove(local); continue
                print('  ↑ 上传 gd6 (rclone): %s' % new_name)
                try:
                    rclone_upload(local, new_name, gd_parent)
                except Exception as e:
                    print('  上传失败: %s' % str(e)[:200])
                    if os.path.exists(local): os.remove(local)
                    continue
                try: os.remove(local)
                except Exception: pass
                state['done'].append(code or new_name)
                save_state(state)
                transferred += 1
                print('  ✓ 完成 %s' % new_name)
                _mark_source(c, f, name)
    print('\n本轮搬运 %d 个' % transferred)


def _mark_source(c, f, name):
    """给源文件加「已搬_」标记（保留清理后的名字）。
    必须用真正生效的 /disk/rename(id,name)——/file/rename 会假成功。"""
    try:
        marked = '已搬_' + clean_name(name)
        if marked == name:
            return
        r = c.rename(f.get('id'), marked)
        ok = (r.get('code') == 200)
        print('    源文件标记: %s -> %s [%s]' % (name, marked, 'OK' if ok else r.get('msg')))
    except Exception as e:
        print('    源文件标记失败: %s' % str(e)[:80])


if __name__ == '__main__':
    main()
