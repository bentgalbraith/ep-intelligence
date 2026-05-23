function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function escapeAttr(str) {
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function exportTimestamp() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${now.getFullYear()}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 600000) {
    if (!navigator.onLine) throw new Error('You appear to be offline — check your internet connection and try again.');

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    let res;
    try {
        res = await fetch(url, { ...options, signal: controller.signal });
    } catch (err) {
        clearTimeout(timer);
        if (!navigator.onLine) throw new Error('You appear to be offline — check your internet connection and try again.');
        if (err.name === 'AbortError') throw new Error('The request timed out — please try again.');
        throw new Error('The connection was lost before a response was received — please try again.');
    }
    clearTimeout(timer);
    return res;
}

function initToolForm({ formId, btnId, onSubmit }) {
    const form = document.getElementById(formId);
    const btn = document.getElementById(btnId);
    const btnLabel = btn.querySelector('.btn-label');
    const spinner = btn.querySelector('.spinner');
    const resultEmpty = document.getElementById('result-empty');
    const resultContent = document.getElementById('result-content');

    let hasRun = false;
    let startTime = null;

    let elapsedEl = document.getElementById('elapsed-time');
    if (!elapsedEl) {
        elapsedEl = document.createElement('p');
        elapsedEl.id = 'elapsed-time';
        elapsedEl.className = 'result-elapsed';
        btn.insertAdjacentElement('afterend', elapsedEl);
    }

    function resetResults() {
        resultContent.style.display = 'none';
        resultContent.innerHTML = '';
        resultEmpty.style.display = '';
    }

    function setLoading(on) {
        if (on) {
            startTime = Date.now();
            elapsedEl.textContent = '';
            resetResults();
        }
        btn.disabled = on;
        spinner.hidden = !on;
        form.classList.toggle('is-loading', on);
        return { btnLabel, hasRun };
    }

    function showResult(html) {
        hasRun = true;
        if (startTime) {
            const totalSecs = (Date.now() - startTime) / 1000;
            const display = totalSecs >= 60
                ? `${Math.floor(totalSecs / 60)}m ${(totalSecs % 60).toFixed(0)}s`
                : `${totalSecs.toFixed(1)}s`;
            elapsedEl.textContent = `Completed in ${display}`;
        }
        resultEmpty.style.display = 'none';
        resultContent.style.display = 'block';
        resultContent.innerHTML = html;
    }

    function submitForm() {
        return onSubmit({ setLoading, showResult, showError, btnLabel, hasRun: () => hasRun });
    }

    function showError(msg) {
        hasRun = true;
        resultEmpty.style.display = 'none';
        resultContent.style.display = 'block';
        resultContent.innerHTML =
            `<p class="result-error">${escapeHtml(msg)}</p>` +
            `<button type="button" class="btn-pill btn-retry" id="btn-retry">RETRY</button>`;
        document.getElementById('btn-retry').addEventListener('click', () => submitForm());
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        await submitForm();
    });

    return { form, btn, btnLabel, spinner, resultEmpty, resultContent, setLoading, showResult, showError, hasRun: () => hasRun };
}
