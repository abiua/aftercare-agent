"""Safety checks for the disposable demo lifecycle; no Docker access."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('offline_demo', SCRIPTS / 'offline_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class DemoSafetyTests(unittest.TestCase):
    def state(self, root):
        run_id = 'a' * 32
        return {'run_id': run_id, 'directory': str(root / 'browser-demo-test'),
                'resources': [('container', f'aftercare-fresh-{run_id}-api')]}

    def test_stop_rejects_shared_resource_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.state(root)
            state['resources'] = [('container', 'aftercare-v2-api-app-1')]
            with patch.object(demo, 'STATE', root / 'state.json'):
                with self.assertRaisesRegex(Exception, 'INVALID_RESOURCE_NAME'):
                    demo.restore(state)

    def test_stop_rejects_external_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.state(root)
            state['directory'] = '/tmp/browser-demo-other'
            with patch.object(demo, 'STATE', root / 'state.json'):
                with self.assertRaisesRegex(Exception, 'INVALID_DEMO_DIRECTORY'):
                    demo.restore(state)

    def test_stop_rejects_invalid_resource_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.state(root)
            state['resources'][0] = ('image', state['resources'][0][1])
            with patch.object(demo, 'STATE', root / 'state.json'):
                with self.assertRaisesRegex(Exception, 'INVALID_RESOURCE_NAME'):
                    demo.restore(state)

    def test_start_rejects_remote_docker_override_before_creating_resources(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(demo, 'STATE', root / 'state.json'), \
                 patch.dict(demo.os.environ, {'DOCKER_HOST': 'tcp://remote:2375'}), \
                 patch.object(demo, 'FreshStack') as stack:
                with self.assertRaisesRegex(Exception, 'LOCAL_DOCKER_REQUIRED'):
                    demo.start(SimpleNamespace(port=5190, object_port=58352))
                stack.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])

    def test_state_persistence_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            with patch.object(demo, 'STATE', target):
                demo.save({'status': 'STARTING'})
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                demo.save({'status': 'READY'})
                self.assertIn('READY', target.read_text())
                self.assertFalse(target.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()
