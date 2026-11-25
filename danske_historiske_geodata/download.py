"""Public Zenodo record downloads, independent of QGIS."""
import hashlib
import json
import logging
import os
import re
import zipfile
import shutil
import stat
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


def validate_download_url(url):
    """Permit HTTPS downloads only, including every redirect."""
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError('Invalid download URL')
    parts = urlsplit(url)
    if (parts.scheme.lower() != 'https' or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or chr(92) in parts.netloc):
        raise ValueError('Only HTTPS download URLs without credentials are permitted')
    return url


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url):
    url = validate_download_url(url)
    opener = build_opener(HTTPSOnlyRedirectHandler())
    return opener.open(Request(url, headers={'User-Agent': 'Danske-historiske-geodata/0.4.3', 'Accept': '*/*'}), timeout=45)


def file_entries(record_id):
    with request(f'https://zenodo.org/api/records/{int(record_id)}') as response:
        data = json.load(response)
    files = data.get('files', [])
    if isinstance(files, dict):
        files = files.get('entries', [])
        if isinstance(files, dict):
            files = list(files.values())
    result = []
    for entry in files:
        name = entry.get('key') or entry.get('filename')
        if not name or Path(name).name != name or re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name in ('.', '..'):
            raise ValueError('Unsafe or missing filename in Zenodo record')
        result.append((name, entry.get('size'), entry.get('checksum')))
    if not result:
        raise ValueError('Zenodo returned no public files')
    return result


def valid_file(path, size, checksum):
    if not path.is_file() or (size is not None and path.stat().st_size != int(size)):
        return False
    if checksum:
        algorithm, expected = checksum.split(':', 1)
        if algorithm not in ('md5', 'sha256', 'sha1'):
            raise ValueError('Unsupported checksum: ' + algorithm)
        digest = hashlib.new(algorithm)
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(262144), b''):
                digest.update(chunk)
        return digest.hexdigest().lower() == expected.lower()
    return size is not None


DIRECT = {'.gpkg', '.geojson', '.gml', '.fgb', '.kml', '.sqlite', '.tif', '.tiff', '.img', '.gdb'}
SIDE = {'.shp', '.shx', '.dbf', '.prj', '.cpg', '.qpj', '.sbn', '.sbx', '.tab', '.dat', '.map', '.id', '.ind', '.mif', '.mid', '.gfs', '.tfw', '.qml'}


def available_layers(folder):
    result = []
    for p in sorted(folder.rglob('*')):
        if any(x.name.endswith('_extracting') or x.name.startswith('.dhg-tmp-') for x in p.parents):
            continue
        ext = p.suffix.lower()
        if any(x.suffix.lower() == '.gdb' for x in p.parents):
            continue
        if p.is_dir() and ext == '.gdb':
            result.append(p)
        elif p.is_file():
            names = {x.name.lower() for x in p.parent.iterdir() if x.is_file()} if ext in ('.shp', '.tab', '.mif') else set()
            if ext in DIRECT - {'.gdb'} or (ext == '.shp' and all(p.stem.lower()+x in names for x in ('.dbf','.shx'))) or ext in ('.tab','.mif'):
                result.append(p)
    return result


def extraction_dir(archive):
    # Keep old cache paths valid for existing QGIS projects.
    old = archive.parent / (archive.name + '_extracted')
    if old.is_dir():
        return old
    return archive.parent / ('z-' + hashlib.sha256(archive.name.encode('utf-8')).hexdigest()[:12])


def filesystem_path(path):
    # Extended-length paths are for filesystem operations only, not QGIS URIs.
    if os.name != 'nt':
        return path
    value = os.path.abspath(str(path))
    prefix = chr(92) * 2 + '?' + chr(92)
    if value.startswith(prefix):
        return Path(value)
    if value.startswith(chr(92) * 2):
        return Path(prefix + 'UNC' + chr(92) + value[2:])
    return Path(prefix + value)


