import json
import sqlite3
from datetime import datetime, timezone
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
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, candidates_count INTEGER DEFAULT 0, selected_count INTEGER DEFAULT 0, actions_ok INTEGER DEFAULT 0, actions_failed INTEGER DEFAULT 0, error_code TEXT);
            CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id), bvid TEXT, aid INTEGER, cid INTEGER, title TEXT, description TEXT, author TEXT, duration INTEGER, pubdate INTEGER, subtitle_available INTEGER DEFAULT 0, seen_at TEXT, UNIQUE(run_id,bvid));
            CREATE TABLE IF NOT EXISTS judgments (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), worth_learning INTEGER, score INTEGER, reason TEXT, takeaways_json TEXT, risk TEXT, model TEXT, raw_json TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), kind TEXT, status TEXT, request_json TEXT, response_json TEXT, error_code TEXT, created_at TEXT, UNIQUE(candidate_id,kind));
            CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id), rpid INTEGER, message TEXT, mention_uid INTEGER, mention_verified INTEGER DEFAULT 0, notification_confirmed INTEGER DEFAULT 0, status TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS replies (id INTEGER PRIMARY KEY, comment_id INTEGER REFERENCES comments(id), parent_rpid INTEGER, reply_rpid INTEGER, message TEXT, status TEXT, created_at TEXT, UNIQUE(comment_id,parent_rpid));
            CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, platform TEXT, uid INTEGER, nickname TEXT, credential_blob BLOB, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            ''')

    def start_run(self):
        with self.connect() as db:
            return db.execute('INSERT INTO runs(started_at,status) VALUES (?,?)', (now(), 'running')).lastrowid

    def finish_run(self, run_id, status, error_code=None):
        with self.connect() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,error_code=? WHERE id=?', (now(), status, error_code, run_id))

    def add_candidate(self, run_id, item):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO candidates(run_id,bvid,aid,cid,title,description,author,duration,pubdate,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)', (run_id, item['bvid'], item.get('aid'), item.get('cid'), item.get('title'), item.get('description'), item.get('author'), item.get('duration'), item.get('pubdate'), now()))
            return db.execute('SELECT id FROM candidates WHERE run_id=? AND bvid=?', (run_id, item['bvid'])).fetchone()['id']

    def seen_bvid(self, bvid):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM actions a JOIN candidates c ON c.id=a.candidate_id WHERE c.bvid=? AND a.kind NOT IN ('draft','judge') LIMIT 1", (bvid,)).fetchone())

    def record_judgment(self, candidate_id, judgment, model):
        with self.connect() as db:
            db.execute('INSERT INTO judgments(candidate_id,worth_learning,score,reason,takeaways_json,risk,model,raw_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (candidate_id, int(judgment.worth_learning), judgment.score, judgment.reason, json.dumps(judgment.takeaways, ensure_ascii=False), judgment.risk, model, judgment.model_dump_json(), now()))

    def set_subtitle_available(self, candidate_id):
        with self.connect() as db:
            db.execute('UPDATE candidates SET subtitle_available=1 WHERE id=?', (candidate_id,))

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
