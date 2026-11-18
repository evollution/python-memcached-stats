#!/usr/bin/env python3
"""sbstatus: starea thread-urilor Claude Code (working / waiting / ready / idle / done)
scrisa automat intr-un SQLite de hook-uri. Zero tokeni: ruleaza doar cod, nu modelul.

  sbstatus.py hook                 (apelat de Claude Code, citeste JSON pe stdin)
  sbstatus.py list [--all]         tabel in terminal
  sbstatus.py serve [--port 8765]  pagina locala cu refresh automat
  sbstatus.py settle <feature>     inchide munca pe modul: stare 'done' + HANDOVER.md generat din git (fara model)
  sbstatus.py install [--apply]    arata (sau scrie, cu backup) hook-urile in ~/.claude/settings.json

Hook-ul NU scrie nimic pe stdout: la SessionStart/UserPromptSubmit stdout-ul ar intra in contextul modelului.
"""
import contextlib
import html
import http.server
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

DB = Path(os.environ.get("SBSTATUS_DB", str(Path.home() / ".claude" / "status" / "status.db")))
STALE_WORKING_S = 20 * 60          # "working" fara semn de viata > 20 min => probabil intrerupt
ORDER = ["waiting", "ready", "working", "stale", "idle", "done", "ended"]
ICON = {"waiting": "🙋", "ready": "🚀", "working": "⏳", "stale": "❔", "idle": "💤", "done": "✅", "ended": "▫️"}
LABEL = {"waiting": "Te asteapta", "ready": "Gata de livrare", "working": "In lucru",
         "stale": "In lucru? (fara semn de viata)", "idle": "Idle", "done": "Gata", "ended": "Inchise"}
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Notification", "Stop", "SessionEnd"]


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
        session_id TEXT PRIMARY KEY, feature TEXT, module TEXT, cwd TEXT, branch TEXT,
        status TEXT, detail TEXT, started_at REAL, updated_at REAL)""")
    try:
        c.execute("ALTER TABLE sessions ADD COLUMN last_msg TEXT")   # migrare pentru baze mai vechi
    except sqlite3.OperationalError:
        pass
    return c


def feature_of(cwd):
    m = re.search(r"/features/([^/]+)", cwd or "")
    if not m:
        return None, None
    feature = m.group(1)
    return feature, re.sub(r"-\d{1,2}[a-z]{3}$", "", feature)   # shipments-13nov -> shipments


def git_branch(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() or None
    except Exception:
        return None


def last_assistant_text(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        c = (o.get("message") or {}).get("content")
        text = c if isinstance(c, str) else "\n".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    return ""


def classify(text):
    """Starea din primul rand al ultimului mesaj (marcajul din CLAUDE.md), apoi din ultimul rand."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    first = lines[0] if lines else ""
    if first.startswith(("🙋", "❓")):
        return "waiting", first[:120]
    if first.startswith("🚀"):
        return "ready", first[:120]
    if first.startswith("✅"):
        return "done", first[:120]
    if lines and lines[-1].endswith("?"):
        return "waiting", lines[-1][:120]          # modelul pune o intrebare
    return "idle", first[:120]


def hook():
    p = json.load(sys.stdin)
    ev, sid = p.get("hook_event_name"), p.get("session_id")
    if not sid:
        return
    cwd = p.get("cwd") or os.getcwd()
    status, detail, branch, msg = None, None, None, None
    if ev == "SessionStart":
        status, branch = "idle", git_branch(cwd)
    elif ev == "UserPromptSubmit":
        status, detail = "working", ""
    elif ev == "PostToolUse":                       # a primit permisiunea si a continuat
        c = connect()
        row = c.execute("SELECT status FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if row and row[0] in ("waiting", "stale"):
            c.execute("UPDATE sessions SET status='working', updated_at=? WHERE session_id=?", (time.time(), sid))
            c.commit()
        elif row:
            c.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (time.time(), sid))
            c.commit()
        return
    elif ev == "Notification":
        msg = (p.get("message") or "").lower()
        if p.get("notification_type") in ("permission_prompt", "elicitation_dialog") or "permission" in msg:
            status, detail = "waiting", "cere permisiune"
        else:
            return
    elif ev == "Stop":
        text = p.get("last_assistant_message") or last_assistant_text(p.get("transcript_path", ""))
        status, detail = classify(text)
        msg = text[:6000]
        branch = git_branch(cwd)
    elif ev == "SessionEnd":
        status = "ended"
    else:
        return
    feature, module = feature_of(cwd)
    now = time.time()
    c = connect()
    c.execute("""INSERT INTO sessions(session_id,feature,module,cwd,branch,status,detail,started_at,updated_at,last_msg)
                 VALUES(?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(session_id) DO UPDATE SET
                   cwd=excluded.cwd, feature=excluded.feature, module=excluded.module,
                   branch=COALESCE(excluded.branch, branch), status=excluded.status,
                   detail=COALESCE(excluded.detail, detail), updated_at=excluded.updated_at,
                   last_msg=COALESCE(excluded.last_msg, last_msg)""",
              (sid, feature, module, cwd, branch, status, detail, now, now, msg))
    c.commit()


