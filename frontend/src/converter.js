'use strict';

const SVG_TRANSFER_DB = 'hatchplot-artwork-transfer-v1';
const SVG_TRANSFER_STORE = 'pending-artwork';
const SVG_TRANSFER_KEY = 'pending';
const SVG_TRANSFER_FALLBACK_KEY = 'hatchplot.pendingArtwork.v1';

const TRACE_MODE_HELP = {
    posterize: 'Creates nested, hierarchy-aware vector regions for several grayscale bands. Broad light regions are drawn first, then darker regions are layered above them.',
    silhouette: 'Creates filled vector silhouettes with holes preserved. This is best for logos, high-contrast subjects, and solid artwork.',
    edges: 'Detects strong image edges and emits native stroked SVG contours. Use blur and a higher edge threshold to suppress texture and compression noise.'
};

let sourceImage = null;
let sourceFile = null;
let sourceFilename = 'converted-image.png';
let generatedSvg = '';
let generatedFilename = 'converted-image.svg';
let lastTraceStats = null;

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

function xmlEscape(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&apos;');
}

function safeBaseName(filename) {
    return String(filename || 'converted-image')
        .replace(/\.[^.]+$/, '')
        .replace(/[^a-zA-Z0-9._-]+/g, '-')
        .replace(/^-+|-+$/g, '') || 'converted-image';
}

function setBusy(busy, message = '') {
    previewButton.disabled = busy || !sourceImage;
    downloadButton.disabled = busy || !generatedSvg;
    sendButton.disabled = busy || !generatedSvg;
    if (message) statusNode.textContent = message;
}

function updateModeVisibility() {
    const mode = traceMode.value;
    document.getElementById('posterizeOptions').hidden = mode !== 'posterize';
    document.getElementById('silhouetteOptions').hidden = mode !== 'silhouette';
    document.getElementById('edgeOptions').hidden = mode !== 'edges';
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
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(image, 0, 0, sourcePreview.width, sourcePreview.height);
}

async function loadImageFile(file) {
    const objectUrl = URL.createObjectURL(file);
    const image = new Image();
    try {
        await new Promise((resolve, reject) => {
            image.onload = resolve;
            image.onerror = () => reject(new Error('The browser could not decode this image format.'));
            image.src = objectUrl;
        });
        sourceImage = image;
        sourceFile = file;
        sourceFilename = file.name || 'converted-image.png';
        generatedSvg = '';
        generatedFilename = `${safeBaseName(sourceFilename)}.svg`;
        lastTraceStats = null;
        drawSourcePreview(image);
        svgPreview.textContent = 'Click Preview SVG to vectorize this image.';
        metadataNode.textContent = `${image.naturalWidth.toLocaleString()} × ${image.naturalHeight.toLocaleString()} source pixels`;
        statusNode.textContent = `Loaded ${sourceFilename}. Adjust the settings, then preview the SVG.`;
        setBusy(false);
    } finally {
        URL.revokeObjectURL(objectUrl);
    }
}

function boxBlur(values, width, height, radius) {
    if (radius <= 0) return values;
    const horizontal = new Float32Array(values.length);
    const output = new Float32Array(values.length);
    const windowSize = (radius * 2) + 1;

    for (let y = 0; y < height; y += 1) {
        let sum = 0;
        const rowOffset = y * width;
        for (let x = -radius; x <= radius; x += 1) {
            sum += values[rowOffset + clamp(x, 0, width - 1)];
        }
        for (let x = 0; x < width; x += 1) {
            horizontal[rowOffset + x] = sum / windowSize;
            sum -= values[rowOffset + clamp(x - radius, 0, width - 1)];
            sum += values[rowOffset + clamp(x + radius + 1, 0, width - 1)];
        }
    }

    for (let x = 0; x < width; x += 1) {
        let sum = 0;
        for (let y = -radius; y <= radius; y += 1) {
            sum += horizontal[(clamp(y, 0, height - 1) * width) + x];
        }
        for (let y = 0; y < height; y += 1) {
            output[(y * width) + x] = sum / windowSize;
            sum -= horizontal[(clamp(y - radius, 0, height - 1) * width) + x];
            sum += horizontal[(clamp(y + radius + 1, 0, height - 1) * width) + x];
        }
    }
    return output;
}

