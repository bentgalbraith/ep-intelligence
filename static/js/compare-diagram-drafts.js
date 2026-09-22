function renderCompareReport(report) {
    if (!report || typeof report !== 'object' || !Array.isArray(report.sections)) {
        const text = typeof report === 'string' ? report.trim() : '';
        if (!text) return '<p class="result-empty-text">No comparison was returned.</p>';
        return `<p class="compare-result">${escapeHtml(text).replace(/\n/g, '<br>')}</p>`;
    }

    const allowed = {
        congruent: true,
        mostly_congruent: true,
        not_congruent: true,
        incomplete: true,
    };
    const verdict = allowed[report.verdict] ? report.verdict : 'incomplete';
    const sections = report.sections.map(renderCompareSection).join('');
    return `<div class="compare-report">
        ${renderCompareVerdict(report, verdict, false)}
        ${sections}
        ${renderCompareVerdict(report, verdict, true)}
    </div>`;
}

function renderCompareVerdict(report, verdict, isFoot) {
    const footClass = isFoot ? ' compare-verdict--foot' : '';
    const summary = isFoot
        ? ''
        : `<p class="compare-verdict-summary">${escapeHtml(report.summary || '')}</p>`;
    const kicker = isFoot ? 'Final conclusion' : 'Conclusion';
    return `<section class="compare-verdict compare-verdict--${escapeAttr(verdict)}${footClass}">
        <p class="compare-verdict-kicker">${kicker}</p>
        <p class="compare-verdict-label">${escapeHtml(report.verdict_label || verdict)}</p>
        ${summary}
    </section>`;
}

function renderCompareSection(section) {
    const open = section.issue_count || (section.intentional && section.intentional.length)
        ? ' open'
        : '';
    const buckets = [
        renderCompareBucket('issues', 'Missing, incorrect, or conflicting', section.issues, 'issue'),
        renderCompareBucket('intent', 'Different but probably intentional', section.intentional, 'intent'),
        renderCompareMatches(section.matches),
        renderCompareAbsent(section.not_found),
    ].filter(Boolean).join('');

    return `<details class="extract-section compare-section"${open}>
        <summary class="extract-section-header compare-section-header">
            <span class="compare-section-title">${escapeHtml(section.title || '')}</span>
            ${renderCompareSectionMeta(section)}
        </summary>
        <div class="extract-section-body">${buckets || '<p class="compare-empty-bucket">No findings in this section.</p>'}</div>
    </details>`;
}

function renderCompareSectionMeta(section) {
    if (section.issue_count) {
        const noun = section.issue_count === 1 ? 'issue' : 'issues';
        return `<span class="compare-section-meta compare-section-meta--issue">${section.issue_count} ${noun}</span>`;
    }
    const intent = (section.intentional || []).length;
    if (intent) {
        return `<span class="compare-section-meta compare-section-meta--intent">${intent} intentional</span>`;
    }
    return '<span class="compare-section-meta">Aligned</span>';
}

function renderCompareBucket(kind, title, items, cardKind) {
    if (!items || !items.length) return '';
    const cards = items.map((item) => renderCompareCard(item, cardKind)).join('');
    return `<div class="compare-bucket compare-bucket--${kind}">
        <h3 class="compare-bucket-title">${title}</h3>
        ${cards}
    </div>`;
}

function renderCompareCard(item, kind) {
    const fields = [
        ['Diagram', item.diagram],
        ['Drafts', item.documents],
        ['Where', item.location],
        ['Fix', item.fix],
    ].filter(([, value]) => value);
    const fieldHtml = fields.length
        ? `<div class="compare-card-fields">${fields.map(([label, value]) => (
            `<div class="compare-card-field">
                <span class="compare-card-k">${label}</span>
                <span class="compare-card-v">${escapeHtml(value)}</span>
            </div>`
        )).join('')}</div>`
        : '';
    const summary = item.summary
        ? `<p class="compare-card-summary">${escapeHtml(item.summary)}</p>`
        : '';
    return `<article class="compare-card compare-card--${kind}">
        <h4 class="compare-card-title">${escapeHtml(item.label || '')}</h4>
        ${summary}
        ${fieldHtml}
    </article>`;
}

function renderCompareMatches(items) {
    if (!items || !items.length) return '';
    const rows = items.map((item) => (
        `<li><span class="compare-list-label">${escapeHtml(item.label || '')}</span>${escapeHtml(item.summary || '')}</li>`
    )).join('');
    return `<div class="compare-bucket compare-bucket--matches">
        <h3 class="compare-bucket-title">Matches</h3>
        <ul class="compare-list compare-list--match">${rows}</ul>
    </div>`;
}

function renderCompareAbsent(items) {
    if (!items || !items.length) return '';
    const rows = items.map((item) => (
        `<li><span class="compare-list-label">${escapeHtml(item.label || '')}</span>${escapeHtml(item.summary || '')}</li>`
    )).join('');
    return `<div class="compare-bucket compare-bucket--absent">
        <h3 class="compare-bucket-title">Not found</h3>
        <ul class="compare-list compare-list--absent">${rows}</ul>
    </div>`;
}

function bindComparePrint(root) {
    if (!root || root.dataset.comparePrintBound) return;
    root.dataset.comparePrintBound = '1';
    window.addEventListener('beforeprint', () => {
        root.querySelectorAll('details.compare-section').forEach((el) => {
            el.open = true;
        });
    });
}
