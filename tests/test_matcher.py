import ctypes as C
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image, ImageCms
import tifffile

import match
from color import ColorManager, ColorError, load_tiff, orient
from matching import infer_crop, GeometryError, OrientationChangeRequired, color_error, coordinate_search


class FilesAndXmp(unittest.TestCase):
    def test_pairing_ambiguity_unicode_and_recursive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); orig=root/'orig'; edit=root/'edit'
            (orig/'trip').mkdir(parents=True); (edit/'trip').mkdir(parents=True)
            for name in ['cafe\u0301.CR3','cafe\u0301.JPG','cafe\u0301.XMP']: (orig/'trip'/name).touch()
            (edit/'trip'/'café.TIFF').touch()
            pairs,issues=match.discover(orig,edit)
            self.assertEqual(len(pairs),0); self.assertIn('Ambiguous',issues[0]['reason'])
            pairs,issues=match.discover(orig,edit,'raw')
            self.assertEqual(len(pairs),1); self.assertFalse(issues)
            self.assertEqual(pairs[0].source.suffix,'.CR3')












    def test_no_clobber_or_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'out.xmp'; match.atomic_write(p,b'first')
            with self.assertRaises(FileExistsError): match.atomic_write(p,b'second')
            self.assertEqual(p.read_bytes(),b'first')
            link=Path(d)/'link'; link.symlink_to(p)
            with self.assertRaises(FileExistsError): match.atomic_write(link,b'second',True)
            self.assertEqual(p.read_bytes(),b'first')

    def test_dotted_output_name(self):
        pair=match.Pair(Path('orig/file.v2.raw'),Path('edit/file.v2.tiff'))
        self.assertEqual(match.output_paths(pair)[0],Path('edit/file.v2.xmp'))

    def test_bridge_exchange(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d); bridge=match.Bridge(folder,2)
            def worker():
                import time
                while not (folder/'request.json').exists(): time.sleep(.01)
                job=json.loads((folder/'request.json').read_text())
                match.atomic_write(folder/f'response-{job["id"]}.json',match.json_bytes({'id':job['id'],'run_id':job['run_id'],'settings':{'Exposure2012':0}}))
            t=threading.Thread(target=worker); t.start()
            result=bridge.request('begin',Path('/test.raw')); t.join()
            self.assertEqual(result['settings']['Exposure2012'],0)

    def test_bridge_rejects_a_response_from_another_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bridge=match.Bridge(root,1,run_id='current')
            write=match.atomic_write
            def exchange(path,data,overwrite=False):
                write(path,data,overwrite)
                if Path(path).name=='request.json':
                    request=json.loads(data)
                    write(root/f'response-{request["id"]}.json',match.json_bytes(
                        {'id':request['id'],'run_id':'stale','settings':{}}))
            with patch('match.atomic_write',side_effect=exchange):
                with self.assertRaisesRegex(RuntimeError,'session'):bridge.request('begin',Path('/original.JPG'))

    def test_snapshot_only_records_owned_controls_and_explicit_orientation(self):
        for sdk, tag in [('AB',1),('BA',2),('CD',3),('DC',4),('CB',5),('BC',6),('AD',7),('DA',8)]:
            with self.subTest(sdk=sdk):
                data=match.settings_snapshot({'Exposure2012':.75,'Temperature':5500,'orientation':sdk,
                       'Vibrance':18,'Texture':12,'Look':{'name':'Keep in catalog'}},Path('source.JPG'),[])
                root=match.parse_xmp(data)
                self.assertEqual(match.crs_values(root),{'Exposure2012':'0.75','Temperature':'5500'})
                desc=match.image_descriptions(root)[0]
                self.assertEqual(desc.get('{'+match.TIFF+'}Orientation'),str(tag))
                self.assertEqual(desc.get('{'+match.PHOTOROOM+'}Scope'),'matched-controls-only')

    def test_original_sidecars_do_not_affect_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);orig=root/'orig';edit=root/'edit';orig.mkdir();edit.mkdir()
            (orig/'photo.ARW').touch();(edit/'photo.tiff').touch()
            (orig/'photo.xmp').write_bytes(b'invalid metadata is ignored')
            (orig/'photo.XMP').touch()
            pairs,issues=match.discover(orig,edit)
            self.assertEqual(len(pairs),1);self.assertFalse(issues)


