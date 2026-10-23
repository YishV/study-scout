import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, candidates_count INTEGER DEFAULT 0, selected_count INTEGER DEFAULT 0, actions_ok INTEGER DEFAULT 0, actions_failed INTEGER DEFAULT 0, error_code TEXT, dry_run INTEGER);
            CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), bvid TEXT, aid INTEGER, cid INTEGER, title TEXT, description TEXT, author TEXT, duration INTEGER, pubdate INTEGER, subtitle_available INTEGER DEFAULT 0, skip_reason TEXT, seen_at TEXT, UNIQUE(run_id,bvid));
            CREATE TABLE IF NOT EXISTS judgments (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), worth_learning INTEGER, score INTEGER, reason TEXT, takeaways_json TEXT, risk TEXT, model TEXT, raw_json TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), kind TEXT, status TEXT, request_json TEXT, response_json TEXT, error_code TEXT, created_at TEXT, UNIQUE(candidate_id,kind));
            CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), rpid INTEGER, message TEXT, mention_uid INTEGER, mention_verified INTEGER DEFAULT 0, notification_confirmed INTEGER DEFAULT 0, status TEXT, visibility_status TEXT DEFAULT 'unchecked', visibility_checked_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS replies (id INTEGER PRIMARY KEY, comment_id INTEGER REFERENCES comments(id), parent_rpid INTEGER, reply_rpid INTEGER, message TEXT, status TEXT, created_at TEXT, UNIQUE(comment_id,parent_rpid));
            CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, platform TEXT, uid INTEGER, nickname TEXT, credential_blob BLOB, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS dm_replies (id INTEGER PRIMARY KEY, account_uid INTEGER NOT NULL, owner_uid INTEGER NOT NULL, msg_key TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(account_uid,owner_uid,msg_key));
            CREATE TABLE IF NOT EXISTS mention_replies (id INTEGER PRIMARY KEY, account_uid INTEGER NOT NULL, notification_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(account_uid,notification_id));
            CREATE TABLE IF NOT EXISTS video_memory (account_uid INTEGER NOT NULL, owner_uid INTEGER NOT NULL, bvid TEXT NOT NULL, context TEXT NOT NULL, subtitle_available INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(account_uid,owner_uid));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(candidates)')}
            if 'skip_reason' not in columns:
                db.execute('ALTER TABLE candidates ADD COLUMN skip_reason TEXT')
            run_columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            if 'dry_run' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN dry_run INTEGER')
            comment_columns = {row['name'] for row in db.execute('PRAGMA table_info(comments)')}
            if 'visibility_status' not in comment_columns:
                db.execute("ALTER TABLE comments ADD COLUMN visibility_status TEXT DEFAULT 'unchecked'")
            if 'visibility_checked_at' not in comment_columns:
                db.execute('ALTER TABLE comments ADD COLUMN visibility_checked_at TEXT')

    def start_run(self, *, dry_run=None):
        with self.connect() as db:
            return db.execute('INSERT INTO runs(started_at,status,dry_run) VALUES (?,?,?)',
                              (now(), 'running', None if dry_run is None else int(dry_run))).lastrowid

    def finish_run(self, run_id, status, error_code=None):
        with self.connect() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,error_code=? WHERE id=?', (now(), status, error_code, run_id))

    def add_candidate(self, run_id, item):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO candidates(run_id,bvid,aid,cid,title,description,author,duration,pubdate,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (run_id, item['bvid'], item.get('aid'), item.get('cid'), item.get('title'), item.get('description'), item.get('author'), item.get('duration'), item.get('pubdate'), now()))
            return db.execute('SELECT id FROM candidates WHERE run_id=? AND bvid=?', (run_id, item['bvid'])).fetchone()['id']

    def seen_bvid(self, bvid, *, include_drafts=False):
        with self.connect() as db:
            if include_drafts:
                return bool(db.execute('SELECT 1 FROM candidates WHERE bvid=? LIMIT 1', (bvid,)).fetchone())
            return bool(db.execute('''SELECT 1 FROM candidates c JOIN runs r ON r.id=c.run_id
                LEFT JOIN actions a ON a.candidate_id=c.id
                WHERE c.bvid=? AND (r.dry_run=0 OR a.kind NOT IN ('draft','judge')) LIMIT 1''',
                (bvid,)).fetchone())

    def record_judgment(self, candidate_id, judgment, model):
        with self.connect() as db:
            db.execute('INSERT INTO judgments(candidate_id,worth_learning,score,reason,takeaways_json,risk,model,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (candidate_id, int(judgment.recommended), judgment.score, judgment.reason, json.dumps(judgment.takeaways, ensure_ascii=False), judgment.risk, model, judgment.model_dump_json(), now()))

    def set_subtitle_available(self, candidate_id):
        with self.connect() as db:
            db.execute('UPDATE candidates SET subtitle_available=1 WHERE id=?', (candidate_id,))

    def record_skip(self, candidate_id, reason):
        with self.connect() as db:
            db.execute('UPDATE candidates SET skip_reason=? WHERE id=?', (reason, candidate_id))

    def record_action(self, candidate_id, kind, status, request=None, response=None, error=None):
        with self.connect() as db:
            cur = db.execute('INSERT OR IGNORE INTO actions(candidate_id,kind,status,request_json,response_json,error_code,created_at) VALUES (?,?,?,?,?,?,?)', (candidate_id, kind, status, json.dumps(request or {}, ensure_ascii=False), json.dumps(response or {}, ensure_ascii=False), error, now()))
            return bool(cur.rowcount)

    def action_exists(self, candidate_id, kind):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM actions WHERE candidate_id=? AND kind=?', (candidate_id, kind)).fetchone())

    def update_action(self, candidate_id, kind, status, error=None):
        with self.connect() as db:
            db.execute('UPDATE actions SET status=?,error_code=? WHERE candidate_id=? AND kind=?', (status, error, candidate_id, kind))

    def save_comment(self, candidate_id, rpid, message, uid, verified, status):
        with self.connect() as db:
            return db.execute('INSERT INTO comments(candidate_id,rpid,message,mention_uid,mention_verified,status,created_at) VALUES (?,?,?,?,?,?,?)', (candidate_id, rpid, message, uid, int(verified), status, now())).lastrowid

    def set_comment_visibility(self, comment_id, status):
        if status not in ('visible', 'unconfirmed'):
            raise ValueError('无效的评论公开状态')
        with self.connect() as db:
            db.execute('UPDATE comments SET visibility_status=?,visibility_checked_at=? WHERE id=?',
                       (status, now(), comment_id))

    def set_notification_confirmed(self, comment_id):
        with self.connect() as db:
            db.execute("UPDATE comments SET notification_confirmed=1,status='ok' WHERE id=? AND mention_verified=1 AND status IN ('pending_confirmation','mention_unverified')", (comment_id,))
            return bool(db.execute('SELECT 1 FROM comments WHERE id=? AND notification_confirmed=1', (comment_id,)).fetchone())

    def mark_notification_missing(self, comment_id):
        with self.connect() as db:
            row = db.execute("SELECT candidate_id FROM comments WHERE id=? AND status='pending_confirmation' AND notification_confirmed=0", (comment_id,)).fetchone()
            if not row:
                return False
            db.execute("UPDATE comments SET status='mention_unverified' WHERE id=?", (comment_id,))
            db.execute("UPDATE actions SET status='mention_unverified',error_code='notification_missing' WHERE candidate_id=? AND kind='mention_test'", (row['candidate_id'],))
            db.execute("UPDATE runs SET status='failed',error_code='notification_missing',actions_failed=actions_failed+1 WHERE id=(SELECT run_id FROM candidates WHERE id=?)", (row['candidate_id'],))
            return True

    def mention_ready(self, uid=None):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM comments WHERE mention_verified=1 AND notification_confirmed=1 AND (? IS NULL OR mention_uid=?) LIMIT 1', (uid, uid)).fetchone())

    def own_comments(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT comments.*,candidates.aid FROM comments JOIN candidates ON candidates.id=comments.candidate_id WHERE comments.status=\'ok\'')]

    def recent_comment_count(self, *, hours=24):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM comments WHERE created_at>=?', (cutoff,)).fetchone()[0]

    def reply_count(self, comment_id):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM replies WHERE comment_id=?', (comment_id,)).fetchone()[0]

    def reply_exists(self, comment_id, parent_rpid):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM replies WHERE comment_id=? AND parent_rpid=?', (comment_id, parent_rpid)).fetchone())

    def save_reply(self, comment_id, parent_rpid, reply_rpid, message, status):
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO replies(comment_id,parent_rpid,reply_rpid,message,status,created_at) VALUES (?,?,?,?,?,?)', (comment_id, parent_rpid, reply_rpid, message, status, now()))
            return bool(result.rowcount)

    def update_reply(self, comment_id, parent_rpid, reply_rpid, status):
        with self.connect() as db:
            db.execute('UPDATE replies SET reply_rpid=?,status=? WHERE comment_id=? AND parent_rpid=?', (reply_rpid, status, comment_id, parent_rpid))

    def pending_mentions(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM comments WHERE mention_verified=1 AND notification_confirmed=0 AND status='pending_confirmation' ORDER BY id DESC")]

    def recent_runs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 10')]

    def get_setting(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, str(value)))

    def claim_dm_reply(self, account_uid, owner_uid, msg_key):
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO dm_replies(account_uid,owner_uid,msg_key,status,created_at) VALUES (?,?,?,?,?)',
                                (account_uid, owner_uid, str(msg_key), 'pending', now()))
            return bool(result.rowcount)

    def update_dm_reply(self, account_uid, owner_uid, msg_key, status):
        with self.connect() as db:
            db.execute('UPDATE dm_replies SET status=? WHERE account_uid=? AND owner_uid=? AND msg_key=?',
                       (status, account_uid, owner_uid, str(msg_key)))

    def dm_reply_count(self):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM dm_replies').fetchone()[0]

    def claim_mention_reply(self, account_uid, notification_id):
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO mention_replies(account_uid,notification_id,status,created_at) VALUES (?,?,?,?)',
                                (account_uid, str(notification_id), 'pending', now()))
            return bool(result.rowcount)

    def update_mention_reply(self, account_uid, notification_id, status):
        with self.connect() as db:
            db.execute('UPDATE mention_replies SET status=? WHERE account_uid=? AND notification_id=?',
                       (status, account_uid, str(notification_id)))

    def save_video_memory(self, account_uid, owner_uid, bvid, context, subtitle_available):
        with self.connect() as db:
            db.execute('''INSERT INTO video_memory(account_uid,owner_uid,bvid,context,subtitle_available,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(account_uid,owner_uid) DO UPDATE SET
                bvid=excluded.bvid,context=excluded.context,
                subtitle_available=excluded.subtitle_available,updated_at=excluded.updated_at''',
                (account_uid, owner_uid, str(bvid)[:32], str(context)[:7000],
                 int(subtitle_available), now()))

    def get_video_memory(self, account_uid, owner_uid, *, max_age_hours=2):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        with self.connect() as db:
            row = db.execute('''SELECT bvid,context,subtitle_available,updated_at FROM video_memory
                WHERE account_uid=? AND owner_uid=? AND updated_at>=?''',
                (account_uid, owner_uid, cutoff)).fetchone()
            return dict(row) if row else None

    def own_comment_by_root(self, aid, rpid):
        with self.connect() as db:
            row = db.execute('''SELECT comments.*,candidates.aid FROM comments
                JOIN candidates ON candidates.id=comments.candidate_id
                WHERE candidates.aid=? AND comments.rpid=? AND comments.status='ok' LIMIT 1''',
                (aid, rpid)).fetchone()
            return dict(row) if row else None

    def run_results(self, run_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT c.bvid,c.title,c.subtitle_available,j.score,j.reason,a.kind,a.status,a.error_code,a.request_json
                FROM candidates c LEFT JOIN judgments j ON j.candidate_id=c.id
                LEFT JOIN actions a ON a.candidate_id=c.id WHERE c.run_id=? ORDER BY c.id,a.id''', (run_id,))]

    def update_run_counts(self, run_id, candidates, selected):
        with self.connect() as db:
            db.execute('''UPDATE runs SET candidates_count=?,selected_count=?,
                actions_ok=(SELECT count(*) FROM actions a JOIN candidates c ON c.id=a.candidate_id WHERE c.run_id=? AND a.status='ok'),
                actions_failed=(SELECT count(*) FROM actions a JOIN candidates c ON c.id=a.candidate_id WHERE c.run_id=? AND a.status IN ('failed','mention_unverified')) WHERE id=?''', (candidates, selected, run_id, run_id, run_id))

    def dashboard_data(self, page=1, run_id=None):
        """Read a bounded snapshot for the local dashboard without a write-capable connection."""
        page = max(1, int(page))
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            total_runs = db.execute('SELECT count(*) FROM runs').fetchone()[0]
            runs = [dict(row) for row in db.execute(
                'SELECT * FROM runs ORDER BY id DESC LIMIT 10 OFFSET ?', ((page - 1) * 10,))]
            selected = (db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                        if run_id is not None else None)
            selected_run = dict(selected) if selected else (runs[0] if runs else None)
            totals = {
                'runs': total_runs,
                'videos': db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                'selected': db.execute('SELECT coalesce(sum(selected_count),0) FROM runs').fetchone()[0],
                'actions_ok': db.execute("SELECT count(*) FROM actions WHERE status='ok' AND kind IN ('like','favorite','comment','private_message')").fetchone()[0],
                'actions_failed': db.execute("SELECT count(*) FROM actions WHERE status='failed' AND kind IN ('like','favorite','comment','private_message')").fetchone()[0],
                'replies_ok': (db.execute("SELECT count(*) FROM replies WHERE status='ok'").fetchone()[0]
                               + db.execute("SELECT count(*) FROM mention_replies WHERE status='ok'").fetchone()[0]),
                'dm_replies_ok': db.execute("SELECT count(*) FROM dm_replies WHERE status='ok'").fetchone()[0],
            }
            videos = []
            if selected_run:
                rows = db.execute('''SELECT c.*,j.worth_learning,j.score,j.reason,j.takeaways_json,j.raw_json
                    FROM candidates c LEFT JOIN judgments j ON j.candidate_id=c.id
                    WHERE c.run_id=? ORDER BY c.id''', (selected_run['id'],))
                for row in rows:
                    video = dict(row)
                    video['takeaways'] = json.loads(video['takeaways_json'] or '[]')
                    video['decision'] = json.loads(video['raw_json'] or '{}')
                    action_rows = db.execute(
                        'SELECT kind,status,error_code,request_json FROM actions WHERE candidate_id=? ORDER BY id',
                        (video['id'],)).fetchall()
                    video['actions'] = {action['kind']: action['status'] for action in action_rows}
                    video['action_reasons'] = {
                        action['kind']: json.loads(action['request_json'] or '{}').get('reason', '')
                        for action in action_rows if action['status'] == 'skipped'}
                    video['action_errors'] = {action['kind']: action['error_code'] for action in db.execute(
                        'SELECT kind,error_code FROM actions WHERE candidate_id=? AND error_code IS NOT NULL',
                        (video['id'],))}
                    video['comments'] = []
                    for comment in db.execute('SELECT * FROM comments WHERE candidate_id=? ORDER BY id', (video['id'],)):
                        entry = dict(comment)
                        entry['replies'] = [dict(reply) for reply in db.execute(
                            'SELECT * FROM replies WHERE comment_id=? ORDER BY id', (entry['id'],))]
                        video['comments'].append(entry)
                    videos.append(video)
        return {'totals': totals, 'runs': runs, 'selected_run': selected_run,
                'videos': videos, 'page': page, 'has_next': page * 10 < total_runs}
