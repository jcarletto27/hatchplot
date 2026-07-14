paper.setup(document.getElementById('canvas'));

// --- STATE ---
let originalSVG = null;
let workingSVG = null;
let originalSVGImage = null;
let originalSVGObjectUrl = null;
let originalSVGText = '';
let sourceSvgSizeMm = null;
let originalSvgDocument = null;
let layerEntries = [];
let machineBed = null;
let originMarker = null;
let activeJobId = null;
let cancelRequested = false;
let previewSvgLoadToken = 0;

let isAnimating = false;
let simulationStoppedByUser = false;
let currentPathIndex = 0;
let currentOffset = 0;
let penHead = null;
let allGeneratedPaths = [];
let generatedPathGcodeRanges = [];
let gcodeLines = [];
let gcodeLineOffsets = [];
let livePreviewCursor = 0;
let livePreviewInitialized = false;
let workspaceFitZoom = 1;
let canvasZoomPercent = 100;
let patternCenterMarker = null;
let pickingPatternCenter = false;
let brightnessCutoffOverlay = null;
let brightnessCutoffPreviewToken = 0;
let brightnessCutoffPreviewTimer = null;

const MACHINE_SETTINGS_KEY = 'hatchPlotter.machineSettings.v1';
const UI_SETTINGS_KEY = 'hatchPlotter.uiSettings.v1';
const FIT_MARGIN = 0.9;
const MM_PER_CSS_PIXEL = 25.4 / 96;
const MAX_BRIGHTNESS_MAP_DIMENSION = 4096;
const INKSCAPE_NAMESPACE = 'http://www.inkscape.org/namespaces/inkscape';
const COLLAPSIBLE_SECTION_IDS = ['machineSection', 'importSection', 'generateSection', 'gcodeSection'];
const MACHINE_SETTING_IDS = [
    'bedX',
    'bedY',
    'zMode',
    'zUp',
    'zDown',
    'xyFeedRate',
    'zPlungeRate',
    'penThickness',
    'densityFudge',
    'brightnessCutoff',
    'patternLayout',
    'waveform',
    'patternCenterX',
    'patternCenterY',
    'patternAngle',
    'patternSpacing',
    'waveAmplitude',
    'waveLength',
    'brightnessModulation'
];

function readPositiveNumber(id, fallback) {
    const value = Number.parseFloat(document.getElementById(id).value);
    return Number.isFinite(value) && value > 0 ? value : fallback;
}

function formatNumber(value, maximumFractionDigits = 2) {
    return Number(value.toFixed(maximumFractionDigits)).toString();
}

function parseSvgLengthToMm(rawValue) {
    if (!rawValue || typeof rawValue !== 'string') return null;
    const match = rawValue.trim().match(/^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*(mm|cm|in|pt|pc|px|q)?$/i);
    if (!match) return null;
    const value = Number.parseFloat(match[1]);
    if (!Number.isFinite(value) || value <= 0) return null;
    const unit = (match[2] || 'px').toLowerCase();
    const factors = {
        mm: 1,
        cm: 10,
        in: 25.4,
        pt: 25.4 / 72,
        pc: 25.4 / 6,
        px: MM_PER_CSS_PIXEL,
        q: 0.25
    };
    return value * factors[unit];
}

function determineSvgPhysicalSize(svgText, image) {
    let widthMm = null;
    let heightMm = null;
    let viewBoxWidth = null;
    let viewBoxHeight = null;

    try {
        const documentNode = new DOMParser().parseFromString(svgText, 'image/svg+xml');
        const root = documentNode.documentElement;
        if (root && root.nodeName.toLowerCase() === 'svg') {
            widthMm = parseSvgLengthToMm(root.getAttribute('width'));
            heightMm = parseSvgLengthToMm(root.getAttribute('height'));
            const viewBox = (root.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
            if (viewBox.length === 4 && viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0) {
                viewBoxWidth = viewBox[2];
                viewBoxHeight = viewBox[3];
            }
        }
    } catch (error) {
        console.warn('Unable to inspect SVG dimensions:', error);
    }

    if (widthMm && !heightMm && viewBoxWidth && viewBoxHeight) {
        heightMm = widthMm * (viewBoxHeight / viewBoxWidth);
    } else if (heightMm && !widthMm && viewBoxWidth && viewBoxHeight) {
        widthMm = heightMm * (viewBoxWidth / viewBoxHeight);
    }

    return {
        width: widthMm || Math.max(0.01, image.naturalWidth * MM_PER_CSS_PIXEL),
        height: heightMm || Math.max(0.01, image.naturalHeight * MM_PER_CSS_PIXEL)
    };
}


function detectSvgLayers(svgText) {
    const documentNode = new DOMParser().parseFromString(svgText, 'image/svg+xml');
    if (documentNode.querySelector('parsererror')) {
        throw new Error('The uploaded SVG contains invalid XML.');
    }

    const root = documentNode.documentElement;
    if (!root || root.nodeName.toLowerCase() !== 'svg') {
        throw new Error('The uploaded file does not contain an <svg> root element.');
    }

    const candidateLayers = [];
    const seenNodes = new Set();
    const groups = Array.from(root.querySelectorAll('g'));
    for (const node of groups) {
        const groupMode = node.getAttributeNS(INKSCAPE_NAMESPACE, 'groupmode') || node.getAttribute('inkscape:groupmode') || '';
        const label = node.getAttributeNS(INKSCAPE_NAMESPACE, 'label') || node.getAttribute('inkscape:label') || node.getAttribute('label') || node.getAttribute('id') || '';
        if (groupMode === 'layer') {
            candidateLayers.push({ node, name: label || `Layer ${candidateLayers.length + 1}` });
            seenNodes.add(node);
        }
    }

    if (candidateLayers.length === 0) {
        for (const node of groups) {
            if (seenNodes.has(node)) continue;
            if (node.parentElement !== root) continue;
            const label = node.getAttributeNS(INKSCAPE_NAMESPACE, 'label') || node.getAttribute('inkscape:label') || node.getAttribute('label') || node.getAttribute('id') || '';
            if (label.trim()) {
                candidateLayers.push({ node, name: label.trim() });
                seenNodes.add(node);
            }
        }
    }

    if (candidateLayers.length === 0) {
        root.setAttribute('data-hatchplot-layer-id', 'layer-root');
        return {
            documentNode,
            layers: [{ id: 'layer-root', name: 'Artwork', enabled: true, root: true }]
        };
    }

    const layers = candidateLayers.map((entry, index) => {
        const id = `layer-${index + 1}`;
        entry.node.setAttribute('data-hatchplot-layer-id', id);
        return {
            id,
            name: String(entry.name || `Layer ${index + 1}`).trim() || `Layer ${index + 1}`,
            enabled: true,
            root: false,
        };
    });

    return { documentNode, layers };
}

function renderLayerControls() {
    const container = document.getElementById('layerControls');
    const enableAllButton = document.getElementById('enableAllLayersBtn');
    const disableAllButton = document.getElementById('disableAllLayersBtn');
    container.innerHTML = '';

    if (!layerEntries.length) {
        container.textContent = 'Upload an SVG to inspect its layers.';
        enableAllButton.disabled = true;
        disableAllButton.disabled = true;
        return;
    }

    enableAllButton.disabled = false;
    disableAllButton.disabled = false;

    const summary = document.createElement('div');
    summary.textContent = `${layerEntries.filter(layer => layer.enabled).length} of ${layerEntries.length} layers enabled`;
    summary.style.marginBottom = '8px';
    container.appendChild(summary);

    layerEntries.forEach((layer, index) => {
        const row = document.createElement('div');
        row.className = 'checkbox-row';
        row.style.margin = '4px 0';

        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = Boolean(layer.enabled);
        input.id = `svgLayerToggle${index}`;
        input.dataset.layerId = layer.id;

        const label = document.createElement('label');
        label.htmlFor = input.id;
        label.textContent = layer.name;
        label.style.flex = '1';

        input.addEventListener('change', async () => {
            layer.enabled = input.checked;
            renderLayerControls();
            await reloadPreviewFromLayerSelection(false);
        });

        row.appendChild(input);
        row.appendChild(label);
        container.appendChild(row);
    });
}

function setAllLayersEnabled(enabled) {
    if (!layerEntries.length) return;
    layerEntries.forEach(layer => { layer.enabled = enabled; });
    renderLayerControls();
    reloadPreviewFromLayerSelection(false).catch(error => {
        console.error('Unable to update layer visibility:', error);
        generationStatus.textContent = `Layer update failed: ${error.message}`;
    });
}

function serializeEnabledSvg() {
    if (!originalSvgDocument) return originalSVGText;
    const clone = originalSvgDocument.cloneNode(true);

    for (const layer of layerEntries) {
        if (layer.enabled) continue;
        const node = clone.querySelector(`[data-hatchplot-layer-id="${layer.id}"]`);
        if (!node) continue;
        if (node === clone.documentElement) {
            while (node.firstChild) node.removeChild(node.firstChild);
        } else {
            node.remove();
        }
    }
    return new XMLSerializer().serializeToString(clone);
}

function getEnabledLayerNames() {
    return layerEntries.filter(layer => layer.enabled).map(layer => layer.name);
}

async function reloadPreviewFromLayerSelection(announceLoad = false) {
    if (!originalSVGText || !sourceSvgSizeMm) return;
    clearGeneratedPreview();
    const enabledSvgText = serializeEnabledSvg();
    const objectUrl = URL.createObjectURL(new Blob([enabledSvgText], { type: 'image/svg+xml' }));
    const image = new Image();
    const loadToken = ++previewSvgLoadToken;

    await new Promise((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error('The selected SVG layers could not be rendered in the browser.'));
        image.src = objectUrl;
    });

    if (loadToken !== previewSvgLoadToken) {
        URL.revokeObjectURL(objectUrl);
        return;
    }

    if (originalSVG) originalSVG.remove();
    if (workingSVG) workingSVG.remove();
    if (originalSVGObjectUrl) URL.revokeObjectURL(originalSVGObjectUrl);

    originalSVGObjectUrl = objectUrl;
    originalSVGImage = image;
    originalSVG = new paper.Raster(image);
    originalSVG.size = new paper.Size(sourceSvgSizeMm.width, sourceSvgSizeMm.height);
    originalSVG.position = new paper.Point(0, 0);
    originalSVG.visible = false;
    applyTransforms();

    const enabledCount = layerEntries.filter(layer => layer.enabled).length;
    if (announceLoad) {
        generationStatus.textContent = enabledCount
            ? `Loaded SVG at ${formatNumber(sourceSvgSizeMm.width)} × ${formatNumber(sourceSvgSizeMm.height)} mm with ${enabledCount} enabled layer${enabledCount === 1 ? '' : 's'}.`
            : 'All SVG layers are currently disabled.';
    } else {
        generationStatus.textContent = enabledCount
            ? `${enabledCount} layer${enabledCount === 1 ? '' : 's'} enabled for preview and generation.`
            : 'All SVG layers are currently disabled.';
    }
}

