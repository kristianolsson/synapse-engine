# QNAP Migration Guide — Plugin System Cutover

This document walks through migrating the **live** QNAP `synapse-engine`
deployment from the old always-everything-on architecture to the plugin
system (service registry, `ENABLED_SERVICES`, `apply()`, `./synapse`).

**⚠️ Prerequisite: you must have pushed the refactored `synapse-engine` to
`github.com/kristianolsson/synapse-engine` first.** QNAP's `synapse-engine`
folder is a `git clone` of that remote — it cannot receive any of this code
any other way. Do not start this guide until you've pushed and are confident
in the local Mac testing from the implementation plan.

## 1. Back up QNAP before touching anything

SSH into QNAP:
```bash
ssh admin@<QNAP_IP>
cd /share/CE_CACHEDEV2_DATA/synapse
tar -czf ~/synapse-qnap-backup-$(date +%Y%m%d).tar.gz \
  notes synapse-engine .env credentials
```
Copy it off the QNAP box (to the Mac, for safety):
```bash
# From the Mac:
scp admin@<QNAP_IP>:~/synapse-qnap-backup-*.tar.gz ~/Documents/code/
```
Do not proceed until this backup exists somewhere off the QNAP box.

## 2. Confirm the templates were seeded from THIS vault

The implementation plan's Tasks 4-7 seeded every `services/<name>/PROTOCOL.md`
template by copying the live content from the `notes/` clone on the Mac —
the same content that's live on QNAP right now (both are clones of the same
`notes` repo). This is what makes the migration safe. Before proceeding,
spot check on the Mac:
```bash
cd /Users/kristianolsson/Documents/code/synapse-engine
diff services/ingestion/services/calendar/PROTOCOL.md /Users/kristianolsson/Documents/code/notes/calendar/PROTOCOL.md
diff services/ingestion/services/reminder/PROTOCOL.md /Users/kristianolsson/Documents/code/notes/reminders/PROTOCOL.md
```
Expected: no diff output for either. If either shows a diff, **stop** —
someone edited a `PROTOCOL.md` (locally or upstream) since the templates
were seeded; resolve the discrepancy before continuing, or the QNAP vault
will get overwritten with stale content.

## 3. Set ENABLED_SERVICES on QNAP's .env

SSH into QNAP:
```bash
cd /share/CE_CACHEDEV2_DATA/synapse
grep "^ENABLED_CHANNELS=" .env   # note the old value for reference, then:
sed -i '/^ENABLED_CHANNELS=/d' .env
echo "ENABLED_SERVICES=email,telegram,calendar,gmail,reminder,etrade,options-bot,amazon-fresh" >> .env
```
**This exact list matters.** Every one of these was unconditionally available
before this migration — omitting any of them here is a silent regression.
`reminder` is the single highest-risk omission: it ran unconditionally in the
old `main.py` and is easy to forget since it was never something you
"enabled" before.

## 4. Pull, rebuild, restart

```bash
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
git pull
./synapse update
```
(`./synapse update` will detect this pull touched more than just
`Dockerfile`/`requirements.txt` isn't the right signal here for a first-time
migration with a large diff — for this one-time cutover, force a rebuild
explicitly instead: `docker compose build && docker compose up -d`.)

## 5. Verify

```bash
docker compose logs --tail=50
```
Look for:
- `Enabled services: amazon-fresh, calendar, email, etrade, gmail, options-bot, reminder, telegram` (all 8, alphabetical)
- No `RegistryError` or startup exceptions
- The reminder scheduler thread starting (`Reminder scheduler enabled.`)

Then check the vault:
```bash
cd /share/CE_CACHEDEV2_DATA/synapse/notes
git log -1 --stat
```
Expected: either no new commit (if `apply()` found everything already in
sync, per step 2's confirmation) or a single new commit titled
`synapse-engine apply(): sync service protocols` touching only the
`CLAUDE.md` router-marker block — nothing else. If you see any other file
changed, **stop and investigate** (`git diff HEAD~1`) before trusting the
deploy — do not let a bad `apply()` run compound with further commits.

Functionally: send a test message via Telegram, confirm a reply. Create a
test reminder and confirm it still fires on schedule.

## 6. Rollback

If anything is wrong:
```bash
cd /share/CE_CACHEDEV2_DATA/synapse
docker compose down
# Restore from the step-1 backup:
tar -xzf ~/synapse-qnap-backup-<date>.tar.gz -C /share/CE_CACHEDEV2_DATA/synapse
cd synapse-engine
git log --oneline -5   # find the commit SHA from before this migration
git checkout <prior-sha>
docker compose build && docker compose up -d
```
