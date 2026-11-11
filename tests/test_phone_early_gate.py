from unittest.mock import patch
import unittest

from auto_promotion.queue_store import JobQueue
from test_persistent_pools import PersistentPoolTests


class EarlyGateTests(unittest.TestCase):
    setUp=PersistentPoolTests.setUp
    ready=PersistentPoolTests.ready
    def test_deterministic_blocker_stays_unleased_until_local_state_changes(self):
        task,account=self.ready()
        queue=JobQueue(self.engine);queue.sync(task['id'])
        job=self.store.one("SELECT * FROM execution_jobs WHERE task_id=?",(task['id'],))
        with patch('auto_promotion.phone_local_gate.blocker',return_value='meta_outcome_unknown'):
            for _ in range(10):assert queue.claim(job['pool'],'synthetic-worker') is None
            queue.recover()
        row=self.store.one("SELECT * FROM execution_jobs WHERE id=?",(job['id'],))
        assert row['state']=='pending' and row['owner'] is None
        assert 'local_state_change' in row['result']
        assert self.store.one('SELECT count(*) n FROM attempts')['n']==0
        with patch('auto_promotion.phone_local_gate.blocker',return_value=None):
            queue.recover()
            assert queue.claim(job['pool'],'synthetic-worker') is not None