function ensurePenThickness() {
    const input = document.getElementById('penThickness');
    let thickness = Number.parseFloat(input.value);
    if (Number.isFinite(thickness) && thickness >= 0.05 && thickness <= 10) return thickness;

    const response = window.prompt(
        'Enter the physical pen-tip thickness in millimeters. This controls zig-zag density and G-code preview width:',
        '0.5'
    );
    if (response === null) throw new Error('Pen thickness is required to generate the brightness-driven toolpath.');
    thickness = Number.parseFloat(response);
    if (!Number.isFinite(thickness) || thickness < 0.05 || thickness > 10) {
        throw new Error('Pen thickness must be between 0.05 and 10 mm.');
    }
    input.value = formatNumber(thickness, 3);
    saveMachineSettings();
    return thickness;
}

function restoreStoredSettings() {
    try {
        const machineSettings = JSON.parse(localStorage.getItem(MACHINE_SETTINGS_KEY) || '{}');
        MACHINE_SETTING_IDS.forEach(id => {
            if (machineSettings[id] !== undefined && machineSettings[id] !== null) {
                document.getElementById(id).value = machineSettings[id];
            }
        });

        const uiSettings = JSON.parse(localStorage.getItem(UI_SETTINGS_KEY) || '{}');
        if (typeof uiSettings.autoCenter === 'boolean') {
            document.getElementById('autoCenter').checked = uiSettings.autoCenter;
        }
        if (typeof uiSettings.autoPreviewGeneration === 'boolean') {
            document.getElementById('autoPreviewGeneration').checked = uiSettings.autoPreviewGeneration;
        }
        const hideSourceSvg = typeof uiSettings.hideSourceSvg === 'boolean'
            ? uiSettings.hideSourceSvg
            : uiSettings.hideSvgDuringSimulation;
        if (typeof hideSourceSvg === 'boolean') {
            document.getElementById('hideSourceSvg').checked = hideSourceSvg;
        }
        if (typeof uiSettings.showBrightnessCutoffPreview === 'boolean') {
            document.getElementById('showBrightnessCutoffPreview').checked = uiSettings.showBrightnessCutoffPreview;
        }
        const sectionStates = uiSettings.sectionStates || {};
        COLLAPSIBLE_SECTION_IDS.forEach(id => {
            const section = document.getElementById(id);
            if (section && typeof sectionStates[id] === 'boolean') section.open = sectionStates[id];
        });
    } catch (error) {
        console.warn('Unable to restore saved Hatch Plotter settings:', error);
    }
}

function saveMachineSettings() {
    try {
        const settings = {};
        MACHINE_SETTING_IDS.forEach(id => {
            settings[id] = document.getElementById(id).value;
        });
        localStorage.setItem(MACHINE_SETTINGS_KEY, JSON.stringify(settings));
        const status = document.getElementById('machineSettingsStatus');
        status.textContent = 'Machine settings saved in this browser.';
        window.clearTimeout(saveMachineSettings.statusTimer);
        saveMachineSettings.statusTimer = window.setTimeout(() => {
            status.textContent = 'Changes are saved automatically.';
        }, 1800);
    } catch (error) {
        console.warn('Unable to save Hatch Plotter machine settings:', error);
        document.getElementById('machineSettingsStatus').textContent = 'Browser storage is unavailable; settings were not saved.';
    }
}

function saveUiSettings() {
    try {
        const sectionStates = {};
        COLLAPSIBLE_SECTION_IDS.forEach(id => {
            const section = document.getElementById(id);
            if (section) sectionStates[id] = section.open;
        });
        localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify({
            autoCenter: document.getElementById('autoCenter').checked,
            autoPreviewGeneration: document.getElementById('autoPreviewGeneration').checked,
            hideSourceSvg: document.getElementById('hideSourceSvg').checked,
            showBrightnessCutoffPreview: document.getElementById('showBrightnessCutoffPreview').checked,
            sectionStates
        }));
    } catch (error) {
        console.warn('Unable to save Hatch Plotter UI settings:', error);
    }
}

function syncSourceSvgVisibility() {
    if (!workingSVG) return;
    workingSVG.visible = !document.getElementById('hideSourceSvg').checked;
}

function clearBrightnessCutoffPreview(message = 'Enable the preview to highlight excluded SVG pixels in red.') {
    brightnessCutoffPreviewToken += 1;
    if (brightnessCutoffOverlay) {
        brightnessCutoffOverlay.remove();
        brightnessCutoffOverlay = null;
    }
    const status = document.getElementById('brightnessCutoffPreviewStatus');
    if (status) status.textContent = message;
}

function scheduleBrightnessCutoffPreview(delay = 120) {
    window.clearTimeout(brightnessCutoffPreviewTimer);
    brightnessCutoffPreviewTimer = window.setTimeout(() => {
        refreshBrightnessCutoffPreview().catch(error => {
            console.error('Unable to render brightness cutoff preview:', error);
            clearBrightnessCutoffPreview(`Cutoff preview unavailable: ${error.message}`);
        });
    }, Math.max(0, delay));
}

async function refreshBrightnessCutoffPreview() {
    const enabled = document.getElementById('showBrightnessCutoffPreview').checked;
    if (!enabled) {
        clearBrightnessCutoffPreview();
        return;
    }
    if (!originalSVGImage || !sourceSvgSizeMm) {
        clearBrightnessCutoffPreview('Load an SVG to preview brightness-cutoff exclusions.');
        return;
    }

    const token = ++brightnessCutoffPreviewToken;
    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const maximumDimension = 1400;
    const pixelsPerMm = Math.max(0.25, Math.min(4, maximumDimension / bedX, maximumDimension / bedY));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.ceil(bedX * pixelsPerMm));
    canvas.height = Math.max(1, Math.ceil(bedY * pixelsPerMm));
    const context = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
    if (!context) throw new Error('The browser could not create the cutoff-preview canvas.');

    context.clearRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';

    const scale = (Number.parseFloat(document.getElementById('svgScale').value) || 100) / 100;
    const rotation = (Number.parseFloat(document.getElementById('svgRotate').value) || 0) * Math.PI / 180;
    const posX = Number.parseFloat(document.getElementById('svgPosX').value) || 0;
    const posY = Number.parseFloat(document.getElementById('svgPosY').value) || 0;
    const sourceWidthPixels = sourceSvgSizeMm.width * pixelsPerMm;
    const sourceHeightPixels = sourceSvgSizeMm.height * pixelsPerMm;

    context.save();
    context.translate(posX * pixelsPerMm, posY * pixelsPerMm);
    context.rotate(rotation);
    context.scale(scale, scale);
    context.drawImage(
        originalSVGImage,
        -sourceWidthPixels / 2,
        -sourceHeightPixels / 2,
        sourceWidthPixels,
        sourceHeightPixels
    );
    context.restore();

    let imageData;
    try {
        imageData = context.getImageData(0, 0, canvas.width, canvas.height);
    } catch (error) {
        throw new Error('Embed cross-origin SVG images as data URLs before using the cutoff preview.');
    }

    const cutoff = Math.max(0, Math.min(1, Number.parseFloat(document.getElementById('brightnessCutoff').value) || 0));
    const densityFudge = Math.max(-0.5, Math.min(0.5, Number.parseFloat(document.getElementById('densityFudge').value) || 0));
    const data = imageData.data;
    let sourcePixels = 0;
    let excludedPixels = 0;

    for (let index = 0; index < data.length; index += 4) {
        const alpha = data[index + 3] / 255;
        if (alpha <= 0.01) {
            data[index + 3] = 0;
            continue;
        }

        sourcePixels += 1;
        const luminance = (
            (0.2126 * data[index]) +
            (0.7152 * data[index + 1]) +
            (0.0722 * data[index + 2])
        ) / 255;
        const darkness = Math.max(0, Math.min(1, (1 - luminance) * alpha * (1 + densityFudge)));
        if (darkness < cutoff) {
            data[index] = 255;
            data[index + 1] = 42;
            data[index + 2] = 42;
            data[index + 3] = Math.max(80, Math.round(190 * alpha));
            excludedPixels += 1;
        } else {
            data[index + 3] = 0;
        }
    }

    context.putImageData(imageData, 0, 0);
    if (token !== brightnessCutoffPreviewToken || !document.getElementById('showBrightnessCutoffPreview').checked) return;

    if (brightnessCutoffOverlay) brightnessCutoffOverlay.remove();
    brightnessCutoffOverlay = new paper.Raster(canvas);
    brightnessCutoffOverlay.name = 'brightnessCutoffOverlay';
    brightnessCutoffOverlay.size = new paper.Size(bedX, bedY);
    brightnessCutoffOverlay.position = new paper.Point(bedX / 2, bedY / 2);
    if (workingSVG) brightnessCutoffOverlay.insertAbove(workingSVG);
    else if (machineBed) brightnessCutoffOverlay.insertAbove(machineBed);

    allGeneratedPaths.forEach(path => path.bringToFront());
    if (patternCenterMarker) patternCenterMarker.bringToFront();
    if (penHead) penHead.bringToFront();

    const excludedPercent = sourcePixels > 0 ? (excludedPixels / sourcePixels) * 100 : 0;
    const status = document.getElementById('brightnessCutoffPreviewStatus');
    if (status) {
        status.textContent = `${formatNumber(excludedPercent, 1)}% of visible SVG pixels are below the current cutoff and are shown in red.`;
    }
}

