import logging
import json
import time
import uuid
from urllib.parse import urlparse

import httpx

from .wbi import sign_params

log = logging.getLogger(__name__)

_BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
_BASE_HEADERS = {
    'User-Agent': _BROWSER_UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6',
}


class BiliError(Exception):
    def __init__(self, code, message=''):
        self.code = code
        self.stop_run = code in (412, -509, -105, -352, 12015, 12016, 21020, 21046, 21047) or any(x in message.lower() for x in ('验证码', '频繁', '频率过快', 'captcha', 'risk'))
        super().__init__(f'B 站错误 {code}: {message}')


class BiliClient:
    def __init__(self, http: httpx.AsyncClient, cookie: str = '', *, public_http: httpx.AsyncClient | None = None):
        self.http = http
        self.public_http = public_http or http
        self.cookie = cookie
        self._keys = None
        self._identity = None
        self._buvid_loaded = False
        self._dm_device_id = str(uuid.uuid4()).upper()

    def set_cookie(self, cookie):
        self.cookie = cookie
        self._identity = None
        self._buvid_loaded = False

    @property
    def csrf(self):
        return next((part.strip().split('=', 1)[1] for part in self.cookie.split(';') if part.strip().startswith('bili_jct=')), '')

    async def ensure_buvid(self):
        """Add the device ids the web client sends; some write APIs flag requests without them."""
        if self._buvid_loaded or not self.cookie:
            return
        self._buvid_loaded = True
        try:
            data = (await self.request('GET', '/x/frontend/finger/spi')).get('data') or {}
        except Exception as exc:
            log.debug('获取 buvid 失败：%s', exc)
            return
        existing = {part.strip().split('=', 1)[0] for part in self.cookie.split(';')}
        extra = [f'{name}={value}' for name, value in (('buvid3', data.get('b_3')), ('buvid4', data.get('b_4')))
                 if value and name not in existing]
        if extra:
            self.cookie = '; '.join([self.cookie, *extra])

    async def request(self, method, path, *, params=None, data=None, auth=False):
        if auth:
            if not self.csrf:
                raise BiliError(-1, '未扫码登录')
            await self.ensure_buvid()
        host = urlparse(path).hostname or self.http.base_url.host
        api_host = self.http.base_url.host
        headers = dict(_BASE_HEADERS)
        if self.cookie and host in (api_host, 'passport.bilibili.com', 'api.vc.bilibili.com'):
            headers.update({'Cookie': self.cookie,
                            'Referer': 'https://message.bilibili.com/' if host == 'api.vc.bilibili.com'
                            else 'https://www.bilibili.com/'})
        if method == 'POST' and host == api_host:
            headers['Origin'] = 'https://www.bilibili.com'
        response = await self.http.request(method, path, params=params, data=data, headers=headers)
        if response.status_code == 412:
            raise BiliError(412, '风控')
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get('code', 0) != 0:
            raise BiliError(payload['code'], payload.get('message', ''))
        return payload

    async def nav(self):
        return (await self.request('GET', '/x/web-interface/nav'))['data']

    async def identity(self):
        """Cached login identity; level is None when the response omits it."""
        if self._identity is None:
            nav = await self.nav()
            level_info = nav.get('level_info')
            level = level_info.get('current_level') if isinstance(level_info, dict) else None
            mobile = nav.get('mobile_verified')
            self._identity = {'uid': int(nav.get('mid') or 0),
                              'name': nav.get('uname') or '',
                              'level': int(level) if isinstance(level, int) else None,
                              'mobile_verified': None if mobile is None else bool(mobile)}
        return dict(self._identity)

    async def _wbi_keys(self):
        if not self._keys:
            nav = await self.nav()
            urls = nav['wbi_img']
            self._keys = tuple(urlparse(urls[k]).path.rsplit('/', 1)[-1].split('.')[0] for k in ('img_url', 'sub_url'))

    async def signed(self, path, params):
        await self._wbi_keys()
        return await self.request('GET', path, params=sign_params(params, *self._keys))

    async def send_private_message(self, sender_uid: int, receiver_uid: int, message: str):
        if sender_uid <= 0 or receiver_uid <= 0 or sender_uid == receiver_uid:
            raise ValueError('私信发送者和目标 UID 必须是不同的有效账号')
        if not message.strip() or len(message) > 500:
            raise ValueError('私信内容须为 1～500 字')
        await self._wbi_keys()
        params = sign_params({'w_sender_uid': sender_uid, 'w_receiver_id': receiver_uid,
                              'w_dev_id': self._dm_device_id}, *self._keys)
        fields = {'sender_uid': sender_uid, 'receiver_type': 1, 'receiver_id': receiver_uid,
                  'msg_type': 1, 'msg_status': 0,
                  'content': json.dumps({'content': message}, ensure_ascii=False),
                  'new_face_version': 0, 'dev_id': self._dm_device_id,
                  'timestamp': int(time.time())}
        data = {f'msg[{key}]': value for key, value in fields.items()}
        data.update(n=1, from_firework=0, build=0, mobi_app='web',
                    csrf=self.csrf, csrf_token=self.csrf)
        return await self.request('POST', 'https://api.vc.bilibili.com/web_im/v1/web_im/send_msg',
                                  params=params, data=data, auth=True)

    async def private_messages(self, owner_uid: int):
        if owner_uid <= 0:
            raise ValueError('必须配置大号 UID')
        data = (await self.signed('https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs',
                                  {'sender_device_id': 1, 'talker_id': owner_uid,
                                   'session_type': 1, 'size': 50, 'build': 0, 'mobi_app': 'web'}))['data']
        return data.get('messages') or [], bool(data.get('has_more'))

    async def reply_notifications(self, *, offset_id=None, reply_time=None):
        params = {'platform': 'web', 'build': 0, 'mobi_app': 'web'}
        if offset_id is not None:
            params.update(id=offset_id, reply_time=reply_time)
        return (await self.request('GET', '/x/msgfeed/reply', params=params))['data'] or {}

    async def at_notifications(self, *, offset_id=None, at_time=None):
        params = {'platform': 'web', 'build': 0, 'mobi_app': 'web'}
        if offset_id is not None:
            params.update(id=offset_id, at_time=at_time)
        return (await self.request('GET', '/x/msgfeed/at', params=params))['data'] or {}

    async def discover(self, keyword):
        keywords = [keyword] if isinstance(keyword, str) else keyword
        groups = []
        for word in keywords:
            try:
                data = (await self.signed('/x/web-interface/wbi/search/type',
                                          {'search_type': 'video', 'keyword': word}))['data']
                groups.append(data.get('result') or data.get('item') or [])
            except (BiliError, httpx.HTTPError, KeyError) as exc:
                if isinstance(exc, BiliError) and exc.stop_run:
                    raise
                log.warning('兴趣搜索失败 %s: %s', word, exc)
        ordered, seen = [], set()
        for index in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if index < len(group):
                    item = group[index]
                    bvid = item.get('bvid') or item.get('bvid_str')
                    if bvid and bvid not in seen:
                        seen.add(bvid)
                        ordered.append(item)
        return ordered

    async def detail(self, bvid):
        return (await self.request('GET', '/x/web-interface/view', params={'bvid': bvid}))['data']

    async def detail_by_aid(self, aid):
        return (await self.request('GET', '/x/web-interface/view', params={'aid': aid}))['data']

    async def subtitles(self, aid, cid):
        data = (await self.signed('/x/player/wbi/v2', {'aid': aid, 'cid': cid}))['data']
        entries = ((data.get('subtitle') or {}).get('subtitles') or [])
        if not entries:
            return ''
        url = entries[0]['subtitle_url']
        if url.startswith('//'):
            url = 'https:' + url
        parsed = urlparse(url)
        allowed = bool(parsed.hostname) and (parsed.hostname == self.http.base_url.host or parsed.hostname == 'bilibili.com' or parsed.hostname.endswith(('.bilibili.com', '.hdslb.com', '.bilivideo.com')))
        if parsed.scheme != 'https' or not allowed:
            raise ValueError('字幕地址不可信')
        payload = await self.request('GET', url)
        return '\n'.join(item['content'] for item in payload.get('body', []) if item.get('content'))

    async def like(self, aid):
        return await self.request('POST', '/x/web-interface/archive/like', data={'aid': aid, 'like': 1, 'csrf': self.csrf}, auth=True)

    async def favorite(self, aid, folder_id):
        return await self.request('POST', '/x/v3/fav/resource/deal', data={'rid': aid, 'type': 2, 'add_media_ids': folder_id, 'csrf': self.csrf}, auth=True)

    async def folders(self, uid):
        return ((await self.request('GET', '/x/v3/fav/folder/created/list-all', params={'up_mid': uid, 'type': 2}))['data'] or {}).get('list') or []

    async def create_folder(self, title='学习'):
        return await self.request('POST', '/x/v3/fav/folder/add', data={'title': title, 'intro': '', 'privacy': 0, 'csrf': self.csrf}, auth=True)

    async def comment(self, aid, message, *, root=None, parent=None):
        data = {'type': 1, 'oid': aid, 'message': message, 'csrf': self.csrf, 'plat': 1}
        if root is not None:
            data.update(root=root, parent=parent)
        return await self.request('POST', '/x/v2/reply/add', data=data, auth=True)

    async def _public_get(self, path, params):
        response = await self.public_http.get(
            path, params=params,
            headers={**_BASE_HEADERS, 'Cookie': '', 'Referer': 'https://www.bilibili.com/'})
        if response.status_code == 412:
            raise BiliError(412, '风控')
        response.raise_for_status()
        return response.json()

    async def public_comment_visible(self, aid, rpid):
        """Check a top-level comment anonymously. True=public, False=not found, None=audit blocked."""
        if not rpid:
            return False

        # The exact root lookup is authoritative and avoids the hot/latest page.
        payload = await self._public_get('/x/v2/reply/reply',
                                         {'type': 1, 'oid': aid, 'root': rpid, 'pn': 1, 'ps': 20})
        code = payload.get('code', 0)
        if code == 0 and str(((payload.get('data') or {}).get('root') or {}).get('rpid')) == str(rpid):
            return True
        if code == 12006:
            return False
        if code == -352:
            return None
        if code != 0:
            raise BiliError(code, payload.get('message', ''))
        payload = await self._public_get('/x/v2/reply/main',
                                         {'type': 1, 'oid': aid, 'mode': 2, 'next': 0, 'ps': 20})
        code = payload.get('code', 0)
        if code in (-352, 12006):
            return None if code == -352 else False
        if code != 0:
            raise BiliError(code, payload.get('message', ''))
        replies = (payload.get('data') or {}).get('replies') or []
        return any(str(reply.get('rpid')) == str(rpid) for reply in replies)

    async def public_reply_visible(self, aid, root, rpid):
        """Check a sub-reply inside its root thread. False also covers a hidden root."""
        if not rpid or not root:
            return False
        payload = await self._public_get('/x/v2/reply/reply',
                                         {'type': 1, 'oid': aid, 'root': root, 'pn': 1, 'ps': 20})
        code = payload.get('code', 0)
        if code == 12006:
            return False
        if code == -352:
            return None
        if code != 0:
            raise BiliError(code, payload.get('message', ''))
        replies = (payload.get('data') or {}).get('replies') or []
        return any(str(reply.get('rpid')) == str(rpid) for reply in replies)

    async def replies(self, aid, rpid):
        data = (await self.request('GET', '/x/v2/reply/reply', params={'type': 1, 'oid': aid, 'root': rpid, 'pn': 1, 'ps': 20}))['data']
        return data.get('replies') or []
