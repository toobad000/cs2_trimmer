// ── State ──
let uploadedFiles = [];
let pendingFiles = [];
let lastLog = '';

// Check ffmpeg and tesseract status on load
(async () => {
    try {
        const ffmpegRes = await fetch('/ffmpeg_status');
        const ffmpegData = await ffmpegRes.json();
        if (!ffmpegData.ok) {
            document.getElementById('ffmpegWarn').style.display = 'flex';
        }

        const tesseractRes = await fetch('/tesseract_status');
        const tesseractData = await tesseractRes.json();
        if (!tesseractData.ok) {
            document.getElementById('tesseractWarn').style.display = 'flex';
        }
    } catch (e) { /* ignore */ }
})();

// ── Slider ──
function updateSlider(which) {
    if (which === 'before') {
        const v = parseFloat(document.getElementById('nBefore').value).toFixed(1);
        document.getElementById('beforeVal').innerHTML = v + '<span>s</span>';
    } else {
        const v = parseFloat(document.getElementById('nAfter').value).toFixed(1);
        document.getElementById('afterVal').innerHTML = v + '<span>s</span>';
    }
}

// ── Drag & Drop ──
function handleDragOver(e) {
    e.preventDefault();
    document.getElementById('dropZone').classList.add('dragover');
}

function handleDragLeave() {
    document.getElementById('dropZone').classList.remove('dragover');
}

function handleDrop(e) {
    e.preventDefault();
    document.getElementById('dropZone').classList.remove('dragover');
    addFiles(Array.from(e.dataTransfer.files));
}

function handleFileSelect(e) {
    addFiles(Array.from(e.target.files));
    e.target.value = '';
}

function addFiles(files) {
    const allowed = ['video/mp4', 'video/quicktime', 'video/x-msvideo', 'video/x-matroska', 'video/mkv'];
    const ext = ['.mp4', '.mov', '.avi', '.mkv'];
    files.forEach(f => {
        const isAllowed = allowed.includes(f.type) || ext.some(e => f.name.toLowerCase().endsWith(e));
        if (isAllowed && !pendingFiles.find(p => p.name === f.name && p.size === f.size)) {
            pendingFiles.push(f);
        }
    });
    renderFileList();
    updateTrimBtn();
}

function removeFile(idx) {
    pendingFiles.splice(idx, 1);
    renderFileList();
    updateTrimBtn();
}

function clearFileList() {
    pendingFiles = [];
    renderFileList();
    updateTrimBtn();
}

function fmtSize(bytes) {
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function renderFileList() {
    const list = document.getElementById('fileList');
    list.innerHTML = pendingFiles.map((f, i) => `
    <div class="file-item">
      <span class="file-icon">▶</span>
      <span class="file-name">${f.name}</span>
      <span class="file-size">${fmtSize(f.size)}</span>
      <button class="file-remove" onclick="removeFile(${i})" title="Remove">✕</button>
    </div>
  `).join('');
}

function updateTrimBtn() {
    const hasFiles = pendingFiles.length > 0;
    document.getElementById('trimBtn').disabled = !hasFiles;
}

// ── Event Listeners ──
document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('nBefore').addEventListener('input', () => updateSlider('before'));
    document.getElementById('nAfter').addEventListener('input', () => updateSlider('after'));

    document.getElementById('fullspan').addEventListener('change', function () {
        const grid = document.querySelector('.slider-grid');
        if (this.checked) {
            grid.classList.add('sliders-dimmed');
        } else {
            grid.classList.remove('sliders-dimmed');
        }
    });

    const dropZone = document.getElementById('dropZone');
    dropZone.addEventListener('dragover', handleDragOver);
    dropZone.addEventListener('dragleave', handleDragLeave);
    dropZone.addEventListener('drop', handleDrop);
    dropZone.addEventListener('click', () => document.getElementById('fileInput').click());

    document.getElementById('fileInput').addEventListener('change', handleFileSelect);
    document.getElementById('trimBtn').addEventListener('click', startTrim);
});