function clampBestGuessValue(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
}

function histogramPercentile(histogram, total, percentile) {
    if (!total) return 0;
    const target = clampBestGuessValue(percentile, 0, 1) * total;
    let cumulative = 0;
    for (let index = 0; index < histogram.length; index += 1) {
        cumulative += histogram[index];
        if (cumulative >= target) return index / (histogram.length - 1);
    }
    return 1;
}

function renderBestGuessAnalysisCanvas(maximumDimension = 1200) {
    if (!originalSVGImage || !sourceSvgSizeMm) {
        throw new Error('Load an SVG before running Best Guess.');
    }

    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const pixelsPerMm = Math.max(0.3, Math.min(4, maximumDimension / bedX, maximumDimension / bedY));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.ceil(bedX * pixelsPerMm));
    canvas.height = Math.max(1, Math.ceil(bedY * pixelsPerMm));
    const context = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
    if (!context) throw new Error('The browser could not create the Best Guess analysis canvas.');

    context.clearRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';

    const scale = (Number.parseFloat(document.getElementById('svgScale').value) || 100) / 100;
    const rotation = (Number.parseFloat(document.getElementById('svgRotate').value) || 0) * Math.PI / 180;
    const posX = Number.parseFloat(document.getElementById('svgPosX').value) || 0;
    const posY = Number.parseFloat(document.getElementById('svgPosY').value) || 0;
    const sourceWidthPixels = sourceSvgSizeMm.width * pixelsPerMm;
    const sourceHeightPixels = sourceSvgSizeMm.height * pixelsPerMm;

    context.save();
    context.translate(posX * pixelsPerMm, posY * pixelsPerMm);
    context.rotate(rotation);
    context.scale(scale, scale);
    context.drawImage(
        originalSVGImage,
        -sourceWidthPixels / 2,
        -sourceHeightPixels / 2,
        sourceWidthPixels,
        sourceHeightPixels
    );
    context.restore();

    return { canvas, context, pixelsPerMm };
}

function analyzeArtworkForBestGuess(canvas, context, pixelsPerMm, penThickness) {
    let imageData;
    try {
        imageData = context.getImageData(0, 0, canvas.width, canvas.height);
    } catch (error) {
        throw new Error('Embed cross-origin SVG images as data URLs before using Best Guess.');
    }

    const width = canvas.width;
    const height = canvas.height;
    const pixels = width * height;
    const darknessMap = new Float32Array(pixels);
    const histogram = new Uint32Array(256);
    const data = imageData.data;
    let activePixels = 0;
    let darknessSum = 0;
    let minX = width;
    let minY = height;
    let maxX = -1;
    let maxY = -1;

    for (let pixelIndex = 0, dataIndex = 0; pixelIndex < pixels; pixelIndex += 1, dataIndex += 4) {
        const alpha = data[dataIndex + 3] / 255;
        if (alpha <= 0.01) continue;
        const luminance = (
            (0.2126 * data[dataIndex]) +
            (0.7152 * data[dataIndex + 1]) +
            (0.0722 * data[dataIndex + 2])
        ) / 255;
        const darkness = clampBestGuessValue((1 - luminance) * alpha, 0, 1);
        darknessMap[pixelIndex] = darkness;
        if (darkness <= (1 / 255)) continue;

        const x = pixelIndex % width;
        const y = Math.floor(pixelIndex / width);
        activePixels += 1;
        darknessSum += darkness;
        histogram[Math.max(1, Math.min(255, Math.round(darkness * 255)))] += 1;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
    }

    if (activePixels < 16 || maxX < minX || maxY < minY) {
        throw new Error('The enabled SVG layers do not contain enough visible non-white artwork to analyze.');
    }

    const p04 = histogramPercentile(histogram, activePixels, 0.04);
    const p08 = histogramPercentile(histogram, activePixels, 0.08);
    const p25 = histogramPercentile(histogram, activePixels, 0.25);
    const p50 = histogramPercentile(histogram, activePixels, 0.50);
    const p90 = histogramPercentile(histogram, activePixels, 0.90);
    const contrast = clampBestGuessValue(p90 - p04, 0, 1);
    const meanDarkness = darknessSum / activePixels;

    // Reject only the lowest light tail. The lower-quartile cap prevents a pale drawing
    // from being mistaken for background noise, while the floor removes raster/AA haze.
    const cutoff = clampBestGuessValue(
        Math.max(0.015, Math.min(p08 * 0.88, p25 * 0.48)),
        0.015,
        0.18
    );

    let belowCutoff = 0;
    const cutoffBin = Math.max(0, Math.min(255, Math.floor(cutoff * 255)));
    for (let index = 0; index <= cutoffBin; index += 1) belowCutoff += histogram[index];
    const excludedFraction = belowCutoff / activePixels;

    const orientationBins = new Float64Array(18);
    let orientationWeight = 0;
    let edgeSamples = 0;
    let testedSamples = 0;
    const sampleStep = Math.max(1, Math.round(Math.max(width, height) / 650));
    const xStart = Math.max(1, minX + 1);
    const xEnd = Math.min(width - 2, maxX - 1);
    const yStart = Math.max(1, minY + 1);
    const yEnd = Math.min(height - 2, maxY - 1);

    for (let y = yStart; y <= yEnd; y += sampleStep) {
        for (let x = xStart; x <= xEnd; x += sampleStep) {
            const index = (y * width) + x;
            const center = darknessMap[index];
            const left = darknessMap[index - 1];
            const right = darknessMap[index + 1];
            const above = darknessMap[index - width];
            const below = darknessMap[index + width];
            if (Math.max(center, left, right, above, below) <= cutoff * 0.45) continue;

            testedSamples += 1;
            const gradientX = (right - left) * 0.5;
            const gradientY = (below - above) * 0.5;
            const gradient = Math.hypot(gradientX, gradientY);
            if (gradient < 0.018) continue;

            edgeSamples += 1;
            let tangentAngle = Math.atan2(gradientY, gradientX) + (Math.PI / 2);
            while (tangentAngle < 0) tangentAngle += Math.PI;
            while (tangentAngle >= Math.PI) tangentAngle -= Math.PI;
            const bin = Math.min(orientationBins.length - 1, Math.floor((tangentAngle / Math.PI) * orientationBins.length));
            orientationBins[bin] += gradient;
            orientationWeight += gradient;
        }
    }

    const edgeDensity = testedSamples ? edgeSamples / testedSamples : 0;
    const detailScore = clampBestGuessValue((edgeDensity * 3.2) + (contrast * 0.45), 0, 1);
    let occupiedToneBins = 0;
    const occupiedThreshold = Math.max(1, activePixels * 0.001);
    for (let index = 1; index < histogram.length; index += 1) {
        if (histogram[index] >= occupiedThreshold) occupiedToneBins += 1;
    }
    const tonalComplexity = clampBestGuessValue(occupiedToneBins / 72, 0, 1);

    let dominantBin = 0;
    for (let index = 1; index < orientationBins.length; index += 1) {
        if (orientationBins[index] > orientationBins[dominantBin]) dominantBin = index;
    }
    const orientationCoherence = orientationWeight > 0 ? orientationBins[dominantBin] / orientationWeight : 0;
    const artworkWidth = Math.max(1, maxX - minX + 1);
    const artworkHeight = Math.max(1, maxY - minY + 1);
    let patternAngle;
    if (orientationCoherence >= 0.16) {
        const dominantTangent = ((dominantBin + 0.5) / orientationBins.length) * 180;
        patternAngle = (dominantTangent + 45) % 180;
    } else if (artworkWidth > artworkHeight * 1.35) {
        patternAngle = 35;
    } else if (artworkHeight > artworkWidth * 1.35) {
        patternAngle = 55;
    } else {
        patternAngle = 45;
    }
    patternAngle = Math.round(patternAngle / 5) * 5;

    const noisePenalty = clampBestGuessValue(excludedFraction * 2.5, 0, 0.35);
    const spacingMultiplier = clampBestGuessValue(1.58 - (detailScore * 0.34) + noisePenalty, 1.12, 1.82);
    const patternSpacing = Math.max(penThickness * 1.05, penThickness * spacingMultiplier);
    const waveform = tonalComplexity >= 0.48 ? 'sine' : 'zigzag';
    const waveAmplitude = clampBestGuessValue(
        patternSpacing * (waveform === 'sine' ? 0.42 : 0.50) * (0.82 + (detailScore * 0.18)),
        penThickness * 0.25,
        patternSpacing * 0.56
    );
    const waveLength = Math.max(
        penThickness * 4,
        patternSpacing * (5.4 - (detailScore * 1.9))
    );
    const densityFudge = clampBestGuessValue((0.42 - meanDarkness) * 0.16, -0.08, 0.08);
    const brightnessModulation = excludedFraction > 0.12
        ? 'amplitude'
        : (tonalComplexity >= 0.36 ? 'both' : 'amplitude');

    return {
        cutoff,
        densityFudge,
        patternLayout: 'linear',
        waveform,
        patternSpacing,
        patternAngle,
        patternCenterX: ((minX + maxX) / 2) / pixelsPerMm,
        patternCenterY: ((minY + maxY) / 2) / pixelsPerMm,
        waveAmplitude,
        waveLength,
        brightnessModulation,
        excludedFraction,
        detailScore,
        tonalComplexity,
        activePixels,
    };
}

