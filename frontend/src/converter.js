'use strict';

const ENGINE_HELP = {
    linedraw: 'Plotter-oriented open contours and tonal hatches. Best for sketches, photographs, and mixed contour/hatch output.',
    inkscape: 'Inkscape 1.4+ color multi-scan Trace Bitmap. Uses Inkscape itself for the same quantization, stacking, and Potrace curve fitting as the desktop application.',
    potrace: 'Smooth closed monochrome regions. Best for logos, handwriting, silhouettes, and high-contrast line art.',
    pixels2svg: 'Pixel/color-region polygons. Best for pixel art, indexed graphics, and segmentation masks; not intended to smooth photographic contours.'
};

const TRACE_MODE_HELP = {
    contour: 'Detects image edges and converts them into open plotter polylines. This is the closest equivalent to linedraw contour-only mode.',
    centerline: 'Uses the foreground threshold, thins visible marks to one-pixel skeletons, and emits single-stroke centerline polylines instead of both shape boundaries.',
    hatch: 'Builds tone-dependent hatch strokes directly from image brightness. Dark cells receive additional strokes; light cells receive few or none.',
    'contour-hatch': 'Combines contour polylines with brightness-driven hatch strokes, then orders each pass to reduce pen-up travel.',
    'centerline-hatch': 'Combines skeletonized centerlines with brightness-driven hatch strokes, then orders each pass to reduce pen-up travel.'
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
const vectorEngine = document.getElementById('vectorEngine');
const traceMode = document.getElementById('traceMode');
const sourcePreview = document.getElementById('sourcePreview');
const svgPreview = document.getElementById('svgPreview');
const statusNode = document.getElementById('status');
const metadataNode = document.getElementById('metadata');
const previewButton = document.getElementById('previewBtn');
const downloadButton = document.getElementById('downloadBtn');
const sendButton = document.getElementById('sendBtn');
const workspaceMode = document.getElementById('workspaceMode');
const wireframePreview = document.getElementById('wireframePreview');
const engineBadge = document.getElementById('engineBadge');

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

function normalizedEngineLabel(engine) {
    return { linedraw: 'Linedraw', inkscape: 'Inkscape Trace Bitmap', potrace: 'Potrace', pixels2svg: 'Pixels2SVG' }[engine] || engine;
}

function inspectSvgIdentity(svgText) {
    const documentNode = new DOMParser().parseFromString(svgText, 'image/svg+xml');
    if (documentNode.querySelector('parsererror')) {
        throw new Error('The selected engine returned malformed SVG data.');
    }
    const root = documentNode.documentElement;
    if (!root || root.localName !== 'svg') {
        throw new Error('The selected engine did not return an SVG document.');
    }
    return {
        engine: root.getAttribute('data-hatchplot-engine') || '',
        root
    };
}

function applyPreviewStyle() {
    svgPreview.classList.toggle('wireframe', Boolean(wireframePreview?.checked));
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
    engineBadge.textContent = 'Preview required';
    statusNode.textContent = message;
    setBusy(false);
}

function updateModeVisibility() {
    const engine = vectorEngine.value;
    const mode = traceMode.value;
    const isLinedraw = engine === 'linedraw';
    document.getElementById('linedrawModeGroup').hidden = !isLinedraw;
    document.getElementById('contourOptions').hidden = !isLinedraw || mode === 'hatch';
    document.getElementById('hatchOptions').hidden = !isLinedraw || !['hatch', 'contour-hatch', 'centerline-hatch'].includes(mode);
    document.getElementById('sortStrokesRow').hidden = !isLinedraw;
    document.getElementById('inkscapeOptions').hidden = engine !== 'inkscape';
    document.getElementById('potraceOptions').hidden = engine !== 'potrace';
    document.getElementById('pixels2svgOptions').hidden = engine !== 'pixels2svg';
    document.getElementById('traceModeHelp').textContent = TRACE_MODE_HELP[mode] || '';
    document.getElementById('vectorEngineHelp').textContent = ENGINE_HELP[engine] || '';
    traceMode.title = TRACE_MODE_HELP[mode] || '';
    vectorEngine.title = ENGINE_HELP[engine] || '';
}

async function loadEngineAvailability() {
    try {
        const response = await fetch(apiPath('vectorize/engines'), {
            cache: 'no-store',
            headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' }
        });
        if (!response.ok) throw new Error(await readApiError(response));
        const payload = await response.json();
        const engines = payload?.engines || {};
        Array.from(vectorEngine.options).forEach(option => {
            const info = engines[option.value];
            if (!info) return;
            option.disabled = info.available !== true;
            option.dataset.baseLabel = option.dataset.baseLabel || option.textContent.replace(/ \((?:not installed|unavailable|checking…|checking\.\.\.)\)$/, '');
            option.textContent = info.available === false
                ? `${option.dataset.baseLabel} (unavailable)`
                : option.dataset.baseLabel;
            option.title = [info.description, info.version, info.license, info.reason].filter(Boolean).join(' ');
        });
        if (vectorEngine.selectedOptions[0]?.disabled) vectorEngine.value = 'linedraw';
        updateModeVisibility();
    } catch (error) {
        console.warn('Unable to query optional converter engines:', error);
        Array.from(vectorEngine.options).forEach(option => {
            if (option.value !== 'linedraw') option.disabled = true;
        });
        vectorEngine.value = 'linedraw';
        updateModeVisibility();
        document.getElementById('vectorEngineHelp').textContent = 'Engine availability could not be checked. Optional engines are disabled until the GPL backend can be verified.';
    }
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
        engine: vectorEngine.value,
        mode: traceMode.value,
        outputWidth: numberValue('outputWidth', 150, 1, 5000),
        maxDimension: integerValue('maxDimension', 1024, 64, 4096),
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
        whiteBackground: document.getElementById('whiteBackground').checked,
        inkscapeScans: integerValue('inkscapeScans', 8, 2, 256),
        inkscapeSmooth: document.getElementById('inkscapeSmooth').checked,
        inkscapeStack: document.getElementById('inkscapeStack').checked,
        inkscapeRemoveBackground: document.getElementById('inkscapeRemoveBackground').checked,
        inkscapeSpeckles: integerValue('inkscapeSpeckles', 2, 0, 100000),
        inkscapeSmoothCorners: numberValue('inkscapeSmoothCorners', 1, 0, 1.334),
        inkscapeOptimize: numberValue('inkscapeOptimize', 0.2, 0, 5),
        potraceThreshold: integerValue('potraceThreshold', 128, 0, 255),
        potraceTurdSize: integerValue('potraceTurdSize', 2, 0, 100000),
        potraceTurnPolicy: document.getElementById('potraceTurnPolicy').value,
        potraceAlphaMax: numberValue('potraceAlphaMax', 1, 0, 1.334),
        potraceOptimizeCurves: document.getElementById('potraceOptimizeCurves').checked,
        potraceOptimizeTolerance: numberValue('potraceOptimizeTolerance', 0.2, 0, 5),
        pixelsMaxColors: integerValue('pixelsMaxColors', 16, 2, 256),
        pixelsColorTolerance: integerValue('pixelsColorTolerance', 64, 0, 765),
        pixelsRemoveBackground: document.getElementById('pixelsRemoveBackground').checked,
        pixelsBackgroundTolerance: numberValue('pixelsBackgroundTolerance', 1, 0, 50),
        pixelsArtifactPercent: numberValue('pixelsArtifactPercent', 0.1, 0, 100),
        pixelsGroupByColor: document.getElementById('pixelsGroupByColor').checked
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

    const settings = readSettings();
    const requestedEngine = settings.engine;
    const selectedOption = vectorEngine.selectedOptions[0];
    if (!selectedOption || selectedOption.disabled) {
        statusNode.textContent = `${normalizedEngineLabel(requestedEngine)} is not available in the active backend container.`;
        return;
    }

    generatedSvg = '';
    transferId = '';
    lastTraceStats = null;
    svgPreview.textContent = `Generating ${normalizedEngineLabel(requestedEngine)} output…`;
    metadataNode.textContent = '';
    engineBadge.textContent = `${normalizedEngineLabel(requestedEngine)} · running`;
    setBusy(true, `Generating SVG with ${normalizedEngineLabel(requestedEngine)}...`);
    try {
        const response = await fetch(apiPath('vectorize'), {
            method: 'POST',
            body: buildVectorizeFormData(settings),
            cache: 'no-store',
            headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' }
        });
        if (!response.ok) throw new Error(await readApiError(response));
        const result = await response.json();
        if (!result?.svg || !result?.stats || !result?.transfer_id || !result?.engine) {
            throw new Error('The vectorization service returned an incomplete result.');
        }

        const svgIdentity = inspectSvgIdentity(result.svg);
        const actualEngine = String(result.engine || result.stats.engine || '').toLowerCase();
        if (actualEngine !== requestedEngine || String(result.stats.engine || '').toLowerCase() !== requestedEngine) {
            throw new Error(`Engine mismatch: requested ${requestedEngine}, but the backend returned ${actualEngine || 'unknown'}. Rebuild both containers.`);
        }
        if (svgIdentity.engine.toLowerCase() !== requestedEngine) {
            throw new Error(`SVG identity mismatch: requested ${requestedEngine}, but the SVG was stamped ${svgIdentity.engine || 'unknown'}.`);
        }

        generatedSvg = result.svg;
        generatedFilename = result.filename || `${safeBaseName(sourceFilename)}-${requestedEngine}.svg`;
        transferId = result.transfer_id;
        lastTraceStats = result.stats;
        svgPreview.innerHTML = generatedSvg;
        applyPreviewStyle();

        const stats = result.stats;
        const digest = String(result.svg_sha256 || stats.svg_sha256 || '').slice(0, 12);
        engineBadge.textContent = `${normalizedEngineLabel(actualEngine)}${digest ? ` · ${digest}` : ''}`;
        metadataNode.textContent = [
            `Engine: ${normalizedEngineLabel(actualEngine)}`,
            `File: ${generatedFilename}`,
            digest ? `SVG ID: ${digest}` : '',
            `${Number(stats.trace_width).toLocaleString()} × ${Number(stats.trace_height).toLocaleString()} trace raster`,
            `${Number(stats.path_count || 0).toLocaleString()} vector paths/regions`,
            `${Number(stats.contour_paths || 0).toLocaleString()} contour paths`,
            `${Number(stats.hatch_paths || 0).toLocaleString()} hatch paths`,
            `${Number(stats.point_count || 0).toLocaleString()} points/segments`,
            `${Number(stats.output_width_mm).toFixed(1)} × ${Number(stats.output_height_mm).toFixed(1)} mm`,
            stats.engine_detail || actualEngine
        ].filter(Boolean).join(' · ');
        statusNode.textContent = actualEngine === 'linedraw'
            ? `${TRACE_MODE_HELP[traceMode.value] || 'Linedraw vectorization complete'} Preview ready.`
            : actualEngine === 'inkscape'
                ? 'Inkscape preview ready. Output was produced by Inkscape color multi-scan Trace Bitmap.'
                : actualEngine === 'potrace'
                    ? 'Potrace preview ready. Output contains smooth closed monochrome contour paths.'
                    : 'Pixels2SVG preview ready. Output contains quantized pixel/color-region polygons.';
    } catch (error) {
        generatedSvg = '';
        transferId = '';
        lastTraceStats = null;
        svgPreview.textContent = 'Conversion failed.';
        metadataNode.textContent = '';
        engineBadge.textContent = 'Conversion failed';
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

vectorEngine.addEventListener('change', () => {
    updateModeVisibility();
    invalidatePreview('Vectorization engine changed. Preview again before downloading or sending the SVG.');
    engineBadge.textContent = `${normalizedEngineLabel(vectorEngine.value)} · preview required`;
});
traceMode.addEventListener('change', () => {
    updateModeVisibility();
    invalidatePreview();
});
previewButton.addEventListener('click', previewConversion);
downloadButton.addEventListener('click', downloadSvg);
sendButton.addEventListener('click', sendToWorkspace);
wireframePreview?.addEventListener('change', applyPreviewStyle);

document.querySelectorAll('input:not(#imageInput):not(#wireframePreview), select:not(#traceMode):not(#vectorEngine):not(#workspaceMode)').forEach(control => {
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
applyPreviewStyle();
engineBadge.textContent = `${normalizedEngineLabel(vectorEngine.value)} · preview required`;
loadEngineAvailability();
setBusy(false);
