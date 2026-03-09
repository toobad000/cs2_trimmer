import os
import threading
import uuid
import logging
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import processor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(os.path.dirname(__file__), 'output')
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024  # 4 GB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Check ffmpeg at startup so users get an immediate clear message
_ffmpeg_ok, _ffmpeg_msg = processor.check_ffmpeg()
if _ffmpeg_ok:
    logger.info(f"ffmpeg OK: {_ffmpeg_msg}")
    logger.info(f"ffmpeg path : {processor.FFMPEG_BIN}")
    logger.info(f"ffprobe path: {processor.FFPROBE_BIN}")
else:
    logger.error(f"ffmpeg NOT FOUND: {_ffmpeg_msg}")
    logger.error("Video trimming will fail until ffmpeg is installed.")
    logger.error("Download: https://ffmpeg.org/download.html")

logger.info(f"Upload folder: {app.config['UPLOAD_FOLDER']}")
logger.info(f"Output folder: {app.config['OUTPUT_FOLDER']}")

jobs: dict[str, dict] = {}
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def index():
    ffmpeg_status = {
        'ok': _ffmpeg_ok,
        'msg': _ffmpeg_msg,
        'bin': processor.FFMPEG_BIN,
    }
    return render_template('index.html', ffmpeg_status=ffmpeg_status)


@app.route('/ffmpeg_status')
def ffmpeg_status():
    return jsonify({
        'ok':  _ffmpeg_ok,
        'msg': _ffmpeg_msg,
        'bin': processor.FFMPEG_BIN,
    })


@app.route('/upload', methods=['POST'])
def upload():
    if 'files' not in request.files:
        return jsonify({'error': 'No files in request'}), 400

    files  = request.files.getlist('files')
    saved  = []
    errors = []

    for f in files:
        if not f or not f.filename:
            continue
        if not allowed_file(f.filename):
            errors.append(f"Skipped {f.filename}: unsupported format")
            continue
        filename = secure_filename(f.filename)
        base, ext = os.path.splitext(filename)
        unique = f"{base}_{uuid.uuid4().hex[:6]}{ext}"
        path = os.path.join(app.config['UPLOAD_FOLDER'], unique)
        f.save(path)
        size_mb = os.path.getsize(path) / 1e6
        logger.info(f"Uploaded: {unique} ({size_mb:.1f} MB)")
        saved.append({'original': f.filename, 'saved': unique})

    return jsonify({'uploaded': saved, 'errors': errors})


@app.route('/trim', methods=['POST'])
def trim():
    if not _ffmpeg_ok:
        return jsonify({
            'error': (
                f"ffmpeg is not installed or not in PATH. "
                f"Tried: {processor.FFMPEG_BIN}. "
                "Download from https://ffmpeg.org/download.html and restart the app."
            )
        }), 500

    data      = request.json or {}
    n_before  = float(data.get('n_before', 2.0))  # Changed default to 2.0
    n_after   = float(data.get('n_after',  2.0))
    full_span = bool(data.get('full_span', False))
    stretch_to_fill = bool(data.get('stretch_to_fill', False))  # New
    files     = data.get('files', [])

    if not files:
        return jsonify({'error': 'No files to process'}), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        'status':   'queued',
        'progress': 0,
        'total':    len(files),
        'log':      '',
        'results':  [],
        'errors':   [],
    }
    logger.info(
        f"Job {job_id[:8]}: {len(files)} file(s), "
        f"n_before={n_before} n_after={n_after} full_span={full_span} stretch={stretch_to_fill}"
    )

    def run():
        for i, filename in enumerate(files):
            jobs[job_id]['status'] = 'processing'
            input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            output_dir = app.config['OUTPUT_FOLDER']

            def cb(msg: str):
                logger.info(f"  {msg}")
                jobs[job_id]['log'] = msg

            try:
                result = processor.process_video(
                    input_path=input_path,
                    output_dir=output_dir,
                    n_before=n_before,
                    n_after=n_after,
                    full_span=full_span,
                    stretch_to_fill=stretch_to_fill,  # New
                    progress_callback=cb,
                )
                jobs[job_id]['results'].append(result)

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"Exception on {filename}:\n{tb}")
                jobs[job_id]['errors'].append({'file': filename, 'error': str(e)})
                jobs[job_id]['log'] = f"ERROR: {e}"

            jobs[job_id]['progress'] = i + 1

        jobs[job_id]['status'] = 'done'
        logger.info(f"Job {job_id[:8]} complete.")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id: str):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(jobs[job_id])


@app.route('/download/<path:filename>')
def download(filename: str):
    return send_from_directory(
        app.config['OUTPUT_FOLDER'], filename, as_attachment=True
    )


if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)