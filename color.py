"""16-bit RGB TIFF -> ICC-managed CIELAB D50, using LittleCMS directly.

Pillow is used for preview files only, never to downconvert matching TIFFs.
"""
import ctypes as C
import ctypes.util
import os
from pathlib import Path

import cv2
import numpy as np
import tifffile


class ColorError(ValueError):
    pass


class ColorManager:
    RGB16 = (4 << 16) | (3 << 3) | 2
    LABDOUBLE = (1 << 22) | (10 << 16) | (3 << 3)

    def __init__(self, library=None):
        candidates = [library, os.environ.get('LCMS2_LIBRARY'),
                      ctypes.util.find_library('lcms2'),
                      '/opt/homebrew/lib/liblcms2.dylib',
                      '/usr/local/lib/liblcms2.dylib']
        self.lib = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                self.lib = C.CDLL(candidate)
                break
            except OSError:
                pass
        if self.lib is None:
            raise ColorError('LittleCMS 2 not found. On macOS: brew install little-cms2')
        l = self.lib
        signatures = {
            'cmsOpenProfileFromMem': ([C.c_void_p, C.c_uint32], C.c_void_p),
            'cmsCreateLab4Profile': ([C.c_void_p], C.c_void_p),
            'cmsCreate_sRGBProfile': ([], C.c_void_p),
            'cmsCloseProfile': ([C.c_void_p], C.c_int),
            'cmsCreateTransform': ([C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32], C.c_void_p),
            'cmsDeleteTransform': ([C.c_void_p], None),
            'cmsDoTransform': ([C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint32], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(l, name); fn.argtypes = args; fn.restype = result
        self.lab = l.cmsCreateLab4Profile(None)
        self.srgb = l.cmsCreate_sRGBProfile()
        if not self.lab or not self.srgb:
            raise ColorError('Cannot create LittleCMS profiles')
        self.transforms = {}

    def close(self):
        for transform in self.transforms.values(): self.lib.cmsDeleteTransform(transform)
        self.transforms.clear()
        if self.lab: self.lib.cmsCloseProfile(self.lab); self.lab = None
        if self.srgb: self.lib.cmsCloseProfile(self.srgb); self.srgb = None

    def to_lab(self, rgb, icc):
        rgb = np.ascontiguousarray(rgb, dtype=np.uint16)
        if icc not in self.transforms:
            buf = C.create_string_buffer(icc)
            profile = self.lib.cmsOpenProfileFromMem(buf, len(icc))
            if not profile: raise ColorError('Invalid embedded ICC profile')
            try:
                # Relative colorimetric, no black point compensation or gamut clipping to sRGB.
                tr = self.lib.cmsCreateTransform(profile, self.RGB16, self.lab, self.LABDOUBLE, 1, 0)
            finally:
                self.lib.cmsCloseProfile(profile)
            if not tr: raise ColorError('ICC profile is incompatible with RGB image data')
            self.transforms[icc] = tr
        result = np.empty(rgb.shape, dtype=np.float64)
        self.lib.cmsDoTransform(self.transforms[icc], rgb.ctypes.data, result.ctypes.data, rgb.size // 3)
        if not np.isfinite(result).all(): raise ColorError('Nonfinite color transform')
        return result.astype(np.float32)

    def preview(self, lab):
        lab = np.ascontiguousarray(lab, dtype=np.float64)
        result = np.empty(lab.shape, dtype=np.uint16)
        tr = self.lib.cmsCreateTransform(self.lab, self.LABDOUBLE, self.srgb, self.RGB16, 1, 0)
        if not tr: raise ColorError('Cannot create preview transform')
        try: self.lib.cmsDoTransform(tr, lab.ctypes.data, result.ctypes.data, lab.size // 3)
        finally: self.lib.cmsDeleteTransform(tr)
        return ((result.astype(np.uint32) + 128) // 257).astype(np.uint8)


def orient(rgb, orientation):
    operations = {1: lambda a:a, 2:lambda a:a[:, ::-1], 3:lambda a:a[::-1, ::-1],
                  4:lambda a:a[::-1], 5:lambda a:np.swapaxes(a,0,1),
                  6:lambda a:np.rot90(a,-1), 7:lambda a:np.swapaxes(a,0,1)[::-1,::-1],
                  8:lambda a:np.rot90(a,1)}
    if orientation not in operations: raise ColorError(f'Unknown orientation {orientation}')
    return np.ascontiguousarray(operations[orientation](rgb))


def load_tiff(path, cm, edge=768, fallback_icc=None):
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        # Apple's ordinary TIFF exports and Lightroom's TIFF renders are RGB.
        if int(page.photometric) != 2:
            raise ColorError(f'{path}: expected RGB TIFF, got {page.photometric}')
        rgb = page.asarray()
        if int(page.planarconfig or 1) == 2: rgb = np.moveaxis(rgb, 0, -1)
        if rgb.ndim != 3 or rgb.shape[-1] not in (3, 4):
            raise ColorError(f'{path}: expected RGB or opaque RGBA')
        if rgb.dtype not in (np.dtype('uint8'), np.dtype('uint16')):
            raise ColorError(f'{path}: floating/HDR or non-unsigned TIFF is unsupported')
        if rgb.shape[-1] == 4:
            if not np.all(rgb[..., 3] == np.iinfo(rgb.dtype).max):
                raise ColorError(f'{path}: transparency is unsupported')
            rgb = rgb[..., :3]
        depth = rgb.dtype.itemsize * 8
        if depth == 8: rgb = rgb.astype(np.uint16) * 257
        orientation = int(page.tags[274].value) if 274 in page.tags else 1
        profile = bytes(page.tags[34675].value) if 34675 in page.tags else fallback_icc
        if not profile:
            raise ColorError(f'{path}: missing embedded ICC profile; no color space is guessed')
    rgb = orient(rgb, orientation)
    original_shape = rgb.shape[:2]
    ratio = min(1.0, edge / max(original_shape))
    if ratio < 1:
        rgb = cv2.resize(rgb, (max(1, round(rgb.shape[1]*ratio)), max(1, round(rgb.shape[0]*ratio))), interpolation=cv2.INTER_AREA)
    return cm.to_lab(rgb, profile), {'bit_depth': depth, 'height': original_shape[0], 'width': original_shape[1], 'embedded_or_supplied_icc': True}
