import json
from unittest.mock import patch

from auto_promotion.cloud_control import CloudUnavailable
from auto_promotion.execution_channels import binding
from auto_promotion.local_authorization import activate_local_task_handoff
from auto_promotion.models import canonical, digest
from auto_promotion.queue_store import JobQueue, job_scope
from auto_promotion.task_channels import TaskChannelRouter, channel_binding, save_binding
from auto_promotion.x_history_ack import record_self_unknown_exclusion
from test_x_history_ack import XHistoryAcknowledgementTests


class SelfUnknownTests(XHistoryAcknowledgementTests):
    def settle_unknown(self):
        self.ack()
        JobQueue(self.engine).sync(self.new['id'])
        with job_scope({'owner': 'test-owner'}), self.store.connect(write=True) as db:
            job_id = 'send:' + self.item['id']
            db.execute("UPDATE execution_jobs SET state='leased',owner='test-owner',lease_until=? WHERE id=?",
                       (self.engine.clock() + 120, job_id))
            db.execute("INSERT OR REPLACE INTO account_task_channels VALUES(?,?,?,?,?,'ready','cloud_acknowledged',?,NULL,0)",
                       (self.account['id'], 'local', 'test-generation', self.engine.clock()+3600, self.new['id'], self.engine.clock()))
            db.execute("INSERT OR REPLACE INTO task_channel_claims VALUES(?,?,?,?)",
                       (job_id, 'test-owner', 'test-generation', 'local'))
            db.execute("UPDATE items SET status='unknown',attempt_id='own-attempt',claimed_at=?,result=? WHERE id=?",
                       (self.engine.clock() - 2, canonical({'status': 'unknown'}), self.item['id']))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('own-attempt', self.item['id'], self.account['id'], self.new['id'],
                        'target', 'user', 'live', self.engine.clock()-2, self.engine.clock()-1, 'unknown'))
            db.execute("UPDATE execution_jobs SET state='unknown',owner=NULL,lease_until=0 WHERE reference_id=?",
                       (self.item['id'],))

    def exclude(self):
        return record_self_unknown_exclusion(self.engine, self.new['id'], self.account['id'],
                                             self.item['id'], source='explicit user: exclude unknown, continue distinct targets')

    def test_self_unknown_is_excluded_without_changing_original(self):
        self.settle_unknown()
        before = self.store.one('SELECT * FROM items WHERE id=?', (self.item['id'],))
        self.assertIsNone(self.allowed())
        self.assertFalse(self.exclude()['unchanged'])
        self.assertTrue(self.exclude()['unchanged'])
        self.assertEqual(self.allowed(), {self.old_item['id'], self.item['id']})
        self.scope()
        self.assertEqual(before, self.store.one('SELECT * FROM items WHERE id=?', (self.item['id'],)))
        self.assertEqual(self.store.one('SELECT count(*) n FROM attempts')['n'], 2)

    def test_self_unknown_requires_a_settled_single_attempt_and_job(self):
        for query in (
            "UPDATE attempts SET outcome='claimed' WHERE id='own-attempt'",
            "UPDATE attempts SET finished_at=NULL WHERE id='own-attempt'",
            "UPDATE execution_jobs SET state='pending' WHERE reference_id='%s'" % self.item['id'],
            "UPDATE items SET external_reference='some-reference' WHERE id='%s'" % self.item['id'],
        ):
            with self.subTest(query=query):
                self.settle_unknown()
                with self.store.connect(write=True) as db:
                    db.execute(query)
                with self.assertRaisesRegex(ValueError, 'SelfUnknownUnsafe'):
                    self.exclude()
                # Restore the fixture for the next subtest.
                with self.store.connect(write=True) as db:
                    db.execute("DELETE FROM attempts WHERE id='own-attempt'")
                    db.execute("UPDATE items SET status='not_attempted',attempt_id=NULL,claimed_at=NULL,external_reference=NULL,result='{}' WHERE id=?", (self.item['id'],))
                    db.execute("DELETE FROM execution_jobs WHERE reference_id=?", (self.item['id'],))
                    db.execute("DELETE FROM task_channel_claims WHERE job_id=?", ('send:' + self.item['id'],))
                    db.execute("DELETE FROM account_task_channels WHERE account_id=?", (self.account['id'],))
                    db.execute("DELETE FROM meta WHERE key=?", ('x_history_ack:' + self.new['id'] + ':' + self.account['id'],))

    def test_changed_self_unknown_or_stop_invalidates_authority(self):
        self.settle_unknown()
        self.exclude()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE attempts SET outcome='confirmed' WHERE id='own-attempt'")
        self.assertIsNone(self.allowed())
        with self.assertRaisesRegex(CloudUnavailable, 'XHistoryAcknowledgementChanged'):
            self.scope()

    def test_new_unknown_after_exclusion_invalidates_authority(self):
        self.settle_unknown()
        self.exclude()
        payload = json.loads(self.old_item['payload'])
        payload['target'] = 'https://x.com/example/status/999'
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at,status) VALUES(?,?,?,?,?,?,?,'unknown')",
                       ('new-unapproved-unknown', self.old['id'], self.account['id'], 5,
                        canonical(payload), digest(payload), self.engine.clock()))
        self.assertIsNone(self.allowed())

    def test_self_unknown_requires_prior_frozen_ack_and_user_source(self):
        with self.assertRaisesRegex(ValueError, 'Required'):
            record_self_unknown_exclusion(self.engine, self.new['id'], self.account['id'], self.item['id'], source='')
        with self.assertRaisesRegex(ValueError, 'AcknowledgementRequired'):
            self.exclude()

    def test_exact_new_target_can_handoff_under_authenticated_local_scope(self):
        self.settle_unknown()
        self.exclude()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='simulation' WHERE id=?", (self.account['id'],))
        dest, _ = self.fixture.ready(account=self.account, target_id=333, channel='local', start=False)
        self.engine.prepare(dest['id'])
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET mode='live',status='healthy',identity_hash=? WHERE id=?", ('a'*64, self.account['id']))
            db.execute("UPDATE tasks SET mode='live',state='active',phase='ready' WHERE id=?", (dest['id'],))
        dest = self.store.one('SELECT * FROM tasks WHERE id=?', (dest['id'],))
        item = self.store.one('SELECT * FROM items WHERE task_id=?', (dest['id'],))
        store_id = binding(self.engine)
        now = self.engine.clock()
        account_id = self.account['id']
        state = {'active': 'local', 'authorization': 'local_queue', 'reason': 'explicit_user_local',
                 'offline_only': True, 'until': now + 4000, 'node_id': 'test-node', 'store_id': store_id,
                 'generation': 'local-generation', 'account_ids': [account_id],
                 'account_authorizations': {account_id: {'source': 'explicit_user_local_task'}}}
        bound = {'account_id': account_id, 'task_id': self.new['id'], 'node_id': 'test-node',
                 'store_id': store_id, 'channel': 'local', 'generation': 'test-generation',
                 'until_at': now+3600, 'fence_source': 'cloud_acknowledged',
                 'coordination_task_id': self.new['id'], 'coordination_manifest_hash': self.store.one('SELECT manifest_hash FROM tasks WHERE id=?', (self.new['id'],))['manifest_hash'],
                 'x_history_ack_required': True}
        with self.store.connect(write=True) as db:
            for key, value in [('execution_channel_policy', 'cloud_then_local'),
                               ('local_channel_authority', canonical({'account_ids': [account_id]})),
                               ('execution_channel', canonical(state))]:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))
            save_binding(db, bound)
        with patch.dict('os.environ', {'AP_NODE_ID': 'test-node'}):
            JobQueue(self.engine).sync(dest['id'])
            with self.store.connect(write=True) as db:
                db.execute("UPDATE attempts SET outcome='confirmed' WHERE id='own-attempt'")
            with self.assertRaisesRegex(ValueError, 'OwnershipUnverified'):
                activate_local_task_handoff(self.engine, self.new['id'], dest['id'], account_id, item['id'], source='explicit')
            with self.store.connect(write=True) as db:
                db.execute("UPDATE attempts SET outcome='unknown' WHERE id='own-attempt'")
                db.execute("UPDATE meta SET value='1' WHERE key='global_stop'")
            with self.assertRaisesRegex(ValueError, 'global_stop'):
                activate_local_task_handoff(self.engine, self.new['id'], dest['id'], account_id, item['id'], source='explicit')
            with self.store.connect(write=True) as db:
                db.execute("UPDATE meta SET value='0' WHERE key='global_stop'")
            with self.assertRaisesRegex(ValueError, 'TargetUnsafe'):
                activate_local_task_handoff(self.engine, self.new['id'], dest['id'], account_id, self.item['id'], source='explicit')
            duplicate = json.loads(item['payload'])
            duplicate['target'] = json.loads(self.item['payload'])['target']
            with self.store.connect(write=True) as db:
                db.execute('UPDATE items SET payload=?,payload_hash=? WHERE id=?', (canonical(duplicate), digest(duplicate), item['id']))
            with self.assertRaisesRegex(ValueError, 'TargetUnsafe'):
                activate_local_task_handoff(self.engine, self.new['id'], dest['id'], account_id, item['id'], source='explicit')
            with self.store.connect(write=True) as db:
                db.execute('UPDATE items SET payload=?,payload_hash=? WHERE id=?', (item['payload'], item['payload_hash'], item['id']))
            permit = activate_local_task_handoff(self.engine, self.new['id'], dest['id'], account_id, item['id'],
                                                source='explicit user: this distinct target only')
            self.assertEqual(permit['channel'], 'local')
            self.assertFalse(permit['attempt_created'])
            # Completion of the older task must not invalidate the account-local
            # authority before the later target becomes due. Also exercise renewal
            # when the previous fence ends before the later task window.
            with self.store.connect(write=True) as db:
                db.execute("UPDATE tasks SET state='completed' WHERE id=?", (self.new['id'],))
                prior = channel_binding(db, account_id)
                prior['until_at'] = now + 100
                save_binding(db, prior)
                db.execute('UPDATE account_task_channels SET until_at=? WHERE account_id=?', (now + 100, account_id))
            job_row = self.store.one('SELECT pool FROM execution_jobs WHERE reference_id=?', (item['id'],))
            job = JobQueue(self.engine).claim(job_row['pool'], 'test-worker', reference_id=item['id'])
            self.assertIsNotNone(job)
            with patch('auto_promotion.cloud_control.CloudControl.configured', side_effect=AssertionError('unexpected remote call')):
                context, cloud = TaskChannelRouter(self.engine).prepare(job)
            self.assertIsNone(cloud)
            self.assertEqual(context['task_id'], dest['id'])
            self.assertEqual(context['channel'], 'local')
            self.assertGreaterEqual(self.store.one('SELECT until_at FROM account_task_channels WHERE account_id=?', (account_id,))['until_at'], now + 3600)
            self.assertEqual(self.store.one('SELECT status FROM items WHERE id=?', (self.item['id'],))['status'], 'unknown')
            self.assertEqual(self.store.one('SELECT count(*) n FROM attempts')['n'], 2)
