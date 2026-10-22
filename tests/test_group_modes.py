import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('qqbot_modes_test',Path(__file__).resolve().parents[1]/'plugins/astrbot_plugin_qqbot_guard/group_modes.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class GroupModesTests(unittest.TestCase):
    def test_default_isolation_persistence_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'managed/modes.sqlite3'
            store=module.GroupReplyModes(path)
            self.assertTrue(store.get('napcat:first'))
            self.assertFalse(path.exists())
            store.set('napcat:first',False)
            restored=module.GroupReplyModes(path)
            self.assertFalse(restored.get('napcat:first'))
            self.assertTrue(restored.get('napcat:second'))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
            restored.set('napcat:first',True)
            self.assertTrue(store.get('napcat:first'))

    def test_capacity_failure_does_not_discard_existing_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            store=module.GroupReplyModes(Path(directory)/'modes.sqlite3',max_groups=1)
            store.set('first',False)
            with self.assertRaises(ValueError):store.set('second',False)
            self.assertFalse(store.get('first'))
            self.assertTrue(store.get('second'))
            store.set('first',True)
            self.assertTrue(store.get('first'))

    def test_invalid_mode_and_key_do_not_create_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'modes.sqlite3';store=module.GroupReplyModes(path)
            for key,mode in (('',False),('x'*201,True),('valid','false'),('valid',0)):
                with self.assertRaises(ValueError):store.set(key,mode)
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
