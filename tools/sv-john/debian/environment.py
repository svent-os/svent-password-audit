import hashlib
import importlib.metadata
import json
import os
import pathlib
import posixpath
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
META = json.loads((ROOT / 'upstream.json').read_text())
BUILD = ROOT / '.build'
SOURCE = BUILD / 'source'
ENVIRONMENT = BUILD / 'environment'
PREFIX = '/opt/' + META['package']
ENV = os.environ.copy()
for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'VIRTUAL_ENV',
             'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER', 'PIP_EXTRA_INDEX_URL', 'SETUPTOOLS_USE_DISTUTILS'):
    ENV.pop(name, None)
ENV.update(PYTHONNOUSERSITE='1', PIP_CONFIG_FILE='/dev/null', PIP_NO_INPUT='1',
           PIP_REQUIRE_VIRTUALENV='true', PIP_DISABLE_PIP_VERSION_CHECK='1',
           PIP_INDEX_URL='https://pypi.org/simple')


def run(arguments, cwd=ROOT, stdout=None):
    command = [str(value) for value in arguments]
    print('[+] ' + ' '.join(command), flush=True)
    return subprocess.run(command, cwd=cwd, env=ENV, check=True, stdout=stdout)


def fetch(url, destination, expected):
    if not url.startswith('https://'):
        raise RuntimeError('Source must use HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'Svent-package-builder'})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open('wb') as output:
        if not response.geturl().startswith('https://'):
            raise RuntimeError('Insecure source redirect')
        shutil.copyfileobj(response, output)
    with destination.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise RuntimeError('Source checksum mismatch')


def clean():
    if BUILD.is_symlink():
        raise RuntimeError('Refusing a symlink build directory')
    if BUILD.exists():
        shutil.rmtree(BUILD)


def prepare_source():
    archive = BUILD / 'upstream.tar.gz'
    fetch(META['archive_url'], archive, META['sha256'])
    SOURCE.mkdir()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as content:
            for member in content.infolist():
                parts = pathlib.PurePosixPath(member.filename).parts[1:]
                if not parts:
                    continue
                if '..' in parts or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError('Unsafe source archive member')
                destination = SOURCE.joinpath(*parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content.read(member))
    else:
        with tarfile.open(archive) as content:
            for member in content.getmembers():
                parts = pathlib.PurePosixPath(member.name).parts[1:]
                if not parts:
                    continue
                if '..' in parts or member.islnk() or member.isdev():
                    raise RuntimeError('Unsafe source archive member')
                destination = SOURCE.joinpath(*parts)
                if member.issym():
                    target_name = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                    target = content.getmember(target_name)
                    if target_name.split('/')[0] != member.name.split('/')[0] or not target.isfile():
                        raise RuntimeError('Unsafe source archive link')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content.extractfile(target).read())
                    continue
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with content.extractfile(member) as stream, destination.open('wb') as output:
                        shutil.copyfileobj(stream, output)
                    destination.chmod(member.mode & 0o777)
    for relative, replacements in META.get('source_replacements', {}).items():
        path = (SOURCE / relative).resolve()
        if not path.is_relative_to(SOURCE.resolve()):
            raise RuntimeError('Invalid source patch path')
        data = path.read_text()
        for old, new in replacements.items():
            if old not in data:
                raise RuntimeError('Source patch does not match')
            data = data.replace(old, new)
        path.write_text(data)


def pip(*arguments):
    run([ENVIRONMENT / 'bin/python', '-I', '-m', 'pip', '--no-cache-dir', *arguments])


def smoke(arguments, expected=None):
    result = subprocess.run([str(value) for value in arguments], cwd=ROOT, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=120)
    if (result.returncode not in (0, 1, 2) or not result.stdout.strip()
            or any(marker in result.stdout for marker in ('Traceback (most recent call last)',
                                                         'ModuleNotFoundError', 'ImportError:'))
            or expected and expected not in result.stdout):
        print(result.stdout, file=sys.stderr)
        raise RuntimeError('Tool startup validation failed')


