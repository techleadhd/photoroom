"""Best-effort geometry through Lightroom renders, with explicit review warnings."""
import copy
import math

import cv2
import numpy as np

from matching import (GeometryError, OrientationChangeRequired, ORIENTATION_TURNS,
                      color_error, infer_crop)

FULL_CROP = dict(HasCrop=False, CropLeft=0, CropTop=0, CropRight=1,
                 CropBottom=1, CropAngle=0, CropConstrainToWarp=0)


def refine_crop(settings, full_dimensions, residual):
    """Compose target->current render with current crop->full native frame.

    Lightroom stores the two rotated diagonal endpoints. Reconstruct the other
    two corners in pixels before composing, so portrait aspect ratios work too.
    """
    turns = ORIENTATION_TURNS[settings.get('orientation', 'AB')]
    dimensions = np.array(full_dimensions, dtype=float)
    native_dimensions = dimensions[::-1] if turns % 2 else dimensions
    start = np.array([settings['CropLeft'], settings['CropTop']])*native_dimensions
    end = np.array([settings['CropRight'], settings['CropBottom']])*native_dimensions
    angle = math.radians(settings['CropAngle'])
    u = np.array([math.cos(angle), math.sin(angle)])
    v = np.array([-math.sin(angle), math.cos(angle)])
    corners = np.array([start, start+np.dot(end-start,u)*u,
                        end, start+np.dot(end-start,v)*v])/native_dimensions
    for _ in range(turns):
        corners = np.column_stack((corners[:,1], 1-corners[:,0]))
    corners = np.roll(corners, -turns, axis=0)
    relative = np.array(residual['display_corners'])
    desired = corners[0]+relative[:,0,None]*(corners[1]-corners[0])+relative[:,1,None]*(corners[3]-corners[0])
    for _ in range(turns):
        desired = np.column_stack((1-desired[:,1], desired[:,0]))
    desired = np.roll(desired, turns, axis=0)
    left, top, right, bottom = np.clip(desired[[0,2]].ravel(), 0, 1)
    new_angle = settings['CropAngle']+residual['angle_degrees']
    if left >= right or top >= bottom or abs(new_angle) > 45:
        raise GeometryError('Further crop correction is outside Lightroom’s supported geometry.')
    return dict(HasCrop=True, CropLeft=float(left), CropTop=float(top),
                CropRight=float(right), CropBottom=float(bottom),
                CropAngle=float(new_angle), CropConstrainToWarp=0)


