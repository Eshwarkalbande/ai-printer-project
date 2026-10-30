/* ------------------------------------------------------------------------
 * SmartPrint AI - real-time dashboard client.
 *
 * The Flask server pushes a 'printer_update' Socket.IO event every ~3.5s
 * containing the whole fleet telemetry. Every function in this file is a
 * standalone top-level function so that nothing is nested inside - or
 * accidentally shadows - anything else.
 * --------------------------------------------------------------------- */

let socket = null;
let telemetryChart = null;
let latestPrinters = [];

/* Matches SNMP_POLL_INTERVAL in monitor.py (used in report copy only). */
const SNMP_POLL_SECONDS_DISPLAY = '12 seconds';

/* Plain-language issue states rendered on every printer card. */
const ISSUE_DEFINITIONS = {
    JAM: {
        label: 'Paper Jam',
        icon: 'bi-tools',
        level: 'critical',
        help: 'A sheet of paper is stuck inside the printer.'
    },
    OVERHEAT: {
        label: 'Overheating',
        icon: 'bi-thermometer-high',
        level: 'critical',
        help: 'The printer reports that its fuser unit is overheating.'
    },
    NO_PAPER: {
        label: 'No Paper',
        icon: 'bi-layers',
        level: 'warning',
        help: 'The paper tray is empty and printing is blocked.'
    },
    NO_INK: {
        label: 'No Ink',
        icon: 'bi-droplet-half',
        level: 'warning',
        help: 'A toner or ink cartridge is empty.'
    },
    LOW_TONER: {
        label: 'Toner Low',
        icon: 'bi-droplet',
        level: 'warning',
        help: 'A toner or ink cartridge is running low.'
    },
    LOW_PAPER: {
        label: 'Paper Low',
        icon: 'bi-layers-half',
        level: 'warning',
        help: 'A paper tray is running low.'
    },
    DOOR_OPEN: {
        label: 'Door Open',
        icon: 'bi-door-open',
        level: 'warning',
        help: 'A cover or door of the printer is open.'
    },
    SERVICE: {
        label: 'Service Required',
        icon: 'bi-wrench-adjustable',
        level: 'critical',
        help: 'The printer is requesting service.'
    },
    OUTPUT_FULL: {
        label: 'Output Tray Full',
        icon: 'bi-archive',
        level: 'warning',
        help: 'The output tray is full and needs emptying.'
    },
    OUTPUT_NEAR_FULL: {
        label: 'Output Tray Nearly Full',
        icon: 'bi-archive',
        level: 'warning',
        help: 'The output tray is almost full.'
    },
    INPUT_TRAY_MISSING: {
        label: 'Input Tray Missing',
        icon: 'bi-layers',
        level: 'warning',
        help: 'An input paper tray is missing.'
    },
    OUTPUT_TRAY_MISSING: {
        label: 'Output Tray Missing',
        icon: 'bi-archive',
        level: 'warning',
        help: 'The output tray is missing.'
    },
    MAINTENANCE: {
        label: 'Maintenance Due',
        icon: 'bi-calendar-x',
        level: 'warning',
        help: 'The printer reports that preventive maintenance is overdue.'
    },
    OFFLINE: {
        label: 'Offline',
        icon: 'bi-plug',
        level: 'warning',
        help: 'The printer is not answering on the network.'
    },
    UNKNOWN: {
        label: 'Status Not Available',
        icon: 'bi-question-circle',
        level: 'unknown',
        help: 'This printer has not reported its status yet.'
    },
    OK: {
        label: 'No Issues',
        icon: 'bi-check-circle',
        level: 'ok',
        help: 'All components are working normally.'
    }
};

/* scan-report finding code -> card issue key (real printers only). */
const FINDING_ISSUE_MAP = {
    jammed: 'JAM',
    overheat: 'OVERHEAT',
    noPaper: 'NO_PAPER',
    inputTrayEmpty: 'NO_PAPER',
    noToner: 'NO_INK',
    markerSupplyMissing: 'NO_INK',
    lowToner: 'LOW_TONER',
    lowPaper: 'LOW_PAPER',
    doorOpen: 'DOOR_OPEN',
    serviceRequested: 'SERVICE',
    outputFull: 'OUTPUT_FULL',
    outputNearFull: 'OUTPUT_NEAR_FULL',
    inputTrayMissing: 'INPUT_TRAY_MISSING',
    outputTrayMissing: 'OUTPUT_TRAY_MISSING',
    overduePreventMaintenance: 'MAINTENANCE',
    offline: 'OFFLINE'
};

/* One-click repair offered for each issue state (handler = global function name). */
const ISSUE_FIX_ACTIONS = {
    JAM: { label: 'Fix It: Clear Jam', icon: 'bi-tools', handler: 'clearJam' },
    OVERHEAT: { label: 'Fix It: Cool Down', icon: 'bi-snow', handler: 'coolFuser' },
    NO_PAPER: { label: 'Fix It: Refill Paper', icon: 'bi-arrow-clockwise', handler: 'replenishPrinter' },
    NO_INK: { label: 'Fix It: Replace Ink', icon: 'bi-arrow-clockwise', handler: 'replenishPrinter' },
    OFFLINE: { label: 'Retry Connection', icon: 'bi-arrow-repeat', handler: 'triggerTelemetryPoll' }
};

