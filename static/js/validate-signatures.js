function initValidateSignatures(options) {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('pdf-input');
    const fileList = document.getElementById('file-list');
    let selectedFiles = [];

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
        async onSubmit({ setLoading, showResult, showError, hasRun }) {
            if (!selectedFiles.length) return;
            setLoading(true);
            const elapsedEl = document.getElementById('elapsed-time');
            function showProgress(message) {
                if (elapsedEl && message) elapsedEl.textContent = message.replace(/\.{3}$/, '') + '...';
            }
            showProgress('Checking');
            try {
                const body = new FormData();
                for (const file of selectedFiles) body.append('pdfs', file);
                const uploadRes = await fetchWithTimeout(options.submitUrl, { method: 'POST', body });
                const uploadData = await uploadRes.json();
                if (!uploadRes.ok) throw new Error(uploadData.error || 'Something went wrong');
                const result = await pollJob(uploadData.job_id, showProgress);
                showResult('<div class="sig-report" id="sig-report"></div>');
                renderSignatureReport(document.getElementById('sig-report'), result, {
                    jobId: uploadData.job_id,
                    cropUrl: options.cropUrl,
                    fileUrl: options.fileUrl,
                });
            } catch (err) {
                if (elapsedEl) elapsedEl.textContent = '';
                showError(err.message);
            } finally {
                setLoading(false);
                tool.btn.disabled = !selectedFiles.length;
                tool.btnLabel.textContent = hasRun() ? 'Re-Validate' : 'Validate';
            }
        },
    });

    async function pollJob(jobId, onProgress) {
        const url = options.statusUrl.replace('__JOB__', jobId);
        while (true) {
            const res = await fetchWithTimeout(url);
            const data = await res.json();
            if (data.status === 'processing') {
                if (data.progress) onProgress(data.progress);
                await sleep(3000);
                continue;
            }
            if (!res.ok || data.status === 'error') throw new Error(data.error || 'Something went wrong');
            return data;
        }
    }

}

function renderSignatureReport(root, result, urls) {
    const items = Array.isArray(result.items) ? result.items : [];
    const documents = documentsOf(result, items);
    root.appendChild(summaryBlock(result.pages || 0, items, documents));
    if (!documents.length) {
        root.appendChild(el('p', 'sig-empty', 'No signature, initial, or notary lines found.'));
        return;
    }
    const namesKnown = documents.some((doc) => doc.name_status);
    if (items.length || namesKnown) root.appendChild(filterBar(root, namesKnown));
    documents.forEach((doc) => {
        const docItems = items.filter((item) => item.file === doc.name);
        root.appendChild(renderDocument(doc, docItems, urls, false));
    });
}

function summaryBlock(pages, items, documents) {
    const summary = el('div', 'sig-summary');
    const grid = el('div', 'sig-summary-grid');
    function count(key, value) {
        return items.filter((item) => item[key] === value).length;
    }
    [
        [pages, 'Pages'],
        [items.length, 'Items'],
        [count('type', 'signature'), 'Signatures'],
        [count('type', 'initial'), 'Initials'],
        [count('type', 'notary'), 'Notaries'],
    ].forEach(([value, label]) => {
        const stat = el('div', 'sig-stat');
        stat.appendChild(el('span', 'sig-stat-value', String(value)));
        stat.appendChild(el('span', 'sig-stat-label', label));
        grid.appendChild(stat);
    });
    summary.appendChild(grid);
    const statuses = el('div', 'sig-status-row');
    [
        ['completed', 'completed', 'completed'],
        ['blank', 'blank', 'blanks'],
        ['ambiguous', 'unclear', 'unclear'],
    ].forEach(([status, singular, plural]) => {
        const n = count('status', status);
        const chip = el('span', 'sig-chip' + (status !== 'completed' && n > 0 ? ' is-attention' : ''), countLabel(n, singular, plural));
        statuses.appendChild(chip);
    });
    const namedDocs = (documents || []).filter((doc) => doc.name_status);
    if (namedDocs.length) {
        const unclearNames = namedDocs.filter((doc) => doc.name_status === 'ambiguous').length;
        statuses.appendChild(el(
            'span',
            'sig-chip' + (unclearNames > 0 ? ' is-attention' : ''),
            countLabel(unclearNames, 'unclear name', 'unclear names'),
        ));
    }
    summary.appendChild(statuses);
    return summary;
}

function filterBar(root, includeNames) {
    let active = 'all';
    const filters = el('div', 'sig-filters');
    const choices = [['all', 'All'], ['blank', 'Blanks'], ['ambiguous', 'Unclear'], ['completed', 'Completed']];
    if (includeNames) choices.push(['names', 'Unclear names']);
    choices.forEach(([value, label]) => {
        const button = el('button', 'sig-filter' + (value === 'all' ? ' is-active' : ''), label);
        button.type = 'button';
        button.dataset.filter = value;
        filters.appendChild(button);
    });
    filters.addEventListener('click', (e) => {
        const button = e.target.closest('.sig-filter');
        if (!button) return;
        active = button.dataset.filter;
        filters.querySelectorAll('.sig-filter').forEach((node) => {
            node.classList.toggle('is-active', node === button);
        });
        root.querySelectorAll('.sig-doc').forEach((section) => {
            section.hidden = active === 'names' && section.dataset.nameStatus !== 'ambiguous';
        });
        root.querySelectorAll('.sig-row').forEach((row) => {
            row.hidden = active !== 'all' && active !== 'names' && row.dataset.status !== active;
        });
        root.querySelectorAll('.sig-doc').forEach((section) => {
            const visible = section.querySelectorAll('.sig-row:not([hidden])').length;
            const empty = section.querySelector('.sig-doc-none');
            if (empty) empty.hidden = visible > 0 || active === 'names';
        });
    });
    return filters;
}

