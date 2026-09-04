(function () {
    const root = document.getElementById('feedback-widget');
    if (!root) return;

    const toggle = document.getElementById('feedback-widget-toggle');
    const panel = document.getElementById('feedback-widget-panel');
    const form = document.getElementById('feedback-widget-form');
    const messageEl = document.getElementById('feedback-widget-message');
    const errorEl = document.getElementById('feedback-widget-error');
    const statusEl = document.getElementById('feedback-widget-status');
    const submitBtn = document.getElementById('feedback-widget-submit');
    const cancelBtn = document.getElementById('feedback-widget-cancel');
    let thanksTimer = null;

    function clearThanksTimer() {
        if (thanksTimer) {
            clearTimeout(thanksTimer);
            thanksTimer = null;
        }
    }

    function showForm() {
        clearThanksTimer();
        form.hidden = false;
        statusEl.hidden = true;
    }

    function setOpen(open) {
        showForm();
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        root.classList.toggle('is-open', open);
        if (open) messageEl.focus();
    }

    toggle.addEventListener('click', function () {
        setOpen(panel.hidden);
    });

    cancelBtn.addEventListener('click', function () {
        errorEl.textContent = '';
        setOpen(false);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !panel.hidden) {
            setOpen(false);
        }
    });

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        errorEl.textContent = '';

        const typeInput = form.querySelector('input[name="feedback_type"]:checked');
        const type = typeInput ? typeInput.value : '';
        const message = (messageEl.value || '').trim();
        if (!type) {
            errorEl.textContent = 'Please choose Bug, Idea, or Other.';
            return;
        }
        if (!message) {
            errorEl.textContent = 'Please enter your feedback.';
            return;
        }

        submitBtn.disabled = true;
        const controller = new AbortController();
        const abortTimer = setTimeout(function () { controller.abort(); }, 20000);
        try {
            const res = await fetch('/api/feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: type,
                    message: message,
                    page: window.location.pathname,
                }),
                signal: controller.signal,
            });
            let data = {};
            try {
                data = await res.json();
            } catch (_) { /* non-JSON (e.g. rate-limit HTML) */ }

            if (res.status === 429) {
                errorEl.textContent = 'Too many submissions. Please try again later.';
                return;
            }
            if (!res.ok || !data.ok) {
                errorEl.textContent = data.error || 'Could not send feedback. Please try again.';
                return;
            }

            messageEl.value = '';
            form.hidden = true;
            statusEl.hidden = false;
            clearThanksTimer();
            thanksTimer = setTimeout(function () {
                thanksTimer = null;
                statusEl.hidden = true;
                form.hidden = false;
                setOpen(false);
            }, 1800);
        } catch (_) {
            errorEl.textContent = 'Could not send feedback. Please try again.';
        } finally {
            clearTimeout(abortTimer);
            submitBtn.disabled = false;
        }
    });
})();