async function applyBestGuessSettings() {
    const button = document.getElementById('bestGuessBtn');
    const status = document.getElementById('bestGuessStatus');
    if (!button || !status) return;

    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = 'Analyzing SVG...';
    status.textContent = 'Rasterizing the enabled layers and measuring tone, edge detail, and the light-noise tail...';

    try {
        if (!layerEntries.some(layer => layer.enabled)) {
            throw new Error('Enable at least one SVG layer before running Best Guess.');
        }

        const penInput = document.getElementById('penThickness');
        let penThickness = Number.parseFloat(penInput.value);
        let assumedPen = false;
        if (!Number.isFinite(penThickness) || penThickness < 0.05 || penThickness > 10) {
            penThickness = 0.5;
            penInput.value = '0.5';
            assumedPen = true;
        }

        const rendered = renderBestGuessAnalysisCanvas();
        const guess = analyzeArtworkForBestGuess(
            rendered.canvas,
            rendered.context,
            rendered.pixelsPerMm,
            penThickness
        );

        document.getElementById('brightnessCutoff').value = formatNumber(guess.cutoff, 3);
        document.getElementById('densityFudge').value = formatNumber(guess.densityFudge, 3);
        document.getElementById('patternLayout').value = guess.patternLayout;
        document.getElementById('waveform').value = guess.waveform;
        document.getElementById('patternSpacing').value = formatNumber(guess.patternSpacing, 3);
        document.getElementById('patternAngle').value = formatNumber(guess.patternAngle, 1);
        document.getElementById('patternCenterX').value = formatNumber(guess.patternCenterX, 3);
        document.getElementById('patternCenterY').value = formatNumber(guess.patternCenterY, 3);
        document.getElementById('patternClockwise').checked = true;
        document.getElementById('waveAmplitude').value = formatNumber(guess.waveAmplitude, 3);
        document.getElementById('waveLength').value = formatNumber(guess.waveLength, 3);
        document.getElementById('brightnessModulation').value = guess.brightnessModulation;
        document.getElementById('showBrightnessCutoffPreview').checked = true;

        updatePatternControlVisibility();
        updatePatternCenterMarker();
        saveMachineSettings();
        saveUiSettings();
        await refreshBrightnessCutoffPreview();

        const assumedText = assumedPen ? ' Assumed a 0.5 mm pen because no valid pen size was set.' : '';
        status.textContent = `Applied ${guess.waveform} ${guess.patternLayout} at ${formatNumber(guess.patternAngle, 0)}°, cutoff ${formatNumber(guess.cutoff, 3)}, spacing ${formatNumber(guess.patternSpacing, 2)} mm, amplitude ${formatNumber(guess.waveAmplitude, 2)} mm, and wavelength ${formatNumber(guess.waveLength, 2)} mm. Estimated detail ${formatNumber(guess.detailScore * 100, 0)}%; light-tail exclusion ${formatNumber(guess.excludedFraction * 100, 1)}%.${assumedText}`;
        document.getElementById('generationStatus').textContent = 'Best Guess settings applied. Review the red cutoff preview, then generate the toolpath.';
    } catch (error) {
        console.error('Best Guess analysis failed:', error);
        status.textContent = `Best Guess unavailable: ${error.message}`;
        document.getElementById('generationStatus').textContent = `Best Guess failed: ${error.message}`;
    } finally {
        button.disabled = false;
        button.textContent = originalLabel;
    }
}

function updateSimulationControls() {
    const stopButton = document.getElementById('stopSimBtn');
    if (stopButton) stopButton.disabled = !isAnimating;
}

function clearGeneratedPreview(resetGcode = true, resetStopState = true) {
    allGeneratedPaths.forEach(path => path.remove());
    allGeneratedPaths = [];
    generatedPathGcodeRanges = [];
    currentPathIndex = 0;
    currentOffset = 0;
    if (penHead) {
        penHead.remove();
        penHead = null;
    }
    isAnimating = false;
    if (resetStopState) simulationStoppedByUser = false;
    if (resetGcode) setGcodeLines([]);
    updateSimulationControls();
    syncSourceSvgVisibility();
}

function setCenterInputs() {
    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    document.getElementById('svgPosX').value = formatNumber(bedX / 2, 3);
    document.getElementById('svgPosY').value = formatNumber(bedY / 2, 3);
}

function centerSvgInWorkspace(apply = true) {
    setCenterInputs();
    if (apply) applyTransforms();
}


function updatePatternCenterMarker() {
    const x = Number.parseFloat(document.getElementById('patternCenterX').value);
    const y = Number.parseFloat(document.getElementById('patternCenterY').value);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (patternCenterMarker) patternCenterMarker.remove();
    patternCenterMarker = new paper.Group({ name: 'patternCenterMarker' });
    const radius = Math.max(2.5, readPositiveNumber('penThickness', 0.5) * 2);
    const headCenter = new paper.Point(x, y - (radius * 1.7));
    patternCenterMarker.addChild(new paper.Path.Line({
        from: headCenter.add([0, radius * 0.85]),
        to: [x, y],
        strokeColor: '#f6c85f',
        strokeWidth: 1.4
    }));
    patternCenterMarker.addChild(new paper.Path.Circle({
        center: headCenter,
        radius,
        fillColor: '#f6c85f',
        strokeColor: '#5f4300',
        strokeWidth: 0.8
    }));
    patternCenterMarker.addChild(new paper.Path.Circle({
        center: headCenter,
        radius: radius * 0.34,
        fillColor: '#5f4300'
    }));
    patternCenterMarker.addChild(new paper.Path.Circle({
        center: [x, y],
        radius: Math.max(0.6, radius * 0.18),
        fillColor: '#f6c85f',
        strokeColor: '#5f4300',
        strokeWidth: 0.5
    }));
    patternCenterMarker.bringToFront();
}

function setPatternCenterPicking(active) {
    pickingPatternCenter = Boolean(active);
    const button = document.getElementById('pickPatternCenterBtn');
    const canvas = document.getElementById('canvas');
    button.classList.toggle('is-active', pickingPatternCenter);
    button.setAttribute('aria-pressed', pickingPatternCenter ? 'true' : 'false');
    button.textContent = pickingPatternCenter ? 'Click Canvas to Drop Pin' : 'Pick Center on Canvas';
    canvas.classList.toggle('picking-pattern-center', pickingPatternCenter);
    if (pickingPatternCenter) {
        document.getElementById('generationStatus').textContent = 'Click inside the machine workspace to drop the pattern-center pin. Press Escape to cancel.';
    }
}

function setPatternCenterToWorkspace() {
    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    document.getElementById('patternCenterX').value = formatNumber(bedX / 2, 3);
    document.getElementById('patternCenterY').value = formatNumber(bedY / 2, 3);
    setPatternCenterPicking(false);
    saveMachineSettings();
    updatePatternCenterMarker();
    document.getElementById('generationStatus').textContent = 'Pattern center set to the workspace center.';
}

function updatePatternControlVisibility() {
    const layout = document.getElementById('patternLayout').value;
    const waveform = document.getElementById('waveform').value;
    document.getElementById('patternAngleGroup').style.display = ['linear', 'radial'].includes(layout) ? '' : 'none';
    document.getElementById('patternDirectionGroup').style.display = ['spiral', 'concentric', 'radial'].includes(layout) ? 'flex' : 'none';
    document.getElementById('waveformControls').style.display = waveform === 'straight' ? 'none' : 'flex';
}

// --- WORKSPACE INITIALIZATION ---
function updateZoomUi() {
    const slider = document.getElementById('zoomSlider');
    const label = document.getElementById('zoomLabel');
    if (slider) slider.value = String(Math.round(canvasZoomPercent));
    if (label) label.textContent = `${Math.round(canvasZoomPercent)}%`;
}

function setCanvasZoomPercent(percent, viewAnchor = null) {
    const bounded = Math.max(25, Math.min(800, Number(percent) || 100));
    const anchorPoint = viewAnchor ? paper.view.viewToProject(viewAnchor) : null;
    canvasZoomPercent = bounded;
    paper.view.zoom = workspaceFitZoom * (canvasZoomPercent / 100);
    if (viewAnchor && anchorPoint) {
        const newAnchorPoint = paper.view.viewToProject(viewAnchor);
        paper.view.center = paper.view.center.add(anchorPoint.subtract(newAnchorPoint));
    }
    updateZoomUi();
}

function initWorkspace(resetView = false) {
    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);

    if (machineBed) machineBed.remove();
    machineBed = new paper.Path.Rectangle({
        point: [0, 0],
        size: [bedX, bedY],
        strokeColor: '#007bff',
        strokeWidth: 2,
        dashArray: [5, 5],
        name: 'machineBed'
    });

    if (originMarker) originMarker.remove();
    originMarker = new paper.Path.Circle({
        center: [0, 0],
        radius: 4,
        fillColor: 'red',
        name: 'originMarker'
    });

    const pad = 40;
    const scaleX = paper.view.viewSize.width / (bedX + pad);
    const scaleY = paper.view.viewSize.height / (bedY + pad);
    workspaceFitZoom = Math.min(scaleX, scaleY);
    if (resetView) canvasZoomPercent = 100;
    paper.view.zoom = workspaceFitZoom * (canvasZoomPercent / 100);
    if (resetView || !paper.view.center) {
        paper.view.center = new paper.Point(bedX / 2, bedY / 2);
    }
    updateZoomUi();
    updatePatternCenterMarker();
    scheduleBrightnessCutoffPreview();
}

function getRotatedDimensions(width, height, rotationDegrees) {
    const radians = (rotationDegrees * Math.PI) / 180;
    const cosine = Math.abs(Math.cos(radians));
    const sine = Math.abs(Math.sin(radians));
    return {
        width: (width * cosine) + (height * sine),
        height: (width * sine) + (height * cosine)
    };
}

function calculateFitScalePercent() {
    if (!originalSVG) return 100;

    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const rotation = Number.parseFloat(document.getElementById('svgRotate').value) || 0;
    const rotated = getRotatedDimensions(
        Math.max(originalSVG.bounds.width, 0.0001),
        Math.max(originalSVG.bounds.height, 0.0001),
        rotation
    );

    return Math.min(
        (bedX * FIT_MARGIN) / rotated.width,
        (bedY * FIT_MARGIN) / rotated.height
    ) * 100;
}

function getSvgSizeAtScale(scalePercent = 100) {
    if (!originalSVG) return null;
    const rotation = Number.parseFloat(document.getElementById('svgRotate').value) || 0;
    const rotated = getRotatedDimensions(
        originalSVG.bounds.width,
        originalSVG.bounds.height,
        rotation
    );
    const scale = scalePercent / 100;
    return { width: rotated.width * scale, height: rotated.height * scale };
}

