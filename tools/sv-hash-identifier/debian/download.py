import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


STATE_ROOT = pathlib.Path('/var/lib/svent')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url, destination):
    if not url.startswith('https://'):
        raise RuntimeError('Downloads require HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'Svent-downloader'})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open('wb') as stream:
        shutil.copyfileobj(response, stream)


def command(arguments):
    return subprocess.check_output(arguments, text=True).strip()


def configure(manifest):
    root = pathlib.Path(manifest['root'])
    state = STATE_ROOT / manifest['package']
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or state.is_symlink():
        raise RuntimeError('Refusing a symlink installation directory')
    record_path = state / 'managed.json'
    with (state / 'configure.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(record_path.read_text()) if record_path.exists() else {}
        for item in manifest.get('repositories', []):
            target = root / item['path']
            if target.exists():
                if target.is_symlink() or not (target / '.git').is_dir():
                    raise RuntimeError('Existing personal directory preserved: ' + str(target))
                if command(['git', '-C', str(target), 'rev-parse', 'HEAD']) != item['commit']:
                    raise RuntimeError('Existing repository preserved; move it aside before configuring: ' + str(target))
            else:
                with tempfile.TemporaryDirectory(prefix='.svent-download-', dir=root) as temporary:
                    staging = pathlib.Path(temporary) / 'repository'
                    subprocess.run(['git', 'init', '-q', str(staging)], check=True)
                    subprocess.run(['git', '-C', str(staging), 'remote', 'add', 'origin', item['url']], check=True)
                    subprocess.run(['git', '-C', str(staging), 'fetch', '--depth', '1', 'origin', item['commit']], check=True)
                    subprocess.run(['git', '-C', str(staging), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
                    if command(['git', '-C', str(staging), 'rev-parse', 'HEAD']) != item['commit']:
                        raise RuntimeError('Repository commit mismatch')
                    if not (staging / 'LICENSE').is_file():
                        raise RuntimeError('Upstream license missing')
                    os.replace(staging, target)
        for item in manifest['files']:
            target = root / item['path']
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                raise RuntimeError('Invalid download destination')
            if target.exists() and digest(target) == item['sha256']:
                records[item['path']] = item['sha256']
                continue
            if target.exists() and records.get(item['path']) != digest(target):
                raise RuntimeError('Personal or modified file preserved: ' + str(target))
            with tempfile.TemporaryDirectory(prefix='.svent-download-', dir=root) as temporary:
                staging = pathlib.Path(temporary) / 'payload'
                download(item['url'], staging)
                if staging.stat().st_size != item['size'] or digest(staging) != item['sha256']:
                    raise RuntimeError('Download checksum mismatch: ' + item['path'])
                if target.exists():
                    backups = state / 'backups'
                    backups.mkdir(exist_ok=True)
                    shutil.copy2(target, backups / (target.name + '.' + digest(target)))
                target.parent.mkdir(parents=True, exist_ok=True)
                staging.chmod(0o644)
                os.replace(staging, target)
            records[item['path']] = item['sha256']
            temporary_record = state / 'managed.json.tmp'
            temporary_record.write_text(json.dumps(records, indent=2) + '\n')
            os.replace(temporary_record, record_path)
        if manifest.get('rockyou'):
            target = root / 'rockyou.txt'
            archive = root / manifest['rockyou']
            if not archive.is_file():
                raise RuntimeError('Rockyou archive missing')
            with tarfile.open(archive) as content:
                members = [item for item in content.getmembers() if item.isfile() and pathlib.PurePosixPath(item.name).name == 'rockyou.txt']
                if len(members) != 1:
                    raise RuntimeError('Unexpected rockyou archive')
                with tempfile.TemporaryDirectory(prefix='.svent-rockyou-', dir=root) as temporary:
                    staging = pathlib.Path(temporary) / 'rockyou.txt'
                    with content.extractfile(members[0]) as source, staging.open('wb') as output:
                        shutil.copyfileobj(source, output)
                    checksum = digest(staging)
                    if target.exists() and digest(target) != checksum:
                        raise RuntimeError('Existing rockyou file preserved')
                    if not target.exists():
                        staging.chmod(0o644)
                        os.replace(staging, target)
                    records['rockyou.txt'] = checksum
        temporary_record = state / 'managed.json.tmp'
        temporary_record.write_text(json.dumps(records, indent=2) + '\n')
        os.replace(temporary_record, record_path)
    check(manifest)


def check(manifest):
    root = pathlib.Path(manifest['root'])
    for item in manifest['files']:
        path = root / item['path']
        if not path.is_file() or path.is_symlink() or digest(path) != item['sha256']:
            raise RuntimeError('Missing or modified file: ' + str(path))
    for item in manifest.get('repositories', []):
        target = root / item['path']
        if target.is_symlink() or command(['git', '-C', str(target), 'rev-parse', 'HEAD']) != item['commit']:
            raise RuntimeError('Repository commit mismatch')
        subprocess.run(['git', '-C', str(target), 'diff', '--exit-code', 'HEAD', '--'], check=True)
    if manifest.get('rockyou'):
        records = json.loads((STATE_ROOT / manifest['package'] / 'managed.json').read_text())
        path = root / 'rockyou.txt'
        if not path.is_file() or path.is_symlink() or digest(path) != records.get('rockyou.txt'):
            raise RuntimeError('Rockyou checksum mismatch')
    print('[+] Downloaded files verified successfully.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest')
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--check', action='store_true')
    arguments = parser.parse_args()
    manifest = json.loads(pathlib.Path(arguments.manifest).read_text())
    if arguments.configure:
        if os.geteuid() != 0:
            raise SystemExit('Configuration requires root')
        configure(manifest)
    elif arguments.check:
        check(manifest)
    else:
        print(manifest['root'])


if __name__ == '__main__':
    main()
