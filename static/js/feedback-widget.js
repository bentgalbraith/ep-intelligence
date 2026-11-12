(function () {
    const root = document.getElementById('feedback-widget');
    if (!root) return;

    const STORAGE_KEY = 'ep_feedback_widget_dismissed_at';
    const DISMISS_MS = 32 * 60 * 60 * 1000;

    const toggle = document.getElementById('feedback-widget-toggle');
    const dismissBtn = document.getElementById('feedback-widget-dismiss');
    const panel = document.getElementById('feedback-widget-panel');
    const form = document.getElementById('feedback-widget-form');
    const messageEl = document.getElementById('feedback-widget-message');
    const errorEl = document.getElementById('feedback-widget-error');
    const statusEl = document.getElementById('feedback-widget-status');
    const submitBtn = document.getElementById('feedback-widget-submit');
    const cancelBtn = document.getElementById('feedback-widget-cancel');
    let thanksTimer = null;
    let restoreTimer = null;

    function readDismissedAt() {
        try {
            const raw = sessionStorage.getItem(STORAGE_KEY);
            if (!raw) return null;
            const at = parseInt(raw, 10);
            return Number.isFinite(at) ? at : null;
        } catch (_) {
            return null;
        }
    }

    function clearDismiss() {
        try {
            sessionStorage.removeItem(STORAGE_KEY);
        } catch (_) { /* ignore */ }
    }

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

    function showWidget() {
        if (restoreTimer) {
            clearTimeout(restoreTimer);
            restoreTimer = null;
        }
        clearDismiss();
        root.hidden = false;
    }

    function scheduleRestore(ms) {
        if (restoreTimer) clearTimeout(restoreTimer);
        restoreTimer = setTimeout(function () {
            restoreTimer = null;
            showWidget();
        }, Math.max(0, ms));
    }

    function hideForDismiss(dismissedAt) {
        setOpen(false);
        root.hidden = true;
        const remaining = DISMISS_MS - (Date.now() - dismissedAt);
        if (remaining > 0) scheduleRestore(remaining);
        else showWidget();
    }

    function dismissWidget() {
        const at = Date.now();
        try {
            sessionStorage.setItem(STORAGE_KEY, String(at));
        } catch (_) { /* ignore */ }
        hideForDismiss(at);
    }

    const existingDismiss = readDismissedAt();
    if (existingDismiss != null && (Date.now() - existingDismiss) < DISMISS_MS) {
        hideForDismiss(existingDismiss);
    } else if (existingDismiss != null) {
        clearDismiss();
    }

    if (dismissBtn) {
        dismissBtn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            dismissWidget();
        });
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