function offerAutoScaleToFit(reason = 'upload') {
    if (!originalSVG) return false;

    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const scaleToCheck = reason === 'upload'
        ? 100
        : (Number.parseFloat(document.getElementById('svgScale').value) || 100);
    const size = getSvgSizeAtScale(scaleToCheck);
    if (!size || (size.width <= bedX && size.height <= bedY)) return false;

    const fitScale = calculateFitScalePercent();
    const context = reason === 'upload'
        ? 'At 100% scale, this SVG'
        : 'With the updated machine limits, the SVG';
    const accepted = window.confirm(
        `${context} is ${formatNumber(size.width)} × ${formatNumber(size.height)} mm, ` +
        `which is larger than the ${formatNumber(bedX)} × ${formatNumber(bedY)} mm workspace.\n\n` +
        `Auto-scale it to ${formatNumber(fitScale)}% so it fits with a 5% margin on each side?`
    );

    if (accepted) {
        document.getElementById('svgScale').value = formatNumber(fitScale, 3);
        document.getElementById('autoCenter').checked = true;
        saveUiSettings();
        centerSvgInWorkspace(false);
        applyTransforms();
        document.getElementById('generationStatus').textContent =
            `SVG auto-scaled to ${formatNumber(fitScale)}% and centered.`;
    } else {
        const currentScale = Number.parseFloat(document.getElementById('svgScale').value) || 100;
        const currentSize = getSvgSizeAtScale(currentScale);
        const stillOversized = currentSize && (currentSize.width > bedX || currentSize.height > bedY);
        document.getElementById('generationStatus').textContent = stillOversized
            ? 'Auto-scale declined; geometry outside the machine limits will be clipped.'
            : `Auto-scale declined; the existing ${formatNumber(currentScale)}% scale was retained.`;
    }
    return true;
}

restoreStoredSettings();
if (document.getElementById('autoCenter').checked) setCenterInputs();
initWorkspace(true);

window.addEventListener('resize', () => initWorkspace(false));

const canvasShell = document.querySelector('.canvas-shell');
if (canvasShell && typeof ResizeObserver !== 'undefined') {
    const canvasResizeObserver = new ResizeObserver(entries => {
        const bounds = entries[0]?.contentRect;
        if (!bounds || bounds.width < 1 || bounds.height < 1) return;
        const width = Math.max(1, Math.round(bounds.width));
        const height = Math.max(1, Math.round(bounds.height));
        if (paper.view.viewSize.width === width && paper.view.viewSize.height === height) return;
        paper.view.viewSize = new paper.Size(width, height);
        initWorkspace(false);
    });
    canvasResizeObserver.observe(canvasShell);
}

['bedX', 'bedY'].forEach(id => {
    const input = document.getElementById(id);
    input.addEventListener('input', () => {
        saveMachineSettings();
        initWorkspace();
        if (document.getElementById('autoCenter').checked) setCenterInputs();
        applyTransforms();
    });
    input.addEventListener('change', () => offerAutoScaleToFit('machine-limit-change'));
});

MACHINE_SETTING_IDS.filter(id => !['bedX', 'bedY'].includes(id)).forEach(id => {
    const input = document.getElementById(id);
    input.addEventListener('input', saveMachineSettings);
    input.addEventListener('change', saveMachineSettings);
});

document.getElementById('autoCenter').addEventListener('change', event => {
    saveUiSettings();
    if (event.target.checked) centerSvgInWorkspace();
});

document.getElementById('centerSvgBtn').addEventListener('click', () => {
    document.getElementById('autoCenter').checked = true;
    saveUiSettings();
    centerSvgInWorkspace();
});

document.getElementById('enableAllLayersBtn').addEventListener('click', () => setAllLayersEnabled(true));
document.getElementById('disableAllLayersBtn').addEventListener('click', () => setAllLayersEnabled(false));
document.getElementById('autoPreviewGeneration').addEventListener('change', () => {
    saveUiSettings();
    syncSourceSvgVisibility();
});
document.getElementById('hideSourceSvg').addEventListener('change', () => {
    saveUiSettings();
    syncSourceSvgVisibility();
});
document.getElementById('showBrightnessCutoffPreview').addEventListener('change', () => {
    saveUiSettings();
    scheduleBrightnessCutoffPreview(0);
});
document.getElementById('bestGuessBtn').addEventListener('click', applyBestGuessSettings);
document.getElementById('centerPatternBtn').addEventListener('click', setPatternCenterToWorkspace);
document.getElementById('pickPatternCenterBtn').addEventListener('click', () => {
    setPatternCenterPicking(!pickingPatternCenter);
});
['patternCenterX', 'patternCenterY'].forEach(id => {
    document.getElementById(id).addEventListener('input', updatePatternCenterMarker);
});
['brightnessCutoff', 'densityFudge'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => scheduleBrightnessCutoffPreview());
});
COLLAPSIBLE_SECTION_IDS.forEach(id => {
    const section = document.getElementById(id);
    if (!section) return;
    section.addEventListener('toggle', () => {
        saveUiSettings();
        if (id === 'gcodeSection') window.setTimeout(() => initWorkspace(false), 0);
    });
});
document.getElementById('zoomOutBtn').addEventListener('click', () => setCanvasZoomPercent(canvasZoomPercent / 1.25));
document.getElementById('zoomInBtn').addEventListener('click', () => setCanvasZoomPercent(canvasZoomPercent * 1.25));
document.getElementById('zoomFitBtn').addEventListener('click', () => {
    paper.view.center = new paper.Point(readPositiveNumber('bedX', 210) / 2, readPositiveNumber('bedY', 297) / 2);
    setCanvasZoomPercent(100);
});
document.getElementById('zoomSlider').addEventListener('input', event => setCanvasZoomPercent(event.target.value));
document.getElementById('canvas').addEventListener('wheel', event => {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const anchor = new paper.Point(event.clientX - rect.left, event.clientY - rect.top);
    const factor = event.deltaY < 0 ? 1.12 : (1 / 1.12);
    setCanvasZoomPercent(canvasZoomPercent * factor, anchor);
}, { passive: false });
document.getElementById('canvas').addEventListener('pointerdown', event => {
    if (!pickingPatternCenter) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const viewPoint = new paper.Point(event.clientX - rect.left, event.clientY - rect.top);
    const projectPoint = paper.view.viewToProject(viewPoint);
    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const x = Math.max(0, Math.min(bedX, projectPoint.x));
    const y = Math.max(0, Math.min(bedY, projectPoint.y));
    document.getElementById('patternCenterX').value = formatNumber(x, 3);
    document.getElementById('patternCenterY').value = formatNumber(y, 3);
    saveMachineSettings();
    updatePatternCenterMarker();
    setPatternCenterPicking(false);
    document.getElementById('generationStatus').textContent = `Pattern center pinned at X${formatNumber(x, 2)}, Y${formatNumber(y, 2)} mm.`;
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && pickingPatternCenter) {
        setPatternCenterPicking(false);
        document.getElementById('generationStatus').textContent = 'Pattern-center selection cancelled.';
    }
});
renderLayerControls();

// --- FILE UPLOAD ---
document.getElementById('svgInput').addEventListener('change', function(event) {
    const file = event.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function(loadEvent) {
        if (originalSVG) originalSVG.remove();
        if (workingSVG) workingSVG.remove();
        if (originalSVGObjectUrl) URL.revokeObjectURL(originalSVGObjectUrl);
        clearGeneratedPreview();

        originalSVGText = String(loadEvent.target.result || '');
        try {
            const layerData = detectSvgLayers(originalSVGText);
            originalSvgDocument = layerData.documentNode;
            layerEntries = layerData.layers;
            renderLayerControls();
        } catch (error) {
            originalSvgDocument = null;
            layerEntries = [];
            renderLayerControls();
            generationStatus.textContent = error.message;
            alert(error.message);
            return;
        }

        const temporaryUrl = URL.createObjectURL(new Blob([originalSVGText], { type: 'image/svg+xml' }));
        const image = new Image();

        image.onload = async function() {
            sourceSvgSizeMm = determineSvgPhysicalSize(originalSVGText, image);
            if (document.getElementById('autoCenter').checked) setCenterInputs();
            await reloadPreviewFromLayerSelection(true);
            const prompted = offerAutoScaleToFit('upload');
            if (!prompted) {
                generationStatus.textContent = `Loaded SVG at ${formatNumber(sourceSvgSizeMm.width)} × ${formatNumber(sourceSvgSizeMm.height)} mm.`;
            }
            URL.revokeObjectURL(temporaryUrl);
        };

        image.onerror = function() {
            URL.revokeObjectURL(temporaryUrl);
            originalSVGImage = null;
            sourceSvgSizeMm = null;
            generationStatus.textContent = 'Unable to render the uploaded SVG in the browser.';
            alert('The uploaded SVG could not be rendered. Check it for invalid XML or inaccessible external images.');
        };
        image.src = temporaryUrl;
    };
    reader.readAsText(file);
});

// --- SPATIAL TRANSFORMATIONS ---
['svgScale', 'svgRotate'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
        if (document.getElementById('autoCenter').checked) setCenterInputs();
        applyTransforms();
    });
});

['svgPosX', 'svgPosY'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
        document.getElementById('autoCenter').checked = false;
        saveUiSettings();
        applyTransforms();
    });
});

function applyTransforms() {
    if (!originalSVG) return;

    if (workingSVG) workingSVG.remove();
    clearGeneratedPreview();

    workingSVG = originalSVG.clone();
    workingSVG.visible = true;
    workingSVG.opacity = 0.5;

    const scalePercent = Number.parseFloat(document.getElementById('svgScale').value) || 100;
    const rotation = Number.parseFloat(document.getElementById('svgRotate').value) || 0;
    const posX = Number.parseFloat(document.getElementById('svgPosX').value) || 0;
    const posY = Number.parseFloat(document.getElementById('svgPosY').value) || 0;

    // Scale is now the SVG's actual percentage: 100% preserves its imported size.
    // Auto-fit calculates and inserts the percentage required for the active bed.
    const finalScale = scalePercent / 100;

    workingSVG.pivot = workingSVG.bounds.center;
    workingSVG.scale(finalScale);
    workingSVG.rotate(rotation);
    workingSVG.position = new paper.Point(posX, posY);
    syncSourceSvgVisibility();
    updatePatternCenterMarker();
    scheduleBrightnessCutoffPreview();
}