class ColorTests(unittest.TestCase):
    def setUp(self): self.cm=ColorManager()
    def tearDown(self): self.cm.close()

    def p3_profile(self):
        class xyY(C.Structure): _fields_=[('x',C.c_double),('y',C.c_double),('Y',C.c_double)]
        class Triple(C.Structure): _fields_=[('Red',xyY),('Green',xyY),('Blue',xyY)]
        l=self.cm.lib
        l.cmsBuildParametricToneCurve.argtypes=[C.c_void_p,C.c_int,C.POINTER(C.c_double)]; l.cmsBuildParametricToneCurve.restype=C.c_void_p
        l.cmsCreateRGBProfile.argtypes=[C.POINTER(xyY),C.POINTER(Triple),C.POINTER(C.c_void_p)]; l.cmsCreateRGBProfile.restype=C.c_void_p
        l.cmsSaveProfileToMem.argtypes=[C.c_void_p,C.c_void_p,C.POINTER(C.c_uint32)]; l.cmsSaveProfileToMem.restype=C.c_int
        l.cmsFreeToneCurve.argtypes=[C.c_void_p]
        params=(C.c_double*5)(2.4,1/1.055,.055/1.055,1/12.92,.04045)
        curve=l.cmsBuildParametricToneCurve(None,4,params)
        white=xyY(.3127,.3290,1); primaries=Triple(xyY(.68,.32,1),xyY(.265,.690,1),xyY(.150,.060,1))
        profile=l.cmsCreateRGBProfile(C.byref(white),C.byref(primaries),(C.c_void_p*3)(curve,curve,curve))
        size=C.c_uint32(); l.cmsSaveProfileToMem(profile,None,C.byref(size))
        buf=C.create_string_buffer(size.value); l.cmsSaveProfileToMem(profile,buf,C.byref(size))
        l.cmsCloseProfile(profile); l.cmsFreeToneCurve(curve)
        return buf.raw

    def test_16bit_and_white(self):
        icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        rgb=np.array([[[65535,65535,65535],[32768,0,0],[32769,0,0]]],np.uint16)
        lab=self.cm.to_lab(rgb,icc)
        self.assertAlmostEqual(float(lab[0,0,0]),100,places=3)
        self.assertGreater(float(np.linalg.norm(lab[0,1]-lab[0,2])),0.0001)

    def test_p3_not_treated_as_srgb(self):
        rgb=np.array([[[65535,0,0]]],np.uint16)
        srgb=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        self.assertGreater(float(np.linalg.norm(self.cm.to_lab(rgb,self.p3_profile())-self.cm.to_lab(rgb,srgb))),10)

    def test_tiff_loads_embedded_p3_and_refuses_untagged(self):
        with tempfile.TemporaryDirectory() as d:
            rgb=np.full((20,30,3),30000,np.uint16); path=Path(d)/'target.tiff'; icc=self.p3_profile()
            tifffile.imwrite(path,rgb,photometric='rgb',extratags=[(34675,'B',len(icc),icc,False)])
            lab,info=load_tiff(path,self.cm)
            self.assertEqual(lab.shape,(20,30,3)); self.assertEqual(info['bit_depth'],16)
            tifffile.imwrite(path,rgb,photometric='rgb')
            with self.assertRaises(ColorError):load_tiff(path,self.cm)

    def test_orientation(self):
        rgb=np.zeros((2,3,3),np.uint16); rgb[0,0,0]=1
        rotated=orient(rgb,6)
        self.assertEqual(rotated.shape,(3,2,3));self.assertEqual(rotated[0,1,0],1)


