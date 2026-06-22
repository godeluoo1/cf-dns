import re

with open('cf.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove meta refresh
content = re.sub(r' *<meta http-equiv="refresh" content="60">\n', '', content)

# 2. Add classes to HTML template
content = content.replace('<div class="line-row {color_cls}">', '<div class="line-row {color_cls}" data-sub="{sub}" data-line="{line_key}">')
content = content.replace('<span class="metric-val">{latency_str}</span>', '<span class="metric-val latency-val">{latency_str}</span>')
content = content.replace('<span class="metric-val">{jitter_str}</span>', '<span class="metric-val jitter-val">{jitter_str}</span>')
content = content.replace('<span class="metric-val" style="color: {loss_color}">{loss_str}</span>', '<span class="metric-val loss-val" style="color: {loss_color}">{loss_str}</span>')
content = content.replace('<span class="metric-val" style="color: {rep_color}">{reputation}</span>', '<span class="metric-val rep-val" style="color: {rep_color}">{reputation}</span>')
content = content.replace('<div class="line-cname" title="{cname}">{cname}</div>', '<div class="line-cname cname-val" title="{cname}">{cname}</div>')

# 3. Add JS script
script = """
<script>
async function pollUpdate() {
    try {
        const res = await fetch('./state_snapshot.json?t=' + Date.now());
        if (!res.ok) throw new Error();
        const newState = await res.json();
        reconcile(newState);
    } catch(e) {}
    setTimeout(pollUpdate, 60000);
}

function updateEl(el, val, color) {
    if (!el || el.textContent === String(val)) return;
    el.textContent = val;
    if (color) el.style.color = color;
    el.classList.add('updated');
    setTimeout(() => el.classList.remove('updated'), 600);
}

function reconcile(newState) {
    if (!newState.data) return;
    for (const [sub, lines] of Object.entries(newState.data)) {
        for (const [line_key, stats] of Object.entries(lines)) {
            const row = document.querySelector(`.line-row[data-sub="${sub}"][data-line="${line_key}"]`);
            if (row) {
                const cnameEl = row.querySelector('.cname-val');
                if (cnameEl && cnameEl.textContent !== stats.cname) {
                    cnameEl.textContent = stats.cname;
                    cnameEl.title = stats.cname;
                }
                updateEl(row.querySelector('.latency-val'), stats.latency);
                updateEl(row.querySelector('.jitter-val'), stats.jitter);
                updateEl(row.querySelector('.loss-val'), stats.loss, stats.loss_color);
                updateEl(row.querySelector('.rep-val'), stats.reputation, stats.rep_color);
            }
        }
    }
}

const style = document.createElement('style');
style.textContent = `
    .metric-val.updated { animation: number-tick 0.5s cubic-bezier(0.34, 1.56, 0.64, 1); }
    @keyframes number-tick { 0% { transform: translateY(-4px); opacity: 0.3; } 60% { transform: translateY(1px); } 100% { transform: translateY(0); opacity: 1; } }
`;
document.head.appendChild(style);
setTimeout(pollUpdate, 60000);
</script>
</body>"""
content = content.replace('</body>', script)

# 4. Modify .card CSS
card_css_old = """        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 1.75rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 10px 15px -3px rgba(0, 0, 0, 0.2);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            transition: transform 0.3s ease, border-color 0.3s ease;
            animation: fadeInUp 0.8s ease-out backwards;
            margin-bottom: 1.5rem;
        }"""
card_css_new = """        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 1.75rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 10px 15px -3px rgba(0, 0, 0, 0.2);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            transition: transform 0.3s ease, border-color 0.3s ease;
            animation: fadeInUp 0.8s ease-out backwards;
            margin-bottom: 1.5rem;
            will-change: transform;
            position: relative;
            overflow: hidden;
        }
        .card::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
            background: linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.18) 30%, rgba(255, 255, 255, 0.35) 50%, rgba(255, 255, 255, 0.18) 70%, transparent 100%);
            pointer-events: none;
        }
        .card::after {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 40%;
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.025) 0%, transparent 100%);
            pointer-events: none; border-radius: 20px 20px 0 0;
        }"""
content = content.replace(card_css_old, card_css_new)

# 5. Add state_snapshot.json logic
# Find "with open(filepath, 'w', encoding='utf-8') as f:\n            f.write(html_content)"
# And replace with the snapshot logic
snapshot_logic = """        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        # 额外生成前端所需的轻量级快照
        snapshot_data = {"ts": current_time, "data": {}}
        for sub, monitor_type in SUB_DOMAINS_CONFIG.items():
            snapshot_data["data"][sub] = {}
            champs = state_manager.state.get("champions", {}).get(sub, {
                "Dianxin": "N/A", "Yidong": "N/A", "Liantong": "N/A", "default_view": "N/A"
            })
            for line_key in ["Dianxin", "Yidong", "Liantong", "default_view"]:
                cname = champs.get(line_key, "N/A")
                latency, loss, jitter = get_cname_stats(cname, sub, line_key)
                reputation = get_reputation(sub, line_key, cname) if cname != "N/A" else 0
                
                latency_str = f"{latency} ms" if isinstance(latency, (int, float)) and latency < 9999 else str(latency)
                loss_str = f"{loss} %" if isinstance(loss, (int, float)) else str(loss)
                jitter_str = f"{jitter} ms" if isinstance(jitter, (int, float)) else str(jitter)
                
                loss_color = "var(--loss-ok)"
                if isinstance(loss, (int, float)):
                    if loss > 5.0: loss_color = "var(--color-danger)"
                    elif loss > 1.0: loss_color = "var(--loss-warn)"
                
                rep_color = "var(--color-success)"
                if reputation < 30: rep_color = "var(--color-danger)"
                elif reputation < 60: rep_color = "var(--color-warning)"
                
                snapshot_data["data"][sub][line_key] = {
                    "cname": cname, "latency": latency_str, "loss": loss_str, "jitter": jitter_str,
                    "reputation": reputation, "loss_color": loss_color, "rep_color": rep_color
                }
        snapshot_path = os.path.join(state_dir, "state_snapshot.json") if state_dir else "state_snapshot.json"
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot_data, f, ensure_ascii=False)
"""
content = content.replace("        with open(filepath, 'w', encoding='utf-8') as f:\n            f.write(html_content)", snapshot_logic)

with open('cf.py', 'w', encoding='utf-8') as f:
    f.write(content)