// --- API GENERATION ---
const generateButton = document.getElementById('generateBtn');
const cancelGenerateButton = document.getElementById('cancelGenerateBtn');
const loadingOverlay = document.getElementById('loading');
const loadingMessage = document.getElementById('loadingMessage');
const loadingProgress = document.getElementById('loadingProgress');
const loadingDetails = document.getElementById('loadingDetails');
const generationStatus = document.getElementById('generationStatus');
const gcodeOutput = document.getElementById('gcodeOutput');
const exportGcodeButton = document.getElementById('exportGcodeBtn');
const gcodeLineCount = document.getElementById('gcodeLineCount');

function sleep(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function readApiError(response) {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        const payload = await response.json().catch(() => ({}));
        if (typeof payload.detail === 'string') return payload.detail;
        if (payload.error) return payload.error;
    }
    const text = await response.text().catch(() => '');
    return text || `Request failed with HTTP ${response.status}`;
}

function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return null;
    if (value < 60) return `${Math.max(0, Math.round(value))}s`;
    const minutes = Math.floor(value / 60);
    const remainingSeconds = Math.round(value % 60);
    if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
}

function setLoading(message, percent = null, details = '') {
    loadingMessage.textContent = message;
    if (percent !== null && percent !== undefined && Number.isFinite(Number(percent))) {
        loadingProgress.value = Math.max(0, Math.min(100, Number(percent)));
    } else {
        loadingProgress.removeAttribute('value');
    }
    loadingDetails.textContent = details;
    const useLiveHud = Boolean(activeJobId) && document.getElementById('autoPreviewGeneration').checked;
    loadingOverlay.classList.toggle('live-preview', useLiveHud);
    loadingOverlay.style.display = 'flex';
    updateGenerationButtons(true, Boolean(activeJobId) && !cancelRequested);
}

function showJobProgress(job) {
    const progress = job.progress || {};
    const percent = Number(progress.percent);
    const elapsed = formatDuration(progress.elapsed_seconds);
    const eta = formatDuration(progress.eta_seconds);
    const backend = progress.compute_backend === 'cuda'
        ? 'CUDA GPU'
        : progress.compute_backend === 'numpy-cpu'
            ? 'NumPy CPU'
            : progress.compute_backend === 'geos-cpu'
                ? 'GEOS CPU'
                : null;
    const work = Number(progress.total) > 0
        ? `${Number(progress.completed || 0).toLocaleString()} / ${Number(progress.total).toLocaleString()}`
        : null;
    const previewText = Number(job.preview_total) > 0
        ? `${Number(job.preview_total).toLocaleString()} live preview chunks`
        : null;
    const details = [
        Number.isFinite(percent) ? `${percent.toFixed(1)}%` : null,
        work,
        eta !== null && Number(progress.eta_seconds) > 0 ? `about ${eta} remaining` : null,
        elapsed ? `${elapsed} elapsed` : null,
        previewText,
        backend
    ].filter(Boolean).join(' · ');
    const message = progress.detail || (job.status === 'queued'
        ? 'Waiting for the geometry worker...'
        : 'Generating the toolpath...');
    setLoading(message, Number.isFinite(percent) ? percent : null, details);
    generationStatus.textContent = details ? `${message} ${details}` : message;
}

function updateGenerationButtons(isGenerating, canCancel = false) {
    generateButton.disabled = isGenerating;
    cancelGenerateButton.disabled = !(isGenerating && canCancel);
}

function clearLoading() {
    loadingOverlay.style.display = 'none';
    loadingOverlay.classList.remove('live-preview');
    loadingProgress.removeAttribute('value');
    loadingDetails.textContent = '';
    updateGenerationButtons(false, false);
    updateExportAvailability();
}

function updateExportAvailability() {
    if (!exportGcodeButton) return;
    exportGcodeButton.disabled = Boolean(activeJobId) || gcodeLines.length === 0;
}

function setGcodeLines(lines) {
    gcodeLines = Array.isArray(lines) ? lines.map(line => String(line)) : [];
    gcodeLineOffsets = [];
    let offset = 0;
    for (const line of gcodeLines) {
        gcodeLineOffsets.push(offset);
        offset += line.length + 1;
    }
    gcodeOutput.value = gcodeLines.join('\n');
    if (gcodeLineCount) gcodeLineCount.textContent = `${gcodeLines.length.toLocaleString()} lines`;
    updateExportAvailability();
}

function highlightGcodeLine(lineIndex) {
    if (!Number.isInteger(lineIndex) || lineIndex < 0 || lineIndex >= gcodeLines.length) return;
    const start = gcodeLineOffsets[lineIndex] ?? 0;
    const end = start + gcodeLines[lineIndex].length;
    try {
        if (document.activeElement !== gcodeOutput) gcodeOutput.focus({ preventScroll: true });
        gcodeOutput.setSelectionRange(start, end);
        const lineHeight = Number.parseFloat(window.getComputedStyle(gcodeOutput).lineHeight) || 18;
        gcodeOutput.scrollTop = Math.max(0, (lineIndex * lineHeight) - (gcodeOutput.clientHeight / 2));
    } catch (error) {
        console.debug('Unable to highlight G-code line:', error);
    }
}

function currentGcodeSettings() {
    const sourceFile = document.getElementById('svgInput').files[0];
    return {
        sourceFilename: sourceFile?.name || 'uploaded.svg',
        enabledLayers: getEnabledLayerNames(),
        bedX: readPositiveNumber('bedX', 210),
        bedY: readPositiveNumber('bedY', 297),
        zMode: document.getElementById('zMode').value,
        zUp: document.getElementById('zUp').value,
        zDown: document.getElementById('zDown').value,
        xyFeedRate: Number.parseInt(document.getElementById('xyFeedRate').value, 10) || 2000,
        zPlungeRate: Number.parseInt(document.getElementById('zPlungeRate').value, 10) || 300,
        penThickness: readPositiveNumber('penThickness', 0.5),
        svgScale: Number.parseFloat(document.getElementById('svgScale').value) || 100,
        svgScaleMode: 'absolute',
        svgRotate: Number.parseFloat(document.getElementById('svgRotate').value) || 0,
        svgPosX: Number.parseFloat(document.getElementById('svgPosX').value) || 0,
        svgPosY: Number.parseFloat(document.getElementById('svgPosY').value) || 0,
        densityFudge: Number.parseFloat(document.getElementById('densityFudge').value) || 0,
        brightnessCutoff: Number.parseFloat(document.getElementById('brightnessCutoff').value) || 0,
        patternLayout: document.getElementById('patternLayout').value,
        patternSpacing: readPositiveNumber('patternSpacing', 1),
        patternAngle: Number.parseFloat(document.getElementById('patternAngle').value) || 0,
        patternCenterX: Number.parseFloat(document.getElementById('patternCenterX').value) || 0,
        patternCenterY: Number.parseFloat(document.getElementById('patternCenterY').value) || 0,
        patternClockwise: document.getElementById('patternClockwise').checked,
        waveform: document.getElementById('waveform').value,
        waveAmplitude: Math.max(0, Number.parseFloat(document.getElementById('waveAmplitude').value) || 0),
        waveLength: readPositiveNumber('waveLength', 3),
        brightnessModulation: document.getElementById('brightnessModulation').value,
    };
}

