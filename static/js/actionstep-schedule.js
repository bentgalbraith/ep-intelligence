// Per-bubble print fit. The colored block stays the length of the appointment.
// A title that does not fit may continue into empty space below that block.
// Compression (padding, hyphenation, tracking, line height, then font size)
// is applied only on the axis that is still failing, and only until the title
// fits in the block plus that free space.
const BUBBLE_FIT = {
    fonts: [0.55, 0.52, 0.49, 0.46, 0.43],
    lineHeights: [1.15, 1.05, 1],
    tracks: ['0em', '-0.02em', '-0.04em'],
    padX: [3, 1, 0],
    padY: [1, 0],
    gap: 2,
};

function applyBubbleTitle(title, state, maxH) {
    const padX = BUBBLE_FIT.padX[state.padX];
    const padY = BUBBLE_FIT.padY[state.padY];
    title.style.margin = padY + 'px ' + padX + 'px 0';
    title.style.fontSize = BUBBLE_FIT.fonts[state.font] + 'rem';
    title.style.lineHeight = String(BUBBLE_FIT.lineHeights[state.line]);
    title.style.letterSpacing = BUBBLE_FIT.tracks[state.track];
    title.style.hyphens = state.hyphenate ? 'auto' : 'manual';
    title.style.webkitHyphens = state.hyphenate ? 'auto' : 'manual';
    title.style.overflowWrap = state.breakWord ? 'break-word' : 'normal';
    title.style.maxHeight = Math.max(0, maxH) + 'px';
}

function textStopY(block) {
    // Nearest obstacle that shares this bubble's horizontal span: the next
    // appointment, including one this bubble already overlaps (a short
    // appointment's minimum height can extend into the one after it), or the
    // bottom of the column.
    const body = block.closest('.sched-body');
    const b = block.getBoundingClientRect();
    if (!body || b.width < 1 || b.height < 1) return b.bottom;
    let limit = body.getBoundingClientRect().bottom;
    body.querySelectorAll('.sched-block').forEach((other) => {
        if (other === block) return;
        const o = other.getBoundingClientRect();
        const overlapsX = o.left < b.right - 0.5 && o.right > b.left + 0.5;
        // An earlier appointment can overlap this box because of the minimum
        // bubble height. Its top is above us, so it is not the limit below.
        if (!overlapsX || o.top < b.top - 0.5) return;
        if (o.top < limit) limit = o.top;
    });
    return limit;
}

function fitBubbleTitle(block, title) {
    const state = {
        font: 0,
        line: 0,
        track: 0,
        padX: 0,
        padY: 0,
        hyphenate: false,
        breakWord: false,
    };
    const box = block.getBoundingClientRect();
    const style = getComputedStyle(block);
    const borderTop = parseFloat(style.borderTopWidth) || 0;
    const borderBottom = parseFloat(style.borderBottomWidth) || 0;
    const contentTop = box.top + borderTop;
    const contentBottom = box.bottom - borderBottom;
    const stopY = textStopY(block);
    const covered = stopY < box.bottom - 0.5;
    // Open space below keeps a gap before the next appointment. An appointment
    // that already overlaps this box, or one that starts flush with it, only
    // limits the text to the visible part of this box.
    const insideEnd = covered ? Math.min(contentBottom, stopY) : contentBottom;
    const roomEnd = stopY > box.bottom + 0.5 ? stopY - BUBBLE_FIT.gap : insideEnd;

    for (let n = 0; n < 24; n++) {
        // Measure the natural text. A tight max-height would hide the overflow
        // we are trying to detect.
        applyBubbleTitle(title, state, 4000);
        const padY = BUBBLE_FIT.padY[state.padY];
        const textH = title.scrollHeight;
        const visible = Math.max(0, insideEnd - contentTop);
        const capInside = Math.max(0, visible - padY - (covered ? 0 : padY));
        const capRoom = Math.max(0, roomEnd - contentTop - padY);
        const narrow = title.scrollWidth > title.clientWidth + 1;
        if (!narrow && textH <= capInside) {
            applyBubbleTitle(title, state, capInside);
            return;
        }
        if (!narrow && textH <= capRoom) {
            applyBubbleTitle(title, state, capRoom);
            return;
        }
        if (narrow && state.padX < BUBBLE_FIT.padX.length - 1) {
            state.padX += 1;
            continue;
        }
        if (narrow && !state.hyphenate) {
            state.hyphenate = true;
            continue;
        }
        if (narrow && !state.breakWord) {
            state.breakWord = true;
            continue;
        }
        if (narrow && state.track < BUBBLE_FIT.tracks.length - 1) {
            state.track += 1;
            continue;
        }
        if (!narrow && state.padY < BUBBLE_FIT.padY.length - 1) {
            state.padY += 1;
            continue;
        }
        if (!narrow && state.line < BUBBLE_FIT.lineHeights.length - 1) {
            state.line += 1;
            continue;
        }
        if (state.font < BUBBLE_FIT.fonts.length - 1) {
            state.font += 1;
            continue;
        }
        applyBubbleTitle(title, state, capRoom);
        return;
    }
}

function fitScheduleBubbles(src) {
    if (!src || !src.querySelector('.sched-block')) return;
    const probe = src.cloneNode(true);
    probe.className = 'sched-print-measure';
    probe.setAttribute('aria-hidden', 'true');
    document.body.appendChild(probe);
    void probe.offsetHeight;
    const probeBlocks = probe.querySelectorAll('.sched-block');
    const srcBlocks = src.querySelectorAll('.sched-block');
    probeBlocks.forEach((block, i) => {
        const title = block.querySelector('.sched-block-title');
        const srcTitle = srcBlocks[i] && srcBlocks[i].querySelector('.sched-block-title');
        if (!title || !srcTitle || block.clientWidth < 1) return;
        fitBubbleTitle(block, title);
        srcTitle.style.cssText = title.style.cssText;
    });
    probe.remove();
}
