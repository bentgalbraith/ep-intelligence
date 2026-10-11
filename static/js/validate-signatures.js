function initValidateSignatures(options) {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('pdf-input');
    const fileList = document.getElementById('file-list');
    let selectedFiles = [];
    let activeFilter = 'all';

    function isPdf(file) {
        if (!file) return false;
        return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
    }

    function fileKey(file) {
        return [file.name, file.size, file.lastModified].join('\0');
    }

    fileInput.addEventListener('click', (e) => e.stopPropagation());
    dropZone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) addFiles(fileInput.files);
        fileInput.value = '';
    });
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
    });

    function addFiles(files) {
        const existing = new Set(selectedFiles.map(fileKey));
        for (const file of files) {
            if (!isPdf(file) || existing.has(fileKey(file))) continue;
            existing.add(fileKey(file));
            selectedFiles.push(file);
        }
        renderFileList();
    }

    function renderFileList() {
        tool.btn.disabled = !selectedFiles.length;
        if (!selectedFiles.length) {
            fileList.replaceChildren();
            return;
        }
        fileList.replaceChildren();
        selectedFiles.forEach((file, i) => {
            const row = el('div', 'file-list-item');
            row.appendChild(el('span', 'file-list-name', file.name));
            const remove = el('button', 'file-list-remove');
            remove.type = 'button';
            remove.title = 'Remove';
            remove.dataset.idx = String(i);
            remove.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
            row.appendChild(remove);
            fileList.appendChild(row);
        });
    }

    fileList.addEventListener('click', (e) => {
        const btn = e.target.closest('.file-list-remove');
        if (!btn) return;
        selectedFiles.splice(parseInt(btn.dataset.idx, 10), 1);
        renderFileList();
    });

    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

    const tool = initToolForm({
        formId: 'sig-form',
        btnId: 'btn-validate',
        async onSubmit({ setLoading, showResult, showError, btnLabel, hasRun }) {
            if (!selectedFiles.length) return;
            const state = setLoading(true);
            state.btnLabel.textContent = 'Checking...';
            try {
                const body = new FormData();
                for (const file of selectedFiles) body.append('pdfs', file);
                const uploadRes = await fetchWithTimeout(options.submitUrl, { method: 'POST', body });
                const uploadData = await uploadRes.json();
                if (!uploadRes.ok) throw new Error(uploadData.error || 'Something went wrong');
                const result = await pollJob(uploadData.job_id, state.btnLabel);
                showResult('<div class="sig-report" id="sig-report"></div>');
                renderReport(document.getElementById('sig-report'), result, uploadData.job_id);
            } catch (err) {
                showError(err.message);
            } finally {
                const next = setLoading(false);
                tool.btn.disabled = !selectedFiles.length;
                next.btnLabel.textContent = hasRun() ? 'Re-Validate' : 'Validate';
            }
        },
    });

    async function pollJob(jobId, btnLabel) {
        const url = options.statusUrl.replace('__JOB__', jobId);
        while (true) {
            await sleep(3000);
            const res = await fetchWithTimeout(url);
            const data = await res.json();
            if (data.status === 'processing') {
                if (data.progress) btnLabel.textContent = data.progress;
                continue;
            }
            if (!res.ok || data.status === 'error') throw new Error(data.error || 'Something went wrong');
            return data;
        }
    }

    function renderReport(root, result, jobId) {
        activeFilter = 'all';
        const items = Array.isArray(result.items) ? result.items : [];
        root.appendChild(el('p', 'sig-stats', statsLine(result.pages || 0, items)));
        if (!items.length) {
            root.appendChild(el('p', 'sig-empty', 'No signature, initial, or notary lines found.'));
            return;
        }
        const filters = el('div', 'sig-filters');
        [['all', 'All'], ['blank', 'Blanks'], ['ambiguous', 'Unclear'], ['completed', 'Completed']].forEach(([value, label]) => {
            const button = el('button', 'sig-filter' + (value === 'all' ? ' is-active' : ''), label);
            button.type = 'button';
            button.dataset.filter = value;
            filters.appendChild(button);
        });
        const list = el('div', 'sig-list');
        items.forEach((item) => list.appendChild(renderRow(item, jobId)));
        filters.addEventListener('click', (e) => {
            const button = e.target.closest('.sig-filter');
            if (!button) return;
            activeFilter = button.dataset.filter;
            filters.querySelectorAll('.sig-filter').forEach((node) => {
                node.classList.toggle('is-active', node === button);
            });
            applyFilter(list);
        });
        root.appendChild(filters);
        root.appendChild(list);
    }

    function renderRow(item, jobId) {
        const row = el('article', 'sig-row');
        row.dataset.status = item.status || '';
        const button = el('button', 'sig-crop-btn');
        button.type = 'button';
        const img = document.createElement('img');
        img.className = 'sig-crop';
        img.alt = rowLabel(item);
        img.src = options.cropUrl.replace('__JOB__', jobId).replace('__ITEM__', item.id);
        button.appendChild(img);
        button.addEventListener('click', () => openLightbox(img.src, img.alt));
        row.appendChild(button);

        const info = el('div', 'sig-info');
        const title = el('p', 'sig-title');
        title.appendChild(el('span', null, typeLabel(item.type)));
        title.appendChild(el('span', 'sig-dot', ' · '));
        const status = el('span', statusClass(item.status), statusLabel(item.status));
        title.appendChild(status);
        info.appendChild(title);
        info.appendChild(el('p', 'sig-meta', metaLine(item)));
        const note = noteLine(item);
        if (note) info.appendChild(el('p', 'sig-note', note));
        row.appendChild(info);
        return row;
    }

    function applyFilter(list) {
        list.querySelectorAll('.sig-row').forEach((row) => {
            row.hidden = activeFilter !== 'all' && row.dataset.status !== activeFilter;
        });
    }
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