function gcodeCommentValue(value) {
    return String(value ?? '').replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function gcodeFixed(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(3) : '0.000';
}

function buildGcodeHeader(settings, pathCount = null) {
    const layers = settings.enabledLayers?.length
        ? settings.enabledLayers.map(gcodeCommentValue).join(', ')
        : 'All visible layers';
    const direction = settings.patternClockwise ? 'clockwise' : 'counterclockwise';
    const toolpathCount = Number.isInteger(pathCount) ? String(pathCount) : 'live preview';
    return [
        '; HatchPlot generated G-code',
        `; Source SVG: ${gcodeCommentValue(settings.sourceFilename)}`,
        `; Enabled layers: ${layers}`,
        `; Machine bed: ${gcodeFixed(settings.bedX)} x ${gcodeFixed(settings.bedY)} mm`,
        `; Z control: ${gcodeCommentValue(settings.zMode)}; up=${gcodeCommentValue(settings.zUp)}; down=${gcodeCommentValue(settings.zDown)}; plunge=${settings.zPlungeRate} mm/min`,
        `; XY feed rate: ${settings.xyFeedRate} mm/min`,
        `; Pen size: ${gcodeFixed(settings.penThickness)} mm`,
        `; SVG transform: scale=${gcodeFixed(settings.svgScale)}% (${gcodeCommentValue(settings.svgScaleMode)}); rotation=${gcodeFixed(settings.svgRotate)} deg; center=(${gcodeFixed(settings.svgPosX)}, ${gcodeFixed(settings.svgPosY)}) mm`,
        `; Brightness: cutoff=${gcodeFixed(settings.brightnessCutoff)}; density fudge=${Number(settings.densityFudge) >= 0 ? '+' : ''}${gcodeFixed(settings.densityFudge)}; modulation=${gcodeCommentValue(settings.brightnessModulation)}`,
        `; Pattern: layout=${gcodeCommentValue(settings.patternLayout)}; spacing=${gcodeFixed(settings.patternSpacing)} mm; angle=${gcodeFixed(settings.patternAngle)} deg; center=(${gcodeFixed(settings.patternCenterX)}, ${gcodeFixed(settings.patternCenterY)}) mm; direction=${direction}`,
        `; Waveform: type=${gcodeCommentValue(settings.waveform)}; amplitude=${gcodeFixed(settings.waveAmplitude)} mm; wavelength=${gcodeFixed(settings.waveLength)} mm`,
        `; Toolpaths: ${toolpathCount}`,
        '; End HatchPlot header',
        'G21',
        'G90',
        settings.zMode === 'stepper' ? `G0 Z${settings.zUp}` : `M3 S${settings.zUp}`
    ];
}

function appendGcodeForPaths(paths) {
    const settings = currentGcodeSettings();
    const lines = [...gcodeLines];
    for (const path of paths) {
        if (!Array.isArray(path) || path.length < 2) continue;
        const rapidLine = lines.length;
        lines.push(`G0 X${Number(path[0][0]).toFixed(2)} Y${Number(path[0][1]).toFixed(2)}`);
        const penDownLine = lines.length;
        lines.push(settings.zMode === 'stepper'
            ? `G1 Z${settings.zDown} F${settings.zPlungeRate}`
            : `M3 S${settings.zDown}`);
        const moveStartLine = lines.length;
        path.slice(1).forEach((point, pointIndex) => {
            const feed = pointIndex === 0 ? ` F${settings.xyFeedRate}` : '';
            lines.push(`G1 X${Number(point[0]).toFixed(2)} Y${Number(point[1]).toFixed(2)}${feed}`);
        });
        const penUpLine = lines.length;
        lines.push(settings.zMode === 'stepper' ? `G0 Z${settings.zUp}` : `M3 S${settings.zUp}`);
        generatedPathGcodeRanges.push({
            rapidLine,
            penDownLine,
            moveStartLine,
            moveCount: Math.max(0, path.length - 1),
            penUpLine,
        });
    }
    setGcodeLines(lines);
}

function calculateFinalGcodeRanges(paths, headerLineCount = 3) {
    generatedPathGcodeRanges = [];
    let lineIndex = Math.max(0, Number.parseInt(headerLineCount, 10) || 3);
    for (const path of paths) {
        const pointCount = Math.max(0, path.length - 1);
        generatedPathGcodeRanges.push({
            rapidLine: lineIndex,
            penDownLine: lineIndex + 1,
            moveStartLine: lineIndex + 2,
            moveCount: pointCount,
            penUpLine: lineIndex + 2 + pointCount,
        });
        lineIndex += pointCount + 3;
    }
}

function canvasToBlob(canvas) {
    return new Promise((resolve, reject) => {
        try {
            canvas.toBlob(blob => {
                if (blob) resolve(blob);
                else reject(new Error('The browser could not encode the SVG brightness map.'));
            }, 'image/png');
        } catch (error) {
            reject(new Error(
                'The SVG brightness map could not be read. Remove cross-origin external images or embed them as data URLs.'
            ));
        }
    });
}

async function renderBrightnessMap(penThickness) {
    if (!originalSVGImage || !sourceSvgSizeMm) {
        throw new Error('The SVG preview has not finished loading.');
    }

    const bedX = readPositiveNumber('bedX', 210);
    const bedY = readPositiveNumber('bedY', 297);
    const desiredPixelSizeMm = Math.max(0.05, penThickness / 3);
    let pixelsPerMm = 1 / desiredPixelSizeMm;
    pixelsPerMm = Math.min(
        pixelsPerMm,
        MAX_BRIGHTNESS_MAP_DIMENSION / bedX,
        MAX_BRIGHTNESS_MAP_DIMENSION / bedY
    );

    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.min(MAX_BRIGHTNESS_MAP_DIMENSION, Math.ceil(bedX * pixelsPerMm)));
    canvas.height = Math.max(1, Math.min(MAX_BRIGHTNESS_MAP_DIMENSION, Math.ceil(bedY * pixelsPerMm)));
    const context = canvas.getContext('2d', { alpha: false, willReadFrequently: false });
    if (!context) throw new Error('The browser could not create the brightness-map canvas.');

    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';

    const scale = (Number.parseFloat(document.getElementById('svgScale').value) || 100) / 100;
    const rotation = (Number.parseFloat(document.getElementById('svgRotate').value) || 0) * Math.PI / 180;
    const posX = Number.parseFloat(document.getElementById('svgPosX').value) || 0;
    const posY = Number.parseFloat(document.getElementById('svgPosY').value) || 0;
    const sourceWidthPixels = sourceSvgSizeMm.width * pixelsPerMm;
    const sourceHeightPixels = sourceSvgSizeMm.height * pixelsPerMm;

    context.save();
    context.translate(posX * pixelsPerMm, posY * pixelsPerMm);
    context.rotate(rotation);
    context.scale(scale, scale);
    context.drawImage(
        originalSVGImage,
        -sourceWidthPixels / 2,
        -sourceHeightPixels / 2,
        sourceWidthPixels,
        sourceHeightPixels
    );
    context.restore();

    return canvasToBlob(canvas);
}

async function buildGenerationFormData(penThickness) {
    const fileInput = document.getElementById('svgInput');
    const enabledLayerNames = getEnabledLayerNames();
    if (!enabledLayerNames.length) {
        throw new Error('Enable at least one SVG layer before generating a toolpath.');
    }
    const densityFudge = Number.parseFloat(document.getElementById('densityFudge').value);
    if (!Number.isFinite(densityFudge) || densityFudge < -0.5 || densityFudge > 0.5) {
        throw new Error('Density fudge must be between -0.5 and 0.5.');
    }
    const brightnessCutoff = Number.parseFloat(document.getElementById('brightnessCutoff').value);
    if (!Number.isFinite(brightnessCutoff) || brightnessCutoff < 0 || brightnessCutoff > 1) {
        throw new Error('Brightness cutoff must be between 0 and 1.');
    }
    const patternSpacing = Number.parseFloat(document.getElementById('patternSpacing').value);
    const waveAmplitude = Number.parseFloat(document.getElementById('waveAmplitude').value);
    const waveLength = Number.parseFloat(document.getElementById('waveLength').value);
    if (!Number.isFinite(patternSpacing) || patternSpacing < 0.05) throw new Error('Layout spacing must be at least 0.05 mm.');
    if (!Number.isFinite(waveAmplitude) || waveAmplitude < 0) throw new Error('Wave amplitude cannot be negative.');
    if (!Number.isFinite(waveLength) || waveLength < 0.05) throw new Error('Wavelength must be at least 0.05 mm.');

    const brightnessMap = await renderBrightnessMap(penThickness);
    const formData = new FormData();
    const filteredSvg = new Blob([serializeEnabledSvg()], { type: 'image/svg+xml' });
    formData.append('file', filteredSvg, fileInput.files[0]?.name || 'filtered.svg');
    formData.append('brightnessMap', brightnessMap, 'brightness-map.png');
    formData.append('bedX', document.getElementById('bedX').value);
    formData.append('bedY', document.getElementById('bedY').value);
    formData.append('svgScale', document.getElementById('svgScale').value);
    formData.append('svgScaleMode', 'absolute');
    formData.append('svgRotate', document.getElementById('svgRotate').value);
    formData.append('svgPosX', document.getElementById('svgPosX').value);
    formData.append('svgPosY', document.getElementById('svgPosY').value);
    formData.append('zMode', document.getElementById('zMode').value);
    formData.append('zUp', document.getElementById('zUp').value);
    formData.append('zDown', document.getElementById('zDown').value);
    formData.append('xyFeedRate', document.getElementById('xyFeedRate').value);
    formData.append('zPlungeRate', document.getElementById('zPlungeRate').value);
    formData.append('penThickness', formatNumber(penThickness, 4));
    formData.append('densityFudge', formatNumber(densityFudge, 3));
    formData.append('brightnessCutoff', formatNumber(brightnessCutoff, 3));
    formData.append('patternLayout', document.getElementById('patternLayout').value);
    formData.append('waveform', document.getElementById('waveform').value);
    formData.append('patternCenterX', document.getElementById('patternCenterX').value);
    formData.append('patternCenterY', document.getElementById('patternCenterY').value);
    formData.append('patternAngle', document.getElementById('patternAngle').value);
    formData.append('patternSpacing', formatNumber(patternSpacing, 4));
    formData.append('patternClockwise', document.getElementById('patternClockwise').checked ? 'true' : 'false');
    formData.append('waveAmplitude', formatNumber(waveAmplitude, 4));
    formData.append('waveLength', formatNumber(waveLength, 4));
    formData.append('brightnessModulation', document.getElementById('brightnessModulation').value);
    formData.append('enabledLayers', JSON.stringify(enabledLayerNames));
    return formData;
}

function appendPaperPaths(paths, livePreview = false) {
    const firstNewIndex = allGeneratedPaths.length;
    for (const coords of paths) {
        if (!Array.isArray(coords) || coords.length < 2) continue;
        const path = new paper.Path();
        coords.forEach(point => path.add(new paper.Point(point[0], point[1])));
        path.strokeColor = livePreview ? '#ff8c00' : 'red';
        path.strokeWidth = readPositiveNumber('penThickness', 0.5);
        path.dashArray = [path.length, path.length];
        path.dashOffset = path.length;
        allGeneratedPaths.push(path);
    }
    return firstNewIndex;
}

function initializeLivePreview() {
    clearGeneratedPreview(true);
    generatedPathGcodeRanges = [];
    setGcodeLines(buildGcodeHeader(currentGcodeSettings()));
    livePreviewInitialized = true;
    syncSourceSvgVisibility();
}

function consumeLivePreview(job) {
    const autoPreview = document.getElementById('autoPreviewGeneration').checked;
    const chunks = Array.isArray(job.preview) ? job.preview : [];
    const nextCursor = Number.isInteger(job.preview_next) ? job.preview_next : livePreviewCursor;
    if (!autoPreview || chunks.length === 0) {
        livePreviewCursor = nextCursor;
        return;
    }
    if (!livePreviewInitialized) initializeLivePreview();

    for (const chunk of chunks) {
        const paths = Array.isArray(chunk.paths) ? chunk.paths : [];
        if (!paths.length) continue;
        const firstNewIndex = appendPaperPaths(paths, true);
        appendGcodeForPaths(paths);
        if (simulationStoppedByUser) {
            for (let index = firstNewIndex; index < allGeneratedPaths.length; index += 1) {
                allGeneratedPaths[index].dashOffset = 0;
            }
        } else if (!isAnimating) {
            startSimulation(firstNewIndex, false);
        }
    }
    livePreviewCursor = nextCursor;
}

