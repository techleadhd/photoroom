"""Geometry regressions with textured images and a simulated crop renderer."""
import copy
import math
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from geometry import align_geometry, ColorComparison, FULL_CROP
from matching import (coordinate_search, GeometryError, infer_crop,
                      ORIENTATION_TURNS, OrientationChangeRequired, parameters, register)


def texture():
    rng = np.random.default_rng(52)
    image = np.full((480,640,3),30,np.float32)
    for _ in range(250):
        x,y = rng.integers([15,15],[625,465])
        cv2.circle(image,(int(x),int(y)),int(rng.integers(3,14)),
                   (float(rng.uniform(10,95)),float(rng.uniform(-20,20)),5),-1)
    return image


class Renderer:
    def __init__(self, source, shrink=1, ignore_crop=False):
        self.source = source
        self.shrink = shrink
        self.ignore_crop = ignore_crop
        self.settings = dict(FULL_CROP, Exposure2012=0, orientation='AB')
        self.turns = []
        self.calls = 0

    def render(self, settings):
        self.calls += 1
        self.settings = copy.deepcopy(settings)
        native = self.source
        if settings.get('HasCrop') and not self.ignore_crop:
            h,w = native.shape[:2]
            start = np.array([settings['CropLeft']*w, settings['CropTop']*h])
            end = np.array([settings['CropRight']*w, settings['CropBottom']*h])
            center = (start+end)/2
            start, end = center+(start-center)*self.shrink, center+(end-center)*self.shrink
            angle = math.radians(settings['CropAngle'])
            u, v = np.array([math.cos(angle),math.sin(angle)]),np.array([-math.sin(angle),math.cos(angle)])
            size = (max(16,round(float(np.dot(end-start,u)))),max(16,round(float(np.dot(end-start,v)))))
            matrix = np.column_stack((u,v,start))
            native = cv2.warpAffine(native,matrix,size,flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        return np.ascontiguousarray(np.rot90(native,ORIENTATION_TURNS[settings['orientation']])),copy.deepcopy(settings)

    def orient(self, turns):
        self.turns.append(turns)
        value = (ORIENTATION_TURNS[self.settings['orientation']]+turns)%4
        self.settings['orientation'] = list(ORIENTATION_TURNS)[value]
        return copy.deepcopy(self.settings)

    def align(self, target):
        image, settings = self.render(self.settings)
        return align_geometry(image,settings,target,self.render,self.orient)


class GeometryTests(unittest.TestCase):
    def test_refines_seven_percent_overcrop_even_with_zero_residual_angle(self):
        source = texture()
        renderer = Renderer(source,shrink=.87)
        target = source[70:400,95:550]
        image, settings, info, warnings = renderer.align(target)
        self.assertFalse(warnings,warnings)
        self.assertGreaterEqual(info['attempts'],3)
        self.assertLess(info['validated']['corner_error'],.015)
        self.assertLess(abs(info['validated']['angle_degrees']),.1)
        self.assertTrue(settings['HasCrop'])
        self.assertGreater(renderer.calls,2)

    def test_both_quarter_turns_with_existing_crop_and_straightening(self):
        source = texture()
        transformed = cv2.warpAffine(source,cv2.getRotationMatrix2D((320,240),1.11,1),(640,480))
        for turns in [-1,1,2]:
            for starting in ['AB','BC']:
                with self.subTest(turns=turns,starting=starting):
                    renderer = Renderer(source)
                    renderer.settings.update(HasCrop=True,CropLeft=.25,CropRight=.75,
                                             CropTop=.2,CropBottom=.8,CropAngle=-2,orientation=starting)
                    target = np.ascontiguousarray(np.rot90(transformed[70:400,95:550],turns))
                    image, settings, info, warnings = renderer.align(target)
                    self.assertFalse(warnings,warnings)
                    self.assertEqual(ORIENTATION_TURNS[settings['orientation']],turns%4)
                    self.assertAlmostEqual(settings['CropAngle'],1.11,delta=.12)
                    self.assertLess(info['validated']['corner_error'],.015)

    def test_unresponsive_crop_retains_best_geometry_with_warning(self):
        source = texture(); renderer = Renderer(source,ignore_crop=True)
        image, settings, info, warnings = renderer.align(source[70:400,95:550])
        self.assertTrue(warnings)
        self.assertFalse(settings['HasCrop'])
        self.assertLessEqual(renderer.calls,7)
        comparison = ColorComparison(image,source[70:400,95:550],True,info['validated'])
        self.assertEqual(comparison.mode,'overlap-delta-e76')
        self.assertLess(comparison(image),.5)

    def test_missing_source_edges_warn_but_still_fit_exposure_on_overlap(self):
        source = texture()
        target = cv2.warpAffine(source,np.float32([[1,0,50],[0,1,0]]),(640,480))
        renderer = Renderer(source)
        image, settings, info, warnings = renderer.align(target)
        self.assertTrue(warnings)
        comparison = ColorComparison(image,target+np.array([5,0,0],np.float32),True,info['validated'])
        self.assertEqual(comparison.mode,'overlap-delta-e76')
        initial = comparison(image)
        best,loss,_,_ = coordinate_search({'Exposure2012':0},
                        lambda s:comparison(image+np.array([s['Exposure2012']*10,0,0],np.float32)),30,2)
        self.assertAlmostEqual(best['Exposure2012'],.5)
        self.assertLess(loss,initial/4)

    def test_low_feature_different_aspect_uses_color_distribution(self):
        source = np.full((100,200,3),30,np.float32)
        target = np.full((200,100,3),40,np.float32)
        renderer = Renderer(source)
        image, settings, info, warnings = renderer.align(target)
        self.assertTrue(warnings)
        comparison = ColorComparison(image,target,True,info['validated'])
        self.assertEqual(comparison.mode,'color-quantiles')
        self.assertLess(comparison(image+10),1e-5)
        self.assertGreater(comparison(image),10)

    def test_bridge_errors_still_propagate(self):
        source = texture()
        with self.assertRaisesRegex(RuntimeError,'Lightroom unavailable'):
            align_geometry(source,{'Exposure2012':0},source[70:400,95:550],
                           lambda s:(_ for _ in ()).throw(RuntimeError('Lightroom unavailable')),None)

    def test_explicit_quarter_turn_search_recovers_failed_initial_registration(self):
        source = texture()
        for turns in [-1,1]:
            target = np.ascontiguousarray(np.rot90(source,turns))
            calls = [0]
            def initial_failure(a,b):
                calls[0] += 1
                if calls[0] == 1: raise GeometryError('Insufficient initial features')
                return register(a,b)
            with patch('matching.register',side_effect=initial_failure):
                with self.assertRaises(OrientationChangeRequired) as caught:
                    infer_crop(source,target)
            self.assertEqual(caught.exception.turns%4,turns%4)
            self.assertTrue(caught.exception.diagnostics['explicit_quarter_turn_search'])

    def test_only_eight_basic_controls_are_searched_and_other_settings_preserved(self):
        forbidden = dict(Texture=12,Clarity2012=15,Dehaze=7,Vibrance=18,Saturation=-4,
                         Sharpness=40,HueAdjustmentRed=9,Look={'name':'Profile'},ToneCurvePV2012=[0,0,255,255])
        for wb in [{'Temperature':5500,'Tint':2},{'IncrementalTemperature':3,'IncrementalTint':2}]:
            baseline = dict(forbidden,Exposure2012=0,**wb)
            self.assertEqual(len(parameters(baseline)),8)
            def score(settings):
                for name,value in forbidden.items(): self.assertEqual(settings[name],value)
                return (settings['Exposure2012']-.5)**2
            best,_,_,_ = coordinate_search(baseline,score,40,2)
            self.assertEqual(best['Exposure2012'],.5)
            for name,value in forbidden.items(): self.assertEqual(best[name],value)
