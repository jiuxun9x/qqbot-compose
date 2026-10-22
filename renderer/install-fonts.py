"""Install the exact CTAN font packages and their small supporting macro."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

EXPECTED = {
    'newtx': 'bbe07d4ad91ade9f61326e59f2f3bf389b4ace1c0c1ef2e8c85377e4e2535f4e',
    'adforn': '30ff21fbbb17a29e75d2d63b835763720c7986f6af62f5ff6b6f98c6322285ab',
    'bbding': '2a65a771aae4c3af0103c27cb251c0a6237eaedc3bb7b86c6a7bcd065a567142',
    'fixtounicode': 'd105a39de99cc58e53ad1a04802a7ae2696a8b9e7c13682d08b172c58e347207',
}
root = Path('/usr/local/share/texmf')
with tempfile.TemporaryDirectory() as temp:
    temp = Path(temp)
    for package, digest in EXPECTED.items():
        archive = Path('/opt/font-archives') / (package + '.zip')
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
        original = root / 'doc/fonts' / package
        original.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, original / (package + '-ctan-original.zip'))
        with zipfile.ZipFile(archive) as z:
            z.extractall(temp)
        source = temp / package
        if package == 'adforn':
            subprocess.run(['latex', '-interaction=batchmode', 'adforn.ins'], cwd=source / 'source', check=True)
            for path in (source / 'source').glob('*.sty'):
                shutil.copy(path, source / path.name)
            for path in (source / 'source').glob('*.fd'):
                shutil.copy(path, source / path.name)
        if package in ('bbding', 'fixtounicode'):
            subprocess.run(['latex', '-interaction=batchmode', package + '.ins'], cwd=source, check=True)
        categories = {'type1': 'fonts/type1', 'tfm': 'fonts/tfm', 'vf': 'fonts/vf',
                      'opentype': 'fonts/opentype', 'otf': 'fonts/opentype',
                      'enc': 'fonts/enc/dvips', 'map': 'fonts/map/dvips',
                      'tex': 'tex/latex', 'doc': 'doc/fonts'}
        for directory, destination in categories.items():
            if (source / directory).is_dir():
                shutil.copytree(source / directory, root / destination / package, dirs_exist_ok=True)
        for path in source.glob('*'):
            if path.suffix in ('.sty', '.fd', '.lua', '.def'):
                dest = root / 'tex/latex' / package
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, dest)
            elif path.suffix == '.mf':
                dest = root / 'fonts/source/public' / package
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, dest)
            elif path.name.lower() in ('readme', 'readme.md', 'notice', 'license', 'ofl.txt'):
                dest = root / 'doc/fonts' / package
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, dest)
subprocess.run(['mktexlsr'], check=True)
for name in ('newtx', 'adforn'):
    subprocess.run(['updmap-sys', '--enable', 'Map=' + name + '.map'], check=True)