async function waitForGeneration(jobId) {
    let transientFailures = 0;

    while (true) {
        try {
            const previewAfter = Math.max(0, livePreviewCursor);
            const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}?preview_after=${previewAfter}`, {
                headers: { 'Accept': 'application/json' },
                cache: 'no-store'
            });
            if (!response.ok) throw new Error(await readApiError(response));

            transientFailures = 0;
            const job = await response.json();
            consumeLivePreview(job);
            showJobProgress(job);
            if (job.status === 'completed') return job;
            if (job.status === 'failed' || job.status === 'cancelled') {
                throw new Error(job.error || `Generation ${job.status}.`);
            }
            await sleep(500);
        } catch (error) {
            transientFailures += 1;
            if (transientFailures >= 3 || cancelRequested) throw error;
            setLoading('Backend connection interrupted; retrying...', null, `Retry ${transientFailures} of 3`);
            await sleep(1200);
        }
    }
}

function displayGeneratedResult(data) {
    clearGeneratedPreview(false, false);
    setGcodeLines(String(data.gcode || '').split(/\r?\n/));
    calculateFinalGcodeRanges(data.paths || [], data.stats?.gcode_header_lines);
    appendPaperPaths(data.paths || [], false);

    const autoPreview = document.getElementById('autoPreviewGeneration').checked;
    if (autoPreview && allGeneratedPaths.length && !simulationStoppedByUser) {
        startSimulation(0, true);
    } else {
        allGeneratedPaths.forEach(path => { path.dashOffset = 0; });
    }

    const stats = data.stats || {};
    generationStatus.textContent = [
        `${Number(stats.continuous_paths || stats.hatch_paths || data.paths.length).toLocaleString()} continuous paths`,
        stats.scanlines ? `${Number(stats.scanlines).toLocaleString()} brightness scanlines` : null,
        stats.pen_thickness_mm ? `${formatNumber(Number(stats.pen_thickness_mm), 3)} mm pen` : null,
        stats.density_fudge !== undefined ? `${Number(stats.density_fudge) >= 0 ? '+' : ''}${formatNumber(Number(stats.density_fudge), 2)} density fudge` : null,
        stats.brightness_cutoff !== undefined ? `${formatNumber(Number(stats.brightness_cutoff), 3)} brightness cutoff` : null,
        stats.pattern_layout ? `${stats.pattern_layout} layout` : null,
        stats.waveform ? `${stats.waveform} waveform` : null,
        stats.pattern_spacing_mm ? `${formatNumber(Number(stats.pattern_spacing_mm), 3)} mm layout spacing` : null,
        stats.gcode_lines ? `${Number(stats.gcode_lines).toLocaleString()} G-code lines` : null,
        stats.row_pitch_mm ? `${formatNumber(Number(stats.row_pitch_mm), 3)} mm row pitch` : null,
        stats.sample_step_mm ? `${formatNumber(Number(stats.sample_step_mm), 3)} mm sample step` : null,
        stats.compute_backend === 'cuda' ? 'CUDA GPU sampling' : stats.compute_backend ? `${stats.compute_backend} compute` : null,
        stats.duration_seconds !== undefined ? `${stats.duration_seconds}s backend time` : null
    ].filter(Boolean).join(' · ');
}

generateButton.addEventListener('click', async function() {
    if (activeJobId) return;
    const fileInput = document.getElementById('svgInput');
    if (!fileInput.files.length) {
        alert('Please upload an SVG.');
        return;
    }

    clearGeneratedPreview(true);
    livePreviewCursor = 0;
    livePreviewInitialized = false;
    generationStatus.textContent = '';

    try {
        const penThickness = ensurePenThickness();
        setLoading('Rendering a brightness map from the transformed SVG...', 1, 'Preparing the exact machine-coordinate raster');
        const formData = await buildGenerationFormData(penThickness);
        setLoading('Uploading SVG and starting zig-zag generation...', 2, 'Starting the queued backend job');
        const createResponse = await fetch('/api/jobs', {
            method: 'POST',
            body: formData,
            headers: { 'Accept': 'application/json' }
        });
        if (!createResponse.ok) throw new Error(await readApiError(createResponse));

        const created = await createResponse.json();
        activeJobId = created.job_id;
        cancelRequested = false;
        updateGenerationButtons(true, true);
        syncSourceSvgVisibility();
        await waitForGeneration(created.job_id);

        setLoading('Loading generated toolpath...', 99, 'Transferring G-code and final preview paths');
        const resultResponse = await fetch(`/api/jobs/${encodeURIComponent(created.job_id)}/result`, {
            headers: { 'Accept': 'application/json' },
            cache: 'no-store'
        });
        if (!resultResponse.ok) throw new Error(await readApiError(resultResponse));

        const data = await resultResponse.json();
        displayGeneratedResult(data);
    } catch (error) {
        console.error('Failed to generate G-code:', error);
        const cancelled = cancelRequested || /cancelled/i.test(String(error.message || ''));
        generationStatus.textContent = cancelled
            ? `Generation cancelled: ${error.message}`
            : `Generation failed: ${error.message}`;
        if (!cancelled) alert(`Unable to generate the toolpath:\n\n${error.message}`);
    } finally {
        activeJobId = null;
        cancelRequested = false;
        clearLoading();
        syncSourceSvgVisibility();
    }
});

cancelGenerateButton.addEventListener('click', async () => {
    if (!activeJobId || cancelRequested) return;
    cancelRequested = true;
    updateGenerationButtons(true, false);
    setLoading('Cancelling generation...', null, 'Waiting for the current worker step to stop safely');
    try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
        });
        if (!response.ok) throw new Error(await readApiError(response));
        const result = await response.json();
        generationStatus.textContent = result.cancelled
            ? 'Generation cancelled before the job started.'
            : 'Cancellation requested. Waiting for the worker to stop safely...';
    } catch (error) {
        cancelRequested = false;
        updateGenerationButtons(true, true);
        generationStatus.textContent = `Unable to cancel the current job: ${error.message}`;
        alert(`Unable to cancel the current generation job:\n\n${error.message}`);
    }
});

function exportGcode() {
    const content = gcodeOutput.value.trim();
    if (!content || activeJobId) return;

    const sourceName = document.getElementById('svgInput').files[0]?.name || 'hatchplot';
    const baseName = sourceName
        .replace(/\.[^.]+$/, '')
        .replace(/[^a-zA-Z0-9._-]+/g, '-')
        .replace(/^-+|-+$/g, '') || 'hatchplot';
    const blob = new Blob([`${content}\n`], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${baseName}-hatchplot.gcode`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

exportGcodeButton.addEventListener('click', exportGcode);

// --- SIMULATION LOOP ---
function startSimulation(startIndex = 0, resetAll = true) {
    if (allGeneratedPaths.length === 0 || startIndex >= allGeneratedPaths.length) {
        updateSimulationControls();
        return;
    }

    simulationStoppedByUser = false;

    if (resetAll) {
        allGeneratedPaths.forEach(path => {
            if (path.length > 0) path.dashOffset = path.length;
        });
    } else {
        for (let index = startIndex; index < allGeneratedPaths.length; index += 1) {
            const path = allGeneratedPaths[index];
            if (path.length > 0) path.dashOffset = path.length;
        }
    }

    isAnimating = true;
    updateSimulationControls();
    syncSourceSvgVisibility();
    currentPathIndex = Math.max(0, startIndex);
    currentOffset = 0;
    const firstPath = allGeneratedPaths[currentPathIndex];
    if (!firstPath || !firstPath.firstSegment) {
        isAnimating = false;
        updateSimulationControls();
        syncSourceSvgVisibility();
        return;
    }

    if (penHead) penHead.remove();
    penHead = new paper.Path.Circle({
        center: firstPath.firstSegment.point,
        radius: Math.max(readPositiveNumber('penThickness', 0.5) / 2, machineBed.bounds.width * 0.002),
        fillColor: '#00ff00',
        name: 'previewHead'
    });
    highlightGcodeLine(generatedPathGcodeRanges[currentPathIndex]?.rapidLine);
}

document.getElementById('simBtn').addEventListener('click', function() {
    startSimulation(0, true);
});

function stopSimulation(markAsUserStop = true) {
    if (markAsUserStop) simulationStoppedByUser = true;
    isAnimating = false;
    if (penHead) penHead.fillColor = 'gray';
    updateSimulationControls();
    syncSourceSvgVisibility();
}

document.getElementById('stopSimBtn').addEventListener('click', function() {
    stopSimulation(true);
    generationStatus.textContent = activeJobId
        ? 'Simulation stopped. Toolpath generation is still running.'
        : 'Simulation stopped.';
});

paper.view.onFrame = function() {
    if (!isAnimating) return;

    let activePath = allGeneratedPaths[currentPathIndex];
    if (!activePath || activePath.length === 0) {
        currentPathIndex += 1;
        currentOffset = 0;
        if (currentPathIndex >= allGeneratedPaths.length) {
            isAnimating = false;
            if (penHead) penHead.fillColor = 'gray';
            updateSimulationControls();
            syncSourceSvgVisibility();
        }
        return;
    }

    const range = generatedPathGcodeRanges[currentPathIndex];
    const speed = Number.parseInt(document.getElementById('simSpeed').value, 10) || 20;
    currentOffset += speed * (machineBed.bounds.width / 210);

    if (currentOffset >= activePath.length) {
        activePath.dashOffset = 0;
        if (range) highlightGcodeLine(range.penUpLine);
        currentPathIndex += 1;
        currentOffset = 0;

        if (currentPathIndex >= allGeneratedPaths.length) {
            isAnimating = false;
            if (penHead) penHead.fillColor = activeJobId ? '#00ff00' : 'gray';
            updateSimulationControls();
            syncSourceSvgVisibility();
            return;
        }

        activePath = allGeneratedPaths[currentPathIndex];
        if (activePath.length > 0 && activePath.firstSegment) {
            if (penHead) penHead.position = activePath.firstSegment.point;
            highlightGcodeLine(generatedPathGcodeRanges[currentPathIndex]?.rapidLine);
        }
        return;
    }

    activePath.dashOffset = activePath.length - currentOffset;
    const location = activePath.getLocationAt(currentOffset);
    if (!location) return;
    if (penHead) penHead.position = location.point;
    if (range) {
        const curveIndex = location.curve && Number.isInteger(location.curve.index) ? location.curve.index : 0;
        const moveIndex = Math.max(0, Math.min(range.moveCount - 1, curveIndex));
        highlightGcodeLine(range.moveCount > 0 ? range.moveStartLine + moveIndex : range.penDownLine);
    }
};
