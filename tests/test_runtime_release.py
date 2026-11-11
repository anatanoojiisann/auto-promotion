import pytest
from auto_promotion.runtime_release import build, verify


def test_manifest_catches_noop_patch_unexpected_changes_and_new_files(tmp_path):
    src=tmp_path/'src';src.mkdir();(src/'module.py').write_text('original',encoding='utf-8')
    roots={'service':src};old=build(roots)
    with pytest.raises(ValueError):build(roots,previous=old,expected_changes=['service/module.py'])
    (src/'module.py').write_text('fixed',encoding='utf-8')
    current=build(roots,previous=old,expected_changes=['service/module.py'])
    assert verify(current,roots)
    (src/'extra.py').write_text('unexpected',encoding='utf-8')
    with pytest.raises(ValueError):verify(current,roots)


def test_manifest_excludes_private_and_cache_directories(tmp_path):
    (tmp_path/'module.py').write_text('safe',encoding='utf-8')
    (tmp_path/'.local').mkdir();(tmp_path/'.local'/'private.json').write_text('{"secret":"not-exported"}',encoding='utf-8')
    result=build({'source':tmp_path})
    assert set(result['files'])=={'source/module.py'}


def test_reported_running_process_must_match_reviewed_release():
    from auto_promotion.runtime_release import verify_reported_runtime
    expected={"component":"sender","git_commit_at_start":"new","source_fingerprint":"a"*64}
    assert verify_reported_runtime(expected,{**expected,"pid":123})
    with pytest.raises(ValueError):verify_reported_runtime(expected,{**expected,"pid":123,"git_commit_at_start":"old"})
    with pytest.raises(ValueError):verify_reported_runtime(expected,{**expected,"pid":None})