/* Pick the single, most obvious problem a printer is reporting. */
function detectIssue(printer) {
    if (printer.jammed || printer.status === 'Error') return 'JAM';
    if (numberOr(printer.fuser_temp, 180) > 210) return 'OVERHEAT';
    if (numberOr(printer.paper_level, 100) <= 15) return 'NO_PAPER';
    if (numberOr(printer.toner_black, 100) <= 15) return 'NO_INK';
    if (printer.status === 'Offline') return 'OFFLINE';
    return 'OK';
}

/*
 * Full issue display for one printer.
 *
 * Simulation printers keep the classic heuristic. Real (SNMP/IPP) printers
 * are rendered from the scan report only: when nothing has been reported the
 * state is UNKNOWN - never a healthy default.
 */
function resolveIssue(printer) {
    if (printer.mode === 'snmp') return resolveRealIssue(printer);
    const key = detectIssue(printer);
    const def = ISSUE_DEFINITIONS[key];
    return {
        key: key,
        label: def.label,
        icon: def.icon,
        level: def.level,
        help: def.help,
        fix: ISSUE_FIX_ACTIONS[key] || null,
        topFinding: null,
        findings: []
    };
}

function resolveRealIssue(printer) {
    const report = printer.scan_report || null;
    const findings = report && report.findings ? report.findings : [];

    if (!report) {
        return {
            key: 'UNKNOWN', label: ISSUE_DEFINITIONS.UNKNOWN.label,
            icon: ISSUE_DEFINITIONS.UNKNOWN.icon, level: 'unknown',
            help: 'Waiting for the first scan of this printer.',
            fix: null, topFinding: null, findings: []
        };
    }
    if (report.error || printer.status === 'Offline') {
        const message = report.error && report.error.message
            ? report.error.message
            : 'The printer is not answering on the network.';
        return {
            key: 'OFFLINE', label: ISSUE_DEFINITIONS.OFFLINE.label,
            icon: ISSUE_DEFINITIONS.OFFLINE.icon, level: 'warning',
            help: message,
            fix: null, topFinding: null, findings: []
        };
    }
    if (findings.length === 0) {
        return {
            key: 'OK', label: ISSUE_DEFINITIONS.OK.label,
            icon: ISSUE_DEFINITIONS.OK.icon, level: 'ok',
            help: 'The printer answered the scan and reported no problems.',
            fix: null, topFinding: null, findings: []
        };
    }

    const top = findings[0];   // the report already sorts critical-first
    const mapped = FINDING_ISSUE_MAP[top.code];
    if (mapped && ISSUE_DEFINITIONS[mapped]) {
        const def = ISSUE_DEFINITIONS[mapped];
        return {
            key: mapped, label: def.label, icon: def.icon, level: def.level,
            help: top.plain_message || def.help,
            fix: null, topFinding: top, findings: findings
        };
    }
    return {
        key: 'FINDING',
        label: top.title || 'Attention Needed',
        icon: 'bi-exclamation-triangle',
        level: top.severity === 'critical' ? 'critical'
             : top.severity === 'warning' ? 'warning' : 'info',
        help: top.plain_message || '',
        fix: null, topFinding: top, findings: findings
    };
}

function numberOr(value, fallback) {
    const parsed = Number(value);
    return isNaN(parsed) ? fallback : parsed;
}

/* Escape database values before injecting them into an HTML template. */
function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function setText(elementId, value) {
    const element = document.getElementById(elementId);
    if (element) element.textContent = value;
}

/* Small dependency-free toast so feedback works even if Bootstrap JS fails. */
function showToast(message, tone) {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        stack.className = 'toast-stack';
        document.body.appendChild(stack);
    }
    const toast = document.createElement('div');
    toast.className = 'mini-toast mini-toast-' + (tone || 'success');
    toast.textContent = message;
    stack.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function updateConnectionStatus(isOnline) {
    const badge = document.getElementById('connection-status');
    if (!badge) return;
    if (isOnline) {
        badge.className = 'badge bg-success-subtle text-success border border-success-subtle fs-xs';
        badge.innerHTML = '<i class="bi bi-wifi me-1"></i>Live telemetry connected';
    } else {
        badge.className = 'badge bg-danger-subtle text-danger border border-danger-subtle fs-xs';
        badge.innerHTML = '<i class="bi bi-wifi-off me-1"></i>Disconnected - retrying...';
    }
}

function renderConnectionError(message) {
    const container = document.getElementById('printer-cards-container');
    if (!container) return;
    container.innerHTML =
        '<div class="col-12 text-center py-5">' +
        '<i class="bi bi-exclamation-triangle text-warning fs-2"></i>' +
        '<p class="text-secondary small mt-2 mb-0">' + escapeHtml(message) + '</p>' +
        '</div>';
}

document.addEventListener('DOMContentLoaded', () => {
    initSocket();
    initChart();
    loadRecentJobs();
    loadAlerts();
    triggerTelemetryPoll();
});

