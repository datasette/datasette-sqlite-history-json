function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = String(text);
    return div.innerHTML;
}

function formatValue(val) {
    if (val === null || val === undefined) return "<em>null</em>";
    if (typeof val === "object") {
        if ("null" in val) return "<em>null</em>";
        if ("hex" in val) return `<code>(blob: ${escapeHtml(val.hex)})</code>`;
        return `<code>${escapeHtml(JSON.stringify(val))}</code>`;
    }
    return escapeHtml(String(val));
}

function renderEntryHeader(entry, options) {
    const header = document.createElement("div");
    header.className = "history-entry-header";

    const badge = document.createElement("span");
    badge.className = `op-badge op-${entry.operation}`;
    badge.textContent = entry.operation;
    header.appendChild(badge);

    const ts = document.createElement("span");
    ts.className = "history-timestamp";
    ts.textContent = entry.timestamp;
    header.appendChild(ts);

    if (options && options.showPkLink && entry.pk) {
        const pkSpan = document.createElement("span");
        pkSpan.className = "history-pk";
        const pkParts = Object.entries(entry.pk).map(
            ([k, v]) => `${escapeHtml(k)}=${escapeHtml(v)}`
        );
        if (options.rowHistoryUrlBase) {
            const pkValues = Object.values(entry.pk).map(v => encodeURIComponent(v)).join(",");
            const a = document.createElement("a");
            a.href = `${options.rowHistoryUrlBase}/${pkValues}`;
            a.innerHTML = pkParts.join(", ");
            pkSpan.appendChild(a);
        } else {
            pkSpan.innerHTML = pkParts.join(", ");
        }
        header.appendChild(pkSpan);
    }

    return header;
}

function renderUpdatedValues(updatedValues) {
    if (!updatedValues || Object.keys(updatedValues).length === 0) return null;
    const div = document.createElement("div");
    div.className = "history-values";
    let html = "<table><tr><th>Column</th><th>Value</th></tr>";
    for (const [col, val] of Object.entries(updatedValues)) {
        html += `<tr><td>${escapeHtml(col)}</td><td>${formatValue(val)}</td></tr>`;
    }
    html += "</table>";
    div.innerHTML = html;
    return div;
}

function renderDiff(diff) {
    if (!diff || Object.keys(diff).length === 0) return null;
    const div = document.createElement("div");
    div.className = "history-values";
    let html = "<table><tr><th>Column</th><th>Change</th></tr>";
    for (const [col, change] of Object.entries(diff)) {
        html += `<tr><td>${escapeHtml(col)}</td><td>`;
        html += `<span class="diff-old">${formatValue(change.old)}</span>`;
        html += `<span class="diff-arrow">&rarr;</span>`;
        html += `<span class="diff-new">${formatValue(change.new)}</span>`;
        html += `</td></tr>`;
    }
    html += "</table>";
    div.innerHTML = html;
    return div;
}

function renderState(state) {
    if (!state) return null;
    const details = document.createElement("details");
    details.className = "state-toggle";
    const summary = document.createElement("summary");
    summary.textContent = "Full state at this point";
    details.appendChild(summary);
    const div = document.createElement("div");
    div.className = "state-content";
    let html = "<table><tr><th>Column</th><th>Value</th></tr>";
    for (const [col, val] of Object.entries(state)) {
        html += `<tr><td>${escapeHtml(col)}</td><td>${formatValue(val)}</td></tr>`;
    }
    html += "</table>";
    div.innerHTML = html;
    details.appendChild(div);
    return details;
}

export async function renderTableHistory(options) {
    const { apiUrl, database, table, entriesEl, paginationEl, filtersEl } = options;
    let currentPage = 1;
    let currentOperation = "";

    const rowHistoryUrlBase = apiUrl.replace(/\.json$/, "");

    async function loadPage() {
        entriesEl.innerHTML = "<p>Loading...</p>";
        let url = `${apiUrl}?page=${currentPage}`;
        if (currentOperation) {
            url += `&operation=${currentOperation}`;
        }
        const resp = await fetch(url);
        const data = await resp.json();

        entriesEl.innerHTML = "";
        if (!data.ok) {
            entriesEl.innerHTML = `<p class="no-entries">${escapeHtml(data.error)}</p>`;
            return;
        }

        if (data.entries.length === 0) {
            entriesEl.innerHTML = '<p class="no-entries">No entries found.</p>';
            paginationEl.innerHTML = "";
            return;
        }

        for (const entry of data.entries) {
            const el = document.createElement("div");
            el.className = "history-entry";
            el.appendChild(renderEntryHeader(entry, { showPkLink: true, rowHistoryUrlBase }));
            const vals = renderUpdatedValues(entry.updated_values);
            if (vals) el.appendChild(vals);
            entriesEl.appendChild(el);
        }

        // Pagination
        const totalPages = Math.ceil(data.total_count / data.page_size);
        paginationEl.innerHTML = "";
        if (totalPages > 1) {
            if (currentPage > 1) {
                const prev = document.createElement("button");
                prev.textContent = "Previous";
                prev.onclick = () => { currentPage--; loadPage(); };
                paginationEl.appendChild(prev);
            }
            const info = document.createElement("span");
            info.className = "page-info";
            info.textContent = `Page ${currentPage} of ${totalPages} (${data.total_count} entries)`;
            paginationEl.appendChild(info);
            if (currentPage < totalPages) {
                const next = document.createElement("button");
                next.textContent = "Next";
                next.onclick = () => { currentPage++; loadPage(); };
                paginationEl.appendChild(next);
            }
        }
    }

    // Filter buttons
    if (filtersEl) {
        filtersEl.addEventListener("click", (e) => {
            if (e.target.classList.contains("filter-btn")) {
                filtersEl.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
                e.target.classList.add("active");
                currentOperation = e.target.dataset.operation;
                currentPage = 1;
                loadPage();
            }
        });
    }

    loadPage();
}

export async function renderRowHistory(options) {
    const { apiUrl, database, table, entriesEl } = options;

    entriesEl.innerHTML = "<p>Loading...</p>";
    const resp = await fetch(apiUrl);
    const data = await resp.json();

    entriesEl.innerHTML = "";
    if (!data.ok) {
        entriesEl.innerHTML = `<p class="no-entries">${escapeHtml(data.error)}</p>`;
        return;
    }

    if (data.entries.length === 0) {
        entriesEl.innerHTML = '<p class="no-entries">No history entries found.</p>';
        return;
    }

    for (const entry of data.entries) {
        const el = document.createElement("div");
        el.className = "history-entry";
        el.appendChild(renderEntryHeader(entry));

        if (entry.diff) {
            el.appendChild(renderDiff(entry.diff));
        } else if (entry.updated_values) {
            const vals = renderUpdatedValues(entry.updated_values);
            if (vals) el.appendChild(vals);
        }

        const stateEl = renderState(entry.state);
        if (stateEl) el.appendChild(stateEl);

        entriesEl.appendChild(el);
    }
}