def unpack(archive, cancelled):
    dest = filesystem_path(extraction_dir(archive))
    staging = dest.parent / ('.dhg-tmp-' + hashlib.sha256(archive.name.encode('utf-8')).hexdigest()[:12])
    old_staging = filesystem_path(archive.parent / (archive.name + '_extracting'))
    for stale in (staging, old_staging):
        if stale.exists(): shutil.rmtree(stale)
    staging.mkdir()
    try:
        with zipfile.ZipFile(filesystem_path(archive)) as z:
            infos = z.infolist()
            if len(infos) > 50000 or sum(i.file_size for i in infos) > 8 * 1024**3:
                raise ValueError('ZIP archive exceeds extraction limits')
            members = []
            for info in infos:
                parts = info.filename.replace(chr(92), '/').rstrip('/').split('/')
                if any(not x or x in ('..', '.') or re.search(r'[:*?"<>|\x00-\x1f]', x) or x.endswith((' ', '.')) or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', x, re.I) for x in parts) or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('Unsafe ZIP member: ' + info.filename)
                members.append((info, parts))
            # Remove a redundant common enclosing folder, but never a geodatabase.
            files = [parts for info, parts in members if not info.is_dir()]
            strip = bool(files and all(len(parts) > 1 and parts[0] == files[0][0] for parts in files) and all(parts[0] == files[0][0] for _, parts in members) and not files[0][0].lower().endswith('.gdb'))
            for info, parts in members:
                if cancelled(): raise InterruptedError('Download cancelled')
                if strip: parts = parts[1:]
                if not parts: continue
                target = staging.joinpath(*parts)
                if info.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, target.open('wb') as out:
                    while True:
                        if cancelled(): raise InterruptedError('Download cancelled')
                        chunk = source.read(262144)
                        if not chunk: break
                        out.write(chunk)
        if dest.exists(): shutil.rmtree(dest)
        staging.rename(dest)
    finally:
        if staging.exists(): shutil.rmtree(staging)


def fetch(record_id, folder, progress, cancelled, extensions=None):
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / '.complete.json'
    if marker.exists():
        entries = json.loads(marker.read_text(encoding='utf-8'))
        for n, _, _ in entries:
            if Path(n).suffix.lower() == '.zip' and not extraction_dir(folder / n).exists() and (folder / n).is_file():
                unpack(folder / n, cancelled)
        if entries and all(valid_file(folder / n, s, c) for n, s, c in entries) and available_layers(folder):
            progress(100)
            return available_layers(folder)
    entries = file_entries(record_id)
    allowed = set(extensions) if extensions else DIRECT | SIDE | {'.zip'}
    entries = [(n,s,c) for n,s,c in entries if Path(n).suffix.lower() in allowed]
    if not entries:
        raise ValueError('No supported GIS files in record')
    for i, (name, size, checksum) in enumerate(entries):
        if cancelled():
            raise InterruptedError('Download cancelled')
        target = folder / name
        if not valid_file(target, size, checksum):
            tmp = folder / (name + '.part')
            try:
                url = f'https://zenodo.org/api/records/{int(record_id)}/files/{quote(name, safe="")}/content'
                with request(url) as response, tmp.open('wb') as out:
                    count = 0
                    while True:
                        if cancelled():
                            raise InterruptedError('Download cancelled')
                        chunk = response.read(131072)
                        if not chunk:
                            break
                        out.write(chunk)
                        count += len(chunk)
                        if size:
                            progress(100 * (i + min(count / int(size), 1)) / len(entries))
                if not valid_file(tmp, size, checksum):
                    raise IOError('File verification failed: ' + name)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink()
        progress(100 * (i+1) / len(entries))
    for name, _, _ in entries:
        if Path(name).suffix.lower() == '.zip':
            unpack(folder / name, cancelled)
    layers = available_layers(folder)
    if not layers:
        raise ValueError('No complete GIS layers downloaded (Shapefiles require SHP, SHX, DBF and PRJ).')
    marker.write_text(json.dumps(entries), encoding='utf-8')
    return layers


def fetch_dropbox(entry, storage, progress, cancelled):
    """Download a public shared folder once, retaining only named ZIP files."""
    import tempfile
    from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
    folder = storage / str(entry['record_id'])
    folder.mkdir(parents=True, exist_ok=True)
    def cached(item):
        dest = storage / str(item['id'])
        archive = dest / item['filename']
        marker = dest / '.complete.json'
        if marker.exists() and archive.is_file():
            try:
                if valid_file(archive, None, json.loads(marker.read_text())['checksum']):
                    if not available_layers(dest): unpack(archive, cancelled)
                    return available_layers(dest)
            except InterruptedError:
                raise
            except (ValueError, OSError, KeyError) as exc:
                logging.getLogger(__name__).warning("Could not reuse Dropbox cache %s: %s", dest, exc)
        return []
    selected = {'id': entry['record_id'], 'filename': entry['filename']}
    layers = cached(selected)
    if layers:
        progress(100); return layers
    # An earlier extraction may have failed after the ZIP was fully downloaded.
    archive = folder / selected['filename']
    if archive.is_file() and not (folder / '.complete.json').exists() and zipfile.is_zipfile(archive):
        progress(80)
        unpack(archive, cancelled)  # reads all members and verifies ZIP CRCs
        layers = available_layers(folder)
        if layers:
            digest = hashlib.sha256()
            with archive.open('rb') as stream:
                for chunk in iter(lambda: stream.read(262144), b''):
                    if cancelled(): raise InterruptedError('Download cancelled')
                    digest.update(chunk)
            (folder / '.complete.json').write_text(json.dumps({'checksum':'sha256:'+digest.hexdigest()}))
            progress(100)
            return layers
    parts = urlsplit(entry['source_url'])
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([(k,v) for k,v in parse_qsl(parts.query) if k != 'dl'] + [('dl','1')]), ''))
    with tempfile.TemporaryDirectory(dir=str(storage)) as tmpdir:
        bundle = Path(tmpdir) / 'folder.zip'
        with request(url) as response, bundle.open('wb') as out:
            total = int(response.headers.get('Content-Length') or 0)
            count = 0
            while True:
                if cancelled(): raise InterruptedError('Download cancelled')
                chunk = response.read(262144)
                if not chunk: break
                count += len(chunk)
                if count > 4*1024**3: raise ValueError('Dropbox folder exceeds 4 GiB limit')
                out.write(chunk)
                if total: progress(min(80, 80*count/total))
        if not zipfile.is_zipfile(bundle): raise ValueError('Dropbox did not return a ZIP archive. Check public folder access.')
        with zipfile.ZipFile(bundle) as z:
            for item in entry['folder_items']:
                if cached(item): continue
                def normalized(value):
                    return unicodedata.normalize('NFC', value).casefold()
                def matches_item(info):
                    name = info.filename.replace('\\', '/').split('/')[-1]
                    if info.is_dir(): return False
                    if normalized(name) == normalized(item['filename']): return True
                    prefix = item.get('filename_prefix')
                    return bool(prefix and normalized(name).startswith(normalized(prefix)) and name.lower().endswith('.zip'))
                matches = [i for i in z.infolist() if matches_item(i)]
                if len(matches) != 1:
                    if item['id'] == selected['id']: raise ValueError('Missing or ambiguous file: '+item['filename'])
                    continue
                info = matches[0]
                if info.file_size > 256*1024**2 or stat.S_ISLNK(info.external_attr >> 16): raise ValueError('Unsafe nested archive')
                dest = storage / str(item['id']); dest.mkdir(parents=True, exist_ok=True)
                archive = dest / item['filename']; temp = archive.with_suffix('.part')
                digest = hashlib.sha256()
                try:
                    with z.open(info) as src, temp.open('wb') as out:
                        while True:
                            if cancelled(): raise InterruptedError('Download cancelled')
                            chunk = src.read(262144)
                            if not chunk: break
                            out.write(chunk); digest.update(chunk)
                    os.replace(temp, archive)
                    unpack(archive, cancelled)
                    if available_layers(dest):
                        (dest / '.complete.json').write_text(json.dumps({'checksum':'sha256:'+digest.hexdigest()}))
                finally:
                    if temp.exists(): temp.unlink()
    layers = cached(selected)
    if not layers: raise ValueError('No complete GIS layers found in '+entry['filename'])
    progress(100); return layers
