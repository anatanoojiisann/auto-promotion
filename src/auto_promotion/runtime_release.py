"""Build and verify code-only release manifests without starting a service."""
import argparse
import hashlib
import json
from pathlib import Path


def sources(roots):
    values={}
    for name, root in roots.items():
        root=Path(root).resolve(strict=True)
        if not root.is_dir():raise ValueError("SourceDirectoryRequired")
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.suffix in {'.py','.json','.js'} and '__pycache__' not in path.parts:
                if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('SourceEscapedRoot')
                relative=path.relative_to(root).as_posix()
                if any(part.startswith('.') for part in Path(relative).parts):continue
                values[name+'/'+relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def build(roots, *, previous=None, expected_changes=None):
    files=sources(roots)
    if previous is not None:
        old=previous['files']
        changed={name for name in files.keys()|old.keys() if files.get(name)!=old.get(name)}
        if expected_changes is not None and changed!=set(expected_changes):
            raise ValueError('ReleaseChangedFilesMismatch')
    body={'version':'aurax.release/1','files':files}
    body['source_fingerprint']=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return body


def verify(manifest, roots):
    actual=build(roots)
    if manifest!=actual:raise ValueError('ReleaseFingerprintMismatch')
    return True


def verify_reported_runtime(expected, observed):
    """An HTTP success or surviving old PID never establishes a loaded release."""
    fields=("component","git_commit_at_start","source_fingerprint")
    if any(not expected.get(k) or expected.get(k)!=observed.get(k) for k in fields):
        raise ValueError("RunningReleaseMismatch")
    if not isinstance(observed.get("pid"),int) or observed["pid"]<=0:
        raise ValueError("RunningProcessIdentityMissing")
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',action='append',required=True,help='name=absolute source directory')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--base',type=Path)
    parser.add_argument('--expect-change',action='append')
    args=parser.parse_args()
    roots=dict(value.split('=',1) for value in args.source)
    old=json.loads(args.base.read_text(encoding='utf-8')) if args.base else None
    manifest=build(roots,previous=old,expected_changes=args.expect_change)
    verify(manifest,roots)
    with args.output.open('x',encoding='utf-8') as output:json.dump(manifest,output,indent=2)
    print(json.dumps({'files':len(manifest['files']),'source_fingerprint':manifest['source_fingerprint'],'service_started':False}))


if __name__=='__main__':main()