def record_dependencies():
    directories = list(ENVIRONMENT.glob('lib/python*/site-packages'))
    if len(directories) != 1:
        raise RuntimeError('Unexpected private package directory')
    records = []
    sources = BUILD / 'dependency-sources'
    sources.mkdir()
    for distribution in importlib.metadata.distributions(path=[str(directories[0])]):
        name = distribution.metadata['Name']
        version = distribution.version
        if name.lower() in ('pip', 'setuptools', 'wheel'):
            continue
        licenses = {}
        for relative in distribution.files or []:
            if any(word in str(relative).lower() for word in ('license', 'copying', 'notice')):
                path = pathlib.Path(distribution.locate_file(relative)).resolve()
                if path.is_relative_to(ENVIRONMENT.resolve()) and path.is_file():
                    licenses[str(relative)] = path.read_text(errors='replace')
        metadata_url = 'https://pypi.org/pypi/' + name + '/' + version + '/json'
        with urllib.request.urlopen(metadata_url, timeout=90) as response:
            data = json.load(response)
        archive = next((item for item in data['urls'] if item['packagetype'] == 'sdist'), None)
        if archive is None:
            raise RuntimeError('Missing dependency source archive: ' + name)
        filename = pathlib.Path(archive['filename']).name
        fetch(archive['url'], sources / filename, archive['digests']['sha256'])
        records.append({'name': name, 'version': version,
                        'license': distribution.metadata.get('License'),
                        'license_expression': distribution.metadata.get('License-Expression'),
                        'license_files': licenses, 'source_file': filename,
                        'source_url': archive['url'], 'source_sha256': archive['digests']['sha256']})
    (BUILD / 'third-party-notices.json').write_text(json.dumps(records, indent=2) + '\n')


def build():
    clean()
    BUILD.mkdir()
    prepare_source()
    if META['kind'] == 'john':
        run(['./configure', '--disable-native-tests', '--disable-native-march', '--enable-simd=sse2'], cwd=SOURCE / 'src')
        jobs = str(max(1, int(os.environ.get('SVENT_BUILD_JOBS', '2'))))
        run(['make', '-j' + jobs], cwd=SOURCE / 'src')
        smoke([SOURCE / 'run/john', '--config=' + str(SOURCE / 'run/john.conf'), '--list=build-info'], 'Version:')
    run(['/usr/bin/python3', '-m', 'venv', ENVIRONMENT])
    pip('install', '--upgrade', 'pip==26.2.1', 'setuptools==80.9.0', 'wheel==0.48.0')
    constraints = BUILD / 'constraints.txt'
    constraints.write_text('\n'.join(META['constraints']) + '\n')
    ENV['PIP_CONSTRAINT'] = str(constraints)
    if META['kind'] == 'john':
        pip('install', *META['requirements'])
    else:
        pip('install', SOURCE)
    pip('uninstall', '--yes', 'wheel')
    pip('check')
    for command, entry in META['commands'].items():
        smoke([ENVIRONMENT / 'bin/python', '-I', ENVIRONMENT / 'bin' / entry, '--help'])
    if META['kind'] == 'john':
        for module in ('pyhanko.pdf_utils.reader', 'olefile', 'Crypto', 'asn1crypto.pkcs12'):
            run([ENVIRONMENT / 'bin/python', '-I', '-c', 'import ' + module])
        for filename, arguments in (('pdf2john.py', ['--help']), ('office2john.py', [])):
            smoke([ENVIRONMENT / 'bin/python', '-I', SOURCE / 'run' / filename, *arguments])
    record_dependencies()
    with (BUILD / 'requirements-installed.txt').open('w') as output:
        run([ENVIRONMENT / 'bin/python', '-I', '-m', 'pip', 'freeze', '--all'], stdout=output)


def wrapper(destination, command):
    destination.write_text('#!/bin/sh\nset -eu\nunset PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONSTARTUP\n' + command + ' "$@"\n')
    destination.chmod(0o755)


