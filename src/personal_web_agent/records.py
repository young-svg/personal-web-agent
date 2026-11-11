"""Small, synchronous SQLite journal. No browser data or arbitrary error text."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import hashlib
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4
from urllib.parse import urlsplit

DEFAULT_DB = Path(__file__).resolve().parents[2] / '.local/tasks/tasks.sqlite3'
TRANSITIONS = {
    'CREATED': {'RUNNING', 'FAILED', 'UNKNOWN'},
    'RUNNING': {'REVIEW_REQUIRED', 'FAILED', 'UNKNOWN'},
    'REVIEW_REQUIRED': {'OUTCOME_PENDING', 'CANCELLED', 'UNKNOWN'},
    'OUTCOME_PENDING': {'COMPLETED', 'CANCELLED', 'UNKNOWN'},
    'COMPLETED': set(), 'CANCELLED': set(), 'FAILED': set(), 'UNKNOWN': set(),
}
SUMMARIES = {'created', 'running', 'review_required', 'outcome_pending',
             'verified_completed', 'verified_cancelled', 'verified_no_effect_failure',
             'unverified_exit'}


class TaskBlocked(ValueError):
    pass


def identity(inputs):
    if getattr(inputs, 'task_type', '') == 'publish':
        if getattr(inputs, 'site', 'fixture') == 'csdn':
            from personal_web_agent.csdn import editor_url, SITE
            import re
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', inputs.local_user_id):
                raise ValueError('Explicit opaque local user identity is required')
            import json
            payload = json.dumps(['publish', 'csdn', inputs.local_user_id, inputs.title, inputs.body,
                                  *inputs.image_hashes], ensure_ascii=False, separators=(',', ':'))
            return SITE, editor_url(inputs.url), hashlib.sha256(payload.encode()).hexdigest()
        if getattr(inputs, 'site', 'fixture') != 'fixture':
            raise ValueError('Unsupported publication platform')
        parsed = urlsplit(inputs.url)
        origin = f'http://127.0.0.1:{parsed.port}'
        payload = '\0'.join(['publish', origin, 'fixture-local-user', inputs.title, inputs.body, *inputs.image_hashes])
        return origin, origin + '/publish/', hashlib.sha256(payload.encode()).hexdigest()
    if getattr(inputs, 'site', 'fixture') == 'greenhouse':
        from personal_web_agent.greenhouse import job_identity
        board, job, canonical = job_identity(inputs.url)
        user = inputs.local_user_id
        if not user:
            raise ValueError('Explicit local user identity is required')
        digest = hashlib.sha256(('apply-prepare\0greenhouse\0' + board + '\0' + job + '\0' + user).encode()).hexdigest()
        return 'https://job-boards.greenhouse.io/' + board, canonical, digest
    # Local fixture only: URL identifies the target, synthetic email the user.
    # Query modes intentionally share identity; changing PDF/instructions cannot bypass it.
    parsed = urlsplit(inputs.url)
    site = f'{parsed.scheme}://{parsed.netloc.lower()}'
    url = site + (parsed.path or '/')
    digest = hashlib.sha256(('apply-prepare\0' + url + '\0' + inputs.values['email'].strip().lower()).encode()).hexdigest()
    return site, inputs.url, digest


class TaskStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Precreate privately before SQLite opens it (including on first use).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                site TEXT NOT NULL, target_url TEXT NOT NULL, dedup_key TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('CREATED','RUNNING','REVIEW_REQUIRED',
                'OUTCOME_PENDING','COMPLETED','CANCELLED','FAILED','UNKNOWN')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                summary TEXT NOT NULL, no_side_effects INTEGER NOT NULL DEFAULT 0
            )''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            for name in ('article_id', 'article_url'):
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS task_dedup ON tasks(dedup_key)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('Task not found')
        return dict(row)

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY created_at DESC')]

    def create(self, inputs, *, confirm_no_side_effects=False):
        site, url, key = identity(inputs)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT task_id,state,no_side_effects FROM tasks WHERE dedup_key=?', (key,)).fetchall()
            for row in previous:
                if row['state'] not in {'FAILED', 'CANCELLED'}:
                    raise TaskBlocked(f"Duplicate blocked: {row['task_id']} {row['state']}; inspect existing task; no automatic retry")
                if not row['no_side_effects'] or not confirm_no_side_effects:
                    raise TaskBlocked('Retry requires verified no side effects AND --confirm-no-side-effects')
            task_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO tasks (task_id,task_type,site,target_url,dedup_key,state,created_at,updated_at,summary,no_side_effects) VALUES (?,?,?,?,?,?,?,?,?,0)',
                       (task_id, getattr(inputs, 'task_type', 'apply-prepare'), site, url, key, 'CREATED', now, now, 'created'))
        return task_id

    def transition(self, task_id, state, summary, *, no_side_effects=False, article_id=None, article_url=None):
        expected = {'CREATED': 'created', 'RUNNING': 'running', 'REVIEW_REQUIRED': 'review_required',
                    'OUTCOME_PENDING': 'outcome_pending', 'COMPLETED': 'verified_completed',
                    'CANCELLED': 'verified_cancelled', 'FAILED': 'verified_no_effect_failure',
                    'UNKNOWN': 'unverified_exit'}
        if summary not in SUMMARIES or expected.get(state) != summary:
            raise ValueError('Only fixed redacted summary codes may be persisted')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,task_type,site FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None or state not in TRANSITIONS[row['state']]:
                raise ValueError('Illegal task state transition')
            if state in {'CANCELLED', 'FAILED'} and not no_side_effects:
                raise ValueError('Known no-side-effects evidence is required')
            if row['task_type'] == 'publish' and state == 'COMPLETED':
                if row['site'] == 'https://blog.csdn.net':
                    from personal_web_agent.csdn import article_identity
                    parsed = article_identity(article_url) if article_url else None
                    if not parsed or parsed[1] != article_id or parsed[2] != article_url:
                        raise ValueError('Matching CSDN article ID and canonical URL are required')
                elif not article_id or str(UUID(article_id)) != article_id or article_url != row['site'] + '/publish/articles/' + article_id:
                    raise ValueError('A verified local publication ID and URL are required')
            elif article_id is not None or article_url is not None:
                raise ValueError('Publication evidence only belongs to COMPLETED publish tasks')
            db.execute('UPDATE tasks SET state=?,summary=?,updated_at=?,no_side_effects=?,article_id=?,article_url=? WHERE task_id=?',
                       (state, summary, datetime.now(timezone.utc).isoformat(), int(no_side_effects), article_id, article_url, task_id))
