"""本机 SQLite 会话事实来源；事务提交和版本检查阻止在途结果复活删除内容。"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sessions.lifecycle import TRANSITIONS


def now():
    return datetime.now(timezone.utc).isoformat()


class SessionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            with connection:
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA secure_delete=ON')
                yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                raise SessionError('unsupported_schema')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                context TEXT NOT NULL DEFAULT '{}', context_source TEXT);
            CREATE TABLE IF NOT EXISTS turns (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL, message TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, response TEXT,
                PRIMARY KEY(session_id, request_id));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(turns)')}
            for column in ('updated_at', 'reason'):
                if column not in columns:
                    db.execute(f'ALTER TABLE turns ADD COLUMN {column} TEXT')
            db.execute("UPDATE turns SET status='interrupted',reason='service_restart',updated_at=? WHERE status IN ('pending','accepted','running','cancelling')", (now(),))
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    def _session(self, db, owner, sid):
        row = db.execute('SELECT * FROM sessions WHERE id=? AND owner=?', (sid, owner)).fetchone()
        if row is None:
            raise SessionError('not_found')
        return dict(row)

    def _version(self, session, version):
        if session['version'] != version:
            raise SessionError('version_conflict')

    def create(self, owner):
        sid = uuid.uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO sessions(id,owner,title,updated_at) VALUES(?,?,?,?)', (sid, owner, '新对话', now()))
        return self.read(owner, sid)

    def list(self, owner):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT id,title,updated_at,version FROM sessions WHERE owner=? ORDER BY updated_at DESC', (owner,))]

    def read(self, owner, sid):
        with self.connection() as db:
            session = self._session(db, owner, sid)
            session.pop('owner')
            session['context'] = json.loads(session['context'])
            session['turns'] = [self._turn(row) for row in db.execute('SELECT * FROM turns WHERE session_id=? ORDER BY rowid', (sid,))]
            return session

    def _turn(self, row):
        return {key: (json.loads(row[key]) if row[key] else None) if key == 'response' else row[key]
                for key in ['request_id', 'message', 'status', 'created_at', 'updated_at', 'reason', 'response']}

    def turn(self, owner, sid, rid):
        with self.connection() as db:
            self._session(db, owner, sid)
            row = db.execute('SELECT * FROM turns WHERE session_id=? AND request_id=?', (sid, rid)).fetchone()
            if row is None:
                raise SessionError('not_found')
            return self._turn(row)

    def begin(self, owner, sid, rid, version, message, options):
        fingerprint = hashlib.sha256(json.dumps([message, options], sort_keys=True).encode()).hexdigest()
        with self.connection() as db:
            session = self._session(db, owner, sid)
            previous = db.execute('SELECT * FROM turns WHERE session_id=? AND request_id=?', (sid, rid)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise SessionError('request_conflict')
                return False, self._turn(previous)
            self._version(session, version)
            if db.execute("SELECT 1 FROM turns WHERE session_id=? AND status IN ('accepted','running','cancelling')", (sid,)).fetchone():
                raise SessionError('busy')
            db.execute('INSERT INTO turns(session_id,request_id,message,fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (sid, rid, message, fingerprint, 'accepted', now(), now()))
            db.execute('UPDATE sessions SET version=version+1,updated_at=?,title=CASE WHEN title=? THEN ? ELSE title END WHERE id=?', (now(), '新对话', message[:40], sid))
            return True, version + 1

    def complete(self, owner, sid, rid, version, response, status='completed', reason=None):
        with self.connection() as db:
            session = self._session(db, owner, sid)
            self._version(session, version)
            previous = db.execute('SELECT status FROM turns WHERE session_id=? AND request_id=?', (sid, rid)).fetchone()
            if previous is None or status not in TRANSITIONS.get(previous['status'], set()):
                raise SessionError('invalid_transition')
            changed = db.execute("UPDATE turns SET response=?,status=?,reason=?,updated_at=? WHERE session_id=? AND request_id=? AND status IN ('accepted','running','cancelling')", (json.dumps(response, ensure_ascii=False) if response else None, status, reason, now(), sid, rid)).rowcount
            if not changed:
                raise SessionError('request_conflict')
            db.execute('UPDATE sessions SET version=version+1,updated_at=? WHERE id=?', (now(), sid))
        return self.turn(owner, sid, rid)

    def update_context(self, owner, sid, version, context):
        with self.connection() as db:
            session = self._session(db, owner, sid)
            self._version(session, version)
            if db.execute("SELECT 1 FROM turns WHERE session_id=? AND status IN ('accepted','running','cancelling')", (sid,)).fetchone():
                raise SessionError('busy')
            db.execute('UPDATE sessions SET context=?,context_source=?,version=version+1,updated_at=? WHERE id=?', (json.dumps(context, ensure_ascii=False), 'user-edit:' + uuid.uuid4().hex, now(), sid))
        return self.read(owner, sid)

    def delete(self, owner, sid, version):
        with self.connection() as db:
            session = self._session(db, owner, sid)
            self._version(session, version)
            db.execute('DELETE FROM sessions WHERE id=?', (sid,))

    def transition(self, owner, sid, rid, target):
        with self.connection() as db:
            self._session(db, owner, sid)
            row = db.execute('SELECT status FROM turns WHERE session_id=? AND request_id=?', (sid, rid)).fetchone()
            if row is None:
                raise SessionError('not_found')
            if row['status'] == target:
                return
            if target not in TRANSITIONS.get(row['status'], set()):
                raise SessionError('invalid_transition')
            db.execute('UPDATE turns SET status=?,updated_at=? WHERE session_id=? AND request_id=?', (target, now(), sid, rid))
