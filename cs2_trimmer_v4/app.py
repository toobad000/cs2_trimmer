import os
import sys
import time
import threading
import uuid
import logging
import atexit
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import processor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# Production Configuration
# ============================================================================
DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
SECRET_KEY = os.environ.get('SECRET_KEY', os.urandom(24).hex())
HOST = '0.0.0.0' if not DEBUG else '127.0.0.1'
PORT = int(os.environ.get('PORT', 5000))

# File storage configuration - 750MB limit
MAX_FILE_SIZE = int(os.environ.get('MAX_FILE_SIZE', 750 * 1024 * 1024))  # 750MB default
MAX_TOTAL_SIZE = int(os.environ.get('MAX_TOTAL_SIZE', 1500 * 1024 * 1024))  # 1.5GB total
FILE_RETENTION_HOURS = int(os.environ.get('FILE_RETENTION_HOURS', 24))  # Auto-delete after 24h
CLEANUP_INTERVAL = int(os.environ.get('CLEANUP_INTERVAL', 3600))  # Run cleanup every hour

# Rate limiting - Updated to be more generous
RATE_LIMIT_DEFAULT = os.environ.get('RATE_LIMIT_DEFAULT', "1000 per day, 200 per hour")
RATE_LIMIT_UPLOAD = os.environ.get('RATE_LIMIT_UPLOAD', "20 per minute")
RATE_LIMIT_TRIM = os.environ.get('RATE_LIMIT_TRIM', "10 per minute")

# ============================================================================
# Initialize Flask App
# ============================================================================
app = Flask(__name__)
app.config['DEBUG'] = DEBUG
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_TOTAL_SIZE  # 1.5GB total request limit

# Use /tmp for cloud hosting (ephemeral storage), otherwise local folders
if os.environ.get('CLOUD_HOSTING', 'False').lower() == 'true':
    app.config['UPLOAD_FOLDER'] = '/tmp/killcut/uploads'
    app.config['OUTPUT_FOLDER'] = '/tmp/killcut/output'
else:
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
    app.config['OUTPUT_FOLDER'] = os.path.join(os.path.dirname(__file__), 'output')

# Create directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# ============================================================================
# Rate Limiter - UPDATED with status endpoint exemption
# ============================================================================
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri="memory://",
)

# ============================================================================
# Security Headers
# ============================================================================
@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