function renderAnalysisRaster(settings) {
    const scale = Math.min(1, settings.maxDimension / Math.max(sourceImage.naturalWidth, sourceImage.naturalHeight));
    const width = Math.max(1, Math.round(sourceImage.naturalWidth * scale));
    const height = Math.max(1, Math.round(sourceImage.naturalHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: true, willReadFrequently: true });
    if (!context) throw new Error('The browser could not create the image-analysis canvas.');
    context.clearRect(0, 0, width, height);
    if (settings.whiteBackground) {
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, width, height);
    }
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    context.drawImage(sourceImage, 0, 0, width, height);

    const imageData = context.getImageData(0, 0, width, height);
    const gray = new Float32Array(width * height);
    const alpha = new Uint8Array(width * height);
    for (let pixel = 0, offset = 0; pixel < gray.length; pixel += 1, offset += 4) {
        const opacity = imageData.data[offset + 3] / 255;
        let luminance = (imageData.data[offset] * 0.2126)
            + (imageData.data[offset + 1] * 0.7152)
            + (imageData.data[offset + 2] * 0.0722);
        if (settings.whiteBackground) {
            luminance = (luminance * opacity) + (255 * (1 - opacity));
            alpha[pixel] = 255;
        } else {
            alpha[pixel] = imageData.data[offset + 3];
            if (alpha[pixel] < settings.alphaThreshold) luminance = 255;
        }
        gray[pixel] = settings.invert ? 255 - luminance : luminance;
    }
    return {
        width,
        height,
        gray: boxBlur(gray, width, height, settings.blurRadius),
        alpha
    };
}

function sobelMask(raster, threshold, alphaThreshold) {
    const { width, height, gray, alpha } = raster;
    const mask = new Uint8Array(width * height);
    for (let y = 1; y < height - 1; y += 1) {
        for (let x = 1; x < width - 1; x += 1) {
            const index = (y * width) + x;
            if (alpha[index] < alphaThreshold) continue;
            const a = gray[index - width - 1];
            const b = gray[index - width];
            const c = gray[index - width + 1];
            const d = gray[index - 1];
            const f = gray[index + 1];
            const g = gray[index + width - 1];
            const h = gray[index + width];
            const i = gray[index + width + 1];
            const gx = (-a + c) + (-2 * d + 2 * f) + (-g + i);
            const gy = (-a - 2 * b - c) + (g + 2 * h + i);
            const magnitude = Math.min(255, Math.hypot(gx, gy) / 4);
            if (magnitude >= threshold) mask[index] = 1;
        }
    }
    return mask;
}

function dilateMask(mask, width, height, radius) {
    let current = mask;
    for (let pass = 1; pass < radius; pass += 1) {
        const next = new Uint8Array(current);
        for (let y = 0; y < height; y += 1) {
            for (let x = 0; x < width; x += 1) {
                const index = (y * width) + x;
                if (!current[index]) continue;
                for (let dy = -1; dy <= 1; dy += 1) {
                    const ny = y + dy;
                    if (ny < 0 || ny >= height) continue;
                    for (let dx = -1; dx <= 1; dx += 1) {
                        const nx = x + dx;
                        if (nx >= 0 && nx < width) next[(ny * width) + nx] = 1;
                    }
                }
            }
        }
        current = next;
    }
    return current;
}

function addBoundaryEdge(edges, outgoing, stride, startX, startY, endX, endY) {
    const startKey = (startY * stride) + startX;
    const endKey = (endY * stride) + endX;
    const dx = endX - startX;
    const dy = endY - startY;
    const direction = dx === 1 ? 0 : dy === 1 ? 1 : dx === -1 ? 2 : 3;
    const edgeIndex = edges.length;
    edges.push({ startX, startY, endX, endY, startKey, endKey, direction });
    const entries = outgoing.get(startKey);
    if (entries) entries.push(edgeIndex);
    else outgoing.set(startKey, [edgeIndex]);
}

