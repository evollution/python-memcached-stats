#!/usr/bin/env python3
"""sbstatus: starea thread-urilor Claude Code (working / waiting / ready / idle / done)
scrisa automat intr-un SQLite de hook-uri. Zero tokeni: ruleaza doar cod, nu modelul.

  sbstatus.py hook                 (apelat de Claude Code, citeste JSON pe stdin)
  sbstatus.py list [--all] [--json] tabel in terminal (--json: o linie de stare per feature, pentru alte scripturi)
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
                        branch=br or "", status=st, detail=det or "", msg=lm, age=age(now - upd), upd=upd, started=_s, cwd=cwd or ""))
    out.sort(key=lambda r: (ORDER.index(r["status"]) if r["status"] in ORDER else 99, -r["upd"]))
    return out


_GIT = {}
WAIT_WORDS = ("revin", "am pornit", "în curs", "in curs")


def feature_git(cwd):
    """(necomise, neimpinse) pentru toate repo-urile din folderul feature-ului; cache 30 s."""
    m = re.match(r"(.*/features/[^/]+)", cwd or "")
    if not m:
        return None
    hit = _GIT.get(m.group(1))
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    dirty = ahead = 0
    try:
        for d in Path(m.group(1)).iterdir():
            if (d / ".git").exists():
                dirty += len([l for l in _git(d, "status", "--porcelain").splitlines() if l.strip()])
                a = _git(d, "rev-list", "--count", "@{u}..")
                ahead += int(a) if a.isdigit() else 0
    except OSError:
        return None
    _GIT[m.group(1)] = (time.time(), (dirty, ahead))
    return dirty, ahead


def verdict(g):
    """Motivele pentru care NU e bine sa dai Settle inca, plus starea git (None daca nu e feature)."""
    m, why, gi = g["main"], [], feature_git(g["main"]["cwd"])
    if g["busy"]:
        why.append("%d sub-sesiuni încă rulează" % g["busy"])
    if m["status"] == "waiting":
        why.append("te așteaptă o întrebare sau o permisiune")
    if gi and gi[0]:
        why.append("%d modificări necomise" % gi[0])
    if gi and gi[1]:
        why.append("%d commituri neîmpinse" % gi[1])
    if any(w in (m["msg"] or m["detail"])[-300:].lower() for w in WAIT_WORDS):
        why.append("ultimul mesaj spune că a pornit ceva sau revine")
    return why, gi


def groups(rows):
    """Un card per feature: thread-ul principal (cel mai vechi neinchis) + sub-sesiunile lui (review-uri etc.)."""
    by = {}
    for r in rows:
        by.setdefault(r["name"], []).append(r)
    out = []
    for name, ms in by.items():
        main = min([m for m in ms if m["status"] != "ended"] or ms, key=lambda m: m["started"])
        subs = sorted((m for m in ms if m is not main), key=lambda m: m["started"])
        busy = sum(1 for m in subs if m["status"] == "working")
        st = "working" if main["status"] in ("idle", "done") and busy else main["status"]
        out.append(dict(name=name, main=main, subs=subs, busy=busy, status=st, upd=max(m["upd"] for m in ms)))
    out.sort(key=lambda g: (ORDER.index(g["status"]) if g["status"] in ORDER else 99, -g["upd"]))
    return out


def cmd_list(show_all, as_json=False):
    if as_json:
        print(json.dumps([dict(feature=g["name"], status=g["status"], age=g["main"]["age"], subsessions=len(g["subs"]),
                               busy_subsessions=g["busy"], last=g["main"]["detail"]) for g in groups(load(show_all))],
                         ensure_ascii=False, indent=1))
        return
    rows, last = load(show_all), None
    if not rows:
        print("(nicio sesiune inregistrata)")
    for r in rows:
        if r["status"] != last:
            print("\n%s %s" % (ICON.get(r["status"], "?"), LABEL.get(r["status"], r["status"]).upper()))
            last = r["status"]
        print("  %-34s %-5s %-24s %s" % (r["name"][:34], r["age"], r["branch"][:24], r["detail"][:70]))


DOT = {"waiting":"#f5a623","ready":"#2fbf71","working":"#4c8dff","stale":"#c9b458","idle":"#8a8a8a","done":"#3ba55d","ended":"#555"}


def card_msg(r, cls=""):
    if r["msg"]:
        return "<details%s><summary>ⓘ %s</summary><pre>%s</pre></details>" % (
            cls, html.escape(r["detail"][:140] or "ultimul mesaj"), html.escape(r["msg"]))
    return "<div class=d>%s</div>" % html.escape(r["detail"][:140]) if r["detail"] else ""


def page(token="", msg=""):
    parts = ["<!doctype html><meta charset=utf-8><title>Thread-uri</title>"
             "<style>body{font:14px system-ui;background:#1b1b1b;color:#ddd;margin:24px;max-width:980px}"
             "h2{margin:24px 0 8px;font-size:15px}"
             ".card{background:#242424;border:1px solid #333;border-radius:10px;padding:10px 14px;margin:8px 0}"
             ".top{display:flex;gap:12px;align-items:center}.dot{width:10px;height:10px;border-radius:50%;flex:none}"
             ".ttl b{font-weight:600}.sub{color:#9a9a9a;font-size:12.5px;margin-top:2px}.sp{flex:1}.d{color:#999;margin-top:4px}"
             ".age{color:#9a9a9a;font-size:12.5px}.v{margin-top:8px;font-size:13px}"
             "details{margin-top:6px}summary{cursor:pointer;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
             "pre{white-space:pre-wrap;word-break:break-word;background:#1b1b1b;border-radius:6px;padding:10px;margin:8px 0 2px;"
             "color:#ddd;font:13px/1.45 ui-monospace,monospace;max-height:420px;overflow:auto}"
             ".subs{margin:10px 0 0 22px;border-left:2px solid #333;padding-left:12px}"
             ".subrow{margin:6px 0;font-size:13px}.subrow .age{margin-left:8px}"
             "button{font:inherit;cursor:pointer}</style>"
             "<script>setInterval(function(){if(!document.querySelector('details[open]'))location.reload()},5000)</script>"]
    if msg:
        parts.append("<pre>%s</pre>" % html.escape(msg))
    last = None
    for g in groups(load()):
        if g["status"] != last:
            parts.append("<h2>%s %s</h2>" % (ICON.get(g["status"], ""), html.escape(LABEL.get(g["status"], g["status"]))))
            last = g["status"]
        m = g["main"]
        busy_any = m["status"] == "working" or g["busy"]
        btn = "" if (g["status"] not in ("idle", "waiting", "ready", "stale") or busy_any) else (
            "<form method=post action=/settle style=margin:0><input type=hidden name=t value=%s>"
            "<input type=hidden name=f value='%s'><button>Settle</button></form>" % (token, html.escape(g["name"], quote=True)))
        sub = [LABEL.get(m["status"], m["status"]), html.escape(m["branch"])]
        if g["busy"]:
            sub.append("așteaptă %d sub-sesiuni" % g["busy"])
        why, gi = verdict(g)
        vline = "" if g["status"] in ("done", "ended") else (
            "<div class=v style='color:#f5a623'>⚠ Nu închide încă: %s</div>" % html.escape("; ".join(why)) if why else
            "<div class=v style='color:#2fbf71'>✓ Poți închide: nimic nu rulează%s</div>" % (", totul comis și împins" if gi else ""))
        parts.append("<div class=card><div class=top><span class=dot style='background:%s'></span>"
                     "<div class=ttl><b>%s</b><div class=sub>%s</div></div><span class=sp></span>"
                     "<span class=age>%s</span>%s</div>%s%s" % (
                         DOT.get(g["status"], "#888"), html.escape(g["name"]), " · ".join(x for x in sub if x),
                         m["age"], btn, vline, card_msg(m)))
        if g["subs"]:
            parts.append("<div class=subs>")
            for r in g["subs"]:
                parts.append("<div class=subrow>%s sub-sesiune %s<span class=age>%s</span>%s</div>" % (
                    ICON.get(r["status"], ""), html.escape(r["sid"][:6]), r["age"], card_msg(r)))
            parts.append("</div>")
        parts.append("</div>")
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
        cmd_list("--all" in argv, "--json" in argv)
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
