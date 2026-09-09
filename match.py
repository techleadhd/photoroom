#!/usr/bin/env python3
"""PhotoRoom — match Apple Photos edits in Lightroom Classic.

Run python3 photoroom.py --help. Requires the included PhotoRoom.lrplugin.
"""
import argparse
import copy
import errno
import fcntl
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET

from PIL import Image, ImageCms, ImageDraw, ImageOps

from color import ColorManager, load_tiff
from matching import GeometryError, ORIENTATION_EXIF, coordinate_search
from geometry import align_geometry, ColorComparison

RAW = {'.3fr','.arw','.cr2','.cr3','.crw','.dcr','.dng','.erf','.fff','.iiq','.kdc',
       '.mef','.mos','.mrw','.nef','.nrw','.orf','.pef','.raf','.raw','.rw2','.rwl','.sr2','.srf','.srw','.x3f'}
JPEG = {'.jpg', '.jpeg'}
CRS = 'http://ns.adobe.com/camera-raw-settings/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
X = 'adobe:ns:meta/'
TIFF = 'http://ns.adobe.com/tiff/1.0/'
PHOTOROOM = 'urn:photoroom:matching:1.0'
SESSION_DIR = Path(__file__).resolve().parent/'match-session'
ET.register_namespace('photoroom',PHOTOROOM)
ET.register_namespace('x', X); ET.register_namespace('rdf', RDF); ET.register_namespace('crs', CRS)
for prefix, ns in [('dc','http://purl.org/dc/elements/1.1/'),('xmp','http://ns.adobe.com/xap/1.0/'),
                   ('photoshop','http://ns.adobe.com/photoshop/1.0/'),('exif','http://ns.adobe.com/exif/1.0/'),
                   ('tiff','http://ns.adobe.com/tiff/1.0/')]: ET.register_namespace(prefix, ns)