function documentsOf(result, items) {
    if (Array.isArray(result.documents) && result.documents.length) return result.documents;
    const names = [];
    items.forEach((item) => {
        if (item.file && names.indexOf(item.file) === -1) names.push(item.file);
    });
    return names.map((name, index) => ({ index: index, name: name, pages: null }));
}

function renderDocument(doc, items, urls, startOpen) {
    const section = el('section', 'sig-doc' + (startOpen ? ' is-open' : ''));
    section.dataset.nameStatus = doc.name_status || '';
    const head = el('div', 'sig-doc-head');
    const toggle = el('button', 'sig-doc-toggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', startOpen ? 'true' : 'false');
    toggle.appendChild(chevron());
    const text = el('span', 'sig-doc-text');
    const unclearName = doc.name_status === 'ambiguous';
    const heading = doc.proposed_name || (unclearName ? 'Unclear name' : (doc.name || 'Document'));
    text.appendChild(el('span', 'sig-doc-name' + (unclearName ? ' is-attention' : ''), heading));
    if (unclearName && doc.name_reason) text.appendChild(el('span', 'sig-doc-reason', doc.name_reason));
    text.appendChild(docFacts(doc, items));
    toggle.appendChild(text);
    head.appendChild(toggle);
    if (urls.fileUrl && urls.jobId) {
        const link = document.createElement('a');
        link.className = 'sig-download';
        link.href = urls.fileUrl.replace('__JOB__', urls.jobId).replace('__FILE__', String(doc.index));
        const downloadName = doc.name_status === 'ready' && doc.proposed_name
            ? doc.proposed_name
            : (doc.name || 'document.pdf');
        link.download = downloadName;
        link.title = 'Download PDF';
        link.setAttribute('aria-label', 'Download ' + downloadName);
        link.appendChild(downloadIcon());
        head.appendChild(link);
    }
    const body = el('div', 'sig-doc-body');
    body.hidden = !startOpen;
    if (!items.length) {
        body.appendChild(el('p', 'sig-empty', 'No signature, initial, or notary lines found.'));
    } else {
        const none = el('p', 'sig-doc-none', 'Nothing in this filter.');
        none.hidden = true;
        body.appendChild(none);
        items.forEach((item) => body.appendChild(renderRow(item, urls)));
    }
    toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        body.hidden = !open;
        section.classList.toggle('is-open', open);
    });
    section.appendChild(head);
    section.appendChild(body);
    return section;
}

function docFacts(doc, items) {
    const wrap = el('span', 'sig-doc-facts');
    const bits = [];
    if (doc.pages != null) bits.push([countLabel(doc.pages, 'page', 'pages'), false]);
    bits.push([countLabel(items.length, 'item', 'items'), false]);
    bits.push([countLabel(statusCount(items, 'completed'), 'completed', 'completed'), false]);
    const blanks = statusCount(items, 'blank');
    const unclear = statusCount(items, 'ambiguous');
    bits.push([countLabel(blanks, 'blank', 'blanks'), blanks > 0]);
    bits.push([countLabel(unclear, 'unclear', 'unclear'), unclear > 0]);
    bits.forEach(([text, attention], index) => {
        if (index) wrap.appendChild(el('span', 'sig-sep', ' · '));
        wrap.appendChild(el('span', attention ? 'is-attention' : null, text));
    });
    return wrap;
}

function statusCount(items, status) {
    return items.filter((item) => item.status === status).length;
}

function renderRow(item, urls) {
    const row = el('article', 'sig-row');
    row.dataset.status = item.status || '';
    const button = el('button', 'sig-crop-btn');
    button.type = 'button';
    button.setAttribute('aria-label', rowLabel(item));
    const img = document.createElement('img');
    img.className = 'sig-crop';
    img.alt = rowLabel(item);
    img.src = urls.cropUrl.replace('__JOB__', urls.jobId).replace('__ITEM__', item.id);
    button.appendChild(img);
    button.addEventListener('click', () => openLightbox(img.src, img.alt));
    row.appendChild(button);

    const info = el('div', 'sig-info');
    const title = el('p', 'sig-title');
    title.appendChild(el('span', null, typeLabel(item.type)));
    title.appendChild(el('span', 'sig-sep', ' · '));
    title.appendChild(el('span', statusClass(item.status), statusLabel(item.status)));
    info.appendChild(title);
    info.appendChild(el('p', 'sig-meta', [item.page != null ? 'p. ' + item.page : '', item.region].filter(Boolean).join(' · ')));
    const note = noteLine(item);
    if (note) info.appendChild(el('p', 'sig-note', note));
    row.appendChild(info);
    return row;
}

function chevron() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'sig-doc-chevron');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm9 18 6-6-6-6');
    svg.appendChild(path);
    return svg;
}

function downloadIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '15');
    svg.setAttribute('height', '15');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    ['M12 3v12', 'm7 11 5 5 5-5', 'M5 21h14'].forEach((d) => {
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', d);
        svg.appendChild(path);
    });
    return svg;
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
