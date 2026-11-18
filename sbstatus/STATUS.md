# Status: thread-uri, tokeni, sbstatus (2026-11-18)

## Gata și testat
- **`sbstatus.py`**: hook-urile Claude Code scriu starea fiecărui thread (working / waiting / ready / idle / done) într-un SQLite, fără apeluri de model.
  - `list`: tabel în terminal.
  - `serve`: pagină locală `http://127.0.0.1:8765`, cu buton **Settle** pe fiecare thread (token anti-CSRF).
  - `settle <feature>`: marchează thread-ul „done” și scrie `HANDOVER.md` din git (branch, ultimele 8 commituri, necomise, neîmpinse).
  - `install`: dry-run implicit, `--apply` scrie în `~/.claude/settings.json` cu backup.
- **Testat** cu evenimente simulate: toate stările, „fără semn de viață” (working > 20 min), JSON stricat, Settle cu token greșit (refuzat) și bun, `install` repetat (nu dublează hook-urile).
- **Netestat:** pe Mac și în aplicația desktop. Nu știu sigur că hook-urile pornesc pentru thread-urile din aplicație (ar trebui, folosesc același `settings.json`).

## Blocat
- **Push pe GitHub: 403.** Contul nu are acces la `evollution/python-memcached-stats`. Commit-urile `2da777a` și `8783d3a` sunt doar local, pe `claude/bonus-pool-vanzare-9rggvg`.
- **Transfer pe Mac:** nu am folder partajat, iar sesiunea SIDEBAR e inactivă.

## De făcut (tu)
1. Salvezi `sbstatus/sbstatus.py` ca `~/sellerboard/scripts/sbstatus.py`.
2. `python3 ~/sellerboard/scripts/sbstatus.py install`, apoi `--apply` dacă arată bine.
3. `python3 ~/sellerboard/scripts/sbstatus.py serve`, deschizi `http://127.0.0.1:8765`.
4. Opțional: reconectezi GitHub (claude.ai Settings → Connectors) ca să pot împinge.

## Ce am constatat despre tokeni
- Context măsurat pe 14 nov: topP ~435k, product_wip ~432k, inventory ~235k, shipments ~182k, SIDEBAR ~150k. Fiecare mesaj dintr-un thread îi reciteşte tot contextul.
- Un thread nou, cu `MODULE.md` și `HANDOVER.md`, pornește de la câteva mii de tokeni. Asta aduce economia, nu butonul în sine.
- Notificările de la `courier-handover` către SIDEBAR: 6 ture în ~1,6 h, ~900.000 de tokeni citiți din cache, toate „Nimic nou”.
- Nu am găsit dovezi că un push pornește un agent care rescrie branch-uri. Am verificat doar 5 din ~30 de sesiuni.
- Aplicația are deja butonul **Settle thread**. Nu știu exact ce face: dacă scrie un handover și dacă declanșează hook-urile.

## Neclar sau neverificat
- Ce face Settle-ul nativ al aplicației.
- `courier-handover-14nov`: la 14 nov, 30.000 de scanări fără erori, dar numărul de event listener-e și memoria paginii creșteau (730 → 3.300 listener-e, 40 MB). Starea de după nu e verificată.

## Idei discutate, neimplementate
- `MODULE.md` pe modul, plus `sb create` care generează `CLAUDE.md` cu import către el.
- `sb status` din git, ca dashboard fără model.
- Task-ul de sidebar o dată pe zi, nu din oră în oră. Există deja un „Keeper v2” pe Haiku, la 08:00, 14:00 și 20:00.
- „Release captain”: un coordonator care strânge ce e 🚀 READY și dă un singur ship.
  - Buguri găsite în sistemul de ship: thread-uri care dau merge direct, ocolind coada; `.sb-ship-state.healthy` rămas în urmă; `ship-auto.blocat` fals, imposibil de scos cu `--continue`; `~/sellerboard` nu e repo git; `test-ship-recovery.py` pică din cauza unui fișier lipsă.
- `courier-handover-14nov` a fost creat din cloud, prin SIDEBAR.