def age(s):
    s = int(max(0, s))
    return "%dz" % (s // 86400) if s >= 86400 else "%dh" % (s // 3600) if s >= 3600 \
        else "%dm" % (s // 60) if s >= 60 else "%ds" % s


def load(show_all=False):
    now, out = time.time(), []
    c = connect()
    for sid, feat, mod, cwd, br, st, det, _s, upd, lm in c.execute(
            "SELECT session_id,feature,module,cwd,branch,status,detail,started_at,updated_at,COALESCE(last_msg,'') FROM sessions"):
        if st == "ended" and not show_all and now - upd > 3600:
            continue
        if st == "working" and now - upd > STALE_WORKING_S:
            st = "stale"
        out.append(dict(sid=sid, name=feat or (Path(cwd).name if cwd else "?"), module=mod or "-",
                        branch=br or "", status=st, detail=det or "", msg=lm, age=age(now - upd), upd=upd))
    out.sort(key=lambda r: (ORDER.index(r["status"]) if r["status"] in ORDER else 99, -r["upd"]))
    return out


def cmd_list(show_all):
    rows, last = load(show_all), None
    if not rows:
        print("(nicio sesiune inregistrata)")
    for r in rows:
        if r["status"] != last:
            print("\n%s %s" % (ICON.get(r["status"], "?"), LABEL.get(r["status"], r["status"]).upper()))
            last = r["status"]
        print("  %-34s %-5s %-24s %s" % (r["name"][:34], r["age"], r["branch"][:24], r["detail"][:70]))


def page(token="", msg=""):
    parts = ["<!doctype html><meta charset=utf-8><title>Thread-uri</title>"
             "<style>body{font:14px system-ui;background:#1b1b1b;color:#ddd;margin:24px;max-width:980px}"
             "h2{margin:22px 0 8px;font-size:15px}"
             ".card{background:#242424;border:1px solid #333;border-radius:8px;padding:8px 12px;margin:6px 0}"
             ".top{display:flex;gap:12px;align-items:center}.top b{font-weight:600}.sp{flex:1}.d{color:#999}"
             "details{margin-top:4px}summary{cursor:pointer;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
             "pre{white-space:pre-wrap;word-break:break-word;background:#1b1b1b;border-radius:6px;padding:10px;margin:8px 0 2px;"
             "color:#ddd;font:13px/1.45 ui-monospace,monospace;max-height:420px;overflow:auto}"
             "button{font:inherit;cursor:pointer}</style>"
             "<script>setInterval(function(){if(!document.querySelector('details[open]'))location.reload()},5000)</script>"]
    if msg:
        parts.append("<pre>%s</pre>" % html.escape(msg))
    last = None
    for r in load():
        if r["status"] != last:
            parts.append("<h2>%s %s</h2>" % (ICON.get(r["status"], ""), html.escape(LABEL.get(r["status"], r["status"]))))
            last = r["status"]
        btn = "" if r["status"] not in ("idle", "waiting", "ready", "stale") else (
            "<form method=post action=/settle style=margin:0><input type=hidden name=t value=%s>"
            "<input type=hidden name=f value='%s'><button>Settle</button></form>" % (token, html.escape(r["name"], quote=True)))
        if r["msg"]:
            body = "<details><summary>ⓘ %s</summary><pre>%s</pre></details>" % (
                html.escape(r["detail"][:140] or "ultimul mesaj"), html.escape(r["msg"]))
        else:
            body = "<div class=d>%s</div>" % html.escape(r["detail"][:140]) if r["detail"] else ""
        parts.append("<div class=card><div class=top><b>%s</b><span class=d>%s</span><span class=d>%s</span>"
                     "<span class=sp></span>%s</div>%s</div>" % (
                         html.escape(r["name"]), r["age"], html.escape(r["branch"]), btn, body))
    if last is None:
        parts.append("<p>Nicio sesiune inregistrata.</p>")
    return "".join(parts).encode()


def cmd_serve(port):
    token, msg = secrets.token_hex(8), [""]

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            if self.path == "/settle" and form.get("t", [""])[0] == token and form.get("f"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cmd_settle(form["f"][0])
                msg[0] = buf.getvalue()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self):
            body = page(token, msg[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    print("http://127.0.0.1:%d  (Ctrl+C opreste)" % port)
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()


def _git(d, *a):
    try:
        return subprocess.run(["git", "-C", str(d)] + list(a), capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def cmd_settle(pattern):
    """Inchide thread-urile unui feature si scrie HANDOVER.md din git, ca urmatorul thread sa porneasca curat."""
    c = connect()
    like, stale = "%" + pattern + "%", time.time() - STALE_WORKING_S
    busy = c.execute("SELECT COUNT(*) FROM sessions WHERE status='working' AND updated_at>=? AND (feature LIKE ? OR cwd LIKE ?)",
                     (stale, like, like)).fetchone()[0]
    rows = c.execute("SELECT session_id,cwd FROM sessions WHERE status NOT IN ('ended','done') "
                     "AND (status!='working' OR updated_at<?) AND (feature LIKE ? OR cwd LIKE ?)",
                     (stale, like, like)).fetchall()
    if busy:
        print("%d thread(uri) in lucru acum, nu le ating." % busy)
    if not rows:
        print("Nicio sesiune activa care sa se potriveasca cu %r" % pattern)
        return
    root = next((m.group(1) for m in (re.match(r"(.*/features/[^/]+)", r[1] or "") for r in rows) if m), None)
    c.execute("UPDATE sessions SET status='done', detail='settled', updated_at=? WHERE session_id IN (%s)"
              % ",".join("?" * len(rows)), [time.time()] + [r[0] for r in rows])
    c.commit()
    print("%d sesiune(i) marcate 'done'." % len(rows))
    if not root or not Path(root).is_dir():
        print("Nu am gasit folderul de feature, deci nu scriu HANDOVER.md.")
        return
    out = ["# HANDOVER %s (inchis %s)" % (Path(root).name, time.strftime("%Y-%m-%d %H:%M")), ""]
    for d in sorted(Path(root).iterdir()):
        if not (d / ".git").exists():
            continue
        out += ["## %s (branch %s)" % (d.name, _git(d, "rev-parse", "--abbrev-ref", "HEAD")), "",
                "Ultimele commituri:", _git(d, "log", "--oneline", "-8") or "(niciunul)", "",
                "Necomise:", _git(d, "status", "--short") or "(curat)", "",
                "Neimpinse:", _git(d, "log", "--oneline", "@{u}..") or "(nimic sau fara upstream)", ""]
    out += ["## Thread nou", "Citeste MODULE.md si acest fisier, apoi continua. Nu reciti tot codul."]
    (Path(root) / "HANDOVER.md").write_text("\n".join(out) + "\n")
    print("Scris %s/HANDOVER.md. Thread nou: cd %s && claude" % (root, root))


def cmd_install(apply):
    settings = Path.home() / ".claude" / "settings.json"
    data = json.loads(settings.read_text()) if settings.exists() else {}
    cmd = "python3 %s hook" % Path(__file__).resolve()
    hooks, added = data.setdefault("hooks", {}), []
    for ev in EVENTS:
        groups = hooks.setdefault(ev, [])
        if any(cmd in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if ev == "PostToolUse":
            group["matcher"] = "*"
        groups.append(group)
        added.append(ev)
    print("Hook-uri de adaugat in %s: %s" % (settings, ", ".join(added) or "(niciunul, deja instalat)"))
    if not apply:
        print("Dry-run. Rulează cu --apply ca să scrii (cu backup).")
        return
    if added:
        if settings.exists():
            shutil.copy2(str(settings), str(settings) + ".bak-sbstatus-%d" % int(time.time()))
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("Scris. Hook-urile se aplica thread-urilor pornite de acum.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "hook":
        try:
            hook()
        except Exception:
            pass                                    # un hook nu trebuie sa strice niciodata un thread
        return 0
    if cmd == "list":
        cmd_list("--all" in argv)
    elif cmd == "serve":
        cmd_serve(int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765)
    elif cmd == "settle" and len(argv) > 2:
        cmd_settle(argv[2])
    elif cmd == "install":
        cmd_install("--apply" in argv)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
