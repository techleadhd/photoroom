#!/usr/bin/env python3
"""PhotoRoom — match Apple Photos edits in Lightroom Classic.

Usage: python3 photoroom.py --orig /path/to/originals --edit /path/to/edited-tiffs

Options: --limit 1            Try one photo first
         --overwrite          Replace previous results
         --virtual-copies     Match Lightroom virtual copies
         --dry-run            Check filename pairing only
         --geometry prealigned  Trust manually matched crop and rotation
         --max-evals 120       Color matching budget per photo
         --keep-renders       Keep temporary exports for troubleshooting

The first run sets up .venv and installs dependencies. Later runs reuse it.
Requires Python 3.10+, Lightroom Classic 12+, and Homebrew for missing native
libraries. Install PhotoRoom.lrplugin in Lightroom's Plug-in Manager once.
"""
import ctypes
import ctypes.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def has_lcms():
    for path in (os.environ.get('LCMS2_LIBRARY'),ctypes.util.find_library('lcms2'),
                 '/opt/homebrew/lib/liblcms2.dylib','/usr/local/lib/liblcms2.dylib'):
        if not path: continue
        try:
            ctypes.CDLL(path)
            return True
        except OSError:
            pass
    return False


def ensure_native(argv):
    if '--dry-run' in argv: return
    missing = [] if has_lcms() else ['little-cms2']
    if not missing: return
    brew = shutil.which('brew')
    if not brew:
        raise RuntimeError('Install Homebrew (https://brew.sh), then rerun this command. Missing: '+', '.join(missing))
    print('First-time setup: installing '+', '.join(missing)+' with Homebrew.',flush=True)
    subprocess.run([brew,'install',*missing],check=True)
    if not has_lcms():
        raise RuntimeError('Native dependencies are still unavailable. Check Homebrew installation and LCMS2_LIBRARY.')


def ensure_python(root):
    directory = root/'.venv'
    python = directory/'bin'/'python'
    if not python.is_file():
        print('First-time setup: creating Python environment.',flush=True)
        venv.EnvBuilder(with_pip=True).create(directory)
    requirements = root/'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = directory/'.photoroom-requirements'
    ready = stamp.is_file() and stamp.read_text().strip() == digest
    probe = subprocess.run([str(python),'-c','import numpy, cv2, tifffile, imagecodecs, PIL'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if not ready or probe.returncode:
        print('Setting up Python dependencies (reused on later runs).',flush=True)
        subprocess.run([str(python),'-m','pip','install','-r',str(requirements)],check=True)
        stamp.write_text(digest+'\n')
    return python


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or '--help' in argv or '-h' in argv:
        print(__doc__)
        return 0
    if sys.version_info < (3,10):
        print('PhotoRoom requires Python 3.10 or newer.',file=sys.stderr)
        return 2
    # GUI-launched terminals sometimes omit Homebrew from PATH.
    os.environ['PATH'] = os.environ.get('PATH','')+os.pathsep+'/opt/homebrew/bin:/usr/local/bin'
    try:
        ensure_native(argv)
        python = ensure_python(ROOT)
        return subprocess.run([str(python),str(ROOT/'match.py'),*argv]).returncode
    except (OSError,RuntimeError,subprocess.CalledProcessError) as error:
        print(f'PhotoRoom setup failed: {error}',file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