function countLabel(n, singular, plural) {
    return n + ' ' + (n === 1 ? singular : plural);
}

function statsLine(pages, items) {
    const count = (key, value) => items.filter((item) => item[key] === value).length;
    return [
        countLabel(pages, 'page', 'pages'),
        countLabel(items.length, 'item', 'items'),
        countLabel(count('type', 'signature'), 'signature', 'signatures'),
        countLabel(count('type', 'initial'), 'initial', 'initials'),
        countLabel(count('type', 'notary'), 'notary', 'notaries'),
        countLabel(count('status', 'completed'), 'completed', 'completed'),
        countLabel(count('status', 'blank'), 'blank', 'blanks'),
        countLabel(count('status', 'ambiguous'), 'unclear', 'unclear'),
    ].join(' · ');
}

function typeLabel(type) {
    return { signature: 'Signature', initial: 'Initial', notary: 'Notary' }[type] || 'Item';
}

function statusLabel(status) {
    return { completed: 'Completed', blank: 'Blank', ambiguous: 'Unclear' }[status] || 'Unclear';
}

function statusClass(status) {
    if (status === 'blank' || status === 'ambiguous') return 'sig-status sig-status-attention';
    return 'sig-status';
}

function rowLabel(item) {
    return typeLabel(item.type) + ', ' + statusLabel(item.status) + ', page ' + item.page;
}

function metaLine(item) {
    return [item.file, 'p. ' + item.page, item.region].filter(Boolean).join(' · ');
}

function noteLine(item) {
    const parts = [];
    if (item.label) parts.push(item.label);
    if (item.date_line === 'blank') parts.push('Date line blank');
    if (item.date_line === 'unclear') parts.push('Date line unclear');
    return parts.join(' · ');
}

function openLightbox(src, alt) {
    document.querySelectorAll('.sig-lightbox').forEach((node) => {
        if (typeof node._close === 'function') node._close();
    });
    const overlay = el('div', 'sig-lightbox');
    const img = document.createElement('img');
    img.src = src;
    img.alt = alt;
    overlay.appendChild(img);
    function close() {
        overlay.remove();
        document.removeEventListener('keydown', onKey);
    }
    function onKey(e) {
        if (e.key === 'Escape') close();
    }
    overlay._close = close;
    overlay.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
}
