"""Geometry checks and bounded, derivative-free Lightroom parameter search."""
from dataclasses import dataclass
import copy
import math

import cv2
import numpy as np


class GeometryError(ValueError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class OrientationChangeRequired(GeometryError):
    def __init__(self, turns, diagnostics):
        super().__init__(f'Photo needs a {turns*90:+d} degree counterclockwise orientation change.',diagnostics)
        self.turns = turns


# Lightroom's two-letter orientation codes, expressed as EXIF orientation.
# Codes describe the image's orientation, not a correction to rotate it upright.
# BC is 90 degrees clockwise; DA is 90 degrees counterclockwise.
# https://community.adobe.com/bug-reports-674/p-rotated-and-flipped-raw-opens-upside-down-in-photoshop-664676
ORIENTATION_EXIF = {'AB':1,'BA':2,'CD':3,'DC':4,'CB':5,'BC':6,'AD':7,'DA':8}
ORIENTATION_TURNS = {'AB':0,'DA':1,'CD':2,'BC':3}
ORIENTATION_LABELS = {1:'upright',2:'mirrored horizontally',3:'180 degrees',
                      4:'mirrored vertically',5:'transposed',6:'90 degrees clockwise',
                      7:'transposed and rotated 180 degrees',8:'90 degrees counterclockwise'}


def gray(lab):
    return np.clip(lab[..., 0] * 2.55, 0, 255).astype(np.uint8)


def gradient_correlation(a, b):
    a = cv2.resize(gray(a), (256, 256)).astype(np.float32)
    b = cv2.resize(gray(b), (256, 256)).astype(np.float32)
    a = cv2.Laplacian(cv2.GaussianBlur(a, (0, 0), 1.0), cv2.CV_32F)
    b = cv2.Laplacian(cv2.GaussianBlur(b, (0, 0), 1.0), cv2.CV_32F)
    x, y = a.ravel(), b.ravel()
    if x.std() < 0.01 or y.std() < 0.01: return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def register(source, target):
    """Measure a similarity transform mapping target pixels into source pixels."""
    detector = cv2.SIFT_create(nfeatures=5000)
    kp_t, desc_t = detector.detectAndCompute(gray(target), None)
    kp_s, desc_s = detector.detectAndCompute(gray(source), None)
    correlation = gradient_correlation(source, target)
    same_aspect = abs(math.log((source.shape[1]/source.shape[0]) / (target.shape[1]/target.shape[0]))) < 0.01
    if desc_t is None or desc_s is None:
        good = []
    else:
        pairs = cv2.BFMatcher().knnMatch(desc_t, desc_s, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.72*p[1].distance]
    if len(good) < 12:
        if same_aspect and correlation > 0.80:
            return None, {'method':'identity-gradient', 'correlation':correlation}
        raise GeometryError('Insufficient features to verify geometry. Match crop/rotation manually, then use --geometry prealigned if necessary.')
    pts_t = np.float32([kp_t[m.queryIdx].pt for m in good])
    pts_s = np.float32([kp_s[m.trainIdx].pt for m in good])
    matrix, mask = cv2.estimateAffinePartial2D(pts_t, pts_s, method=cv2.RANSAC,
                                               ransacReprojThreshold=2.5, maxIters=4000, confidence=0.999)
    if matrix is None or mask is None or int(mask.sum()) < 12 or mask.mean() < 0.40:
        raise GeometryError('No reliable geometric match; crop/rotation needs manual review.')
    inliers = mask.ravel().astype(bool)
    predicted = pts_t @ matrix[:, :2].T + matrix[:, 2]
    residual = float(np.median(np.linalg.norm(predicted[inliers]-pts_s[inliers], axis=1)))
    # Require spatial coverage, so a tiny matching object cannot define a crop.
    spread = np.ptp(pts_t[inliers], axis=0) / [target.shape[1], target.shape[0]]
    if min(spread) < 0.25 or residual > 1.6:
        raise GeometryError('Matched features cover too little of the picture or have inconsistent geometry.')
    return matrix, {'method':'SIFT-RANSAC', 'inliers':int(mask.sum()), 'median_error_px':residual}


def infer_crop(source, target, orientation='AB', *, allow_outside=False, tolerance=.006):
    """Register images and convert rotated crop diagonals to native SDK coordinates.

    CropLeft/Top and CropRight/Bottom are rotated endpoints, not a bounding box.
    https://community.adobe.com/questions-675/sdk-computing-the-corners-of-a-crop-rectangle-962108
    Always verify the proposed crop against a fresh Lightroom render.
    """
    try:
        matrix, report = register(source,target)
    except GeometryError as original_error:
        # SIFT is rotation invariant in principle, but sampled features near
        # image edges can differ. Retry explicit quarter turns when it fails.
        hs,ws = source.shape[:2]
        inverses = [np.array([[0,-1,ws-1],[1,0,0],[0,0,1]]),
                    np.array([[-1,0,ws-1],[0,-1,hs-1],[0,0,1]]),
                    np.array([[0,1,0],[-1,0,hs-1],[0,0,1]])]
        alternatives = []
        for turns, inverse in enumerate(inverses,1):
            try:
                rotated_matrix, info = register(np.ascontiguousarray(np.rot90(source,turns)),target)
            except GeometryError:
                continue
            if rotated_matrix is None:
                rotated = np.rot90(source,turns)
                rotated_matrix = np.array([[rotated.shape[1]/target.shape[1],0,0],
                                           [0,rotated.shape[0]/target.shape[0],0]])
            original_matrix = (inverse @ np.vstack((rotated_matrix,[0,0,1])))[:2]
            alternatives.append((info.get('inliers',0),original_matrix,info))
        if not alternatives:
            raise original_error
        _,matrix,report = max(alternatives,key=lambda item:item[0])
        report['explicit_quarter_turn_search'] = True
    if matrix is None:
        return None, report
    same_aspect = abs(math.log((source.shape[1]/source.shape[0]) / (target.shape[1]/target.shape[0]))) < 0.01
    angle = math.degrees(math.atan2(matrix[1,0], matrix[0,0]))
    report.update(angle_degrees=angle,source_orientation=orientation,
                  source_dimensions=[int(source.shape[1]),int(source.shape[0])],
                  target_to_source=matrix.tolist())
    if abs(angle) > 45:
        raise OrientationChangeRequired(int(round(angle/90)),report)
    ht, wt = target.shape[:2]; hs, ws = source.shape[:2]
    corners = np.array([[0,0], [wt,0], [wt,ht], [0,ht]]) @ matrix[:,:2].T + matrix[:,2]
    normalized_corners = corners / [ws,hs]
    report['display_corners'] = normalized_corners.tolist()
    width = np.linalg.norm(corners[1]-corners[0])
    height = np.linalg.norm(corners[3]-corners[0])
    outside = max(0.0,float(-normalized_corners.min()),float(normalized_corners.max()-1))
    report['outside_fraction'] = outside
    if outside > .02 and not allow_outside:
        raise GeometryError(f'Estimated crop extends {outside:.1%} outside the current Lightroom render. '
                            'Check the starting crop, orientation, and lens/perspective corrections.',report)
    if min(width,height) < 16:
        raise GeometryError('Estimated crop is smaller than 16 pixels in the matching render; geometry is too uncertain.',report)
    if orientation not in ORIENTATION_TURNS:
        raise GeometryError(f'Automatic crop conversion does not support mirrored orientation {orientation!r}. '
                            'Align manually and use --geometry prealigned.',report)
    turns = ORIENTATION_TURNS[orientation]
    # Invert the display orientation, then reorder to native TL, TR, BR, BL.
    native_corners = normalized_corners.copy()
    for _ in range(turns):
        native_corners = np.column_stack((1-native_corners[:,1],native_corners[:,0]))
    native_corners = np.roll(native_corners,turns,axis=0)
    bounds = native_corners[[0,2]].ravel()
    bounds = np.clip(bounds, 0, 1)
    report['bounds'] = bounds.tolist()
    # Verify in the displayed frame; the caller may be checking a cropped render.
    identity_corners = np.array([[0,0],[1,0],[1,1],[0,1]])
    report['corner_error'] = float(np.max(np.abs(normalized_corners-identity_corners)))
    if report['corner_error'] < tolerance and same_aspect and abs(angle) <= .35:
        return None, report
    left, top, right, bottom = bounds.tolist()
    if left >= right or top >= bottom:
        raise GeometryError('Estimated crop diagonal is not representable in Lightroom; align geometry manually.',report)
    return {'HasCrop':True, 'CropLeft':left, 'CropTop':top, 'CropRight':right,
            'CropBottom':bottom, 'CropAngle':angle,
            'CropConstrainToWarp':0}, report


def comparable(candidate, target):
    aspect_error = abs(math.log((candidate.shape[1]/candidate.shape[0]) / (target.shape[1]/target.shape[0])))
    if aspect_error > 0.015:
        raise GeometryError('Lightroom crop aspect does not match target. Set crop manually and rerun.')
    return cv2.resize(candidate, (target.shape[1], target.shape[0]), interpolation=cv2.INTER_AREA)


def color_error(candidate, target):
    """Mean DeltaE76 after modest blur; no outlier deletion or gamut clipping.

    This is an image matching score, not a percentage of recovered edits.
    """
    candidate = comparable(candidate, target)
    a = cv2.GaussianBlur(candidate, (0,0), 1.2)
    b = cv2.GaussianBlur(target, (0,0), 1.2)
    d = np.linalg.norm(a[2:-2,2:-2] - b[2:-2,2:-2], axis=2)
    if not d.size or not np.isfinite(d).all(): raise ValueError('Invalid comparison pixels')
    return float(d.mean())


@dataclass(frozen=True)
class Parameter:
    name: str
    low: float
    high: float
    step: float
    integer: bool = True
    mired: bool = False


def parameters(settings):
    result = []
    # SDK representation differs for raw and rendered originals.
    if float(settings.get('Temperature', 0)) > 1000:
        result += [Parameter('Temperature', 20, 500, 20, False, True), Parameter('Tint',-150,150,12)]
    elif 'IncrementalTemperature' in settings:
        result += [Parameter('IncrementalTemperature',-100,100,12), Parameter('IncrementalTint',-100,100,12)]
    elif 'Temperature' in settings:
        result += [Parameter('Temperature',-100,100,12), Parameter('Tint',-100,100,12)]
    # Each pass uses this order; accepted changes carry into the next control.
    result += [Parameter('Exposure2012', -5, 5, .5, False),
               Parameter('Highlights2012', -100,100,30),
               Parameter('Shadows2012', -100,100,30),
               Parameter('Contrast2012', -100,100,25),
               Parameter('Whites2012', -100,100,20),
               Parameter('Blacks2012', -100,100,20)]
    return result


def coordinate_search(base, evaluate, max_evals=120, passes=5):
    """Greedy multiscale coordinate search, always retaining the best render.

    evaluate(settings) must return a real-render score. It is called at most
    max_evals times. Full baseline settings are supplied for every candidate.
    """
    current = copy.deepcopy(base)
    best = float(evaluate(current)); count = 1
    history = [best]
    params = parameters(base)
    for iteration in range(passes):
        scale = 0.5 ** iteration
        for p in params:
            start = current.get(p.name, 0)
            if p.mired: start = 1e6 / max(2000.0, float(start))
            local_best, winning = best, None
            for sign in [1,-1]:
                if count >= max_evals: return current, best, count, history
                value = np.clip(start + sign*p.step*scale, p.low, p.high)
                if p.mired: value = round(1e6 / value)
                elif p.integer: value = round(float(value))
                else: value = round(float(value), 4)
                if value == current.get(p.name, 0): continue
                trial = copy.deepcopy(current); trial[p.name] = value
                if p.name in ('Temperature','Tint','IncrementalTemperature','IncrementalTint'):
                    trial['WhiteBalance'] = 'Custom'
                loss = float(evaluate(trial)); count += 1; history.append(loss)
                if loss < local_best - 1e-5: local_best, winning = loss, trial
            if winning is not None: current, best = winning, local_best
    return current, best, count, history
