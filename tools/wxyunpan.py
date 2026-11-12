# -*- coding: utf-8 -*-
"""无限云盘 (WxYunPan / api.xbapi.com) 纯 Python 客户端。
接口来源：alistx drivers/wxyunpan。鉴权：登录 sessionKey -> 请求头 thirdSession + platform: APP。
纯标准库。
"""
import json
import time
import urllib.request
import urllib.error

API = "https://api.xbapi.com/v1/app"
UA = "okhttp/3.12.13"


class WxYunPan:
    def __init__(self, account, password, session_key=""):
        self.account = (account or "").strip()
        self.password = password or ""
        self.session = session_key or ""

    def _req(self, path, method="POST", body=None, retry_login=True):
        headers = {"User-Agent": UA, "platform": "APP", "thirdSession": self.session or "",
                   "Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        j = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as e:
                try:
                    j = json.loads(e.read().decode("utf-8", "replace"))
                except Exception:
                    j = {"code": -1, "msg": "HTTP %s" % e.code}
                break
            except Exception as e:
                if attempt == 2:
                    return {"code": -1, "msg": str(e)[:150]}
                time.sleep(3 * (attempt + 1))
        if retry_login and j.get("code") in (60001, 60002):
            self.session = ""
            if self.login():
                return self._req(path, method, body, retry_login=False)
        return j

    def login(self):
        acc = self.account
        bodies = ({"email": acc, "account": acc, "password": self.password},
                  {"account": acc, "phone": acc, "password": self.password}) if "@" in acc else \
                 ({"account": acc, "phone": acc, "password": self.password},
                  {"email": acc, "account": acc, "password": self.password})
        for b in bodies:
            j = self._req("/m/user/login", "POST", b, retry_login=False)
            if j.get("code") == 200 and (j.get("data") or {}).get("sessionKey"):
                self.session = j["data"]["sessionKey"]
                return True
        return False

    def list_dir(self, parent_id="0"):
        out = []
        for cur in range(1, 51):
            j = self._req("/disk/fileList", "POST", {"parentId": parent_id, "current": cur, "size": 200})
            if j.get("code") != 200:
                break
            data = j.get("data") or {}
            files = data.get("files") or []
            out.extend(files)
            if len(files) < 200 or cur >= (data.get("pages") or 1):
                break
        return out

    def add_offline(self, url):
        return self._req("/magnet/upload", "POST", {"url": url})

    def offline_tasks(self):
        return self._req("/task/list", "GET", None)

    def prepare_play(self, file_id, share_id=""):
        body = {"fileId": file_id}
        if share_id:
            body["shareId"] = share_id
        j = self._req("/magnet/preparePlay", "POST", body)
        if j.get("code") not in (200, 5555):
            return None, j.get("msg")
        d = j.get("data") or {}
        url = d.get("webContentLink") or d.get("defaultPlayUrl") or d.get("originPlayUrl")
        return url, None

    def move(self, file_db_id, parent_id):
        return self._req("/disk/moveFile", "POST", {"id": file_db_id, "parentId": parent_id})

    def add_folder(self, parent_id, name):
        return self._req("/disk/addFolder", "POST", {"parentId": parent_id, "name": name})

    def rename(self, file_db_id, new_name):
        """重命名文件/文件夹（用数据库 id）。
        注意：/file/rename 会返回 200「重命名成功」但实际不生效；
        真正生效的是 /disk/rename（body {id, name}）。"""
        return self._req("/disk/rename", "POST", {"id": file_db_id, "name": new_name})
