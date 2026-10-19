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
                    zipUrl: options.zipUrl,
                    nameUrl: options.nameUrl,
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
    const names = documents.map(displayName);
    const issues = issueEntries(documents, items);
    function onRename(index, name) {
        root.querySelectorAll('[data-name-for="' + index + '"]').forEach((node) => {
            node.textContent = name;
        });
    }
    root.appendChild(summaryCard(result.pages || 0, items, documents, names, urls, issues));
    if (!documents.length) {
        root.appendChild(el('p', 'sig-empty', 'No documents to name.'));
        return;
    }
    documents.forEach((doc, index) => {
        const docItems = items.filter((item) => item.file === doc.name);
        root.appendChild(renderDocument(doc, docItems, urls, names, index, onRename));
    });
}

function summaryCard(pages, items, documents, names, urls, issues) {
    const blanks = issues.filter((entry) => entry.item.status === 'blank').length;
    const unclear = issues.length - blanks;
    const state = blanks ? 'is-issue' : unclear ? 'is-potential' : 'is-clear';
    const card = el('section', 'sig-summary ' + state);
    if (documents.length && urls.zipUrl && urls.jobId) {
        const download = el('div', 'sig-download-row');
        const button = el('button', 'btn-download btn-download--primary sig-zip', 'Download all');
        button.type = 'button';
        button.addEventListener('click', () => downloadZip(button, names, urls));
        download.appendChild(button);
        card.appendChild(download);
    }
    const top = el('div', 'sig-summary-top');
    const counts = el('div', 'sig-counts');
    [
        [documents.length, 'Documents'],
        [pages, 'Pages'],
        [items.filter((item) => item.type === 'signature').length, 'Signatures'],
        [items.filter((item) => item.type === 'initial').length, 'Initials'],
        [items.filter((item) => item.type === 'notary').length, 'Notarizations'],
    ].forEach(([value, label]) => {
        const stat = el('div', 'sig-count');
        stat.appendChild(el('span', 'sig-count-value', String(value)));
        stat.appendChild(el('span', 'sig-count-label', label));
        counts.appendChild(stat);
    });
    top.appendChild(counts);
    card.appendChild(top);

    const foot = el('div', 'sig-summary-foot');
    const status = el('div', 'sig-summary-status');
    status.appendChild(state === 'is-clear' ? checkIcon() : warningIcon());
    const label = blanks
        ? countLabel(issues.length, 'Issue Found', 'Issues Found')
        : unclear ? 'Potential Issues' : 'No Issues Found';
    status.appendChild(el('p', 'sig-status-label', label));
    foot.appendChild(status);
    if (issues.length) foot.appendChild(issueLog(issues, names));
    card.appendChild(foot);
    return card;
}

function issueEntries(documents, items) {
    const entries = [];
    documents.forEach((doc, index) => {
        items.filter((item) => item.file === doc.name && (item.status === 'blank' || item.status === 'ambiguous'))
            .slice()
            .sort((a, b) => (a.page || 0) - (b.page || 0) || (a.status === 'blank' ? 0 : 1) - (b.status === 'blank' ? 0 : 1))
            .forEach((item) => entries.push({ item: item, docIndex: index }));
    });
    return entries;
}

function issueLog(entries, names) {
    const log = el('div', 'sig-issue-log');
    entries.forEach((entry) => {
        const button = el('button', 'sig-issue' + (entry.item.status === 'blank' ? ' is-blank' : ' is-unclear'));
        button.type = 'button';
        button.appendChild(el('span', 'sig-issue-kind', statusLabel(entry.item.status) + ' ' + typeLabel(entry.item.type).toLowerCase()));
        const where = el('span', 'sig-issue-where');
        const docName = el('span', null, names[entry.docIndex]);
        docName.dataset.nameFor = String(entry.docIndex);
        where.appendChild(docName);
        const place = [entry.item.page != null ? 'p. ' + entry.item.page : '', entry.item.region].filter(Boolean).join(' · ');
        if (place) where.appendChild(document.createTextNode(' · ' + place));
        button.appendChild(where);
        button.addEventListener('click', () => jumpToItem(entry.item.id));
        log.appendChild(button);
    });
    return log;
}

function jumpToItem(itemId) {
    const row = document.getElementById('sig-item-' + itemId);
    if (!row) return;
    const section = row.closest('.sig-doc');
    if (section) setDocOpen(section, true);
    row.scrollIntoView({ block: 'center' });
    row.classList.add('is-target');
    window.clearTimeout(row._jumpTimer);
    row._jumpTimer = window.setTimeout(() => row.classList.remove('is-target'), 1800);
}

function documentsOf(result, items) {
    if (Array.isArray(result.documents) && result.documents.length) return result.documents;
    const names = [];
    items.forEach((item) => {
        if (item.file && names.indexOf(item.file) === -1) names.push(item.file);
    });
    return names.map((name, index) => ({ index: index, name: name, pages: null }));
}

