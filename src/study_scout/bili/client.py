import logging
import json
import time
import uuid
from urllib.parse import urlparse

import httpx

from .wbi import sign_params

log = logging.getLogger(__name__)


class BiliError(Exception):
    def __init__(self, code, message=''):
        self.code = code
        self.stop_run = code in (412, -509, -105, -352, 12015, 12016, 21020, 21046, 21047) or any(x in message.lower() for x in ('验证码', '频繁', '频率过快', 'captcha', 'risk'))
        super().__init__(f'B 站错误 {code}: {message}')


class BiliClient:
    def __init__(self, http: httpx.AsyncClient, cookie: str = ''):
        self.http = http
        self.cookie = cookie
        self._keys = None
        self._dm_device_id = str(uuid.uuid4()).upper()

    def set_cookie(self, cookie):
        self.cookie = cookie

    @property
    def csrf(self):
        return next((part.strip().split('=', 1)[1] for part in self.cookie.split(';') if part.strip().startswith('bili_jct=')), '')

    async def request(self, method, path, *, params=None, data=None, auth=False):
        if auth and not self.csrf:
            raise BiliError(-1, '未扫码登录')
        host = urlparse(path).hostname or self.http.base_url.host
        api_host = self.http.base_url.host
        headers = {'User-Agent': 'Mozilla/5.0 StudyScout/0.1'}
        if self.cookie and host in (api_host, 'passport.bilibili.com', 'api.vc.bilibili.com'):
            headers.update({'Cookie': self.cookie, 'Referer': 'https://message.bilibili.com/' if host == 'api.vc.bilibili.com' else 'https://www.bilibili.com/'})
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
        return (await self.request('GET', '/x/msgfeed/reply', params=params))['data']

    async def at_notifications(self, *, offset_id=None, at_time=None):
        params = {'platform': 'web', 'build': 0, 'mobi_app': 'web'}
        if offset_id is not None:
            params.update(id=offset_id, at_time=at_time)
        return (await self.request('GET', '/x/msgfeed/at', params=params))['data']

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

    async def replies(self, aid, rpid):
        data = (await self.request('GET', '/x/v2/reply/reply', params={'type': 1, 'oid': aid, 'root': rpid, 'pn': 1, 'ps': 20}))['data']
        return data.get('replies') or []
