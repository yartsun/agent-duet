"""Bounded PNG inputs, stored outside worktrees and verified before each worker."""
import base64
import binascii
import hashlib
import re
import shutil
import struct
import uuid
import zlib
from pathlib import Path

MAX_COUNT = 10
MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
MAX_BODY = (MAX_TOTAL * 4 // 3) + 128 * 1024
SIGNATURE = b'\x89PNG\r\n\x1a\n'
FIELDS = {'id', 'name', 'size', 'width', 'height', 'sha256'}


def png_dimensions(raw):
    """Validate canvas PNGs without an optional image-processing dependency."""
    if not 45 <= len(raw) <= MAX_FILE or not raw.startswith(SIGNATURE):
        raise ValueError('Expected a PNG image up to 5 MB')
    offset, pixels, compressed, ended = 8, None, bytearray(), False
    while offset + 12 <= len(raw):
        length, kind = struct.unpack('>I4s', raw[offset:offset + 8])
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError('Corrupted PNG image')
        content = raw[offset + 8:end - 4]
        checksum = struct.unpack('>I', raw[end - 4:end])[0]
        if zlib.crc32(kind + content) & 0xffffffff != checksum:
            raise ValueError('Corrupted PNG image')
        if pixels is None and kind != b'IHDR':
            raise ValueError('Missing PNG header')
        if kind == b'IHDR':
            if pixels is not None or length != 13:
                raise ValueError('Invalid PNG header')
            width, height, depth, colour, compression, filtering, interlace = struct.unpack('>IIBBBBB', content)
            if (not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 16000000
                    or depth != 8 or colour not in (2, 6) or compression or filtering or interlace):
                raise ValueError('Expected a plain 8-bit RGB/RGBA PNG up to 4096 px and 16 megapixels')
            pixels = width, height, (3 if colour == 2 else 4)
        elif kind == b'IDAT':
            compressed.extend(content)
        elif kind == b'IEND':
            if length or end != len(raw):
                raise ValueError('Invalid PNG end')
            ended = True
            break
        elif kind[:1].isupper() and kind != b'PLTE':
            raise ValueError('Unsupported PNG feature')
        offset = end
    if not ended or not compressed or pixels is None:
        raise ValueError('Incomplete PNG image')
    width, height, channels = pixels
    stride = width * channels + 1
    expected = stride * height
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, expected + 1)
    except zlib.error:
        raise ValueError('Corrupted PNG pixels') from None
    if (len(decoded) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or any(decoded[row * stride] > 4 for row in range(height))):
        raise ValueError('Corrupted PNG pixels')
    return width, height


def checked_metadata(items):
    if not isinstance(items, list) or len(items) > MAX_COUNT:
        raise ValueError('Up to 10 images per task')
    total, seen = 0, set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != FIELDS
                or not isinstance(item['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', item['id'])
                or item['id'] in seen or not isinstance(item['name'], str)
                or not 1 <= len(item['name']) <= 120 or any(ord(c) < 32 for c in item['name'])
                or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or any(type(item[key]) is not int for key in ('size', 'width', 'height'))
                or not 1 <= item['size'] <= MAX_FILE
                or not 1 <= item['width'] <= 4096 or not 1 <= item['height'] <= 4096
                or item['width'] * item['height'] > 16000000):
            raise ValueError('Invalid image metadata')
        total += item['size']
        seen.add(item['id'])
    if total > MAX_TOTAL:
        raise ValueError('Images may total at most 20 MB')
    return items


def folder_for(root, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', task_id):
        raise ValueError('Invalid task for an image')
    root = Path(root).resolve()
    parent, folder = root / 'attachments', root / 'attachments' / task_id
    if parent.is_symlink() or folder.is_symlink():
        raise ValueError('Symbolic links are not allowed for attachments')
    return folder


def store_uploads(root, task_id, uploads):
    if not isinstance(uploads, list) or len(uploads) > MAX_COUNT:
        raise ValueError('Up to 10 images per task')
    prepared, total = [], 0
    for upload in uploads:
        if not isinstance(upload, dict) or set(upload) != {'name', 'data'}:
            raise ValueError('Invalid image')
        name, encoded = upload['name'], upload['data']
        if (not isinstance(name, str) or not 1 <= len(name) <= 120 or any(ord(c) < 32 for c in name)
                or not isinstance(encoded, str) or not encoded.startswith('data:image/png;base64,')
                or len(encoded) > MAX_FILE * 4 // 3 + 30):
            raise ValueError('Expected a PNG image up to 5 MB')
        try:
            raw = base64.b64decode(encoded.split(',', 1)[1], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('Could not decode the image') from None
        width, height = png_dimensions(raw)
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError('Images may total at most 20 MB')
        prepared.append(({'id': uuid.uuid4().hex, 'name': name, 'size': len(raw),
                          'width': width, 'height': height, 'sha256': hashlib.sha256(raw).hexdigest()}, raw))
    if not prepared:
        return []
    folder = folder_for(root, task_id)
    folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=False)
    try:
        for item, raw in prepared:
            path = folder / (item['id'] + '.png')
            with path.open('xb') as stream:
                path.chmod(0o600)
                stream.write(raw)
    except Exception:
        shutil.rmtree(folder)
        raise
    return [item for item, raw in prepared]


def read_image(root, task_id, item):
    checked_metadata([item])
    path = folder_for(root, task_id) / (item['id'] + '.png')
    if path.is_symlink() or not path.is_file() or path.stat().st_size != item['size']:
        raise ValueError('The image is missing or was modified')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise ValueError('The image was modified')
    return raw


def image_paths(root, task_id, items):
    checked_metadata(items)
    paths = []
    for item in items:
        read_image(root, task_id, item)
        paths.append(str(folder_for(root, task_id) / (item['id'] + '.png')))
    return paths