function renderDocument(doc, items, urls, names, index, onRename) {
    const section = el('section', 'sig-doc');
    const head = el('div', 'sig-doc-head');
    const toggle = el('button', 'sig-doc-toggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Show signatures');
    toggle.appendChild(chevron());
    head.appendChild(toggle);

    const field = el('div', 'sig-name-field');
    const input = document.createElement('input');
    input.className = 'sig-name';
    input.type = 'text';
    input.value = names[index];
    input.spellcheck = false;
    input.setAttribute('aria-label', 'File name');
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') input.blur();
    });
    const fileIndex = doc.index != null ? doc.index : index;
    input.addEventListener('blur', () => {
        const next = pdfName(input.value) || names[index];
        input.value = next;
        names[index] = next;
        if (onRename) onRename(index, next);
        rememberName(fileIndex, next, urls, input, names, index, onRename);
    });
    field.appendChild(input);
    if (doc.name_status === 'ambiguous' && doc.name_reason) {
        field.appendChild(el('p', 'sig-name-note', doc.name_reason));
    }
    head.appendChild(field);

    if (urls.fileUrl && urls.jobId) {
        const link = document.createElement('a');
        link.className = 'sig-download';
        link.href = urls.fileUrl.replace('__JOB__', urls.jobId).replace('__FILE__', String(fileIndex));
        link.title = 'Download PDF';
        link.setAttribute('aria-label', 'Download PDF');
        link.appendChild(downloadIcon());
        link.addEventListener('click', (event) => {
            event.preventDefault();
            const next = pdfName(input.value) || names[index];
            input.value = next;
            names[index] = next;
            if (onRename) onRename(index, next);
            postDownload(link.href, [['name', next]]);
        });
        head.appendChild(link);
    }

    const body = el('div', 'sig-doc-body');
    body.hidden = true;
    if (!items.length) {
        body.appendChild(el('p', 'sig-empty', 'No signature, initial, or notary lines found.'));
    } else {
        orderedItems(items).forEach((item) => body.appendChild(renderRow(item, urls)));
    }
    toggle.addEventListener('click', () => {
        setDocOpen(section, toggle.getAttribute('aria-expanded') !== 'true');
    });
    section.appendChild(head);
    section.appendChild(body);
    return section;
}

function setDocOpen(section, open) {
    const toggle = section.querySelector('.sig-doc-toggle');
    const body = section.querySelector('.sig-doc-body');
    if (toggle) {
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.setAttribute('aria-label', open ? 'Hide signatures' : 'Show signatures');
    }
    if (body) body.hidden = !open;
    section.classList.toggle('is-open', open);
}

function orderedItems(items) {
    return items.slice().sort((a, b) => (a.page || 0) - (b.page || 0));
}

function displayName(doc) {
    return doc.download_name || doc.proposed_name || 'Unnamed.pdf';
}

function pdfName(value) {
    let name = String(value || '')
        .replace(/[<>:"|?*\\/\u0000-\u001f\u2028\u2029]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    if (name.toLowerCase().endsWith('.pdf')) name = name.slice(0, -4).trim();
    name = name.replace(/^\.+|\.+$/g, '').trim();
    if (name.length > 176) name = name.slice(0, 176).trim();
    if (!name) return '';
    return name + '.pdf';
}

function rememberName(fileIndex, name, urls, input, names, index, onRename) {
    if (!urls.nameUrl || !urls.jobId || !name) return;
    fetch(urls.nameUrl.replace('__JOB__', urls.jobId).replace('__FILE__', String(fileIndex)), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name }),
    }).then((res) => res.ok ? res.json() : null).then((data) => {
        if (!data || !data.name || input.value !== name) return;
        input.value = data.name;
        names[index] = data.name;
        if (onRename) onRename(index, data.name);
    }).catch(() => {});
}

function downloadZip(button, names, urls) {
    const report = button.closest('.sig-report');
    const fields = report ? report.querySelectorAll('.sig-name') : [];
    const payload = names.map((name, index) => {
        const next = pdfName(fields[index] ? fields[index].value : name) || name;
        if (fields[index]) fields[index].value = next;
        names[index] = next;
        if (report) {
            report.querySelectorAll('[data-name-for="' + index + '"]').forEach((node) => {
                node.textContent = next;
            });
        }
        return next;
    });
    postDownload(
        urls.zipUrl.replace('__JOB__', urls.jobId),
        payload.map((name) => ['names', name]),
    );
}

function postDownload(action, fields) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = action;
    form.acceptCharset = 'UTF-8';
    form.hidden = true;
    fields.forEach(([key, value]) => {
        const field = document.createElement('input');
        field.type = 'hidden';
        field.name = key;
        field.value = value;
        form.appendChild(field);
    });
    document.body.appendChild(form);
    form.submit();
    form.remove();
}

function renderRow(item, urls) {
    const row = el('article', 'sig-row');
    row.id = 'sig-item-' + item.id;
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

function checkIcon() {
    const svg = statusIcon('m9 12 2 2 4-4', false);
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '12');
    circle.setAttribute('cy', '12');
    circle.setAttribute('r', '10');
    svg.insertBefore(circle, svg.firstChild);
    return svg;
}

function warningIcon() {
    const svg = statusIcon('', true);
    const mark = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    mark.setAttribute('d', 'M12 9v4');
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    dot.setAttribute('d', 'M12 17h.01');
    svg.appendChild(mark);
    svg.appendChild(dot);
    return svg;
}

function statusIcon(d, triangle) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'sig-status-icon');
    svg.setAttribute('width', '18');
    svg.setAttribute('height', '18');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', triangle ? 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z' : d);
    svg.appendChild(path);
    return svg;
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
    if (status === 'blank') return 'sig-status-attention';
    if (status === 'ambiguous') return 'sig-status-unclear';
    return '';
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
