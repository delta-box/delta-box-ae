import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backends.deltabox.gsd.criu_page_stats import collect_page_stats, read_pagemap_stats

MAGIC = bytes.fromhex('1943565425400856')

def record(payload):
    return struct.pack('<I', len(payload)) + payload

# CRIU 4.2: vaddr=4096, compat_nr_pages=0, flags=parent, nr_pages=2.
PARENT_TWO = bytes.fromhex('088020100020012802')
# vaddr=12288, compat_nr_pages=0, flags=present|lazy, nr_pages=1.
PRESENT_ONE = bytes.fromhex('088060100020062801')


class CriuPageStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / 'pagemap-1.img'
        self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(PARENT_TWO) + record(PRESENT_ONE))
        (self.root / 'pages-7.img').write_bytes(b'x' * 4096)

    def test_new_uint64_page_count_and_parent_flags_are_reported(self):
        stats = collect_page_stats(self.root)
        self.assertEqual((stats['parent_pages'], stats['present_pages'], stats['total_pages']), (2, 1, 3))
        self.assertEqual(stats['lazy_eligible_pages'], 1)
        self.assertEqual(stats['private_pages_bytes'], 4096)
        self.assertEqual(stats['tasks'][0]['pages_id'], 7)

    def test_lazy_parent_pages_are_both_references_and_fault_eligible(self):
        parent_lazy = PARENT_TWO.replace(bytes.fromhex('2001'), bytes.fromhex('2003'))
        self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(parent_lazy) + record(PRESENT_ONE))
        stats = collect_page_stats(self.root)
        self.assertEqual(stats['parent_lazy_pages'], 2)
        self.assertEqual(stats['lazy_eligible_pages'], 3)
        self.assertEqual(stats['private_pages_bytes'], 4096)

    def test_stats_never_open_page_payload(self):
        original = Path.open
        def guarded(path, *args, **kw):
            self.assertFalse(path.name.startswith('pages-'), 'page content must not be read')
            return original(path, *args, **kw)
        with patch.object(Path, 'open', guarded):
            self.assertEqual(collect_page_stats(self.root)['parent_pages'], 2)

    def test_legacy_nr_pages_and_in_parent_boolean_are_supported(self):
        self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(bytes.fromhex('08802010021801')))
        stats = read_pagemap_stats(self.image)
        self.assertEqual((stats['parent_pages'], stats['present_pages']), (2, 0))

    def test_shared_payload_bytes_are_separate_from_private_bytes(self):
        (self.root / 'pages-8.img').write_bytes(b's' * 8192)
        (self.root / 'pagemap-shmem-8.img').write_bytes(b'shared metadata is not a task map')
        stats = collect_page_stats(self.root)
        self.assertEqual(stats['private_pages_bytes'], 4096)
        self.assertEqual(stats['pages_data_bytes'], 12288)

    def test_truncated_or_bad_magic_images_fail(self):
        for data in (b'BAD!', MAGIC, MAGIC + b'\x01', MAGIC + record(b'\x08')):
            with self.subTest(data=data):
                self.image.write_bytes(data)
                with self.assertRaises(ValueError):read_pagemap_stats(self.image)

    def test_oversized_record_does_not_allocate_its_advertised_payload(self):
        self.image.write_bytes(MAGIC + struct.pack('<I', 1 << 30))
        with self.assertRaisesRegex(ValueError, 'record size'):read_pagemap_stats(self.image)

    def test_unknown_or_conflicting_flags_are_rejected(self):
        for flag in (0, 2, 5, 8):
            with self.subTest(flag=flag):
                row = bytes.fromhex('088020100020') + bytes([flag]) + bytes.fromhex('2801')
                self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(row))
                with self.assertRaises(ValueError):read_pagemap_stats(self.image)

    def test_overlapping_ranges_and_zero_page_count_are_rejected(self):
        for row in (PARENT_TWO, bytes.fromhex('088060100020042800')):
            with self.subTest(row=row):
                self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(PARENT_TWO) + record(row))
                with self.assertRaises(ValueError):read_pagemap_stats(self.image)

    def test_duplicate_and_unknown_fields_are_rejected(self):
        for suffix in (bytes.fromhex('2801'), bytes.fromhex('3001')):
            self.image.write_bytes(MAGIC + record(b'\x08\x07') + record(PARENT_TWO + suffix))
            with self.assertRaises(ValueError):read_pagemap_stats(self.image)

    def test_truncated_pages_are_not_reported_as_successful_delta(self):
        (self.root / 'pages-7.img').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'length disagrees'):collect_page_stats(self.root)

    def test_missing_task_pagemap_is_rejected(self):
        self.image.unlink()
        with self.assertRaisesRegex(ValueError, 'no task pagemap'):collect_page_stats(self.root)

    def test_page_symlink_is_not_followed(self):
        (self.root / 'pages-7.img').unlink()
        (self.root / 'pages-7.img').symlink_to('/dev/zero')
        with self.assertRaisesRegex(ValueError, 'regular CRIU image'):collect_page_stats(self.root)

    def test_bad_page_sizes_are_rejected(self):
        for size in (0, 3, True, -4096):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):read_pagemap_stats(self.image, size)

