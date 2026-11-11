from types import SimpleNamespace
import pytest
from auto_promotion.store import Store
from auto_promotion.workflow_health import heartbeat,status


def test_server_presence_does_not_mask_missing_or_stale_worker(tmp_path):
    now=[1000.0];engine=SimpleNamespace(store=Store(tmp_path/'test.db'),clock=lambda:now[0])
    assert status(engine)['coordinator_loop_alive'] is False
    heartbeat(engine,{'worker_id':'synthetic-worker','task_queue':'synthetic-queue'})
    assert status(engine)['coordinator_loop_alive'] is True
    assert status(engine)['publication_permission'] is False
    now[0]+=31
    assert status(engine)['coordinator_loop_alive'] is False
    with pytest.raises(ValueError):heartbeat(engine,{'observed_at':now[0]})