function initSocket() {
    if (typeof io === 'undefined') {
        console.error('Socket.IO client library failed to load.');
        updateConnectionStatus(false);
        renderConnectionError('Live telemetry library could not be loaded. Check the internet connection and reload.');
        return;
    }

    socket = io();

    socket.on('connect', () => {
        updateConnectionStatus(true);
    });

    socket.on('disconnect', () => {
        updateConnectionStatus(false);
    });

    socket.on('connect_error', () => {
        updateConnectionStatus(false);
    });

    socket.on('printer_update', (data) => {
        if (!data || !data.printers) return;
        latestPrinters = data.printers;
        renderPrinterCards(latestPrinters);
        renderAIRecommendations(latestPrinters);
        updateKPIs(latestPrinters);
        updateChartRealtime(latestPrinters);
    });
}

function updateKPIs(printers) {
    printers = printers || [];
    setText('kpi-total-printers', printers.length);

    let activeJobs = 0;
    let warnings = 0;
    let totalHealth = 0;
    let healthCount = 0;

    printers.forEach(p => {
        if (p.status === 'Printing') activeJobs++;
        if (p.status === 'Warning' || p.status === 'Error') warnings++;
        const score = p.ai_analysis ? p.ai_analysis.health_score : null;
        if (typeof score === 'number') {
            totalHealth += score;
            healthCount++;
        }
    });

    setText('kpi-active-jobs', activeJobs);
    setText('kpi-warning-count', warnings);

    const healthElem = document.getElementById('kpi-fleet-health');
    if (!healthElem) return;
    if (healthCount === 0) {
        // Nothing was measured yet - showing a healthy number would be a lie.
        healthElem.textContent = '--';
        healthElem.className = 'fw-bold mb-0 mt-1 text-secondary';
        return;
    }
    const avgHealth = Math.round(totalHealth / healthCount);
    healthElem.textContent = avgHealth + '%';
    healthElem.className = 'fw-bold mb-0 mt-1 ' +
        (avgHealth >= 75 ? 'text-success' : avgHealth >= 50 ? 'text-warning' : 'text-danger');
}