# ============================================================================
# File Cleanup System
# ============================================================================
def cleanup_old_files():
    """Delete files older than FILE_RETENTION_HOURS"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        deleted_count = 0
        for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
            folder_path = Path(folder)
            if not folder_path.exists():
                continue
            for file_path in folder_path.glob('*'):
                if file_path.is_file():
                    if now - file_path.stat().st_mtime > FILE_RETENTION_HOURS * 3600:
                        try:
                            file_path.unlink()
                            deleted_count += 1
                            logger.info(f"Cleaned up old file: {file_path.name}")
                        except Exception as e:
                            logger.error(f"Failed to delete {file_path.name}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Cleanup completed: {deleted_count} files removed")

# Start cleanup thread
if not DEBUG:
    cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
    cleanup_thread.start()
    logger.info("File cleanup thread started")

def cleanup_on_exit():
    """Optional: delete ALL files on shutdown"""
    if os.environ.get('CLEANUP_ON_EXIT', 'False').lower() == 'true':
        logger.info("Performing exit cleanup...")
        for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER']]:
            folder_path = Path(folder)
            if folder_path.exists():
                for file_path in folder_path.glob('*'):
                    try:
                        file_path.unlink()
                    except Exception as e:
                        logger.error(f"Failed to delete {file_path.name} on exit: {e}")

atexit.register(cleanup_on_exit)

# ============================================================================
# ffmpeg Check
# ============================================================================
_ffmpeg_ok, _ffmpeg_msg = processor.check_ffmpeg()
if _ffmpeg_ok:
    logger.info(f"ffmpeg OK: {_ffmpeg_msg}")
    logger.info(f"ffmpeg path : {processor.FFMPEG_BIN}")
    logger.info(f"ffprobe path: {processor.FFPROBE_BIN}")
else:
    logger.error(f"ffmpeg NOT FOUND: {_ffmpeg_msg}")
    logger.error("Video trimming will fail until ffmpeg is installed.")

# Check Tesseract OCR
_tesseract_ok, _tesseract_msg = processor.check_tesseract()
if _tesseract_ok:
    logger.info(f"Tesseract OK: {_tesseract_msg}")
else:
    logger.error(f"Tesseract NOT FOUND: {_tesseract_msg}")
    logger.error("Username detection will fail. Install Tesseract OCR:")
    logger.error("  - Windows: https://github.com/UB-Mannheim/tesseract/wiki")
    logger.error("  - Linux: sudo apt-get install tesseract-ocr")
    logger.error("  - macOS: brew install tesseract")

logger.info(f"Upload folder: {app.config['UPLOAD_FOLDER']}")
logger.info(f"Output folder: {app.config['OUTPUT_FOLDER']}")
logger.info(f"Production mode: {not DEBUG}")
logger.info(f"Max file size: {MAX_FILE_SIZE / (1024*1024):.0f}MB")
logger.info(f"Max total upload: {MAX_TOTAL_SIZE / (1024*1024):.0f}MB")
logger.info(f"File retention: {FILE_RETENTION_HOURS} hours")

# ============================================================================
# Job Tracking
# ============================================================================
jobs: dict[str, dict] = {}
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ============================================================================
# Routes
# ============================================================================
@app.route('/')
def index():
    ffmpeg_status = {
        'ok': _ffmpeg_ok,
        'msg': _ffmpeg_msg,
        'bin': processor.FFMPEG_BIN,
    }
    tesseract_status = {
        'ok': _tesseract_ok,
        'msg': _tesseract_msg,
    }
    return render_template('index.html', ffmpeg_status=ffmpeg_status, tesseract_status=tesseract_status)

@app.route('/ffmpeg_status')
def ffmpeg_status():
    return jsonify({
        'ok': _ffmpeg_ok,
        'msg': _ffmpeg_msg,
        'bin': processor.FFMPEG_BIN,
    })

@app.route('/tesseract_status')
def tesseract_status():
    return jsonify({
        'ok': _tesseract_ok,
        'msg': _tesseract_msg,
    })

@app.route('/upload', methods=['POST'])
@limiter.limit(RATE_LIMIT_UPLOAD)
def upload():
    if 'files' not in request.files:
        return jsonify({'error': 'No files in request'}), 400

    files = request.files.getlist('files')
    saved = []
    errors = []
    
    # Check total upload size
    total_size = 0
    for f in files:
        if f and f.filename:
            f.seek(0, os.SEEK_END)
            total_size += f.tell()
            f.seek(0)
    
    if total_size > MAX_TOTAL_SIZE:
        max_total_mb = MAX_TOTAL_SIZE / (1024 * 1024)
        return jsonify({
            'error': f'Total upload size ({total_size/(1024*1024):.1f}MB) exceeds maximum allowed ({max_total_mb:.0f}MB). Please upload fewer or smaller files.'
        }), 400

    for f in files:
        if not f or not f.filename:
            continue
            
        # Check individual file size
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        f.seek(0)
        
        max_file_mb = MAX_FILE_SIZE / (1024 * 1024)
        if file_size > MAX_FILE_SIZE:
            errors.append({
                'file': f.filename,
                'error': f'File size ({file_size/(1024*1024):.1f}MB) exceeds maximum allowed ({max_file_mb:.0f}MB)'
            })
            continue
            
        if not allowed_file(f.filename):
            errors.append({
                'file': f.filename,
                'error': f'Unsupported file format. Allowed formats: {", ".join(ALLOWED_EXTENSIONS)}'
            })
            continue
            
        filename = secure_filename(f.filename)
        base, ext = os.path.splitext(filename)
        unique = f"{base}_{uuid.uuid4().hex[:8]}{ext}"
        path = os.path.join(app.config['UPLOAD_FOLDER'], unique)
        
        try:
            f.save(path)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            logger.info(f"Uploaded: {unique} ({size_mb:.1f} MB)")
            saved.append({'original': f.filename, 'saved': unique})
        except Exception as e:
            errors.append({
                'file': f.filename,
                'error': f'Failed to save file: {str(e)}'
            })

    return jsonify({'uploaded': saved, 'errors': errors})

@app.route('/trim', methods=['POST'])
@limiter.limit(RATE_LIMIT_TRIM)
def trim():
    if not _ffmpeg_ok:
        return jsonify({
            'error': (
                f"ffmpeg is not installed or not in PATH. "
                f"Tried: {processor.FFMPEG_BIN}. "
                "Please contact the administrator."
            )
        }), 500
    
    if not _tesseract_ok:
        return jsonify({
            'error': (
                f"Tesseract OCR is not installed. "
                "Username detection requires Tesseract. "
                "Please install Tesseract OCR and restart the app."
            )
        }), 500

    data = request.json or {}
    n_before = float(data.get('n_before', 2.0))
    n_after = float(data.get('n_after', 2.0))
    full_span = bool(data.get('full_span', False))
    stretch_to_fill = bool(data.get('stretch_to_fill', False))
    username = data.get('username', '').strip()
    files = data.get('files', [])

    if not files:
        return jsonify({'error': 'No files to process'}), 400
    
    if not username:
        return jsonify({'error': 'Username is required'}), 400
    
    if len(username) < 2 or len(username) > 20:
        return jsonify({'error': 'Username must be between 2 and 20 characters'}), 400

    # Verify files exist
    for filename in files:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(file_path):
            return jsonify({'error': f'File not found: {filename}'}), 404

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        'status': 'queued',
        'progress': 0,
        'total': len(files),
        'log': '',
        'results': [],
        'errors': [],
        'created_at': time.time(),
    }
    
    logger.info(
        f"Job {job_id[:8]}: {len(files)} file(s), username='{username}', "
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
                    stretch_to_fill=stretch_to_fill,
                    username=username,
                    progress_callback=cb,
                )
                jobs[job_id]['results'].append(result)

                if os.environ.get('DELETE_AFTER_PROCESSING', 'False').lower() == 'true':
                    try:
                        os.remove(input_path)
                        logger.info(f"Deleted input file: {filename}")
                    except Exception as e:
                        logger.error(f"Failed to delete input file {filename}: {e}")

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

# ============================================================================
# Status endpoint - EXEMPT from rate limiting
# ============================================================================
@limiter.exempt
@app.route('/status/<job_id>')
def status(job_id: str):
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    
    job_data = jobs[job_id].copy()
    if 'created_at' in job_data:
        job_data['age'] = int(time.time() - job_data['created_at'])
    
    return jsonify(job_data)

@app.route('/download/<path:filename>')
def download(filename: str):
    try:
        response = send_from_directory(
            app.config['OUTPUT_FOLDER'], filename, as_attachment=True
        )
        
        if os.environ.get('DELETE_AFTER_DOWNLOAD', 'True').lower() == 'true':
            @response.call_on_close
            def delete_file():
                file_path = os.path.join(app.config['OUTPUT_FOLDER'], filename)
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Deleted output file after download: {filename}")
                except Exception as e:
                    logger.error(f"Failed to delete {filename} after download: {e}")
        
        return response
    except Exception as e:
        logger.error(f"Download failed for {filename}: {e}")
        return jsonify({'error': 'File not found'}), 404

# Health check endpoint for cloud platforms
@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'ffmpeg': _ffmpeg_ok,
        'tesseract': _tesseract_ok,
        'timestamp': time.time()
    })

# Clean up old jobs periodically
def cleanup_old_jobs():
    """Remove job data older than 1 hour"""
    while True:
        time.sleep(3600)
        now = time.time()
        old_jobs = [jid for jid, jdata in jobs.items() 
                   if now - jdata.get('created_at', 0) > 3600]
        for jid in old_jobs:
            jobs.pop(jid, None)
        if old_jobs:
            logger.info(f"Cleaned up {len(old_jobs)} old jobs")

if not DEBUG:
    job_cleanup_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
    job_cleanup_thread.start()

# ============================================================================
# Main Entry Point
# ============================================================================
if __name__ == '__main__':
    if DEBUG:
        app.run(debug=True, host='127.0.0.1', port=PORT)
    else:
        app.run(debug=False, host='0.0.0.0', port=PORT)