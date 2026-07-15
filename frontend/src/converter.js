'use strict';

const TRACE_MODE_HELP = {
    contour: 'Detects image edges and converts them into open plotter polylines. This is the closest equivalent to linedraw contour-only mode.',
    hatch: 'Builds tone-dependent hatch strokes directly from image brightness. Dark cells receive additional strokes; light cells receive few or none.',
    'contour-hatch': 'Combines contour polylines with brightness-driven hatch strokes, then orders each pass to reduce pen-up travel.'
};

let sourceImage = null;
let sourceFile = null;
let sourceFilename = 'converted-image.png';
let generatedSvg = '';
let generatedFilename = 'converted-image.svg';
let transferId = '';
let lastTraceStats = null;
let busy = false;

const imageInput = document.getElementById('imageInput');
const traceMode = document.getElementById('traceMode');
const sourcePreview = document.getElementById('sourcePreview');
const svgPreview = document.getElementById('svgPreview');
const statusNode = document.getElementById('status');
const metadataNode = document.getElementById('metadata');
const previewButton = document.getElementById('previewBtn');
const downloadButton = document.getElementById('downloadBtn');
const sendButton = document.getElementById('sendBtn');
const workspaceMode = document.getElementById('workspaceMode');

function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
}

function numberValue(id, fallback, minimum, maximum) {
    const value = Number.parseFloat(document.getElementById(id).value);
    if (!Number.isFinite(value)) return fallback;
    return clamp(value, minimum, maximum);
}

function integerValue(id, fallback, minimum, maximum) {
    return Math.round(numberValue(id, fallback, minimum, maximum));
}

function safeBaseName(filename) {
    return String(filename || 'converted-image')
        .replace(/\.[^.]+$/, '')
        .replace(/[^a-zA-Z0-9._-]+/g, '-')
        .replace(/^-+|-+$/g, '') || 'converted-image';
}

function apiPath(path) {
    return `/api/${String(path).replace(/^\/+/, '')}`;
}

function setBusy(isBusy, message = '') {
    busy = isBusy;
    previewButton.disabled = isBusy || !sourceFile;
    downloadButton.disabled = isBusy || !generatedSvg;
    sendButton.disabled = isBusy || !generatedSvg || !transferId;
    if (message) statusNode.textContent = message;
}

function invalidatePreview(message = 'Settings changed. Preview again before downloading or sending the SVG.') {
    if (busy || !generatedSvg) return;
    generatedSvg = '';
    transferId = '';
    lastTraceStats = null;
    svgPreview.textContent = 'Preview is out of date.';
    metadataNode.textContent = '';
    statusNode.textContent = message;
    setBusy(false);
}

function updateModeVisibility() {
    const mode = traceMode.value;
    document.getElementById('contourOptions').hidden = mode === 'hatch';
    document.getElementById('hatchOptions').hidden = mode === 'contour';
    document.getElementById('traceModeHelp').textContent = TRACE_MODE_HELP[mode] || '';
    traceMode.title = TRACE_MODE_HELP[mode] || '';
}

function drawSourcePreview(image) {
    const maximum = 1200;
    const scale = Math.min(1, maximum / Math.max(image.naturalWidth, image.naturalHeight));
    sourcePreview.width = Math.max(1, Math.round(image.naturalWidth * scale));
    sourcePreview.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = sourcePreview.getContext('2d', { alpha: true });
    context.clearRect(0, 0, sourcePreview.width, sourcePreview.height);
    context.drawImage(image, 0, 0, sourcePreview.width, sourcePreview.height);
}

async function loadImageFile(file) {
    const objectUrl = URL.createObjectURL(file);
    try {
        const image = new Image();
        await new Promise((resolve, reject) => {
            image.onload = resolve;
            image.onerror = () => reject(new Error('The selected file could not be decoded by this browser.'));
            image.src = objectUrl;
        });
        sourceImage = image;
        sourceFile = file;
        sourceFilename = file.name || 'converted-image.png';
        drawSourcePreview(image);
        generatedSvg = '';
        generatedFilename = `${safeBaseName(sourceFilename)}.svg`;
        transferId = '';
        lastTraceStats = null;
        svgPreview.textContent = 'Click Preview SVG to generate plotter polylines.';
        metadataNode.textContent = '';
        statusNode.textContent = `Loaded ${sourceFilename} (${image.naturalWidth.toLocaleString()} × ${image.naturalHeight.toLocaleString()} px).`;
    } finally {
        URL.revokeObjectURL(objectUrl);
        setBusy(false);
    }
}

