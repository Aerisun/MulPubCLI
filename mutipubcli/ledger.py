from __future__ import annotations

import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .core import Article, PublishResult, content_fingerprint
from .http import HTTPFailure, private_json


class ResultLedger:
    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def _locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory / '.publish.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _read(path: Path):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('not an object')
            return data
        except (ValueError, OSError):
            raise HTTPFailure('本地发布记录无法读取；停止提交，请先恢复记录', kind='local_state_invalid') from None

    @staticmethod
    def _remote_write(record):
        return bool(record.get('remote_id')) and record.get('stage') in ('draft', 'submitted')

    def _path(self, platform: str, article: Article) -> Path:
        content = json.dumps([article.title, article.body], ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()[:20]
        return self.directory / f"{platform}-{digest}.json"

    def _legacy_path(self, platform: str, article: Article) -> Path:
        digest = hashlib.sha256()
        digest.update(article.title.encode("utf-8"))
        digest.update(article.body.encode("utf-8"))
        digest.update(str(article.cover).encode("utf-8"))
        if article.cover.is_file():
            digest.update(article.cover.read_bytes())
        return self.directory / f"{platform}-{digest.hexdigest()[:20]}.json"

    def may_submit(self, platform: str, article: Article) -> bool:
        for path in (self._path(platform, article), self._legacy_path(platform, article)):
            if path.exists():
                data = self._read(path)
                if data.get("status") != "failed" or self._remote_write(data):
                    return False
        wanted = content_fingerprint(platform, article.title, article.body)
        legacy_text = hashlib.sha256((article.title + article.body).encode()).hexdigest()
        for path in self.directory.glob(f'{platform}-*.json'):
            data = self._read(path)
            proof = data.get('content_check')
            if not isinstance(proof, dict) or proof.get('version') != 1 or not proof.get('sha256'):
                raise HTTPFailure('存在未关联原稿的旧发布记录，请用 migrate-record 校验迁移；不会冒险重复投稿', kind='legacy_journal_requires_migration')
            if (all(proof.get(key) == value for key, value in wanted.items())
                    or data.get('legacy_migration', {}).get('text_sha256') == legacy_text):
                if data.get('status') != 'failed' or self._remote_write(data):
                    return False
        return True

    def migrate_record(self, platform: str, article: Article, *, original_cover=None):
        """Associate a legacy record only after recomputing its original full-input key."""
        with self._locked():
            path = self._path(platform, article)
            if original_cover is not None:
                digest = hashlib.sha256(article.title.encode() + article.body.encode() + original_cover.encode() + article.cover.read_bytes()).hexdigest()[:20]
                path = self.directory / f'{platform}-{digest}.json'
            if not path.is_file():
                raise HTTPFailure('原稿、原封面路径及文件字节未匹配旧记录，未修改任何记录', kind='legacy_record_mismatch')
            data = self._read(path)
            wanted = content_fingerprint(platform, article.title, article.body)
            if data.get('content_check') and any(data['content_check'].get(key) != value for key, value in wanted.items()):
                raise HTTPFailure('已有记录内容指纹冲突，未修改', kind='local_state_invalid')
            data.setdefault('content_check', wanted)
            data.setdefault('legacy_migration', {'original_cover': original_cover, 'source_record': path.name,
                'at': datetime.now(timezone.utc).isoformat(), 'basis': 'exact_original_key'})
            if original_cover is not None:
                # The old hash had no delimiter between title/body. Preserve its ambiguity conservatively.
                data['legacy_migration']['text_sha256'] = hashlib.sha256((article.title + article.body).encode()).hexdigest()
            private_json(path, data)
            return path

    def reserve(self, platform: str, article: Article, *, daily_limit: int = 2) -> bool:
        """Serialize reservations across CLI processes before any network mutation."""
        with self._locked():
            if not self.may_submit(platform, article):
                return False
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            count = 0
            for path in self.directory.glob(f'{platform}-*.json'):
                record = self._read(path)
                if record.get('status') == 'failed' and not self._remote_write(record):
                    continue
                stamp = record.get('reserved_at', record.get('saved_at'))
                try:
                    # Unknown dates conservatively occupy quota until reconciled.
                    if not stamp or datetime.fromisoformat(stamp) >= cutoff:
                        count += 1
                except (ValueError, TypeError):
                    raise HTTPFailure('本地记录时间无效；停止提交，请先恢复记录', kind='local_state_invalid') from None
            if count >= daily_limit:
                return False
            self._save(platform, article, PublishResult('pending', '已预留提交；中断或超时后先核验，不自动重发', platform=platform), new_reservation=True)
            return True

    def save(self, platform: str, article: Article, result: PublishResult) -> Path:
        with self._locked():
            return self._save(platform, article, result)

    def _save(self, platform: str, article: Article, result: PublishResult, *, new_reservation=False) -> Path:
        path = self._path(platform, article)
        previous = self._read(path) if path.exists() else {}
        now = datetime.now(timezone.utc).isoformat()
        reserved = now if new_reservation else previous.get('reserved_at', previous.get('saved_at', now))
        payload = {"title": article.title, **previous, **asdict(result), "platform": platform, "saved_at": now, 'reserved_at': reserved}
        payload.setdefault('content_check', content_fingerprint(platform, article.title, article.body))
        private_json(path, payload)
        return path

    def checkpoint(self, platform: str, article: Article, stage: str, remote_id: str):
        with self._locked():
            path = self._path(platform, article)
            payload = self._read(path) if path.exists() else {'status': 'pending', 'platform': platform}
            proof = payload.setdefault('content_check', content_fingerprint(platform, article.title, article.body))
            if stage == 'uploaded':
                proof['media'] = [str(remote_id)]
            payload.update(stage=stage, remote_id=str(remote_id))
            private_json(path, payload)

    def _matching(self, platform: str, remote_id: str):
        matches = [(path, self._read(path)) for path in self.directory.glob(f'{platform}-*.json')]
        matches = [(path, data) for path, data in matches if str(data.get('remote_id', '')) == remote_id and self._remote_write(data)]
        if len(matches) > 1:
            raise HTTPFailure('多个本地记录使用同一远端 ID；需先核对记录', kind='local_state_invalid')
        return matches[0] if matches else (None, {})

    def verification_evidence(self, platform: str, remote_id: str, article: Article | None = None):
        with self._locked():
            _, record = self._matching(platform, remote_id)
            proof = record.get('content_check', {}).copy()
            if article is not None:
                provided = content_fingerprint(platform, article.title, article.body)
                if 'sha256' in proof and any(proof.get(key) != value for key, value in provided.items()):
                    raise ValueError('核验稿件与原提交指纹不同，停止核验')
                proof.update(provided)
            return proof

    def reconcile(self, platform: str, remote_id: str, result: PublishResult, *, evidence=None) -> bool:
        """Only update an already checkpointed article; this never permits resubmission."""
        if result.platform not in (None, platform) or result.status not in ('published', 'pending', 'failed'):
            raise ValueError('核验结果的平台或状态无效')
        with self._locked():
            path, data = self._matching(platform, remote_id)
            if path is None:
                return False
            now = datetime.now(timezone.utc).isoformat()
            data.setdefault('reserved_at', data.get('saved_at', now))
            # A temporary unreadable result cannot erase evidence of a previous successful publication.
            if data.get('status') == 'published':
                data['last_published'] = {key: data.get(key) for key in ('status', 'message', 'url', 'saved_at', 'verification')}
            if not (data.get('status') == 'published' and result.status == 'pending' and result.verification != 'mismatch'):
                data.update(asdict(result), platform=platform)
            if result.verification == 'verified' and evidence:
                data['content_check'] = evidence
            data.update(saved_at=now, last_verification={**asdict(result), 'at': now})
            private_json(path, data)
            return True
