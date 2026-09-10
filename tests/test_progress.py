"""Connection gating, skip filtering, and processing progress regressions."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import match


class ProgressTests(unittest.TestCase):
    def test_connects_then_checks_every_file_before_processing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit';work=root/'work'
            orig.mkdir();edit.mkdir()
            pairs=[match.Pair(orig/(name+'.JPG'),edit/(name+'.tiff'))
                   for name in ['ready-a','skip-xmp','skip-preview','ready-b']]
            (edit/'skip-xmp.xmp').touch()
            (edit/'skip-preview.match-preview.jpg').touch()
            issues=[{'status':'skipped','target':str(edit/'missing.tiff'),'reason':'No matching original'}]
            output=io.StringIO();events=[]
            real_check=match.check_outputs
            def check(pair,args):
                events.append('check '+pair.source.stem)
                return real_check(pair,args)
            class Bridge:
                def __init__(self,*args):pass
                def request(self,action,*args,**kwargs):
                    if action != 'hello': raise AssertionError(action)
                    # The connection instructions stay visible until a response arrives.
                    self_output=output.getvalue()
                    assert 'Waiting for Lightroom to connect' in self_output
                    assert 'Checking' not in self_output
                    assert 'SKIP' not in self_output
                    assert events == []
                    events.append('hello')
                    return {'ready':True,'bridge_version':'0.3.0'}
                def stop(self): events.append('stop')
            def process(pair,args,bridge,cm,progress=None):
                self.assertEqual(sum(e.startswith('check ') for e in events),len(pairs))
                events.append('process '+pair.source.stem)
                progress.evaluation(10,'eval 10/120, error 2.000, best 2.000')
                return dict(status='matched',source=str(pair.source),target=str(pair.target),
                            xmp=str(pair.target.with_suffix('.xmp')),warnings=[],
                            baseline_mean_delta_e76=5,final_mean_delta_e76=2)
            with patch('match.discover',return_value=(pairs,issues)), \
                 patch('match.check_outputs',side_effect=check),patch('match.match_one',side_effect=process), \
                 patch('match.Bridge',Bridge),patch('match.ColorManager'), \
                 patch('match.SESSION_DIR',root/'work'),contextlib.redirect_stdout(output):
                status=match.main(['--orig',str(orig),'--edit',str(edit)])
            self.assertEqual(status,2)  # Pairing/skip status remains visible to callers.
            self.assertEqual(events,['hello',*[f'check {p.source.stem}' for p in pairs],
                                     'process ready-a','process skip-preview','process ready-b','stop'])
            text=output.getvalue()
            self.assertIn('(5/5) Checking ready-b.JPG',text)
            self.assertIn('(1/3) Processing ready-a.JPG',text)
            self.assertIn('(3/3) Processing ready-b.JPG',text)
            self.assertIn('elapsed ',text);self.assertIn('left ~',text)
            self.assertIn('SKIP skip-xmp.JPG: output already exists: edit/skip-xmp.xmp',text)
            self.assertNotIn('SKIP skip-preview.JPG:',text)
            self.assertIn('(2/3) Processing skip-preview.JPG',text)
            self.assertEqual(text.count('Use --overwrite'),1)
            self.assertNotIn(str(edit/'skip-xmp.xmp'),text)
            summary=(work/'report.txt').read_text()
            self.assertNotIn(str(root),summary)
            self.assertIn('Settings: edit/ready-a.xmp',summary)
            self.assertIn('Passed: 3\nWarnings: 0\nFailed: 0\nSkipped: 2',text)

    def test_plugin_cancellation_stops_batch_and_cleans_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit'
            orig.mkdir();edit.mkdir()
            pairs=[match.Pair(orig/f'{i}.JPG',edit/f'{i}.tiff') for i in range(2)]
            with patch('match.discover',return_value=(pairs,[])), \
                 patch('match.match_one',side_effect=match.MatchingCancelled('Matching cancelled in Lightroom')) as process, \
                 patch('match.Bridge') as bridge,patch('match.ColorManager'), \
                 patch('match.SESSION_DIR',root/'work'),contextlib.redirect_stdout(io.StringIO()):
                bridge.return_value.request.return_value={'ready':True,'bridge_version':'0.3.0'}
                status=match.main(['--orig',str(orig),'--edit',str(edit)])
            self.assertEqual(status,2)
            process.assert_called_once()
            bridge.return_value.stop.assert_called_once()
            self.assertFalse((root/'work'/'python-heartbeat.json').exists())

    def test_connection_timeout_does_not_check_or_skip_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit'
            orig.mkdir();edit.mkdir()
            pair=match.Pair(orig/'photo.JPG',edit/'photo.tiff')
            (edit/'photo.xmp').touch()
            output=io.StringIO()
            with patch('match.discover',return_value=([pair],[])),patch('match.check_outputs') as check, \
                 patch('match.match_one') as process,patch('match.Bridge') as bridge, \
                 patch('match.ColorManager'),patch('match.SESSION_DIR',root/'work'), \
                 contextlib.redirect_stdout(output):
                bridge.return_value.request.side_effect=TimeoutError('Lightroom bridge timed out.')
                status=match.main(['--orig',str(orig),'--edit',str(edit)])
            self.assertEqual(status,2)
            check.assert_not_called();process.assert_not_called()
            bridge.return_value.stop.assert_called_once()
            self.assertNotIn('Checking',output.getvalue())
            self.assertNotIn('SKIP ',output.getvalue())
            self.assertIn('ABORTED Run: Lightroom bridge timed out.',output.getvalue())

    def test_all_existing_outputs_still_waits_then_skips_without_evaluations(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit'
            orig.mkdir();edit.mkdir()
            pair=match.Pair(orig/'photo.JPG',edit/'photo.tiff')
            (edit/'photo.xmp').touch()
            with patch('match.discover',return_value=([pair],[])),patch('match.match_one') as process, \
                 patch('match.Bridge') as bridge,patch('match.ColorManager'), \
                 patch('match.SESSION_DIR',root/'work'),contextlib.redirect_stdout(io.StringIO()):
                bridge.return_value.request.return_value={'ready':True,'bridge_version':'0.3.0'}
                match.main(['--orig',str(orig),'--edit',str(edit)])
            bridge.return_value.request.assert_called_once_with('hello')
            process.assert_not_called()

    def test_limit_applies_after_existing_results_are_filtered(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit';orig.mkdir();edit.mkdir()
            pairs=[match.Pair(orig/'done.JPG',edit/'done.tiff'),match.Pair(orig/'next.JPG',edit/'next.tiff')]
            (edit/'done.xmp').touch()
            report=dict(status='matched',source=str(pairs[1].source),xmp=str(edit/'next.xmp'),
                        warnings=[],baseline_mean_delta_e76=1,final_mean_delta_e76=0)
            with patch('match.discover',return_value=(pairs,[])),patch('match.match_one',return_value=report) as process, \
                 patch('match.Bridge') as bridge,patch('match.ColorManager'),patch('match.SESSION_DIR',root/'work'), \
                 contextlib.redirect_stdout(io.StringIO()):
                bridge.return_value.request.return_value={'ready':True,'bridge_version':'0.3.0'}
                match.main(['--orig',str(orig),'--edit',str(edit),'--limit','1'])
            process.assert_called_once()
            self.assertEqual(process.call_args.args[0].source,pairs[1].source)

    def test_session_lock_rejects_concurrent_run_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder)/'match-session'
            with match.SessionLock(directory):
                with self.assertRaisesRegex(RuntimeError,'Another PhotoRoom run is active'):
                    with match.SessionLock(directory): pass
            with match.SessionLock(directory): pass

    def test_estimate_uses_processing_time_and_evaluation_progress(self):
        now=[100.]
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            progress=match.ProcessingProgress(2,100,clock=lambda:now[0])
            progress.start(1,'first.JPG')
            now[0]=110.
            progress.evaluation(50,'eval 50/100')
            now[0]=120.
            progress.start(2,'second.JPG')
            now[0]=130.
            progress.finish()
        lines=output.getvalue().splitlines()
        self.assertIn('left estimating',lines[0])
        self.assertIn('elapsed 00:00:10 | left ~00:00:34',lines[1])
        self.assertIn('(2/2) Processing second.JPG | elapsed 00:00:20 | left ~00:00:20',lines[2])
        self.assertEqual(lines[3],'Processing time: 00:00:30')

    def test_final_summary_repeats_warning_and_failure_messages(self):
        output=io.StringIO()
        reports=[{'status':'matched','source':'/orig/pass.JPG'},
                 {'status':'warning','source':'/orig/warn.JPG','warnings':['Crop needs review.']},
                 {'status':'failed','source':'/orig/fail.JPG','reason':'Render failed.'}]
        with contextlib.redirect_stdout(output):match.print_run_summary(reports)
        text=output.getvalue()
        self.assertIn('Passed: 1\nWarnings: 1\nFailed: 1',text)
        self.assertIn('WARNING warn.JPG: Crop needs review.',text)
        self.assertIn('FAILED fail.JPG: Render failed.',text)

    def test_color_percentage_is_relative_to_start_and_can_exceed_100(self):
        self.assertEqual(match.color_progress(5.983,5.619,10),
                         'color error: 59.8% of start, best: 56.2% of start')
        self.assertIn('120.0% of start',match.color_progress(12,10,10))
        self.assertEqual(match.color_result(10,5),'color error: 100% -> 50.0% of start')
        self.assertEqual(match.color_result(10,0),'color error: 100% -> 0.0% of start')

    def test_zero_starting_color_error_does_not_invent_percentage(self):
        text=match.color_progress(1,0,0)
        self.assertNotIn('%',text)
        self.assertIn('starting error was zero',text)
        self.assertNotIn('%',match.color_result(0,0))

    def test_dry_run_does_not_require_lightroom(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit'
            orig.mkdir();edit.mkdir()
            (orig/'photo.JPG').touch();(edit/'photo.tiff').touch()
            with patch('match.Bridge') as bridge,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(match.main(['--orig',str(orig),'--edit',str(edit),'--dry-run']),0)
            bridge.assert_not_called()
