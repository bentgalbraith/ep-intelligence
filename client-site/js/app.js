const API_URL = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://localhost:8080/api/tracker/lookup'
    : 'https://ep-intelligence.com/api/tracker/lookup';

const form = document.getElementById('login-form');
const result = document.getElementById('result');
const errMsg = document.getElementById('error-msg');
const clientName = document.getElementById('client-name');
const stepsEl = document.getElementById('steps');
const btnSubmit = document.getElementById('btn-submit');
const btnBack = document.getElementById('btn-back');

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errMsg.textContent = '';
    btnSubmit.disabled = true;
    btnSubmit.textContent = 'Loading...';

    const firm = document.getElementById('f-firm').value.trim();
    const client_id = document.getElementById('f-id').value.trim();
    const access_code = document.getElementById('f-code').value.trim();

    try {
        const res = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ firm, client_id, access_code }),
        });
        const data = await res.json();

        if (!res.ok) {
            errMsg.textContent = data.error || 'Invalid credentials.';
            btnSubmit.disabled = false;
            btnSubmit.textContent = 'View Progress';
            return;
        }

        clientName.textContent = data.client_name;
        stepsEl.innerHTML = '';

        const steps = data.steps || [];
        for (const step of steps) {
            const div = document.createElement('div');
            div.className = `step step--${step.status}`;
            div.innerHTML = `
                <div class="step-indicator">
                    <div class="step-dot">${step.status === 'complete' ? '✓' : ''}</div>
                </div>
                <div class="step-info">
                    <div class="step-name">${esc(step.name)}</div>
                    ${step.description ? `<div class="step-desc">${esc(step.description)}</div>` : ''}
                    ${step.notes ? `<div class="step-notes">${esc(step.notes)}</div>` : ''}
                </div>
            `;
            stepsEl.appendChild(div);
        }

        form.style.display = 'none';
        result.style.display = '';
    } catch {
        errMsg.textContent = 'Connection error. Please try again.';
    }

    btnSubmit.disabled = false;
    btnSubmit.textContent = 'View Progress';
});

btnBack.addEventListener('click', () => {
    result.style.display = 'none';
    form.style.display = '';
});

function esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}