function traceMaskBoundaries(mask, width, height) {
    const stride = width + 1;
    const edges = [];
    const outgoing = new Map();
    const filled = (x, y) => x >= 0 && x < width && y >= 0 && y < height && mask[(y * width) + x] !== 0;

    for (let y = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1) {
            if (!filled(x, y)) continue;
            if (!filled(x, y - 1)) addBoundaryEdge(edges, outgoing, stride, x, y, x + 1, y);
            if (!filled(x + 1, y)) addBoundaryEdge(edges, outgoing, stride, x + 1, y, x + 1, y + 1);
            if (!filled(x, y + 1)) addBoundaryEdge(edges, outgoing, stride, x + 1, y + 1, x, y + 1);
            if (!filled(x - 1, y)) addBoundaryEdge(edges, outgoing, stride, x, y + 1, x, y);
        }
    }

    const used = new Uint8Array(edges.length);
    const loops = [];
    const turnRank = new Map([[1, 0], [0, 1], [3, 2], [2, 3]]);

    for (let startIndex = 0; startIndex < edges.length; startIndex += 1) {
        if (used[startIndex]) continue;
        const first = edges[startIndex];
        const points = [[first.startX, first.startY]];
        let edgeIndex = startIndex;
        let closed = false;
        let guard = 0;

        while (guard <= edges.length) {
            guard += 1;
            if (used[edgeIndex]) break;
            used[edgeIndex] = 1;
            const edge = edges[edgeIndex];
            points.push([edge.endX, edge.endY]);
            if (edge.endKey === first.startKey) {
                closed = true;
                break;
            }
            const candidates = (outgoing.get(edge.endKey) || []).filter(index => !used[index]);
            if (!candidates.length) break;
            candidates.sort((leftIndex, rightIndex) => {
                const leftTurn = (edges[leftIndex].direction - edge.direction + 4) % 4;
                const rightTurn = (edges[rightIndex].direction - edge.direction + 4) % 4;
                return (turnRank.get(leftTurn) ?? 9) - (turnRank.get(rightTurn) ?? 9);
            });
            edgeIndex = candidates[0];
        }
        if (closed && points.length >= 4) loops.push(points);
    }
    return loops;
}

function polygonArea(points) {
    let area = 0;
    for (let index = 0; index < points.length - 1; index += 1) {
        area += (points[index][0] * points[index + 1][1]) - (points[index + 1][0] * points[index][1]);
    }
    return area / 2;
}

function squaredDistanceToSegment(point, start, end) {
    const vx = end[0] - start[0];
    const vy = end[1] - start[1];
    const lengthSquared = (vx * vx) + (vy * vy);
    if (lengthSquared === 0) {
        const dx = point[0] - start[0];
        const dy = point[1] - start[1];
        return (dx * dx) + (dy * dy);
    }
    const t = clamp((((point[0] - start[0]) * vx) + ((point[1] - start[1]) * vy)) / lengthSquared, 0, 1);
    const dx = point[0] - (start[0] + (t * vx));
    const dy = point[1] - (start[1] + (t * vy));
    return (dx * dx) + (dy * dy);
}

function simplifyOpen(points, tolerance) {
    if (points.length <= 2 || tolerance <= 0) return points.slice();
    const keep = new Uint8Array(points.length);
    keep[0] = 1;
    keep[points.length - 1] = 1;
    const stack = [[0, points.length - 1]];
    const toleranceSquared = tolerance * tolerance;
    while (stack.length) {
        const [startIndex, endIndex] = stack.pop();
        let maximumDistance = 0;
        let splitIndex = -1;
        for (let index = startIndex + 1; index < endIndex; index += 1) {
            const distance = squaredDistanceToSegment(points[index], points[startIndex], points[endIndex]);
            if (distance > maximumDistance) {
                maximumDistance = distance;
                splitIndex = index;
            }
        }
        if (splitIndex >= 0 && maximumDistance > toleranceSquared) {
            keep[splitIndex] = 1;
            stack.push([startIndex, splitIndex], [splitIndex, endIndex]);
        }
    }
    return points.filter((_, index) => keep[index]);
}