// ── Upload & Trim ──
async function startTrim() {
    const nBefore = parseFloat(document.getElementById('nBefore').value);
    const nAfter = parseFloat(document.getElementById('nAfter').value);
    const fullSpan = document.getElementById('fullspan').checked;
    const stretchToFill = document.getElementById('stretchToFill').checked;
    const username = document.getElementById('username').value.trim();

    if (!username) {
        alert('Please enter your CS2 username');
        return;
    }

    if (username.length < 2 || username.length > 20) {
        alert('Username must be between 2 and 20 characters');
        return;
    }

    if (pendingFiles.length === 0) return;

    document.getElementById('trimBtn').disabled = true;
    const prog = document.getElementById('progressPanel');
    prog.classList.add('active');
    document.getElementById('resultsPanel').classList.remove('active');
    document.getElementById('resultsList').innerHTML = '';
    clearLog();

    addLog(`Processing for username: ${username}`, 'success');
    addLog('Uploading ' + pendingFiles.length + ' file(s)...', '');

    const formData = new FormData();
    pendingFiles.forEach(f => formData.append('files', f));

    let uploaded;
    let uploadErrors = [];
    try {
        const res = await fetch('/upload', { method: 'POST', body: formData });
        const data = await res.json();

        // Handle case where response is an error
        if (data.error) {
            addLog(`✗ ${data.error}`, 'error');
            document.getElementById('trimBtn').disabled = false;
            return;
        }

        uploaded = data.uploaded || [];
        uploadErrors = data.errors || [];

        if (uploadErrors.length > 0) {
            uploadErrors.forEach(err => {
                if (typeof err === 'string') {
                    addLog(`✗ ${err}`, 'error');
                } else if (err.file && err.error) {
                    addLog(`✗ ${err.file}: ${err.error}`, 'error');
                } else {
                    addLog(`✗ Upload error: ${JSON.stringify(err)}`, 'error');
                }
            });
        }

        if (uploaded.length > 0) {
            addLog(`✓ Uploaded ${uploaded.length} file(s)`, 'success');
        } else {
            addLog('✗ No files were uploaded successfully', 'error');
            document.getElementById('trimBtn').disabled = false;
            return;
        }
    } catch (e) {
        addLog('✗ Upload failed: ' + e.message, 'error');
        document.getElementById('trimBtn').disabled = false;
        return;
    }

    const savedNames = uploaded.map(u => u.saved);
    addLog('Starting kill detection with OCR...', '');
    if (stretchToFill) {
        addLog('  Stretch to Fill: enabled', '');
    }

    let jobId;
    try {
        const res = await fetch('/trim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                n_before: nBefore,
                n_after: nAfter,
                full_span: fullSpan,
                stretch_to_fill: stretchToFill,
                username: username,
                files: savedNames
            })
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        jobId = data.job_id;
    } catch (e) {
        addLog('✗ Failed to start job: ' + e.message, 'error');
        document.getElementById('trimBtn').disabled = false;
        return;
    }

    pollJob(jobId, savedNames.length);
}

async function pollJob(jobId, total) {
    const interval = setInterval(async () => {
        try {
            const res = await fetch('/status/' + jobId);
            const data = await res.json();

            const pct = total > 0 ? (data.progress / total) * 100 : 0;
            document.getElementById('progressBar').style.width = pct + '%';

            if (data.log && data.log !== lastLog) {
                let logClass = '';
                if (data.log.includes('[KILL]')) {
                    logClass = 'success';
                } else if (data.log.includes('ERROR') || data.log.includes('✗')) {
                    logClass = 'error';
                } else if (data.log.includes('✓') || data.log.includes('Found') || data.log.includes('complete')) {
                    logClass = 'success';
                }
                addLog(data.log, logClass);
                lastLog = data.log;
            }

            if (data.status === 'done') {
                clearInterval(interval);
                document.getElementById('progressBar').style.width = '100%';
                document.getElementById('statusChip').textContent = 'COMPLETE';
                document.getElementById('statusChip').className = 'chip chip-done';
                showResults(data.results, data.errors);
                document.getElementById('trimBtn').disabled = false;
                clearFileList();
                addLog('✓ Queue cleared — ready for new files', 'success');
            }
        } catch (e) {
            // ignore transient errors
        }
    }, 1200);
}

function showResults(results, errors) {
    const panel = document.getElementById('resultsPanel');
    const list = document.getElementById('resultsList');
    panel.classList.add('active');

    let html = '';

    (results || []).forEach(r => {
        if (r.output) {
            html += `
        <div class="result-item">
          <div class="result-info">
            <div class="result-filename">${r.input}</div>
            <div class="result-meta">
              <strong>${r.kills_found}</strong> kill(s) for <strong>${r.username || 'you'}</strong> · 
              <strong>${r.segments}</strong> clip(s) · <strong>${r.output_duration}s</strong> total
            </div>
          </div>
          <a href="/download/${r.output}" class="dl-btn">↓ Download</a>
        </div>`;
        } else {
            html += `
        <div class="result-item no-kills">
          <div class="result-info">
            <div class="result-filename">${r.input}</div>
            <div class="result-meta">${r.message}</div>
          </div>
        </div>`;
        }
    });

    (errors || []).forEach(e => {
        html += `
      <div class="result-item error">
        <div class="result-info">
          <div class="result-filename">${e.file}</div>
          <div class="result-meta">${e.error}</div>
        </div>
      </div>`;
    });

    list.innerHTML = html || '<div style="color:var(--text3);font-size:0.85rem">No results.</div>';
}

function addLog(msg, cls) {
    const log = document.getElementById('progressLog');
    const line = document.createElement('span');
    line.className = 'log-line' + (cls ? ' ' + cls : '');
    line.textContent = '> ' + msg;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
}

function clearLog() {
    document.getElementById('progressLog').innerHTML = '';
    lastLog = '';
    document.getElementById('progressBar').style.width = '0%';
    document.getElementById('statusChip').textContent = 'SCANNING';
    document.getElementById('statusChip').className = 'chip chip-processing';
}

document.getElementById('username').addEventListener('keypress', function (e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        if (pendingFiles.length > 0 && document.getElementById('username').value.trim()) {
            startTrim();
        }
    }
});