function renderPrinterCards(printers) {
    const container = document.getElementById('printer-cards-container');
    if (!container) return;

    printers = printers || [];
    if (printers.length === 0) {
        container.innerHTML =
            '<div class="col-12 text-center py-5">' +
            '<p class="text-secondary small mb-0">No printers registered yet. Use "Add Printer" to register one.</p>' +
            '</div>';
        return;
    }

    let html = '';
    printers.forEach(p => {
        const isReal = p.mode === 'snmp';
        const toner = typeof p.toner_black === 'number' ? p.toner_black : null;
        const paper = typeof p.paper_level === 'number' ? p.paper_level : null;
        const drum = typeof p.drum_wear === 'number' ? p.drum_wear : null;
        const temp = typeof p.fuser_temp === 'number' ? p.fuser_temp : null;
        const pages = typeof p.total_pages === 'number' ? p.total_pages : null;
        const ai = p.ai_analysis || {};
        const health = typeof ai.health_score === 'number' ? ai.health_score : null;
        const issue = resolveIssue(p);

        const tonerColor = toner === null ? 'bg-secondary' : toner > 35 ? 'bg-primary' : toner > 15 ? 'bg-warning' : 'bg-danger';
        const paperColor = paper === null ? 'bg-secondary' : paper > 30 ? 'bg-info' : paper > 15 ? 'bg-warning' : 'bg-danger';
        const healthColor = health === null ? 'text-secondary' : health >= 75 ? 'text-success' : health >= 45 ? 'text-warning' : 'text-danger';
        const statusBadgeClass = 'badge-status-' + escapeHtml(p.status);

        const healthText = health === null ? '&mdash;' : health + '%';
        const tonerText = toner === null ? '&mdash;' : toner + '%';
        const paperText = paper === null ? '&mdash;' : paper + '%';
        const tempText = temp === null ? 'n/a' : temp + '°C';
        const drumText = drum === null ? 'n/a' : drum + '%';
        const pagesText = pages === null ? '&mdash;' : pages.toLocaleString();

        const findingsBadge = (isReal && issue.findings && issue.findings.length > 0)
            ? `<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle fs-xxs ms-auto">${issue.findings.length} finding${issue.findings.length === 1 ? '' : 's'}</span>`
            : '';

        /* Real printers get the finding's "what to do" text instead of a
           one-click Fix It button - the physical fix happens at the device. */
        let actionArea = '';
        if (isReal) {
            if (issue.topFinding && issue.topFinding.what_to_do) {
                actionArea = `
                    <div class="what-todo-panel mb-2">
                        <i class="bi bi-lightbulb"></i>
                        <span>${escapeHtml(issue.topFinding.what_to_do)}</span>
                    </div>
                `;
            }
            actionArea += `
                <div class="d-flex justify-content-between align-items-center">
                    <button class="btn btn-outline-primary btn-sm" onclick="openReport('${escapeHtml(p.id)}')" title="View the full diagnostic report">
                        <i class="bi bi-clipboard2-pulse me-1"></i> Diagnose
                    </button>
                    <button class="btn btn-link text-danger btn-sm p-0" onclick="deletePrinter('${escapeHtml(p.id)}')" title="Delete this printer">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            `;
        } else {
            const fix = issue.fix;
            actionArea = `
                ${fix ? `
                    <button class="btn btn-lg w-100 issue-fix-button issue-fix-${issue.level} mb-2"
                            onclick="${fix.handler}('${escapeHtml(p.id)}')">
                        <i class="bi ${fix.icon} me-1"></i> ${fix.label}
                    </button>
                ` : `
                    <button class="btn btn-lg w-100 issue-fix-button issue-fix-ok mb-2" disabled>
                        <i class="bi bi-check2-circle me-1"></i> Working Correctly
                    </button>
                `}
                <div class="d-flex justify-content-between align-items-center">
                    <button class="btn btn-outline-primary btn-sm" onclick="sendTestJob('${escapeHtml(p.id)}')" title="Send a test print job">
                        <i class="bi bi-printer"></i> Test Print
                    </button>
                    <button class="btn btn-outline-secondary btn-sm" onclick="replenishPrinter('${escapeHtml(p.id)}')" title="Refill paper and ink">
                        <i class="bi bi-arrow-repeat"></i> Refill
                    </button>
                    <button class="btn btn-link text-danger btn-sm p-0" onclick="deletePrinter('${escapeHtml(p.id)}')" title="Delete this printer">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            `;
        }

        html += `
        <div class="col-12 col-md-6 col-xl-4" id="card-${escapeHtml(p.id)}">
            <div class="dashboard-card p-4 h-100 issue-card issue-card-${issue.level}">

                <div class="issue-banner issue-banner-${issue.level}">
                    <i class="bi ${issue.icon}"></i>
                    <span>${escapeHtml(issue.label)}</span>
                    ${findingsBadge}
                </div>

                <h5 class="fw-bold mb-1 mt-3 text-truncate" style="max-width: 100%;" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</h5>
                <small class="text-secondary d-block mb-2">${escapeHtml(p.model)} &bull; <code>${escapeHtml(p.id)}</code></small>

                <p class="issue-help fs-xs mb-3">${escapeHtml(issue.help)}</p>

                <div class="d-flex justify-content-between align-items-center mb-1">
                    <span class="badge ${statusBadgeClass} px-2 py-1 rounded-pill fw-semibold fs-xxs text-uppercase">${escapeHtml(p.status)}</span>
                    <span class="fs-xs text-secondary">AI Health <span class="fw-bold ${healthColor}">${healthText}</span></span>
                </div>

                <div class="mt-3">
                    <div class="d-flex justify-content-between fs-xs mb-1">
                        <span class="text-secondary"><i class="bi bi-droplet-fill text-primary me-1"></i>Toner Black</span>
                        <span class="fw-semibold">${tonerText}</span>
                    </div>
                    <div class="progress mb-3" style="height: 6px;">
                        <div class="progress-bar ${tonerColor}" role="progressbar" style="width: ${toner === null ? 0 : toner}%"></div>
                    </div>

                    <div class="d-flex justify-content-between fs-xs mb-1">
                        <span class="text-secondary"><i class="bi bi-layers-fill text-info me-1"></i>Paper Tray</span>
                        <span class="fw-semibold">${paperText}</span>
                    </div>
                    <div class="progress mb-3" style="height: 6px;">
                        <div class="progress-bar ${paperColor}" role="progressbar" style="width: ${paper === null ? 0 : paper}%"></div>
                    </div>

                    <div class="row g-2 text-center pt-2 border-top border-secondary-subtle">
                        <div class="col-4">
                            <span class="fs-xxs text-secondary text-uppercase d-block">Fuser</span>
                            <span class="fw-semibold fs-xs ${temp !== null && temp > 210 ? 'text-danger' : 'text-light'}">${tempText}</span>
                        </div>
                        <div class="col-4">
                            <span class="fs-xxs text-secondary text-uppercase d-block">Drum</span>
                            <span class="fw-semibold fs-xs ${drum !== null && drum > 70 ? 'text-warning' : 'text-light'}">${drumText}</span>
                        </div>
                        <div class="col-4">
                            <span class="fs-xxs text-secondary text-uppercase d-block">Pages</span>
                            <span class="fw-semibold fs-xs text-light">${pagesText}</span>
                        </div>
                    </div>
                </div>

                <div class="mt-3 pt-3 border-top border-secondary-subtle">
                    ${actionArea}
                </div>
            </div>
        </div>
        `;
    });

    container.innerHTML = html;
}


