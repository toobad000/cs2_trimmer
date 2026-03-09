// ── State ──
let uploadedFiles = []; // {original, saved, path, file}
let pendingFiles = [];  // File objects waiting to be uploaded
let lastLog = '';

// Check ffmpeg status on load
(async () => {
    try {
        const r = await fetch('/ffmpeg_status');
        const d = await r.json();
        if (!d.ok) {
            document.getElementById('ffmpegWarn').style.display = 'flex';
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
    // Slider event listeners
    document.getElementById('nBefore').addEventListener('input', () => updateSlider('before'));
    document.getElementById('nAfter').addEventListener('input', () => updateSlider('after'));

    // Fullspan checkbox
    document.getElementById('fullspan').addEventListener('change', function () {
        const grid = document.querySelector('.slider-grid');
        if (this.checked) {
            grid.classList.add('sliders-dimmed');
        } else {
            grid.classList.remove('sliders-dimmed');
        }
    });

    // Drop zone events
    const dropZone = document.getElementById('dropZone');
    dropZone.addEventListener('dragover', handleDragOver);
    dropZone.addEventListener('dragleave', handleDragLeave);
    dropZone.addEventListener('drop', handleDrop);
    dropZone.addEventListener('click', () => document.getElementById('fileInput').click());

    // File input
    document.getElementById('fileInput').addEventListener('change', handleFileSelect);

    // Trim button
    document.getElementById('trimBtn').addEventListener('click', startTrim);
});

// ── Upload & Trim ──
async function startTrim() {
    const nBefore = parseFloat(document.getElementById('nBefore').value);
    const nAfter = parseFloat(document.getElementById('nAfter').value);
    const fullSpan = document.getElementById('fullspan').checked;
    const stretchToFill = document.getElementById('stretchToFill').checked;

    if (pendingFiles.length === 0) return;

    // Lock UI
    document.getElementById('trimBtn').disabled = true;
    const prog = document.getElementById('progressPanel');
    prog.classList.add('active');
    document.getElementById('resultsPanel').classList.remove('active');
    document.getElementById('resultsList').innerHTML = '';
    clearLog();

    addLog('Uploading ' + pendingFiles.length + ' file(s)...', '');

    // Upload files
    const formData = new FormData();
    pendingFiles.forEach(f => formData.append('files', f));

    let uploaded;
    try {
        const res = await fetch('/upload', { method: 'POST', body: formData });
        const data = await res.json();
        uploaded = data.uploaded;
        addLog(`✓ Uploaded ${uploaded.length} file(s)`, 'success');
    } catch (e) {
        addLog('✗ Upload failed: ' + e.message, 'error');
        document.getElementById('trimBtn').disabled = false;
        return;
    }

    // Start trim job
    const savedNames = uploaded.map(u => u.saved);
    addLog('Starting kill detection...', '');
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

    // Poll status
    pollJob(jobId, savedNames.length);
}

async function pollJob(jobId, total) {
    const interval = setInterval(async () => {
        try {
            const res = await fetch('/status/' + jobId);
            const data = await res.json();

            // Update progress bar
            const pct = total > 0 ? (data.progress / total) * 100 : 0;
            document.getElementById('progressBar').style.width = pct + '%';

            // Log new messages
            if (data.log && data.log !== lastLog) {
                addLog(data.log, data.log.startsWith('✓') || data.log.startsWith('Found') ? 'success' : '');
                lastLog = data.log;
            }

            if (data.status === 'done') {
                clearInterval(interval);
                document.getElementById('progressBar').style.width = '100%';
                document.getElementById('statusChip').textContent = 'COMPLETE';
                document.getElementById('statusChip').className = 'chip chip-done';
                showResults(data.results, data.errors);
                document.getElementById('trimBtn').disabled = false;

                // Clear the file list after processing completes
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
              <strong>${r.kills_found}</strong> kill(s) · <strong>${r.segments}</strong> clip(s) · <strong>${r.output_duration}s</strong> total
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