function simplifyClosedLoop(points, tolerance) {
    const ring = points.slice(0, -1);
    if (ring.length <= 4 || tolerance <= 0) return [...ring, ring[0]];
    let firstIndex = 0;
    let secondIndex = 1;
    let maximumDistance = -1;
    for (let index = 1; index < ring.length; index += 1) {
        const dx = ring[index][0] - ring[firstIndex][0];
        const dy = ring[index][1] - ring[firstIndex][1];
        const distance = (dx * dx) + (dy * dy);
        if (distance > maximumDistance) {
            maximumDistance = distance;
            secondIndex = index;
        }
    }
    const firstArc = ring.slice(firstIndex, secondIndex + 1);
    const secondArc = ring.slice(secondIndex).concat(ring.slice(0, firstIndex + 1));
    const simplified = simplifyOpen(firstArc, tolerance)
        .slice(0, -1)
        .concat(simplifyOpen(secondArc, tolerance).slice(0, -1));
    if (simplified.length < 3) return [...ring, ring[0]];
    return [...simplified, simplified[0]];
}

function chaikinSmooth(points, passes) {
    let ring = points.slice(0, -1);
    for (let pass = 0; pass < passes; pass += 1) {
        const next = [];
        for (let index = 0; index < ring.length; index += 1) {
            const current = ring[index];
            const following = ring[(index + 1) % ring.length];
            next.push([
                (current[0] * 0.75) + (following[0] * 0.25),
                (current[1] * 0.75) + (following[1] * 0.25)
            ]);
            next.push([
                (current[0] * 0.25) + (following[0] * 0.75),
                (current[1] * 0.25) + (following[1] * 0.75)
            ]);
        }
        ring = next;
    }
    return [...ring, ring[0]];
}

function loopToPath(points) {
    const coordinates = points.slice(0, -1);
    if (coordinates.length < 3) return '';
    const number = value => Number(value.toFixed(3));
    return `M${coordinates.map(point => `${number(point[0])} ${number(point[1])}`).join('L')}Z`;
}

function vectorizeMask(mask, width, height, settings) {
    const rawLoops = traceMaskBoundaries(mask, width, height);
    const paths = [];
    let pointCount = 0;
    for (const rawLoop of rawLoops) {
        if (Math.abs(polygonArea(rawLoop)) < settings.despeckleArea) continue;
        let loop = simplifyClosedLoop(rawLoop, settings.simplifyTolerance);
        if (settings.smoothingPasses > 0) {
            loop = chaikinSmooth(loop, settings.smoothingPasses);
            loop = simplifyClosedLoop(loop, Math.max(0.05, settings.simplifyTolerance * 0.35));
        }
        const pathData = loopToPath(loop);
        if (!pathData) continue;
        paths.push(pathData);
        pointCount += loop.length - 1;
    }
    return { paths, pointCount };
}

function readSettings() {
    return {
        mode: traceMode.value,
        outputWidth: numberValue('outputWidth', 150, 1, 5000),
        maxDimension: integerValue('maxDimension', 900, 128, 2048),
        grayLevels: integerValue('grayLevels', 4, 2, 8),
        lumaThreshold: integerValue('lumaThreshold', 160, 0, 255),
        edgeThreshold: integerValue('edgeThreshold', 70, 1, 255),
        edgeWidth: integerValue('edgeWidth', 2, 1, 6),
        blurRadius: integerValue('blurRadius', 1, 0, 8),
        alphaThreshold: integerValue('alphaThreshold', 16, 0, 255),
        despeckleArea: numberValue('despeckleArea', 12, 0, 100000),
        simplifyTolerance: numberValue('simplifyTolerance', 0.8, 0, 12),
        smoothingPasses: integerValue('smoothingPasses', 1, 0, 3),
        invert: document.getElementById('invertImage').checked,
        whiteBackground: document.getElementById('whiteBackground').checked
    };
}

