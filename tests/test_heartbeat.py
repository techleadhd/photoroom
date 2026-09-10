"""The heartbeat stays alive independently of the matching thread."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from match import PythonHeartbeat


class HeartbeatTests(unittest.TestCase):
    def test_pulses_while_main_thread_waits_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'python-heartbeat.json'
            updated = threading.Event()
            class Heartbeat(PythonHeartbeat):
                def pulse(self):
                    super().pulse()
                    if threading.current_thread().name == 'PhotoRoom heartbeat':
                        updated.set()
            with Heartbeat(root,'test-run',interval=.01) as heartbeat:
                self.assertTrue(updated.wait(2),'No background heartbeat')
                payload = json.loads(path.read_text())
                self.assertEqual(payload['run_id'],'test-run')
                self.assertLess(abs(time.time()-payload['time']),2)
            self.assertFalse(path.exists())
            self.assertFalse(heartbeat.thread.is_alive())

    def test_exception_stops_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                with PythonHeartbeat(Path(directory),'test-run') as heartbeat:
                    raise RuntimeError('interrupted')
            self.assertFalse(heartbeat.path.exists())
            self.assertFalse(heartbeat.thread.is_alive())
