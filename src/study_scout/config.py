import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path = Path('.env')) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


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

    @classmethod
    def from_env(cls):
        load_env()
        get = os.environ.get
        s = cls(get('STUDY_TOPICS', 'AI,Python,软件工程'), int(get('BILI_OWNER_UID') or 0), get('BILI_OWNER_NAME', ''), int(get('BILI_FAV_FOLDER_ID') or 0), min(5, max(1, int(get('BILI_BATCH_LIMIT', '5')))), min(3, max(0, int(get('BILI_COMMENT_MAX_REPLIES', '3')))), float(get('BILI_WRITE_DELAY_MIN', '5')), float(get('BILI_WRITE_DELAY_MAX', '20')), get('MODEL_BASE_URL', ''), get('MODEL_API_KEY', ''), get('MODEL_NAME', ''), get('DRY_RUN', 'true').lower() != 'false')
        if s.write_delay_min < 5 or s.write_delay_max < s.write_delay_min:
            raise ValueError('写操作等待必须至少 5 秒，最大值不得小于最小值')
        return s