function maskFromThreshold(raster, threshold, alphaThreshold) {
    const mask = new Uint8Array(raster.width * raster.height);
    for (let index = 0; index < mask.length; index += 1) {
        if (raster.alpha[index] >= alphaThreshold && raster.gray[index] <= threshold) mask[index] = 1;
    }
    return mask;
}

function generateSvg(settings, raster) {
    const outputHeight = settings.outputWidth * (raster.height / raster.width);
    const groups = [];
    let pathCount = 0;
    let pointCount = 0;

    if (settings.mode === 'posterize') {
        for (let level = 0; level < settings.grayLevels; level += 1) {
            const threshold = Math.round(((level + 0.5) / settings.grayLevels) * 255);
            const fillValue = Math.round(255 * (1 - ((level + 1) / settings.grayLevels)));
            const vector = vectorizeMask(
                maskFromThreshold(raster, threshold, settings.alphaThreshold),
                raster.width,
                raster.height,
                settings
            );
            if (!vector.paths.length) continue;
            const fill = `rgb(${fillValue},${fillValue},${fillValue})`;
            groups.push(`<g id="tone-${level + 1}" data-threshold="${threshold}" fill="${fill}" stroke="none" fill-rule="evenodd"><path d="${vector.paths.join('')}"/></g>`);
            pathCount += vector.paths.length;
            pointCount += vector.pointCount;
        }
    } else {
        let mask;
        if (settings.mode === 'edges') {
            mask = dilateMask(
                sobelMask(raster, settings.edgeThreshold, settings.alphaThreshold),
                raster.width,
                raster.height,
                settings.edgeWidth
            );
        } else {
            mask = maskFromThreshold(raster, settings.lumaThreshold, settings.alphaThreshold);
        }
        const vector = vectorizeMask(mask, raster.width, raster.height, settings);
        groups.push(`<g id="${settings.mode}" fill="#000000" stroke="none" fill-rule="evenodd"><path d="${vector.paths.join('')}"/></g>`);
        pathCount = vector.paths.length;
        pointCount = vector.pointCount;
    }

    if (!pathCount) {
        throw new Error('No vector regions survived the current threshold and despeckle settings. Lower the threshold or despeckle area, or invert the image.');
    }

    const svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        `<svg xmlns="http://www.w3.org/2000/svg" width="${settings.outputWidth.toFixed(4)}mm" height="${outputHeight.toFixed(4)}mm" viewBox="0 0 ${raster.width} ${raster.height}">`,
        `<title>${xmlEscape(sourceFilename)} converted by HatchPlot</title>`,
        `<metadata>mode=${xmlEscape(settings.mode)}; trace=${raster.width}x${raster.height}; simplify=${settings.simplifyTolerance}; smoothing=${settings.smoothingPasses}</metadata>`,
        ...groups,
        '</svg>'
    ].join('\n');

    return {
        svg,
        outputHeight,
        pathCount,
        pointCount,
        rasterWidth: raster.width,
        rasterHeight: raster.height
    };
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

function buildVectorizeFormData(settings) {
    const formData = new FormData();
    formData.append('file', sourceFile, sourceFilename);
    formData.append('mode', settings.mode);
    formData.append('outputWidth', String(settings.outputWidth));
    formData.append('maxDimension', String(settings.maxDimension));
    formData.append('grayLevels', String(settings.grayLevels));
    formData.append('lumaThreshold', String(settings.lumaThreshold));
    formData.append('edgeThreshold', String(settings.edgeThreshold));
    formData.append('edgeWidth', String(settings.edgeWidth));
    formData.append('blurRadius', String(settings.blurRadius));
    formData.append('alphaThreshold', String(settings.alphaThreshold));
    formData.append('despeckleArea', String(settings.despeckleArea));
    formData.append('simplifyTolerance', String(settings.simplifyTolerance));
    formData.append('smoothingPasses', String(settings.smoothingPasses));
    formData.append('invert', settings.invert ? 'true' : 'false');
    formData.append('whiteBackground', settings.whiteBackground ? 'true' : 'false');
    return formData;
}