def align_geometry(image, settings, target, render, orient):
    """Try orientation, crop, then at most three measured crop corrections.

    Keep the best measured render, including the starting crop. Only geometry
    uncertainty becomes a warning; bridge/IO failures propagate to rollback.
    """
    candidates = []
    orientation_adjustment = None

    def inspect(lab, actual):
        try:
            crop, info = infer_crop(lab, target, actual.get('orientation','AB'),
                                    allow_outside=True, tolerance=.015)
            error = None
        except GeometryError as exc:
            crop, info, error = None, exc.diagnostics, exc
        info = dict(info, aligned=crop is None and error is None)
        score = info.get('corner_error', 0 if info.get('method') == 'identity-gradient' else math.inf)
        candidates.append((score, lab, copy.deepcopy(actual), info, error))
        return crop, info, error

    crop, initial, error = inspect(image, settings)
    if crop is None and error is None:
        return image, settings, initial, []
    # Registration of a cropped render describes a relative crop, not absolute
    # SDK coordinates. Expose the whole source before estimating a new crop.
    if any(abs(float(settings.get(k,v))-v) > .0001 for k,v in FULL_CROP.items()
           if k not in ('HasCrop','CropConstrainToWarp')):
        image, settings = render(dict(settings, **FULL_CROP))
        crop, initial, error = inspect(image, settings)
    if isinstance(error, OrientationChangeRequired):
        turns = error.turns
        orientation_adjustment = {'quarter_turns_ccw':turns, 'detected':error.diagnostics}
        image, settings = render(orient(turns))
        crop, initial, error = inspect(image, settings)
    full_dimensions = [image.shape[1], image.shape[0]]
    for _ in range(4):
        if error is not None or crop is None:
            break
        image, settings = render(dict(settings, **crop))
        remaining, validation, error = inspect(image, settings)
        if remaining is None and error is None:
            break
        if 'display_corners' not in validation or isinstance(error, OrientationChangeRequired):
            break
        try:
            crop = refine_crop(settings, full_dimensions, validation)
        except GeometryError:
            break

    best = min(candidates, key=lambda item:(not item[3]['aligned'],item[0]))
    score, best_image, best_settings, validation, error = best
    # Prefer a validated candidate over tiny numerical differences in the score.
    aligned = validation['aligned']
    if best_settings != settings:
        current_orientation = settings.get('orientation','AB')
        wanted_orientation = best_settings.get('orientation','AB')
        if current_orientation != wanted_orientation:
            delta = (ORIENTATION_TURNS[wanted_orientation]-ORIENTATION_TURNS[current_orientation]) % 4
            orient(delta if delta <= 2 else delta-4)
        best_image, best_settings = render(best_settings)
    report = dict(initial, validated=validation, attempts=len(candidates))
    if orientation_adjustment:
        report['orientation_adjustment'] = orientation_adjustment
    warnings = []
    if not aligned:
        if math.isfinite(score):
            reason = (f'Crop/rotation remains approximate (edge mismatch {score:.1%}, '
                      f'residual angle {validation.get("angle_degrees",0):+.2f} degrees).')
        else:
            reason = str(error or 'Could not verify crop/rotation.')
        warnings.append(reason+' Best available geometry retained; review the preview and adjust in Lightroom if needed.')
    return best_image, best_settings, report, warnings


class ColorComparison:
    """Freeze the comparison geometry before changing any color controls.

    For uncertain geometry compare only registered overlapping pixels. If no
    reliable registration exists, color quantiles give a weaker spatially
    independent objective; the caller must label that result as a warning.
    """
    def __init__(self, baseline, target, approximate=False, registration=None):
        self.shape = baseline.shape
        self.target = target
        self.matrix = None
        self.mode = 'pixel-delta-e76'
        if not approximate:
            return
        self.mode = 'color-quantiles'
        matrix = (registration or {}).get('target_to_source')
        if matrix is not None:
            matrix = np.array(matrix, dtype=float)
            height, width = target.shape[:2]
            valid = cv2.warpAffine(np.ones(baseline.shape[:2],np.uint8),matrix,(width,height),
                                   flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP)
            valid = cv2.erode(valid,np.ones((7,7),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0).astype(bool)
            if valid.mean() >= .25:
                self.matrix, self.mask = matrix, valid
                self.mode = 'overlap-delta-e76'
                self.coverage = float(valid.mean())
                self.blurred_target = cv2.GaussianBlur(target,(0,0),1.2)
        if self.matrix is None:
            self.target_quantiles = self.quantiles(target)

    @staticmethod
    def quantiles(image):
        # Equal spatial sampling keeps large images cheap; no geometry is inferred.
        sampled = cv2.resize(image,(128,128),interpolation=cv2.INTER_AREA)
        return np.quantile(sampled.reshape(-1,3),np.linspace(.02,.98,49),axis=0)

    def __call__(self, candidate):
        if candidate.shape != self.shape:
            raise RuntimeError('Lightroom render dimensions changed during color matching.')
        if self.mode == 'pixel-delta-e76':
            return color_error(candidate,self.target)
        if self.matrix is None:
            return float(np.linalg.norm(self.quantiles(candidate)-self.target_quantiles,axis=1).mean())
        height,width = self.target.shape[:2]
        warped = cv2.warpAffine(candidate,self.matrix,(width,height),
                                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        difference = cv2.GaussianBlur(warped,(0,0),1.2)-self.blurred_target
        return float(np.linalg.norm(difference[self.mask],axis=1).mean())
