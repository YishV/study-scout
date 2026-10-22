"""QR login lives only in process memory. A fresh scan is needed each start."""
from .client import BiliError


class QRLogin:
    def __init__(self, http, bili):
        self.http, self.bili = http, bili
        self.key = ''
        self.url = ''

    async def generate(self):
        response = (await self.bili.request('GET', 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'))['data']
        self.key, self.url = response['qrcode_key'], response['url']
        return self.url

    async def poll(self):
        if not self.key:
            raise ValueError('先获取二维码')
        response = await self.bili.request('GET', 'https://passport.bilibili.com/x/passport-login/web/qrcode/poll', params={'qrcode_key': self.key})
        data = response['data']
        code = data['code']
        if code == 0:
            cookies = []
            for name in ('SESSDATA', 'bili_jct', 'DedeUserID'):
                value = self.http.cookies.get(name)
                if value:
                    cookies.append(f'{name}={value}')
            if len(cookies) != 3:
                raise BiliError(-1, '扫码成功但登录 Cookie 不完整')
            self.bili.set_cookie('; '.join(cookies))
            nav = await self.bili.nav()
            if not nav.get('isLogin') or not nav.get('mid'):
                self.bili.set_cookie('')
                raise BiliError(-1, '扫码 Cookie 未通过登录状态验证')
            self.key = ''
            return 'logged_in'
        return {86101: 'waiting', 86090: 'scanned', 86038: 'expired'}.get(code, 'waiting')