def install(destination):
    destination = pathlib.Path(destination).resolve()
    if destination != (ROOT / 'debian' / META['package']).resolve():
        raise RuntimeError('Unexpected staging directory')
    prefix = destination / PREFIX.lstrip('/')
    prefix.mkdir(parents=True, exist_ok=True)
    documentation = destination / 'usr/share/doc' / META['package']
    documentation.mkdir(parents=True, exist_ok=True)
    venv_prefix = PREFIX + '/.venv' if META['kind'] == 'john' else PREFIX
    venv = destination / venv_prefix.lstrip('/')
    shutil.copytree(ENVIRONMENT, venv, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for path in (venv / 'bin').iterdir():
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if data.startswith(b'#!') and b'\x00' not in data:
                path.write_bytes(data.replace(str(ENVIRONMENT).encode(), venv_prefix.encode()))
                path.chmod(0o755)
    configuration = venv / 'pyvenv.cfg'
    configuration.write_text(configuration.read_text().replace(str(ENVIRONMENT), venv_prefix))
    for path in venv.rglob('*.pth'):
        if str(BUILD) in path.read_text(errors='replace'):
            raise RuntimeError('Build path leaked into installed environment')
    commands = destination / 'usr/bin'
    commands.mkdir(parents=True, exist_ok=True)
    if META['kind'] == 'john':
        shutil.copytree(SOURCE / 'run', prefix, symlinks=True, dirs_exist_ok=True)
        for path in sorted(prefix.iterdir()):
            name = path.name
            if name in ('john', 'unshadow', 'unafs', 'undrop', 'base64conv') or name.endswith('2john'):
                if path.is_file() and os.access(path, os.X_OK):
                    wrapper(commands / name, 'exec ' + PREFIX + '/' + name)
            elif name.endswith('2john.py'):
                wrapper(commands / name[:-3], 'exec ' + venv_prefix + '/bin/python -I -c ' +
                        "'import runpy, sys; sys.path.insert(0, \"" + PREFIX +
                        '\"); sys.argv[0] = "' + PREFIX + '/' + name +
                        '"; runpy.run_path(sys.argv[0], run_name="__main__")' + "'")
            elif name.endswith('2john.pl'):
                wrapper(commands / name[:-3], 'exec /usr/bin/perl ' + PREFIX + '/' + name)
    else:
        for command, entry in META['commands'].items():
            wrapper(commands / command, 'exec ' + PREFIX + '/bin/python -I ' + PREFIX + '/bin/' + entry)
    for path in SOURCE.rglob('*'):
        if path.is_file() and path.name.lower().startswith(('license', 'copying', 'notice')):
            target = documentation / 'licenses' / path.relative_to(SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for name in ('requirements-installed.txt', 'third-party-notices.json', 'upstream.tar.gz'):
        shutil.copy2(BUILD / name, documentation / name)
    shutil.copytree(BUILD / 'dependency-sources', documentation / 'dependency-sources')
    shutil.copy2(ROOT / 'upstream.json', documentation / 'upstream.json')
    shutil.copy2(ROOT / 'debian/environment.py', documentation / 'build-helper.py')
    shutil.copytree(ROOT / 'catalog.d', destination / 'usr/share/svent/catalog.d', dirs_exist_ok=True)


def shlibdeps():
    staging = ROOT / 'debian' / META['package']
    directories = set()
    for path in (staging / PREFIX.lstrip('/')).rglob('*'):
        if path.is_file() and '.so' in path.name:
            with path.open('rb') as stream:
                if stream.read(4) == b'\x7fELF':
                    directories.add(str(path.parent.resolve()))
    arguments = ['dh_shlibdeps']
    if directories:
        arguments.extend(['-l' + ':'.join(sorted(directories)), '--', '--ignore-missing-info'])
    run(arguments)


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'build':
        build()
    elif action == 'install':
        install(sys.argv[2])
    elif action == 'clean':
        clean()
    elif action == 'shlibdeps':
        shlibdeps()
    else:
        raise SystemExit('Unknown build action')