class GeometryAndSearch(unittest.TestCase):
    @staticmethod
    def texture():
        rng=np.random.default_rng(11)
        image=np.zeros((480,640,3),np.float32)+30
        for _ in range(180):
            x,y=rng.integers([10,10],[630,470]); radius=int(rng.integers(3,15))
            cv2.circle(image,(int(x),int(y)),radius,(float(rng.uniform(10,95)),10,5),-1)
        return image

    def test_crop_recovered(self):
        source=self.texture(); target=source[60:420,80:560].copy();target[...,0]*=.9
        crop,report=infer_crop(source,target)
        self.assertIsNotNone(crop)
        self.assertAlmostEqual(crop['CropLeft'],.125,delta=.01)
        self.assertAlmostEqual(crop['CropBottom'],.875,delta=.01)

    def test_rotation_rejected(self):
        source=self.texture(); m=cv2.getRotationMatrix2D((320,240),7,1)
        target=cv2.warpAffine(source,m,(640,480))
        with self.assertRaises(GeometryError):infer_crop(source,target)

    def test_straightened_crop_recovered(self):
        source=self.texture()
        for angle in [1.11,-1.11,7,-7]:
            with self.subTest(angle=angle):
                matrix=cv2.getRotationMatrix2D((320,240),angle,1)
                target=cv2.warpAffine(source,matrix,(640,480))[60:420,80:560].copy()
                crop,report=infer_crop(source,target)
                self.assertAlmostEqual(crop['CropAngle'],angle,delta=.08)
                inverse=cv2.invertAffineTransform(matrix)
                diagonal=np.array([[80,60,1],[560,420,1]]) @ inverse.T / [640,480]
                for name,want in zip(['CropLeft','CropTop','CropRight','CropBottom'],diagonal.ravel()):
                    self.assertAlmostEqual(crop[name],want,delta=.003)

    def test_portrait_crop_maps_back_to_stored_pixel_coordinates(self):
        raw=self.texture()
        # Deliberately off-center: a centered square hides orientation bugs.
        raw_target=raw[70:390,110:540]
        for turns,orientation in enumerate(['AB','DA','CD','BC']):
            with self.subTest(orientation=orientation):
                source=np.ascontiguousarray(np.rot90(raw,turns))
                target=np.ascontiguousarray(np.rot90(raw_target,turns))
                crop,_=infer_crop(source,target,orientation)
                for name,want in [('CropLeft',110/640),('CropTop',70/480),('CropRight',540/640),('CropBottom',390/480)]:
                    self.assertAlmostEqual(crop[name],want,delta=.003)

    def test_straightened_portrait_uses_native_rotated_diagonal(self):
        raw=self.texture()
        matrix=cv2.getRotationMatrix2D((320,240),1.11,1)
        raw_target=cv2.warpAffine(raw,matrix,(640,480))[70:390,110:540]
        diagonal=np.array([[110,70,1],[540,390,1]]) @ cv2.invertAffineTransform(matrix).T / [640,480]
        for turns,orientation in enumerate(['AB','DA','CD','BC']):
            with self.subTest(orientation=orientation):
                source=np.ascontiguousarray(np.rot90(raw,turns))
                target=np.ascontiguousarray(np.rot90(raw_target,turns))
                crop,_=infer_crop(source,target,orientation)
                self.assertAlmostEqual(crop['CropAngle'],1.11,delta=.08)
                for name,want in zip(['CropLeft','CropTop','CropRight','CropBottom'],diagonal.ravel()):
                    self.assertAlmostEqual(crop[name],want,delta=.003)

    def test_quarter_turn_requests_orientation_change(self):
        source=self.texture()
        for turns in [-1,1,2]:
            with self.subTest(turns=turns):
                target=np.ascontiguousarray(np.rot90(source,turns))
                with self.assertRaises(OrientationChangeRequired) as caught:infer_crop(source,target)
                self.assertEqual(caught.exception.turns % 4,turns % 4)

    def test_outside_crop_has_measurements_for_diagnosis(self):
        source=self.texture()
        target=cv2.warpAffine(source,np.float32([[1,0,50],[0,1,0]]),(640,480))
        with self.assertRaisesRegex(GeometryError,'outside the current Lightroom render') as caught:
            infer_crop(source,target)
        self.assertGreater(caught.exception.diagnostics['outside_fraction'],.02)
        self.assertIn('target_to_source',caught.exception.diagnostics)

    def test_wrong_image_rejected(self):
        with self.assertRaises(GeometryError):infer_crop(self.texture(),np.zeros((480,640,3),np.float32))

    def test_identity_color_and_budget(self):
        source=self.texture(); self.assertEqual(color_error(source,source),0)
        self.assertIsNone(infer_crop(source,source)[0])
        calls=[]
        def objective(s):
            calls.append(s)
            return (s.get('Exposure2012',0)-.75)**2 + ((s.get('Contrast2012',0)-12)/30)**2
        settings,loss,n,_=coordinate_search({'Exposure2012':0,'Contrast2012':0},objective,max_evals=100,passes=5)
        self.assertLess(loss,.002);self.assertLessEqual(len(calls),100);self.assertEqual(n,len(calls))
        self.assertEqual(settings['Exposure2012'],.75)


if __name__=='__main__': unittest.main()
