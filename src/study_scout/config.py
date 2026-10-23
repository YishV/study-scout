import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path = Path('.env')) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"')
    return values


@dataclass
class Settings:
    topics: str = 'AI,Python,软件工程'
    owner_uid: int = 0
    owner_name: str = ''
    fav_folder_id: int = 0
    batch_limit: int = 5
    max_replies: int = 3
    write_delay_min: float = 5
    write_delay_max: float = 20
    model_base_url: str = ''
    model_api_key: str = ''
    model_name: str = ''
    dry_run: bool = True
    persona: str = '认真、简短、只谈视频中的具体做法，不装懂'
    share_method: str = 'private_message'
    listen_interval_seconds: int = 60
    listen_max_per_cycle: int = 5
    listen_max_per_session: int = 20
    run_interval_minutes: int = 10

    @property
    def interests(self) -> list[str]:
        return list(dict.fromkeys(topic.strip() for topic in self.topics.split(',') if topic.strip()))

    @classmethod
    def from_env(cls, path: Path = Path('.env')):
        values = dict(os.environ)
        values.update(load_env(path))
        get = values.get
        s = cls(
            topics=get('STUDY_TOPICS', 'AI,Python,软件工程'),
            owner_uid=int(get('BILI_OWNER_UID') or 0),
            owner_name=get('BILI_OWNER_NAME', ''),
            fav_folder_id=int(get('BILI_FAV_FOLDER_ID') or 0),
            batch_limit=min(5, max(1, int(get('BILI_BATCH_LIMIT', '5')))),
            max_replies=min(3, max(0, int(get('BILI_COMMENT_MAX_REPLIES', '3')))),
            write_delay_min=float(get('BILI_WRITE_DELAY_MIN', '5')),
            write_delay_max=float(get('BILI_WRITE_DELAY_MAX', '20')),
            model_base_url=get('MODEL_BASE_URL', ''),
            model_api_key=get('MODEL_API_KEY', ''),
            model_name=get('MODEL_NAME', ''),
            dry_run=get('DRY_RUN', 'true').lower() != 'false',
            persona=get('STUDY_PERSONA', '认真、简短、只谈视频中的具体做法，不装懂'),
            share_method=get('BILI_SHARE_METHOD', 'private_message'),
            listen_interval_seconds=int(get('BILI_LISTEN_INTERVAL_SECONDS', '60')),
            listen_max_per_cycle=int(get('BILI_LISTEN_MAX_PER_CYCLE', '5')),
            listen_max_per_session=int(get('BILI_LISTEN_MAX_PER_SESSION', '20')),
            run_interval_minutes=int(get('BILI_RUN_INTERVAL_MINUTES', '10')),
        )
        if s.write_delay_min < 5 or s.write_delay_max < s.write_delay_min:
            raise ValueError('写操作等待必须至少 5 秒，最大值不得小于最小值')
        if not s.interests:
            raise ValueError('STUDY_TOPICS 至少填写一个兴趣词')
        if s.share_method not in ('private_message', 'comment_mention'):
            raise ValueError('BILI_SHARE_METHOD 只能是 private_message 或 comment_mention')
        if s.listen_interval_seconds < 60:
            raise ValueError('BILI_LISTEN_INTERVAL_SECONDS 至少为 60')
        if not 1 <= s.listen_max_per_cycle <= 5:
            raise ValueError('BILI_LISTEN_MAX_PER_CYCLE 必须为 1～5')
        if not 1 <= s.listen_max_per_session <= 20:
            raise ValueError('BILI_LISTEN_MAX_PER_SESSION 必须为 1～20')
        if s.run_interval_minutes < 10:
            raise ValueError('BILI_RUN_INTERVAL_MINUTES 至少为 10')
        return s