async function previewConversion() {
    if (!sourceImage || !sourceFile) {
        alert('Choose an image first.');
        return;
    }
    setBusy(true, 'Uploading the image and tracing smooth vector contours...');
    try {
        const settings = readSettings();
        const response = await fetch('/api/vectorize', {
            method: 'POST',
            body: buildVectorizeFormData(settings),
            headers: { Accept: 'application/json' }
        });
        if (!response.ok) throw new Error(await readApiError(response));
        const result = await response.json();
        if (!result?.svg || !result?.stats) throw new Error('The vectorization service returned an incomplete result.');

        generatedSvg = result.svg;
        generatedFilename = `${safeBaseName(sourceFilename)}-${settings.mode}.svg`;
        lastTraceStats = result.stats;
        svgPreview.innerHTML = generatedSvg;
        metadataNode.textContent = `${Number(result.stats.trace_width).toLocaleString()} × ${Number(result.stats.trace_height).toLocaleString()} trace raster · ${Number(result.stats.path_count).toLocaleString()} vector paths · ${Number(result.stats.point_count).toLocaleString()} contour points · ${Number(result.stats.output_width_mm).toFixed(1)} × ${Number(result.stats.output_height_mm).toFixed(1)} mm · OpenCV contour engine`;
        statusNode.textContent = 'SVG preview ready. The result contains native SVG paths and can be downloaded or sent directly to the toolpath workspace.';
    } catch (error) {
        generatedSvg = '';
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

function openTransferDatabase() {
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) {
            reject(new Error('This browser does not support the workspace transfer store. Download the SVG and upload it manually instead.'));
            return;
        }
        const request = indexedDB.open(SVG_TRANSFER_DB, 1);
        request.onupgradeneeded = () => {
            const database = request.result;
            if (!database.objectStoreNames.contains(SVG_TRANSFER_STORE)) {
                database.createObjectStore(SVG_TRANSFER_STORE);
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error('Unable to open workspace transfer storage.'));
    });
}

async function sendToWorkspace() {
    if (!generatedSvg) return;
    setBusy(true, 'Saving the SVG for the toolpath workspace...');
    const record = {
        svgText: generatedSvg,
        filename: generatedFilename,
        createdAt: Date.now(),
        stats: lastTraceStats,
        generationMode: workspaceMode?.value || 'outline'
    };
    let database;
    let stored = false;
    const storageErrors = [];
    try {
        try {
            database = await openTransferDatabase();
            await new Promise((resolve, reject) => {
                const transaction = database.transaction(SVG_TRANSFER_STORE, 'readwrite');
                transaction.objectStore(SVG_TRANSFER_STORE).put(record, SVG_TRANSFER_KEY);
                transaction.oncomplete = resolve;
                transaction.onerror = () => reject(transaction.error || new Error('Unable to store the converted SVG.'));
                transaction.onabort = () => reject(transaction.error || new Error('The SVG transfer was aborted.'));
            });
            stored = true;
        } catch (error) {
            storageErrors.push(`IndexedDB: ${error.message}`);
        }

        try {
            sessionStorage.setItem(SVG_TRANSFER_FALLBACK_KEY, JSON.stringify(record));
            stored = true;
        } catch (error) {
            storageErrors.push(`sessionStorage: ${error.message}`);
        }

        if (!stored) throw new Error(storageErrors.join('; ') || 'Browser storage is unavailable.');
        const workspaceUrl = new URL('./', window.location.href);
        workspaceUrl.searchParams.set('from', 'converter');
        window.location.assign(workspaceUrl.href);
    } catch (error) {
        statusNode.textContent = `Unable to send the SVG: ${error.message}`;
        setBusy(false);
    } finally {
        if (database) database.close();
    }
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

traceMode.addEventListener('change', updateModeVisibility);
previewButton.addEventListener('click', previewConversion);
downloadButton.addEventListener('click', downloadSvg);
sendButton.addEventListener('click', sendToWorkspace);

updateModeVisibility();
setBusy(false);
