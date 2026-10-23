import re


def build_comment(name, point, *, mention=True):
    name = name.strip().lstrip('@')
    point = point.strip()
    if (mention and not name) or not point or re.search(r'@|https?://|www\.|二维码|微信|QQ\d', point, re.I):
        raise ValueError('评论内容不合规')
    message = f'@{name} {point}' if mention else point
    if len(message) > 300 or len([part for part in re.split(r'[。！？!?]', point) if part.strip()]) > 3:
        raise ValueError('评论超过长度或句数限制')
    return message


def verified_member(response, uid):
    members = (((response.get('data') or {}).get('reply') or {}).get('content') or {}).get('members') or []
    return any(str(m.get('mid')) == str(uid) for m in members)


def safe_reply(text):
    return not re.search(r'广告|加微|私信|政治|政府|辱骂|傻逼|垃圾|滚|http|www\.', text, re.I)
