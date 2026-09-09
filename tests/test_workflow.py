"""Catalog-only matching: no source metadata reads, exports, or writes."""
import contextlib
import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from color import ColorManager
import match
from test_geometry import Renderer, texture


class CatalogBridge:
    def __init__(self, root, renderer, images):
        self.root,self.renderer,self.images=root,renderer,images
        self.actions=[]
        self.committed=False
        self.fail_commit=False
        self.initial=copy.deepcopy(renderer.settings)

    def request(self,action,source=None,settings=None,edge=768):
        self.actions.append(action)
        if action=='hello':return {'ready':True,'bridge_version':'0.2.0'}
        if action=='abort':
            self.renderer.settings=copy.deepcopy(self.initial);self.committed=False
            return {'restored':True}
        if action=='begin':return {'settings':copy.deepcopy(self.renderer.settings),'bridge_version':'0.2.0'}
        if action=='orient':return {'settings':self.renderer.orient(settings['quarter_turns_ccw'])}
        if action=='commit':
            if self.fail_commit:raise RuntimeError('Catalog commit failed')
            self.committed=True
            return {'committed':True}
        if action!='render':raise AssertionError('Only rendered pixels may be exported: '+action)
        path=self.root/f'render-{len(self.actions)}.tiff';path.touch()
        image,actual=self.renderer.render(settings)
        image=image.copy();image[...,0]+=settings['Exposure2012']*10
        self.images[path]=image
        return {'path':path,'settings':actual}

    def discard_render(self,path):pass

    def stop(self):self.actions.append('stop')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.orig=self.root/'orig';self.edit=self.root/'edit';self.orig.mkdir();self.edit.mkdir()
        self.source=self.orig/'photo.JPG'
        exif=Image.Exif();exif[274]=6
        Image.new('RGB',(32,24),'red').save(self.source,exif=exif)
        self.original=self.source.read_bytes()
        self.target_path=self.edit/'photo.tiff';self.target_path.touch()
        self.pair=match.Pair(self.source,self.target_path)
        self.renderer=Renderer(texture())
        self.renderer.settings.update(Texture=12,Vibrance=18,Sharpness=40,Look={'name':'Existing profile'})
        self.target=self.renderer.source.copy();self.target[...,0]+=5
        self.images={self.target_path:self.target}
        self.bridge=CatalogBridge(self.root,self.renderer,self.images)
        self.args=SimpleNamespace(overwrite=False,target_icc=None,edge=768,geometry='auto',
                                  keep_renders=False,max_evals=30,passes=2,virtual_copies=False)
        self.cm=ColorManager();self.addCleanup(self.cm.close)

    def run_match(self):
        with patch('match.load_tiff',side_effect=lambda p,*a:(self.images[p],{'bit_depth':16})),contextlib.redirect_stdout(io.StringIO()):
            return match.match_one(self.pair,self.args,self.bridge,self.cm)

    def test_jpeg_matches_without_develop_metadata_and_leaves_original_untouched(self):
        # No ProcessVersion, no source XMP, no Original-format export required.
        result=self.run_match()
        self.assertEqual(result['status'],'matched')
        self.assertTrue(self.bridge.committed)
        self.assertEqual(set(self.bridge.actions),{'begin','render','commit'})
        self.assertAlmostEqual(result['settings']['Exposure2012'],.5)
        self.assertLess(result['final_mean_delta_e76'],result['baseline_mean_delta_e76']/4)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(list(self.orig.iterdir()),[self.source])
        for name in ('Texture','Vibrance','Sharpness','Look'):
            self.assertEqual(result['settings'][name],self.renderer.settings[name])
        self.assertTrue((self.edit/'photo.xmp').exists())
        self.assertTrue((self.edit/'photo.match-preview.jpg').exists())
        self.assertFalse((self.edit/'photo.match.json').exists())
        saved=match.crs_values(match.parse_xmp((self.edit/'photo.xmp').read_bytes()))
        self.assertEqual(saved['Exposure2012'],'0.5')
        self.assertNotIn('Texture',saved);self.assertNotIn('Look',saved)

    def test_existing_original_sidecar_is_neither_required_nor_modified(self):
        metadata=self.orig/'photo.xmp';metadata.write_bytes(b'Existing metadata is not parsed or rewritten')
        self.run_match()
        self.assertEqual(metadata.read_bytes(),b'Existing metadata is not parsed or rewritten')
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_deleting_edit_xmp_retriggers_even_with_preview_present(self):
        self.run_match()
        calls=len(self.bridge.actions)
        with self.assertRaises(match.ExistingOutputError):self.run_match()
        self.assertEqual(len(self.bridge.actions),calls)
        (self.edit/'photo.xmp').unlink()
        self.run_match()
        self.assertGreater(len(self.bridge.actions),calls)
        self.assertTrue((self.edit/'photo.xmp').is_file())

    def test_warning_result_still_commits_and_publishes_marker(self):
        self.renderer.ignore_crop=True
        self.images[self.target_path]=self.target[70:400,95:550]
        result=self.run_match()
        self.assertEqual(result['status'],'warning')
        self.assertTrue(self.bridge.committed)
        root=match.parse_xmp((self.edit/'photo.xmp').read_bytes())
        self.assertEqual(match.image_descriptions(root)[0].get('{'+match.PHOTOROOM+'}Status'),'warning')
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_commit_failure_never_creates_completion_marker(self):
        self.bridge.fail_commit=True
        with self.assertRaisesRegex(RuntimeError,'Catalog commit failed'):self.run_match()
        self.assertFalse((self.edit/'photo.xmp').exists())
        self.bridge.fail_commit=False
        self.assertEqual(self.run_match()['status'],'matched')

    def test_quarter_turn_is_saved_in_catalog_and_snapshot_only(self):
        self.images[self.target_path]=np.ascontiguousarray(np.rot90(self.renderer.source))
        self.args.max_evals=1
        result=self.run_match()
        self.assertEqual(result['settings']['orientation'],'DA')
        self.assertIn('orient',self.bridge.actions)
        self.assertTrue(self.bridge.committed)
        desc=match.image_descriptions(match.parse_xmp((self.edit/'photo.xmp').read_bytes()))[0]
        self.assertEqual(desc.get('{'+match.TIFF+'}Orientation'),'8')
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertFalse((self.orig/'photo.xmp').exists())

    def test_snapshot_write_failure_rolls_back_catalog_and_remains_retryable(self):
        real_write=match.atomic_write
        def failing_write(path,*args,**kwargs):
            if Path(path)==self.edit/'photo.xmp':raise OSError('Disk full')
            return real_write(path,*args,**kwargs)
        with patch('match.SESSION_DIR',self.root/'match-session'),patch('match.Bridge',return_value=self.bridge), \
             patch('match.ColorManager',return_value=self.cm),patch('match.atomic_write',side_effect=failing_write), \
             patch('match.load_tiff',side_effect=lambda p,*a:(self.images[p],{'bit_depth':16})), \
             contextlib.redirect_stdout(io.StringIO()):
            status=match.main(['--orig',str(self.orig),'--edit',str(self.edit),'--max-evals','1'])
        self.assertEqual(status,2)
        self.assertIn('commit',self.bridge.actions);self.assertIn('abort',self.bridge.actions)
        self.assertFalse(self.bridge.committed)
        self.assertFalse((self.edit/'photo.xmp').exists())
        self.assertEqual(self.source.read_bytes(),self.original)
