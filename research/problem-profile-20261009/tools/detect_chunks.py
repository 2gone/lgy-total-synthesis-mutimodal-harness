"""The molecule detector (detect_molecules.py) over any number of images: run in chunks, so that the command line of one run stays under
the length Windows allows (a joined experimental document can have several hundred pages to box), and the outputs joined into one."""
import json, os, subprocess, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
CHUNK = 100


def detect(images, out, conf, iou, imgsz):
    """detect_molecules.py over images (paths) into out (one JSON like its own: the first run's settings, every image); the return code
    of the first run that failed, else 0."""
    joined = None
    for start in range(0, len(images), CHUNK):
        part = Path(tempfile.mkdtemp()) / 'part.json'
        done = subprocess.run([os.environ['TSDAF_DETECTOR_PYTHON'], str(HERE / 'tools/detect_molecules.py'), '--weights', os.environ['TSDAF_DETECTOR_WEIGHTS'],
                               '--images', *[str(p) for p in images[start:start + CHUNK]], '--out', str(part), '--imgsz', str(imgsz), '--conf', str(conf), '--iou', str(iou),
                               '--device', os.environ.get('TSDAF_DETECTOR_DEVICE', 'cpu')], capture_output=True, text=True, encoding='utf-8', errors='replace', env={**os.environ, 'KMP_DUPLICATE_LIB_OK': 'TRUE'})
        if done.returncode != 0 or not part.exists(): return done.returncode or 1, (done.stderr or '')[-300:]
        found = json.loads(part.read_text(encoding='utf-8'))
        if joined is None: joined = found
        else: joined['images'] += found['images']
    Path(out).write_text(json.dumps(joined or {'images': []}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0, ''