function readSettings() {
    const low = integerValue('contourLowThreshold', 70, 1, 254);
    const high = Math.max(low + 1, integerValue('contourHighThreshold', 180, 2, 255));
    const light = integerValue('hatchLightThreshold', 160, 1, 254);
    const mid = Math.min(light, integerValue('hatchMidThreshold', 96, 0, 254));
    const dark = Math.min(mid, integerValue('hatchDarkThreshold', 40, 0, 254));
    return {
        mode: traceMode.value,
        outputWidth: numberValue('outputWidth', 150, 1, 5000),
        maxDimension: integerValue('maxDimension', 1024, 128, 4096),
        strokeWidthMm: numberValue('strokeWidthMm', 0.35, 0.05, 10),
        autoContrastCutoff: numberValue('autoContrastCutoff', 2, 0, 20),
        blurRadius: integerValue('blurRadius', 1, 0, 12),
        alphaThreshold: integerValue('alphaThreshold', 16, 0, 255),
        contourLowThreshold: low,
        contourHighThreshold: high,
        contourSimplify: numberValue('contourSimplify', 1.5, 0, 20),
        minimumContourLength: numberValue('minimumContourLength', 10, 0, 100000),
        hatchSize: integerValue('hatchSize', 16, 4, 128),
        hatchLightThreshold: light,
        hatchMidThreshold: mid,
        hatchDarkThreshold: dark,
        sortStrokes: document.getElementById('sortStrokes').checked,
        invert: document.getElementById('invertImage').checked,
        whiteBackground: document.getElementById('whiteBackground').checked
    };
}

function buildVectorizeFormData(settings) {
    const formData = new FormData();
    formData.append('file', sourceFile, sourceFilename);
    Object.entries(settings).forEach(([key, value]) => {
        formData.append(key, typeof value === 'boolean' ? (value ? 'true' : 'false') : String(value));
    });
    return formData;
}

async function readApiError(response) {
    try {
        const payload = await response.json();
        if (typeof payload?.detail === 'string') return payload.detail;
        if (typeof payload?.error === 'string') return payload.error;
    } catch (_error) {
        // Fall through to the status text.
    }
    return `${response.status} ${response.statusText}`.trim();
}

async function previewConversion() {
    if (!sourceImage || !sourceFile) {
        alert('Choose an image first.');
        return;
    }
    setBusy(true, 'Generating plotter-oriented contours and hatches...');
    try {
        const settings = readSettings();
        const response = await fetch(apiPath('vectorize'), {
            method: 'POST',
            body: buildVectorizeFormData(settings),
            headers: { Accept: 'application/json' }
        });
        if (!response.ok) throw new Error(await readApiError(response));
        const result = await response.json();
        if (!result?.svg || !result?.stats || !result?.transfer_id) {
            throw new Error('The vectorization service returned an incomplete result.');
        }

        generatedSvg = result.svg;
        generatedFilename = result.filename || `${safeBaseName(sourceFilename)}-${settings.mode}.svg`;
        transferId = result.transfer_id;
        lastTraceStats = result.stats;
        svgPreview.innerHTML = generatedSvg;
        const stats = result.stats;
        metadataNode.textContent = [
            `${Number(stats.trace_width).toLocaleString()} × ${Number(stats.trace_height).toLocaleString()} trace raster`,
            `${Number(stats.contour_paths || 0).toLocaleString()} contour strokes`,
            `${Number(stats.hatch_paths || 0).toLocaleString()} hatch strokes`,
            `${Number(stats.point_count || 0).toLocaleString()} points`,
            `${Number(stats.output_width_mm).toFixed(1)} × ${Number(stats.output_height_mm).toFixed(1)} mm`,
            'linedraw-inspired polyline engine'
        ].join(' · ');
        statusNode.textContent = 'SVG preview ready. Every visible mark is an open or closed stroked polyline; there are no filled raster-cell regions.';
    } catch (error) {
        generatedSvg = '';
        transferId = '';
        lastTraceStats = null;
        svgPreview.textContent = 'Conversion failed.';
        metadataNode.textContent = '';
        statusNode.textContent = `Conversion failed: ${error.message}`;
        console.error(error);
    } finally {
        setBusy(false);
    }
}

function downloadSvg() {
    if (!generatedSvg) return;
    const blob = new Blob([generatedSvg], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = generatedFilename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function sendToWorkspace() {
    if (!generatedSvg || !transferId) return;
    const workspaceUrl = new URL('./', window.location.href);
    workspaceUrl.searchParams.set('transfer', transferId);
    workspaceUrl.searchParams.set('mode', workspaceMode?.value || 'outline');
    window.location.assign(workspaceUrl.href);
}

imageInput.addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    setBusy(true, 'Loading image...');
    try {
        await loadImageFile(file);
    } catch (error) {
        sourceImage = null;
        sourceFile = null;
        statusNode.textContent = `Unable to load the image: ${error.message}`;
        setBusy(false);
    }
});

traceMode.addEventListener('change', () => {
    updateModeVisibility();
    invalidatePreview();
});
previewButton.addEventListener('click', previewConversion);
downloadButton.addEventListener('click', downloadSvg);
sendButton.addEventListener('click', sendToWorkspace);

document.querySelectorAll('input:not(#imageInput), select:not(#traceMode):not(#workspaceMode)').forEach(control => {
    control.addEventListener('change', () => invalidatePreview());
});

document.querySelectorAll('input[type="number"]').forEach(input => {
    input.addEventListener('wheel', event => {
        if (document.activeElement !== input) input.focus({ preventScroll: true });
        event.preventDefault();
        const step = Number.parseFloat(input.step) || 1;
        const current = Number.parseFloat(input.value) || 0;
        const minimum = input.min === '' ? Number.NEGATIVE_INFINITY : Number.parseFloat(input.min);
        const maximum = input.max === '' ? Number.POSITIVE_INFINITY : Number.parseFloat(input.max);
        input.value = String(clamp(current + (event.deltaY < 0 ? step : -step), minimum, maximum));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }, { passive: false });
});

updateModeVisibility();
setBusy(false);
