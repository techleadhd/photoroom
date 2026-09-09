"""Launcher setup should happen once, without activation or repeated installs."""
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import photoroom


class LauncherTests(unittest.TestCase):
    def test_help_needs_no_setup(self):
        with patch('photoroom.ensure_native') as native,patch('photoroom.ensure_python') as python,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(photoroom.main(['--help']),0)
        native.assert_not_called();python.assert_not_called()

    def test_python_setup_reused_and_requirements_changes_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'requirements.txt').write_text('numpy>=1.26,<3\n')
            def create(directory):
                (directory/'bin').mkdir(parents=True)
                (directory/'bin'/'python').touch()
            with patch('photoroom.venv.EnvBuilder') as builder, \
                 patch('photoroom.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                builder.return_value.create.side_effect=create
                first=photoroom.ensure_python(root)
                second=photoroom.ensure_python(root)
                self.assertEqual(first,second)
                builder.return_value.create.assert_called_once()
                installs=[c for c in run.call_args_list if 'pip' in c.args[0]]
                self.assertEqual(len(installs),1)
                (root/'requirements.txt').write_text('numpy>=2,<3\n')
                photoroom.ensure_python(root)
                self.assertEqual(len([c for c in run.call_args_list if 'pip' in c.args[0]]),2)

    def test_native_packages_only_installed_if_missing(self):
        with patch('photoroom.has_lcms',return_value=True), \
             patch('photoroom.shutil.which',return_value='/usr/local/bin/brew'), \
             patch('photoroom.subprocess.run') as run:
            photoroom.ensure_native([])
            run.assert_not_called()
        with patch('photoroom.has_lcms',side_effect=[False,True]), \
             patch('photoroom.shutil.which',side_effect=lambda name:'/opt/homebrew/bin/'+name), \
             patch('photoroom.subprocess.run') as run, contextlib.redirect_stdout(io.StringIO()):
            photoroom.ensure_native([])
            run.assert_called_once_with(['/opt/homebrew/bin/brew','install','little-cms2'],check=True)


    def test_launcher_tracks_renamed_project_folder(self):
        import runpy
        with tempfile.TemporaryDirectory() as folder:
            old=Path(folder)/'old-name';old.mkdir()
            (old/'photoroom.py').write_bytes(Path(photoroom.__file__).read_bytes())
            new=old.with_name('PhotoRoom');old.rename(new)
            self.assertEqual(runpy.run_path(str(new/'photoroom.py'))['ROOT'],new.resolve())
