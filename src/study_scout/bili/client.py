import logging
from urllib.parse import urlparse

import httpx

from .wbi import sign_params

log = logging.getLogger(__name__)


class BiliError(Exception):
    def __init__(self, code, message=''):
        self.code = code
        self.stop_run = code in (412, -509, -105, -352, 12015, 12016) or any(x in message.lower() for x in ('验证码', '频繁', 'captcha', 'risk'))
        super().__init__(f'B 站错误 {code}: {message}')


class BiliClient:
    def __init__(self, http: httpx.AsyncClient, cookie: str = ''):
        self.http = http
        self.cookie = cookie
        self._keys = None

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
        if self.cookie and host in (api_host, 'passport.bilibili.com'):
            headers.update({'Cookie': self.cookie, 'Referer': 'https://www.bilibili.com/'})
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

    async def signed(self, path, params):
        if not self._keys:
            nav = await self.nav()
            urls = nav['wbi_img']
            self._keys = tuple(urlparse(urls[k]).path.rsplit('/', 1)[-1].split('.')[0] for k in ('img_url', 'sub_url'))
        return await self.request('GET', path, params=sign_params(params, *self._keys))

    async def discover(self, keyword):
        paths = [('/x/web-interface/wbi/index/top/feed/rcmd', {}), ('/x/web-interface/wbi/search/type', {'search_type': 'video', 'keyword': keyword}), ('/x/web-interface/popular', {})]
        for path, params in paths:
            try:
                data = (await (self.signed(path, params) if '/wbi/' in path else self.request('GET', path, params=params)))['data']
                items = data.get('item') or data.get('result') or data.get('list') or []
                if items:
                    return items
            except (BiliError, httpx.HTTPError, KeyError) as exc:
                if isinstance(exc, BiliError) and exc.stop_run:
                    raise
                log.warning('发现接口失败: %s', exc)
        return []

    async def find_users(self, exact_name):
        data = (await self.signed('/x/web-interface/wbi/search/type', {'search_type': 'bili_user', 'keyword': exact_name}))['data']
        return [{'uid': int(item['mid']), 'name': item['uname']} for item in (data.get('result') or []) if item.get('uname') == exact_name and item.get('mid')]

    async def detail(self, bvid):
        return (await self.request('GET', '/x/web-interface/view', params={'bvid': bvid}))['data']

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