def atomic_write(path, data, overwrite=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise FileExistsError(f'Refusing symlink output: {path}')
    fd, temp = tempfile.mkstemp(prefix=path.name+'.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if overwrite: os.replace(temp, path)
        else:
            try:
                os.link(temp, path)  # Atomic no-clobber, including concurrent creates.
            except OSError as e:
                if e.errno not in {errno.EPERM,errno.ENOTSUP,errno.EOPNOTSUPP,errno.EXDEV}:
                    raise
                # exFAT does not implement hard links. Exclusive creation still
                # guarantees no clobber; remove our partial file on write errors.
                with open(path,'xb') as out:
                    try:
                        out.write(data); out.flush(); os.fsync(out.fileno())
                    except BaseException:
                        out.close(); path.unlink(); raise
    finally:
        if os.path.exists(temp): os.unlink(temp)


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')


def key(path):
    return unicodedata.normalize('NFC', path.with_suffix('').as_posix()).casefold()


@dataclass
class Pair:
    source: Path
    target: Path


def discover(orig, edit, prefer=None):
    originals, targets = {}, {}
    for p in sorted(orig.rglob('*')):
        if not p.is_file(): continue
        k = key(p.relative_to(orig)); ext = p.suffix.lower()
        if ext in RAW | JPEG: originals.setdefault(k, []).append(p)
    for p in sorted(edit.rglob('*')):
        if p.is_file() and p.suffix.lower() in {'.tif','.tiff'}:
            targets.setdefault(key(p.relative_to(edit)), []).append(p)
    pairs, issues = [], []
    for k, t in sorted(targets.items()):
        candidates = originals.get(k, [])
        if len(candidates) > 1 and prefer:
            candidates = [p for p in candidates if p.suffix.lower() in (RAW if prefer == 'raw' else JPEG)]
        problem = None
        if len(t) != 1: problem = 'Multiple edited TIFFs share the same relative stem'
        elif not candidates: problem = 'No matching original'
        elif len(candidates) != 1: problem = 'Ambiguous originals; use --prefer raw or --prefer jpeg, or separate them'
        elif t[0].resolve() == candidates[0].resolve(): problem = 'Original and target resolve to the same file'
        if problem:
            issues.append({'target':str(t[0]), 'status':'skipped', 'reason':problem})
        else:
            pairs.append(Pair(candidates[0],t[0]))
    return pairs, issues


def parse_xmp(data):
    if len(data) > 64 * 1024 * 1024: raise ValueError('XMP exceeds 64 MiB limit')
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): raise ValueError('DTD/entity XMP is unsupported')
    return ET.fromstring(data)


def image_descriptions(root):
    """Image-level RDF only: nested profile/mask descriptions are not globals."""
    rdf = root if root.tag == '{'+RDF+'}RDF' else root.find('.//{'+RDF+'}RDF')
    return [] if rdf is None else list(rdf.findall('{'+RDF+'}Description'))


def crs_values(root):
    values = {}
    for desc in image_descriptions(root):
        for name, value in desc.attrib.items():
            if name.startswith('{'+CRS+'}'): values[name.split('}',1)[1]] = value
        for child in desc:
            if child.tag.startswith('{'+CRS+'}') and len(child) == 0 and child.text:
                values[child.tag.split('}',1)[1]] = child.text.strip()
    return values


SNAPSHOT_NUMERIC_CONTROLS = (
    'Exposure2012','Contrast2012','Highlights2012','Shadows2012','Whites2012','Blacks2012',
    'Temperature','Tint','IncrementalTemperature','IncrementalTint',
    'CropLeft','CropRight','CropTop','CropBottom','CropAngle','CropConstrainToWarp',
)


def settings_snapshot(settings, source, warnings):
    """Record fitted controls from the catalog; this is not a full Develop backup.

    The snapshot doubles as the completion marker in edit/. It deliberately
    does not read source metadata or attempt to serialize profiles/curves.
    """
    root = ET.Element('{'+X+'}xmpmeta')
    rdf = ET.SubElement(root,'{'+RDF+'}RDF')
    desc = ET.SubElement(rdf,'{'+RDF+'}Description',{'{'+RDF+'}about':''})
    desc.set('{'+PHOTOROOM+'}Scope','matched-controls-only')
    desc.set('{'+PHOTOROOM+'}Status','warning' if warnings else 'matched')
    desc.set('{'+PHOTOROOM+'}Source',source.name)
    for name in SNAPSHOT_NUMERIC_CONTROLS:
        value = settings.get(name)
        if value is None: continue
        if type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError(f'Invalid Lightroom setting: {name}')
        desc.set('{'+CRS+'}'+name,format(value,'.15g'))
    for name in ('WhiteBalance','ProcessVersion'):
        if isinstance(settings.get(name),str): desc.set('{'+CRS+'}'+name,settings[name])
    if type(settings.get('HasCrop')) is bool:
        desc.set('{'+CRS+'}HasCrop','True' if settings['HasCrop'] else 'False')
    orientation = settings.get('orientation')
    if orientation is not None:
        if orientation not in ORIENTATION_EXIF: raise ValueError(f'Unknown Lightroom orientation: {orientation}')
        desc.set('{'+TIFF+'}Orientation',str(ORIENTATION_EXIF[orientation]))
    if warnings:
        seq = ET.SubElement(ET.SubElement(desc,'{'+PHOTOROOM+'}Warnings'),'{'+RDF+'}Seq')
        for warning in warnings: ET.SubElement(seq,'{'+RDF+'}li').text = warning
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


class SessionLock:
    """One Python run per persistent bridge folder; OS releases this on a crash."""
    def __init__(self, directory): self.directory = directory

    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        path = self.directory/'python.lock'
        if path.is_symlink(): raise RuntimeError('Refusing symlink session lock')
        self.file = path.open('a')
        try: fcntl.flock(self.file.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another PhotoRoom run is active. Wait for it to finish before starting another.')
        return self

    def __exit__(self, *unused):
        self.file.close()


class Bridge:
    def __init__(self, directory, timeout=300, run_id=None, keep_responses=False):
        self.directory = directory
        self.timeout = timeout
        self.sequence = 0
        self.run_id = run_id or uuid.uuid4().hex
        self.connected = False
        self.keep_responses = keep_responses

    def request(self, action, source=None, settings=None, edge=768):
        self.sequence += 1
        job_id = f'{self.run_id}-{self.sequence:08d}'
        job = {'id':job_id, 'run_id':self.run_id, 'action':action, 'edge':edge,
               'label':f'{source.name if source else "Session"}: {action} {self.sequence}'}
        if source is not None: job['source'] = str(source)
        if settings is not None: job['settings'] = settings
        atomic_write(self.directory/'request.json',json_bytes(job),overwrite=True)
        response = self.directory/f'response-{job_id}.json'
        deadline = time.monotonic()+self.timeout
        while not response.exists():
            if (self.directory/'cancelled').exists() and (self.directory/'cancelled').read_text() == self.run_id:
                raise RuntimeError('Matching cancelled in Lightroom')
            if time.monotonic() >= deadline:
                raise TimeoutError('Lightroom bridge timed out. Check the plug-in, then start a new run.')
            time.sleep(.15)
        result = json.loads(response.read_text('utf-8'))
        if result.get('id') != job_id or result.get('run_id') != self.run_id:
            raise RuntimeError('Unexpected bridge response ID or session')
        if not self.keep_responses: response.unlink()
        self.connected = True
        if action == 'hello': require_bridge_version(result)
        if result.get('error'): raise RuntimeError(result['error'])
        if 'path' in result:
            path = Path(result['path']).resolve()
            allowed = (self.directory/'renders'/job_id).resolve()
            if not path.is_relative_to(allowed): raise RuntimeError('Export returned outside its private job directory')
            if not path.is_file(): raise FileNotFoundError(path)
            result['path'] = path
        return result

    def discard_render(self, path):
        path = Path(path).resolve()
        root = (self.directory/'renders').resolve()
        if path.parent.parent == root and path.parent.name.startswith(self.run_id+'-'):
            shutil.rmtree(path.parent)

    def stop(self):
        # One-way stop: safe even if Lightroom is still finishing a timed-out job.
        self.sequence += 1
        atomic_write(self.directory/'request.json',json_bytes({'id':f'{self.run_id}-stop','run_id':self.run_id,'action':'stop'}),overwrite=True)


def write_preview(path, cm, baseline, target, final, overwrite=False, warning=False):
    height, width = target.shape[:2]
    header = 56 if warning else 32
    canvas = Image.new('RGB',(width*3,height+header),'white')
    draw = ImageDraw.Draw(canvas)
    if warning:
        draw.text((8,8),'WARNING: approximate geometry / color match - review in Lightroom',fill='#a04400')
    for i,(lab,label) in enumerate(zip((baseline,target,final),
                                  ['Lightroom baseline','Apple Photos target','Best Lightroom match'])):
        # Letterbox instead of stretching: the preview must reveal crop differences.
        panel = ImageOps.pad(Image.fromarray(cm.preview(lab)),(width,height),color='#dddddd')
        canvas.paste(panel,(i*width,header))
        draw.text((i*width+8,header-24),label,fill='black')
    import io
    data = io.BytesIO()
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    canvas.save(data,format='JPEG',quality=94,icc_profile=profile)
    atomic_write(path,data.getvalue(),overwrite)


def output_paths(pair):
    base = pair.target.with_suffix('')
    return pair.target.with_suffix('.xmp'), Path(str(base)+'.match-preview.jpg')


def remove_legacy_report(pair):
    path = Path(str(pair.target.with_suffix(''))+'.match.json')
    if not path.is_file() or path.is_symlink(): return
    try:
        old = json.loads(path.read_text('utf-8'))
        if isinstance(old,dict) and old.get('source') == str(pair.source) and old.get('target') == str(pair.target):
            path.unlink()
    except (OSError, ValueError):
        pass


def display_path(path, root):
    path, root = Path(path), Path(root)
    try: relative = path.relative_to(root)
    except ValueError:
        try: relative = path.resolve().relative_to(root.resolve())
        except (ValueError,OSError): return str(path)
    return str(Path(root.name)/relative)


def require_bridge_version(response):
    if response.get('bridge_version') != '0.2.0':
        raise RuntimeError('PhotoRoom plug-in 0.2.0 is required. Reload the updated plug-in and start a new run.')


def duration(seconds):
    seconds = max(0,int(seconds))
    hours, rest = divmod(seconds,3600)
    minutes, seconds = divmod(rest,60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def color_progress(score, best, baseline):
    if baseline > 1e-9:
        return f'color error: {100*score/baseline:.1f}% of start, best: {100*best/baseline:.1f}% of start'
    # A zero starting error has no meaningful percentage denominator.
    return f'color difference: {score:.2f} points, best: {best:.2f} points (starting error was zero)'


def color_result(baseline, final):
    if baseline > 1e-9:
        return f'color error: 100% -> {100*final/baseline:.1f}% of start'
    return f'color difference: {final:.2f} points (starting error was zero)'


class ProcessingProgress:
    def __init__(self, total, max_evals, clock=None):
        self.total, self.max_evals = total, max_evals
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.index, self.fraction, self.name = 0, 0, ''

    def start(self, index, name):
        self.index, self.fraction, self.name = index, 0, name
        self.show()

    def evaluation(self, number, detail):
        # Reserve time for the final render and catalog commit. Early estimates
        # are approximate; they improve as evaluations and photos complete.
        self.fraction = min(.9,.9*number/self.max_evals)
        self.show(detail)

    def show(self, detail=''):
        elapsed = self.clock()-self.started
        done = self.index-1+self.fraction
        remaining = '~'+duration(elapsed/done*(self.total-done)) if done > 0 else 'estimating'
        message = f'({self.index}/{self.total}) Processing {self.name}'
        if detail: message += ' | '+detail
        print(f'{message} | elapsed {duration(elapsed)} | left {remaining}',flush=True)

    def finish(self):
        print(f'Processing time: {duration(self.clock()-self.started)}',flush=True)


def write_run_report(directory, reports, args=None):
    lines = []
    for report in reports:
        status = {'matched':'SAVED','warning':'WARNING'}.get(report['status'],report['status'].upper())
        name = report.get('source',report.get('target','Run'))
        if args and name != 'Run': name = display_path(name,args.orig if report.get('source') else args.edit)
        lines.append(f'{status} {name}')
        if report.get('xmp'):
            output = display_path(report['xmp'],args.edit) if args else report['xmp']
            lines.append('  Settings: '+output)
        for reason in report.get('warnings',[]) + ([report['reason']] if report.get('reason') else []):
            lines.append('  '+reason)
    atomic_write(directory/'report.txt',('\n'.join(lines)+'\n').encode('utf-8'),overwrite=True)


def print_run_summary(reports):
    print(f"\nPassed: {sum(r['status'] == 'matched' for r in reports)}",flush=True)
    print(f"Warnings: {sum(r['status'] == 'warning' for r in reports)}",flush=True)
    print(f"Failed: {sum(r['status'] == 'failed' for r in reports)}",flush=True)
    skipped = sum(r['status'] == 'skipped' for r in reports)
    if skipped: print(f'Skipped: {skipped}',flush=True)
    for status in ('warning','failed','aborted'):
        for report in reports:
            if report['status'] != status: continue
            name = Path(report.get('source',report.get('target','Run'))).name
            messages = report.get('warnings',[]) + ([report['reason']] if report.get('reason') else [])
            print(f"{status.upper()} {name}: {'; '.join(messages)}",flush=True)


class ExistingOutputError(FileExistsError):
    pass


def check_outputs(pair, args):
    xmp_path, preview_path = output_paths(pair)
    for p in (xmp_path,preview_path):
        if p == xmp_path and p.exists() and not args.overwrite:
            raise ExistingOutputError(f'output already exists: {display_path(p,getattr(args,"edit",pair.target.parent))}')
        if p.is_symlink(): raise ValueError(f'Refusing symlink output {p}')
    return xmp_path, preview_path


def match_one(pair, args, bridge, cm, progress=None):
    xmp_path, preview_path = check_outputs(pair,args)
    fallback = Path(args.target_icc).read_bytes() if args.target_icc else None
    target, target_info = load_tiff(pair.target,cm,args.edge,fallback)
    if target_info['bit_depth'] != 16: raise ValueError('Target must be a 16-bit TIFF')
    begin = bridge.request('begin',pair.source)
    require_bridge_version(begin)
    base = begin['settings']
    if 'Exposure2012' not in base:
        raise ValueError('Photo uses unsupported old process settings; update its process version in Classic first')

    def render(settings):
        response = bridge.request('render',pair.source,settings,args.edge)
        try: lab, _ = load_tiff(response['path'],cm,args.edge)
        finally:
            if not args.keep_renders: bridge.discard_render(response['path'])
        return lab, response['settings']

    baseline, base = render(base)
    warnings = []
    if args.geometry == 'auto':
        def orient(turns):
            direction = 'counterclockwise' if turns > 0 else 'clockwise'
            print(f'  {pair.source.name}: rotating {abs(turns)*90} degrees {direction}',flush=True)
            return bridge.request('orient',pair.source,{'quarter_turns_ccw':turns})['settings']
        baseline, base, geometry, warnings = align_geometry(baseline,base,target,render,orient)
    else:
        geometry = {'method':'user-declared-prealigned'}
    try:
        comparison = ColorComparison(baseline,target,approximate=bool(warnings),
                                     registration=geometry.get('validated',geometry))
        baseline_loss = comparison(baseline)
    except GeometryError as error:
        warnings.append(str(error)+' Continuing with an approximate color match.')
        comparison = ColorComparison(baseline,target,approximate=True)
        baseline_loss = comparison(baseline)
    if comparison.mode == 'color-quantiles':
        warnings.append('Reliable pixel alignment was unavailable; colors were matched by their distributions. Review the result.')
    for warning in warnings:
        print(f'  {pair.source.name}: {warning}',flush=True)
    best_image, best_score, actual_settings = baseline.copy(), baseline_loss, copy.deepcopy(base)
    cache = {}; evaluations = 0

    def evaluate(settings):
        nonlocal best_image, best_score, actual_settings, evaluations
        ident = hashlib.sha256(json_bytes(settings)).hexdigest()
        if ident in cache: return cache[ident]
        # Reuse the baseline already rendered for registration.
        if settings == base:
            lab, actual = baseline, base
        else:
            lab, actual = render(settings)
        score = comparison(lab)
        evaluations += 1; cache[ident] = score
        detail = f'eval {evaluations}/{args.max_evals} | '+color_progress(score,min(best_score,score),baseline_loss)
        if progress: progress.evaluation(evaluations,detail)
        else: print(f'  {pair.source.name}: {detail}',flush=True)
        if score < best_score:
            best_image, best_score, actual_settings = lab.copy(), score, copy.deepcopy(actual)
        return score

    _, _, count, history = coordinate_search(base,evaluate,args.max_evals,args.passes)
    # Restore and re-render the actual winner, then serialize it with Lightroom.
    final_image, final_settings = render(actual_settings)
    final_loss = comparison(final_image)
    if abs(final_loss-best_score) > max(.15,best_score*.05):
        raise RuntimeError('Final render was not repeatable; no XMP written. Check background AI updates or manual edits during matching.')
    snapshot = settings_snapshot(final_settings,pair.source,warnings)
    report = {'status':'warning' if warnings else 'matched','warnings':warnings,
              'comparison':comparison.mode,'source':str(pair.source),'target':str(pair.target),
              'xmp':str(xmp_path),'target_info':target_info,'geometry':geometry,
              'baseline_mean_delta_e76':baseline_loss,'final_mean_delta_e76':final_loss,
              'evaluations':evaluations,'settings':final_settings}
    # Regeneration replaces a leftover preview even when no completion marker exists.
    write_preview(preview_path,cm,baseline,target,final_image,True,warning=bool(warnings))
    bridge.request('commit',pair.source)
    # Publish the completion marker last. A failed write triggers main's catalog
    # rollback; interrupted runs without a marker are automatically retried.
    atomic_write(xmp_path,snapshot,args.overwrite)
    remove_legacy_report(pair)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--orig',type=Path,required=True,help='Original RAW/JPEG tree already imported in Lightroom')
    p.add_argument('--edit',type=Path,required=True,help='Matching 16-bit TIFF tree; results are written here')
    p.add_argument('--prefer',choices=['raw','jpeg'],help='Resolve RAW+JPEG same-stem ambiguity explicitly')
    p.add_argument('--limit',type=int,help='Process at most this many matched pairs')
    p.add_argument('--max-evals',type=int,default=120,help='Maximum objective evaluations per photo (default 120)')
    p.add_argument('--passes',type=int,default=5,help='Search passes with progressively smaller steps')
    p.add_argument('--virtual-copies',action='store_true',help='Match virtual copies instead of the originals in Lightroom’s catalog')
    p.add_argument('--edge',type=int,default=768,help='Maximum matching render edge in pixels (default 768)')
    p.add_argument('--geometry',choices=['auto','prealigned'],default='auto',help='Automatic crop and straightening, or trust manually matched source geometry')
    p.add_argument('--target-icc',help='ICC file for untagged targets only; embedded profiles take precedence')
    p.add_argument('--timeout',type=float,default=600,help='Seconds to wait for each bridge response')
    p.add_argument('--overwrite',action='store_true',help='Replace generated XMP and preview files')
    p.add_argument('--keep-renders',action='store_true',help='Keep temporary Lightroom exports (can use substantial disk space)')
    p.add_argument('--dry-run',action='store_true',help='Check filename pairing only; write nothing and do not contact Lightroom')
    args = p.parse_args(argv)
    args.orig = args.orig.expanduser().resolve(); args.edit = args.edit.expanduser().resolve()
    if not args.orig.is_dir() or not args.edit.is_dir(): p.error('--orig and --edit must exist')
    if args.orig.is_relative_to(args.edit) or args.edit.is_relative_to(args.orig): p.error('orig and edit must be separate, non-nested directories')
    if any(SESSION_DIR.resolve().is_relative_to(root) for root in (args.orig,args.edit)): p.error('The project’s match-session folder must be outside orig and edit')
    if args.max_evals < 1 or args.passes < 1 or not 128 <= args.edge <= 2048 or args.timeout <= 0: p.error('Invalid search budget, edge (128..2048), passes, or timeout')
    if args.limit is not None and args.limit < 1: p.error('--limit must be positive')
    pairs, issues = discover(args.orig,args.edit,args.prefer)
    if args.dry_run:
        for issue in issues: print(f'SKIP {display_path(issue["target"],args.edit)}: {issue["reason"]}',flush=True)
        print(f'{len(pairs)} pairs ready; {len(issues)} pairing issues.',flush=True)
        for pair in pairs[:args.limit]: print(f'{pair.source} -> {pair.target.with_suffix(".xmp")} (reference: {pair.target})')
        return 2 if issues else 0
    if not pairs and not issues:
        print('No matching photos found.',flush=True)
        return 2
    try:
        with SessionLock(SESSION_DIR):
            return run_session(args,pairs,issues)
    except RuntimeError as error:
        print(str(error),file=sys.stderr)
        return 2


def run_session(args, pairs, issues):
    directory = SESSION_DIR
    run_id = uuid.uuid4().hex
    atomic_write(directory/'session.json',json_bytes({'protocol':2,'run_id':run_id,
                 'orig':str(args.orig),'edit':str(args.edit),'virtual_copies':args.virtual_copies}),overwrite=True)
    print('\nIn Lightroom Classic, start Library > Plug-in Extras > PhotoRoom: run matching bridge.\n'
          f'Waiting for Lightroom to connect (up to {args.timeout:g} seconds)...\n',flush=True)
    cm = ColorManager()
    bridge = Bridge(directory,args.timeout,run_id,args.keep_renders)
    reports = []
    progress = None
    active_pair = None
    try:
        response = bridge.request('hello')
        require_bridge_version(response)
        if response.get('ready') is not True: raise RuntimeError('Lightroom bridge did not confirm it is ready.')
        print('Lightroom connected. Checking files...',flush=True)
        pending = []
        total = len(issues)+len(pairs)
        existing = 0
        for index, issue in enumerate(issues,1):
            print(f'({index}/{total}) Checking {display_path(issue["target"],args.edit)}',flush=True)
            print(f'SKIP {display_path(issue["target"],args.edit)}: {issue["reason"]}',flush=True)
            reports.append(issue)
        for index, pair in enumerate(pairs,len(issues)+1):
            print(f'({index}/{total}) Checking {pair.source.name}',flush=True)
            try:
                check_outputs(pair,args)
                pending.append(pair)
            except ExistingOutputError as error:
                existing += 1
                reports.append({'source':str(pair.source),'target':str(pair.target),'status':'skipped','reason':str(error)})
                print(f'SKIP {pair.source.name}: {error}',flush=True)
            except Exception as error:
                reports.append({'source':str(pair.source),'target':str(pair.target),'status':'failed','reason':str(error)})
                print(f'FAILED {pair.source.name}: {error}',flush=True)
        if args.limit: pending = pending[:args.limit]
        print(f'Check complete: {len(pending)} to process, {sum(r["status"] == "skipped" for r in reports)} skipped, '
              f'{sum(r["status"] == "failed" for r in reports)} failed.',flush=True)
        if existing: print('Use --overwrite to replace existing results.',flush=True)
        write_run_report(directory,reports,args)
        progress = ProcessingProgress(len(pending),args.max_evals)
        for index, pair in enumerate(pending,1):
            active_pair = pair
            progress.start(index,pair.source.name)
            try:
                report = match_one(pair,args,bridge,cm,progress=progress); reports.append(report)
                label = 'WARNING' if report['warnings'] else 'SAVED'
                detail = ' Best-effort result saved; '+ ' '.join(report['warnings']) if report['warnings'] else ''
                print(f'{label} {display_path(report["xmp"],args.edit)} ({color_result(report["baseline_mean_delta_e76"],report["final_mean_delta_e76"])}){detail}',flush=True)
            except ExistingOutputError as e:
                reports.append({'source':str(pair.source),'target':str(pair.target),'status':'skipped','reason':str(e)})
                print(f'SKIP {pair.source.name}: {e}',flush=True)
            except (TimeoutError,KeyboardInterrupt): raise
            except Exception as e:
                try: bridge.request('abort',pair.source)
                except Exception as restore_error:
                    e=RuntimeError(f'{e}; restoring original settings also failed: {restore_error}')
                failure = {'source':str(pair.source),'target':str(pair.target),'status':'failed','reason':str(e)}
                if isinstance(e,GeometryError): failure['geometry'] = e.diagnostics
                reports.append(failure)
                print(f'FAILED {pair.source.name}: {e}',flush=True)
            write_run_report(directory,reports,args)
    except (TimeoutError,KeyboardInterrupt,RuntimeError) as e:
        aborted = {'status':'aborted','reason':str(e) or 'Interrupted'}
        if active_pair: aborted['source'] = str(active_pair.source)
        reports.append(aborted)
        print('Run stopped. Completed results remain available.',flush=True)
    finally:
        try: bridge.stop()
        finally:
            cm.close()
            write_run_report(directory,reports,args)
    if progress: progress.finish()
    print(f'Run report: {directory / "report.txt"}',flush=True)
    print_run_summary(reports)
    return 2 if any(r['status'] != 'matched' for r in reports) else 0


if __name__ == '__main__':
    sys.exit(main())
