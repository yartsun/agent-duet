"""Photo input validation and persistence, without paid CLI/GPU sessions."""
import base64
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from duet.attachments import (SIGNATURE, checked_metadata, image_paths,
                                       png_dimensions, read_image, store_uploads)
from duet.agents import build_command


def png(width=2, height=2, pixels=None):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = pixels if pixels is not None else (b'\x00' + b'\x80\x40\x20\xff' * width) * height
    return (SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def upload(raw=None, name='Pasted image.png'):
    return {'name': name, 'data': 'data:image/png;base64,' + base64.b64encode(raw or png()).decode()}


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_valid_png_persisted_as_private_file_and_metadata_only(self):
        items = store_uploads(self.root, 'task-test', [upload()])
        self.assertEqual(items[0]['width'], 2)
        self.assertEqual(items[0]['height'], 2)
        self.assertNotIn('data', items[0])
        self.assertEqual(read_image(self.root, 'task-test', items[0]), png())
        paths = image_paths(self.root, 'task-test', items)
        self.assertEqual(Path(paths[0]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(paths), 1)

    def test_invalid_png_and_decompression_bomb_rejected(self):
        for raw in (b'not a photo', png()[:-1], png() + b'junk', png(5000, 1), png(pixels=b'\x00' * 1000000),
                    png(pixels=b'\x05' + b'\x00' * 8 + b'\x00' * 9)):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                png_dimensions(raw)
        broken = bytearray(png()); broken[-5] ^= 1
        with self.assertRaises(ValueError): png_dimensions(broken)

    def test_uploads_reject_invalid_base64_svg_and_large_counts(self):
        for uploads in ([{'name': 'x', 'data': 'data:image/png;base64,??'}],
                        [{'name': 'x', 'data': 'data:image/svg+xml;base64,AAAA'}],
                        [upload()] * 11, [{'name': 'x', 'data': upload()['data'], 'path': '/etc/passwd'}]):
            with self.assertRaises(ValueError): store_uploads(self.root, 'task-test', uploads)
        self.assertFalse((self.root / 'attachments').exists())

    def test_all_inputs_validated_before_any_files_are_saved(self):
        with self.assertRaises(ValueError): store_uploads(self.root, 'task-test', [upload(), upload(b'invalid')])
        self.assertFalse((self.root / 'attachments').exists())

    def test_file_changes_and_symlinks_block_worker_and_preview(self):
        items = store_uploads(self.root, 'task-test', [upload()])
        file = Path(image_paths(self.root, 'task-test', items)[0])
        file.write_bytes(png().replace(b'IDAT', b'WHAT'))
        with self.assertRaisesRegex(ValueError, 'modified'): image_paths(self.root, 'task-test', items)
        file.unlink(); file.symlink_to(self.root / 'other')
        with self.assertRaises(ValueError): read_image(self.root, 'task-test', items[0])

    def test_no_user_path_is_accepted_in_metadata(self):
        items = store_uploads(self.root, 'task-test', [upload()])
        for replacement in ({**items[0], 'path': '/etc/passwd'}, {**items[0], 'id': '../x'},
                            {**items[0], 'size': True}, {**items[0], 'width': 99999}):
            with self.assertRaises(ValueError): checked_metadata([replacement])
        with self.assertRaises(ValueError): checked_metadata(items * 2)
        with self.assertRaises(ValueError): image_paths(self.root, '../other', items)

    def test_cli_passes_each_image_to_codex_and_read_directory_to_claude(self):
        images = image_paths(self.root, 'task-test', store_uploads(self.root, 'task-test', [upload(), upload()]))
        with patch('duet.agents.shutil.which', side_effect=lambda name: '/bin/' + name):
            for role in ('build', 'review'):
                command = build_command('codex', self.root, self.root / 'run', role, images=images)
                self.assertEqual(command.count('--image'), 2)
                self.assertEqual(command[-1], '-')
                for image in images: self.assertIn(image, command)
                command = build_command('claude', self.root, self.root / 'run', role, images=images)
                self.assertEqual(command.count('--add-dir'), 1)
                self.assertEqual(command[-1], str(Path(images[0]).parent))
                self.assertIn('Read', command[command.index('--allowedTools') + 1])