function renderAIRecommendations(printers) {
    const tbody = document.getElementById('ai-recommendations-body');
    if (!tbody) return;

    printers = printers || [];
    let rows = '';
    printers.forEach(p => {
        const ai = p.ai_analysis || {};
        const risk = ai.risk_category || 'Unknown';
        const badge = ai.risk_badge || 'secondary';
        const failProb = typeof ai.failure_probability === 'number' ? ai.failure_probability : null;
        const recList = ai.recommendations || [];
        const topRec = recList.length > 0 ? recList[0].action : 'No maintenance actions recommended.';
        const issue = resolveIssue(p);
        const failProbText = failProb === null ? '&mdash;' : failProb + '%';
        const failProbNote = ai.model_excluded
            ? '<div class="fs-xxs text-secondary">model excluded: incomplete data</div>' : '';

        rows += `
            <tr>
                <td>
                    <div class="fw-semibold">${escapeHtml(p.name)}</div>
                    <small class="text-secondary">${escapeHtml(p.id)} &bull; ${escapeHtml(p.model)}</small>
                </td>
                <td>
                    <span class="badge bg-${escapeHtml(badge)}-subtle text-${escapeHtml(badge)} border border-${escapeHtml(badge)}-subtle px-2 py-1 rounded-pill">${escapeHtml(risk)}</span>
                    <div class="fs-xxs text-secondary mt-1">${escapeHtml(issue.label)}</div>
                </td>
                <td>
                    <span class="fw-bold ${failProb === null ? 'text-secondary' : failProb > 50 ? 'text-danger' : failProb > 25 ? 'text-warning' : 'text-success'}">${failProbText}</span>
                    ${failProbNote}
                </td>
                <td class="text-secondary small">
                    ${escapeHtml(topRec)}
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = rows || '<tr><td colspan="4" class="text-center py-3">No telemetry inferences recorded.</td></tr>';
}

function initChart() {
    const ctx = document.getElementById('telemetryHistoryChart');
    if (!ctx) return;
    if (typeof Chart === 'undefined') {
        console.warn('Chart.js is unavailable - skipping the telemetry chart.');
        return;
    }

    telemetryChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: ['-30s', '-25s', '-20s', '-15s', '-10s', '-5s', 'Now'],
            datasets: [
                {
                    label: 'Fleet Avg Health %',
                    data: [92, 93, 91, 94, 92, 95, 94],
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.3
                },
                {
                    label: 'Avg Toner Level %',
                    data: [72, 71, 71, 70, 70, 69, 69],
                    borderColor: '#4f46e5',
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    borderDash: [4, 4],
                    tension: 0.3
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: { color: '#94a3b8', font: { family: 'Plus Jakarta Sans', size: 11 } }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#64748b' }
                },
                y: {
                    min: 0,
                    max: 100,
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    ticks: { color: '#64748b' }
                }
            }
        }
    });
}

function updateChartRealtime(printers) {
    if (!telemetryChart || !telemetryChart.data || !telemetryChart.data.datasets) return;

    const healthDataset = telemetryChart.data.datasets[0] ? telemetryChart.data.datasets[0].data : null;
    const tonerDataset = telemetryChart.data.datasets[1] ? telemetryChart.data.datasets[1].data : null;
    if (!healthDataset || !tonerDataset) return;

    printers = printers || [];
    let healthSum = 0, healthCount = 0;
    let tonerSum = 0, tonerCount = 0;
    printers.forEach(p => {
        const score = p.ai_analysis ? p.ai_analysis.health_score : null;
        if (typeof score === 'number') { healthSum += score; healthCount++; }
        if (typeof p.toner_black === 'number') { tonerSum += p.toner_black; tonerCount++; }
    });

    healthDataset.shift();
    healthDataset.push(healthCount > 0 ? Math.round(healthSum / healthCount) : null);

    tonerDataset.shift();
    tonerDataset.push(tonerCount > 0 ? Math.round(tonerSum / tonerCount) : null);

    telemetryChart.update('none');
}

/* Shared helper: run a printer action, then refresh every dashboard panel. */
function postAction(url, successMessage) {
    return fetch(url, { method: 'POST' })
        .then(response => response.json())
        .then(result => {
            if (result && result.status === 'success') {
                showToast(result.message || successMessage, 'success');
            } else {
                showToast((result && result.message) ? result.message : 'Action failed.', 'danger');
            }
        })
        .catch(error => showToast('Network error: ' + error.message, 'danger'))
        .finally(() => {
            loadRecentJobs();
            loadAlerts();
            triggerTelemetryPoll();
        });
}

function sendTestJob(printerId) {
    fetch(`/api/printers/${printerId}/job`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_name: 'Diagnostic Test Page', pages: 3 })
    })
    .then(response => response.json())
    .then(result => {
        if (result && result.status === 'success') {
            showToast(result.message || 'Test page sent.', 'success');
            loadRecentJobs();
            triggerTelemetryPoll();
        } else {
            showToast((result && result.message) ? result.message : 'Could not send the test job.', 'danger');
        }
    })
    .catch(error => showToast('Network error: ' + error.message, 'danger'));
}

function replenishPrinter(printerId) {
    return postAction(`/api/printers/${printerId}/replenish`, 'Paper and ink refilled on ' + printerId);
}

function clearJam(printerId) {
    return postAction(`/api/printers/${printerId}/clear-jam`, 'Paper jam cleared on ' + printerId);
}

function coolFuser(printerId) {
    return postAction(`/api/printers/${printerId}/cool-fuser`, 'Fuser cooled down on ' + printerId);
}

function deletePrinter(printerId) {
    if (!confirm(`Remove printer ${printerId} from the fleet?`)) return;
    fetch(`/api/printers/${printerId}`, { method: 'DELETE' })
    .then(response => response.json())
    .then(result => {
        const card = document.getElementById(`card-${printerId}`);
        if (card) card.remove();
        showToast('Printer ' + printerId + ' removed.', 'success');
        loadAlerts();
    })
    .catch(error => showToast('Network error: ' + error.message, 'danger'));
}

function submitAddPrinter(event) {
    event.preventDefault();
    const mode = document.getElementById('new_printer_mode').value;
    const payload = {
        id: document.getElementById('new_printer_id').value.trim(),
        name: document.getElementById('new_printer_name').value.trim(),
        model: document.getElementById('new_printer_model').value.trim(),
        ip_address: document.getElementById('new_printer_ip').value.trim(),
        mode: mode
    };
    if (mode === 'snmp') {
        payload.snmp_community = document.getElementById('new_printer_community').value.trim() || 'public';
        payload.snmp_port = document.getElementById('new_printer_port').value.trim() || 161;
    }

    fetch('/api/printers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(response => response.json())
    .then(result => {
        if (result && result.status === 'success') {
            const modalElement = document.getElementById('addPrinterModal');
            if (typeof bootstrap !== 'undefined' && modalElement) {
                const modal = bootstrap.Modal.getInstance(modalElement);
                if (modal) modal.hide();
            }
            document.getElementById('addPrinterForm').reset();
            toggleSnmpOptions();
            showToast('Printer ' + payload.id + ' registered.', 'success');
            triggerTelemetryPoll();
        } else {
            showToast('Registration failed: ' + ((result && result.message) || 'unknown error'), 'danger');
        }
    })
    .catch(error => showToast('Network error: ' + error.message, 'danger'));
}

/* Show the SNMP community/port fields only when SNMP mode is selected. */
function toggleSnmpOptions() {
    const mode = document.getElementById('new_printer_mode').value;
    const options = document.getElementById('snmp-options');
    if (options) {
        options.classList.toggle('d-none', mode !== 'snmp');
    }
}

function testPrinterConnection() {
    const ip = document.getElementById('new_printer_ip').value.trim();
    if (!ip) {
        showToast('Enter the printer IP address first.', 'danger');
        return;
    }
    const community = document.getElementById('new_printer_community').value.trim() || 'public';
    const port = document.getElementById('new_printer_port').value.trim() || 161;
    const resultBox = document.getElementById('test-connection-result');
    const button = document.getElementById('test-connection-btn');
    if (button) {
        button.disabled = true;
        button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Testing...';
    }
    if (resultBox) {
        resultBox.innerHTML = '<p class="text-secondary small mb-0">Probing the printer (SNMP + IPP fallback), this can take a few seconds...</p>';
    }

    fetch('/api/printers/test-connection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip: ip, community: community, port: port })
    })
    .then(response => response.json())
    .then(result => {
        if (button) {
            button.disabled = false;
            button.innerHTML = '<i class="bi bi-broadcast me-1"></i> Test Connection';
        }
        if (result && result.status === 'success') {
            renderTestResult(result.report);
        } else {
            if (resultBox) {
                resultBox.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">' +
                    escapeHtml((result && result.message) || 'Connection test failed.') + '</div>';
            }
        }
    })
    .catch(error => {
        if (button) {
            button.disabled = false;
            button.innerHTML = '<i class="bi bi-broadcast me-1"></i> Test Connection';
        }
        if (resultBox) {
            resultBox.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">Network error: ' +
                escapeHtml(error.message) + '</div>';
        }
    });
}

function renderTestResult(report) {
    const resultBox = document.getElementById('test-connection-result');
    if (!resultBox) return;
    if (!report || report.error) {
        const message = report && report.error ? report.error.message : 'The printer did not respond.';
        let html = '<div class="alert alert-danger py-2 px-3 small mb-0"><strong>Connection failed.</strong> ' +
            escapeHtml(message) + '</div>';
        const tips = report && report.error && report.error.troubleshooting ? report.error.troubleshooting : [];
        if (tips.length > 0) {
            html += '<ul class="small text-secondary mt-2 mb-0 ps-4">';
            tips.forEach(tip => { html += '<li>' + escapeHtml(tip) + '</li>'; });
            html += '</ul>';
        }
        resultBox.innerHTML = html;
        return;
    }

    const findings = report.findings || [];
    const topFindings = findings.slice(0, 3)
        .map(f => '<span class="badge ' + (f.severity === 'critical' ? 'bg-danger-subtle text-danger'
            : f.severity === 'warning' ? 'bg-warning-subtle text-warning' : 'bg-info-subtle text-info') +
            ' border-0 me-1">' + escapeHtml(f.title || f.code) + '</span>')
        .join('');
    const pages = typeof report.total_pages === 'number' ? report.total_pages.toLocaleString() : 'unknown';

    resultBox.innerHTML =
        '<div class="alert alert-success py-2 px-3 small mb-0">' +
            '<strong>Printer responded via ' + escapeHtml(report.method ? report.method.toUpperCase() : '?') + '.</strong><br>' +
            'Model: ' + escapeHtml(report.model || 'unknown') + '<br>' +
            'Total pages: ' + pages + '<br>' +
            'Status: ' + escapeHtml(report.status || 'unknown') +
            (findings.length > 0 ? '<br>Findings (' + findings.length + '): ' + topFindings : '') +
        '</div>';
}

/* ----------------------- Diagnostic report modal ----------------------- */

let reportPrinterId = null;

function openReport(printerId) {
    reportPrinterId = printerId;
    const body = document.getElementById('report-body');
    if (body) body.innerHTML = '<p class="text-secondary small mb-0">Loading report...</p>';
    showReportModal();
    loadReport(printerId);
}

function showReportModal() {
    const modalElement = document.getElementById('reportModal');
    if (typeof bootstrap !== 'undefined' && modalElement) {
        const modal = bootstrap.Modal.getInstance(modalElement) || new bootstrap.Modal(modalElement);
        modal.show();
    }
}

function loadReport(printerId) {
    fetch(`/api/printers/${printerId}/report`)
    .then(r => r.json())
    .then(data => renderReport(data))
    .catch(error => {
        const body = document.getElementById('report-body');
        if (body) body.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">Network error: ' +
            escapeHtml(error.message) + '</div>';
    });
}

function rescanPrinter() {
    if (!reportPrinterId) return;
    const body = document.getElementById('report-body');
    const button = document.getElementById('report-rescan-btn');
    if (body) body.innerHTML = '<p class="text-secondary small mb-0">Scanning the printer...</p>';
    if (button) button.disabled = true;

    fetch(`/api/printers/${reportPrinterId}/diagnose`, { method: 'POST' })
    .then(r => r.json())
    .then(result => {
        if (button) button.disabled = false;
        if (result && result.status === 'success') {
            showToast('Diagnostic scan completed.', 'success');
            renderReport({ scan_report: result.report, last_scan_at: null, printer_id: reportPrinterId });
            triggerTelemetryPoll();
        } else {
            if (body) {
                body.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">' +
                    escapeHtml((result && result.message) || 'The scan failed.') + '</div>';
            }
        }
    })
    .catch(error => {
        if (button) button.disabled = false;
        if (body) {
            body.innerHTML = '<div class="alert alert-danger py-2 px-3 small mb-0">Network error: ' +
                escapeHtml(error.message) + '</div>';
        }
    });
}

function formatScanTime(value) {
    if (value === null || value === undefined) return 'never';
    const date = new Date(value * 1000);
    return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function severityClass(severity) {
    if (severity === 'critical') return 'text-danger';
    if (severity === 'warning') return 'text-warning';
    return 'text-info';
}

function supplyStateLabel(supply) {
    if (typeof supply.percent === 'number') return supply.percent + '%';
    return supply.state === 'some-remaining' ? 'some remaining' : 'unknown';
}

function renderReport(data) {
    const body = document.getElementById('report-body');
    if (!body) return;
    const report = data && data.scan_report ? data.scan_report : null;
    const printerId = escapeHtml(data && data.printer_id ? data.printer_id : reportPrinterId || '');
    const lastScan = data && data.last_scan_at ? formatScanTime(data.last_scan_at) : 'just now';

    if (!report) {
        body.innerHTML =
            '<p class="text-secondary small mb-0">No scan report stored for printer <code>' + printerId +
            '</code> yet. The background scanner runs every ' + SNMP_POLL_SECONDS_DISPLAY +
            ' - or press "Re-scan" to run one now.</p>';
        return;
    }

    let html = '';
    html += '<div class="d-flex flex-wrap gap-2 mb-3">';
    html += '<span class="badge bg-primary-subtle text-primary border border-primary-subtle">Method: ' +
        escapeHtml(report.method ? report.method.toUpperCase() : 'unknown') + '</span>';
    html += '<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle">Last scan: ' +
        escapeHtml(lastScan) + '</span>';
    html += '<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle">Status: ' +
        escapeHtml(report.status || 'unknown') + '</span>';
    html += '</div>';

    html += '<table class="table table-dark table-sm mb-3"><tbody>';
    html += '<tr><td class="text-secondary">Model</td><td>' + escapeHtml(report.model || 'Not reported') + '</td></tr>';
    html += '<tr><td class="text-secondary">System name</td><td>' + escapeHtml(report.sysName || 'Not reported') + '</td></tr>';
    html += '<tr><td class="text-secondary">Total pages</td><td>' +
        (typeof report.total_pages === 'number' ? report.total_pages.toLocaleString() : 'Not reported') + '</td></tr>';
    html += '</tbody></table>';

    if (report.error) {
        html += '<div class="alert alert-danger py-2 px-3 small mb-3"><strong>' +
            escapeHtml(report.error.message || 'The printer did not respond.') + '</strong></div>';
        const tips = report.error.troubleshooting || [];
        if (tips.length > 0) {
            html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary">Troubleshooting</h6><ul class="small text-secondary mb-3">';
            tips.forEach(tip => { html += '<li>' + escapeHtml(tip) + '</li>'; });
            html += '</ul>';
        }
        body.innerHTML = html;
        return;
    }

    const findings = report.findings || [];
    html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary mb-2">Findings (' + findings.length + ')</h6>';
    if (findings.length === 0) {
        html += '<p class="small text-success mb-3"><i class="bi bi-check-circle me-1"></i>' +
            'The printer reported no problems.</p>';
    } else {
        findings.forEach(f => {
            html += '<div class="mb-2 p-2 rounded border border-secondary-subtle small">' +
                '<div class="fw-semibold ' + severityClass(f.severity) + '">' +
                '<i class="bi ' + (f.severity === 'critical' ? 'bi-x-octagon' : f.severity === 'warning' ? 'bi-exclamation-triangle' : 'bi-info-circle') + ' me-1"></i>' +
                escapeHtml(f.title || f.code || 'Finding') + '</div>' +
                '<div class="text-secondary">' + escapeHtml(f.plain_message || '') + '</div>' +
                (f.what_to_do ? '<div class="mt-1"><i class="bi bi-lightbulb text-primary me-1"></i>' + escapeHtml(f.what_to_do) + '</div>' : '') +
                '</div>';
        });
    }

    const supplies = report.supplies || [];
    if (supplies.length > 0) {
        html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary mb-2">Supplies</h6><table class="table table-dark table-sm mb-3"><tbody>';
        supplies.forEach(s => {
            const percent = s.percent;
            const barColor = percent === null || percent === undefined ? 'bg-secondary'
                : percent > 35 ? 'bg-primary' : percent > 15 ? 'bg-warning' : 'bg-danger';
            html += '<tr>' +
                '<td style="width: 40%;">' + escapeHtml(s.name) + '</td>' +
                '<td style="width: 45%;"><div class="progress" style="height: 6px;"><div class="progress-bar ' +
                barColor + '" style="width: ' + (typeof percent === 'number' ? percent : 0) + '%"></div></div></td>' +
                '<td class="text-end">' + escapeHtml(supplyStateLabel(s)) +
                (s.state === 'empty' ? ' <span class="badge bg-danger-subtle text-danger">empty</span>'
                 : s.state === 'low' ? ' <span class="badge bg-warning-subtle text-warning">low</span>' : '') +
                '</td></tr>';
        });
        html += '</tbody></table>';
    }

    const trays = report.trays || [];
    if (trays.length > 0) {
        html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary mb-2">Paper trays</h6><ul class="small mb-3">';
        trays.forEach(t => {
            html += '<li>' + escapeHtml(t.name || 'Tray') + ': ' +
                escapeHtml(typeof t.percent === 'number' ? t.percent + '%' : 'unknown') +
                (t.state && t.state !== 'available' && t.state !== 'unknown' ? ' <span class="badge bg-warning-subtle text-warning">' + escapeHtml(t.state) + '</span>' : '') +
                '</li>';
        });
        html += '</ul>';
    }

    const covers = report.covers || [];
    if (covers.length > 0) {
        html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary mb-2">Covers</h6><ul class="small mb-3">';
        covers.forEach(c => { html += '<li>' + escapeHtml(c.name || 'Cover') + ': ' + escapeHtml(c.state || 'unknown') + '</li>'; });
        html += '</ul>';
    }

    const notReported = report.not_reported || [];
    if (notReported.length > 0) {
        html += '<h6 class="fw-bold fs-xs text-uppercase text-secondary mb-2">Not reported by this printer</h6>' +
            '<p class="small text-secondary mb-0">' + notReported.map(escapeHtml).join(' &bull; ') + '</p>';
    }

    body.innerHTML = html;
}

function triggerTelemetryPoll() {
    return fetch('/api/telemetry/poll', { method: 'POST' })
        .then(response => response.json())
        .catch(error => console.error('Telemetry poll failed:', error));
}

function loadRecentJobs() {
    fetch('/api/jobs')
    .then(r => r.json())
    .then(jobs => {
        const tbody = document.getElementById('jobs-table-body');
        if (!tbody) return;
        if (!jobs || jobs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-3">No recent print jobs.</td></tr>';
            return;
        }

        let html = '';
        jobs.slice(0, 7).forEach(j => {
            html += `
                <tr>
                    <td><code>#${escapeHtml(j.id)}</code></td>
                    <td class="fw-semibold text-truncate" style="max-width: 150px;">${escapeHtml(j.job_name)}</td>
                    <td><span class="badge bg-secondary-subtle text-secondary">${escapeHtml(j.printer_id)}</span></td>
                    <td>${escapeHtml(j.pages)} pgs</td>
                    <td class="text-secondary small">${escapeHtml(j.timestamp ? (j.timestamp.split(' ')[1] || j.timestamp) : 'now')}</td>
                </tr>
            `;
        });
        tbody.innerHTML = html;
    })
    .catch(error => console.error('Could not load recent jobs:', error));
}

function loadAlerts() {
    fetch('/api/alerts')
    .then(r => r.json())
    .then(alerts => {
        const container = document.getElementById('alerts-container');
        if (!container) return;
        if (!alerts || alerts.length === 0) {
            container.innerHTML = '<p class="text-secondary small mb-0"><i class="bi bi-shield-check text-success me-2"></i>No unresolved maintenance alerts detected.</p>';
            return;
        }

        let html = '';
        alerts.slice(0, 5).forEach(a => {
            const badgeClass = a.severity === 'Critical' ? 'danger' : 'warning';
            html += `
                <div class="alert alert-${badgeClass} border-${badgeClass} py-2 px-3 mb-2 d-flex justify-content-between align-items-center">
                    <div>
                        <strong class="d-block fs-xs text-uppercase">${escapeHtml(a.printer_name)} (${escapeHtml(a.printer_id)})</strong>
                        <span class="small">${escapeHtml(a.message)}</span>
                    </div>
                    <button class="btn btn-outline-${badgeClass} btn-sm py-0 px-2 fs-xs" onclick="resolveAlert(${escapeHtml(a.id)})">Resolve</button>
                </div>
            `;
        });
        container.innerHTML = html;
    })
    .catch(error => console.error('Could not load alerts:', error));
}

function resolveAlert(alertId) {
    fetch(`/api/alerts/${alertId}/resolve`, { method: 'POST' })
    .then(r => r.json())
    .then(() => loadAlerts())
    .catch(error => console.error('Could not resolve alert:', error));
}

function refreshAlerts() {
    loadAlerts();
}
