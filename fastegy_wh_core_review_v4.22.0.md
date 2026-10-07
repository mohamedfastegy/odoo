# fastegy_wh_core — Code Review & Fix Brief

| | |
|---|---|
| Module | `fastegy_wh_core` |
| Version reviewed | `18.0.4.22.0` |
| Odoo | 18.0 (Odoo.sh) |
| Review date | 2026-10-07 |
| Scope | Full module: models, wizards, controllers, portal/PWA JS, views, reports, data, security, migrations, hooks, tests |
| Method | Static review (Odoo not runnable in the review environment). Every finding below was traced through the actual code path; items marked ⚠️ need a runtime confirmation on staging before fixing. |

---

## 0. Instructions for Claude Code

You are receiving a review of a module you will be fixing. Work like this:

1. **Verify before you fix.** For every finding, open the cited lines and confirm the problem still exists in the current code. If a finding is wrong or already fixed, say so and skip it. Line numbers refer to v4.22.0.
2. **Work phase by phase** (Phase 1 → 4). Finish, test and report one phase before starting the next. Do not mix phases in one commit.
3. **Ask the owner before any change that alters or deletes production data** (data-repair migrations, cleaning placeholder AWBs, voiding serials). Propose the SQL/script first.
4. **Keep the house style.** Arabic comments with version tags (e.g. `# v4.23.0: ...`), `self.env._(...)` for translatable strings, Odoo 18 view syntax (`invisible=`, `<list>`, `<chatter/>`).
5. **Bump the version** in `__manifest__.py` per phase (suggested: Phase 1 → `18.0.4.23.0`, Phase 2 → `18.0.4.24.0`, …) and add a `migrations/<version>/` script only where existing data must be repaired.
6. **Add a regression test for every fixed finding** in the existing test style. Add `HttpCase` tests for the portal routes (there are none today — that is how `P1-07` shipped broken). Consider splitting `tests/test_scan_engine.py` (8,000 lines, 44 classes) into one file per feature.
7. **Do not break what already works** — see section 6.
8. Run the module tests after each phase:
   `odoo-bin -d <db> -u fastegy_wh_core --test-enable --test-tags /fastegy_wh_core --stop-after-init`

Severity legend: 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low

---

## Summary

| Phase | Theme | Findings |
|---|---|---|
| 1 | Security & hard blockers | 10 |
| 2 | Money: AWBs, COD, settlements, notifications | 24 |
| 3 | Scanner, offline queue, stock integrity | 21 |
| 4 | Migrations, crons, settings, dependencies, cleanup | 12 + low-priority table |

Top risks:
1. Public model methods that run with `sudo()` can be called by **any** authenticated user (including customer portal accounts) through `/web/dataset/call_kw`: stored XSS against admins and WhatsApp sending from the company number.
2. Several stored-XSS sinks (`Html` fields with `sanitize=False`, `innerHTML` in the portal JS) fed by portal-user-controlled text.
3. Money consistency: an AWB can be settled twice; "add missing" marks unpaid AWBs as settled; grouped shipments show different amounts on the label, the AWB and the customer message; kill-switch settings silently turn themselves back on.
4. Offline queue is not bound to a session or user, and session expiry is never detected, so scans can land on the wrong session or user.

---

## Phase 1 — Security & hard blockers

### P1-01 🔴 Public `touch()` writes device telemetry with sudo → stored XSS against WH Managers/admin
- **Where:** `models/wh_device.py:202-245` (`touch`), `models/wh_device.py:92,132-200` (`health_html`, `sanitize=False`), `views/wh_device_views.xml:37`; `controllers/portal.py:229` (`last_ip` from raw `X-Forwarded-For`).
- **Problem:** `touch()` has no leading underscore, does no access check and ends with `self.sudo().write(vals)`. Odoo 18 `call_kw` only checks that the method is public, so any logged-in user can call it on any device id. `health_html` interpolates `device_model`, `os_info`, `browser_info`, `last_ip`, etc. without escaping.
- **Scenario:** a portal customer calls `call_kw('fastegy.wh.device', 'touch', [[<id>]], {'info': {'device': {'model': '<img src=x onerror=...>'}}})`. When a WH Manager (and `base.user_admin` is auto-added to `group_wh_manager`) opens the device form, the script runs in their session and can do anything they can.
- **Fix:**
  - Rename to `_touch` (update callers in `controllers/portal.py` and `scan_event.py:270`) or decorate with `@api.private`.
  - Escape every interpolated value in `_compute_health` with `markupsafe.escape` (or render via QWeb), and validate/clamp telemetry types (ints within int4, strings length-capped).
  - Take the client IP from `request.httprequest.remote_addr` (Odoo.sh runs with `proxy_mode`), not the raw header.
- **Test:** calling `touch` via `call_kw` as a portal user without the scanner group must fail; a value containing `<img onerror>` must render escaped.

### P1-02 🔴 Public `send()` lets any user send WhatsApp from the company number
- **Where:** `models/fastegy_wa.py:391`.
- **Problem:** `send(template_key, numbers, vals, ...)` is public and runs the gateway call with `sudo()`. Template variables such as `{text}` / `{lines}` give full control of the content.
- **Scenario:** `call_kw('fastegy.wh.wa.log', 'send', [[]], {'template_key': 'ship_notify_sales_fail', 'numbers': '2010...,2011...', 'vals': {'text': '<phishing link>'}})` from any portal account → message is sent from the official FastEgy number to arbitrary numbers. Phishing risk and risk of the number being banned.
- **Fix:** rename to `_send` (update all callers) or `@api.private`. If a public entry point is needed, add an explicit group check.
- **Test:** `call_kw` of `send` as a portal user must raise.

### P1-03 🟠 Other public sudo entry points — audit all of them
- **Where:** `models/scan_event.py:575` (`process_offline_batch`), plus methods flagged by a static scan: `stock_picking.action_pull_intake`, `stock_picking.action_fg_notify_approve`, `ship_wizard.action_save`, `awb_collect_wizards.action_apply`, `serial_rule.action_check_coverage`, `wh_session.action_recheck_pending`, `scan_event.action_void_pending`.
- **Problem:** `process_offline_batch` is `@api.model`, public, and loads the session with `sudo().browse(session_id)` from a caller-supplied id, then `message_post`s on it and calls `_notify_offline_conflicts`, which WhatsApps every supervisor with raw (untruncated) barcodes.
- **Scenario:** a portal user loops over session ids with junk barcodes → chatter spam on every session + WhatsApp alerts with attacker-chosen text.
- **Fix:** make `process_offline_batch`, `process_scan_bulk`, `process_scan`, `add_quantity` private (`_`-prefixed) — they are only called from the controller. For each method in the list above, decide: private, or an explicit group/ownership check at the top. Truncate and escape barcodes in notices.
- **Test:** each audited method called via `call_kw` as a portal user must raise.

### P1-04 🟠 Stored XSS sinks fed by portal-user-controlled text
Portal workers can set their own name via `/my/account` and type free-text notes in the scanner. These values reach the following sinks unescaped:

| Sink | Source of attacker text | Victim |
|---|---|---|
| `models/wh_session.py:58,268-277` `review_html` (`sanitize=False`), shown at `views/wh_session_views.xml:110` | `worker_shortage_note` from `/fastegy/wh/submit` (`controllers/portal.py:706-709`, 200 chars is enough) | Supervisor/admin opening the session to approve |
| `static/src/portal/portal_scanner.js:477-479` `sesswarn_txt.innerHTML` | `bn.owner` = `session.user_id.name` (`controllers/portal.py:97`) | Manager opening a worker session via `action_open_scanner` (same origin as backend) |
| `static/src/portal/portal_scanner.js:1576-1579` `short_state.innerHTML` (also labels at `:1591-1594`) | `shortage_note` from `/fastegy/wh/shortage` (`wh_session.py:1031`) | Same |
| `wizard/split_picking.py:83,131` (`sanitize=False`) | `user.name` | Supervisor/admin opening the split wizard |
| `wizard/intake_pull.py:62-67`, `wizard/intake_create.py:236-243,261` | product names | Manager |
| `models/fastegy_wa.py:596` daily-report e-mail HTML | names/notes | E-mail recipients |

- **Fix:** escape every interpolated value (`markupsafe.escape` / `odoo.tools.html_escape` on the Python side, the existing `esc()` helper or `textContent` on the JS side). Prefer `sanitize=True` where the HTML is static markup. The Odoo 18 read-only HTML viewer does not sanitize, so `onerror` handlers fire.
- **Test:** a name/note containing `<img src=x onerror=alert(1)>` renders as text in each sink.

### P1-05 🟠 COD finance approval can be forged by a salesperson
- **Where:** `models/sale_order_ship.py:66-79` (fields), `write()` at `:496-525`.
- **Problem:** `fg_cod_approved_by`, `fg_cod_approved_amount`, `fg_cod_approved_at`, `fg_cod_rejected`, `fg_credit_bypassed` are `readonly=True` on the field only — that is a UI hint; the ORM accepts RPC writes.
- **Scenario:** a salesperson writes `{'fg_cod_approved_by': <any uid>, 'fg_cod_approved_amount': <fg_cod_amount>}` → `fg_cod_approval_state` becomes `approved`, opening the AWB, label and validate gates (`stock_picking._fg_cod_approval_ok`). If credit bypass is enabled, `_fg_credit_bypass_sync` also turns `check_credit` off.
- **Fix:** in `sale.order.write`, reject these keys unless `self.env.su` or the user passes `_fg_cod_user_is_finance()`. The approve/reject wizards should check finance rights, then write with `sudo()`. Do not rely on context flags as a guard (the client controls the context).
- **Test:** a sales user writing these fields gets `AccessError`; the finance wizard still works.

### P1-06 🟠 `_logger` used but never defined → error handlers raise `NameError`
- **Where:** `models/wh_session.py:447`, `models/stock_picking.py:1209`, `models/stock_picking.py:1293` (neither file defines `_logger`; pyflakes confirms).
- **Problem:** the `except Exception:` blocks that are supposed to log and continue raise `NameError` instead.
- **Impact:**
  - `:447`: any failure inside the submit notification blocks the worker's "submit for approval".
  - `:1209`: any failure in `_fg_push_ship_data` during `stock.picking.create` aborts picking creation, and with it sale-order confirmation.
  - `:1293`: blocks picking cancellation.
- **Fix:** `import logging` + `_logger = logging.getLogger(__name__)` in both files. Remove the unused `from datetime import timedelta` in `stock_picking.py` while there.

### P1-07 🟠 Carton audit route always crashes
- **Where:** `controllers/portal.py:384` calls `self._get_active_session()`, which does not exist anywhere in the module.
- **Impact:** every "🔎 فحص" press raises `AttributeError`; the JS catch (`portal_scanner.js:1172-1174`) shows "network dropped".
- **Fix:** use `_get_user_session()` + `state == 'active'` like the other routes. Add an `HttpCase` for this route.

### P1-08 🟠 Vault audit PDF crashes whenever there are differences
- **Where:** `reports/awb_reports.xml:162,165,181,182`.
- **Problem:** `missing_ids` / `extra_ids` are `fastegy.awb` records (`models/awb_audit.py:47-50,104-105`), but the template reads `stock.picking` fields: `fg_awb_number`, `fg_cod_amount`, `fg_awb_custody`.
- **Fix:** use `m.name`, `m.cod_amount`, `<span t-field="e.custody"/>`.
- **Test:** render the report for an audit with one missing and one extra AWB.

### P1-09 🟠 Portal scanner operators can read every AWB
- **Where:** `security/ir.model.access.csv:62` (`access_fg_awb_op`, read=1); record rules in `views/awb_views.xml:6-18` cover supervisor, manager and finance only.
- **Problem:** operators (portal users) have read access on `fastegy.awb` with no record rule. The portal controller never uses `fastegy.awb`.
- **Impact:** any operator can `search_read` customer names, phones and COD amounts for all warehouses/companies.
- **Fix:** drop the operator ACL row (or add a restrictive operator rule).

### P1-10 🟢 Unvalidated input → 500s with tracebacks
- **Where:** `controllers/portal.py:398,417,424,609,623,645,679` (`int(None)` / non-numeric ids), invalid selection values for `reason` in submit/shortage, non-string `barcode`; `:305` returns `str(exc)[:120]` of internal errors to the client.
- **Fix:** validate types at the route boundary and return a clean `{'error': ...}`; log internal errors server-side only.

---

## Phase 2 — Money: AWBs, COD, settlements, notifications

### P2-01 🟠 An AWB can be settled twice
- **Where:** `models/awb_settlement.py:209-231` (`action_match`), `:233-260` (`action_confirm`).
- **Problem:** "already settled" is detected only when `awb.date_settled != self.date_received`; `action_confirm` trusts the `match_state` stored at match time and never re-checks.
- **Scenario:** two settlements matched while AWB X is open, or a second one created on the same day the first was closed (`date_received` defaults to today) → confirming both writes X twice and X is counted in both `net_total`s. The same AWB twice in one statement CSV → both lines "matched", counted twice.
- **Fix:** store `settlement_line_id`/`settlement_id` on the AWB; any existing settlement ⇒ `already`. In `action_confirm`, lock the AWB rows (`SELECT ... FOR UPDATE`) and re-check. Reject duplicate `awb_id`s within one settlement.

### P2-02 🟠 "Add missing" + confirm marks unpaid AWBs as settled
- **Where:** `awb_settlement.py:271-283` (`action_add_missing`) + `:242-251`; `_compute_missing` `:108-117`.
- **Problem:** missing AWBs are added as `diff` lines with `stated_amount = 0`; confirm then writes `date_settled` and `cod_received = 0`.
- **Impact:** unpaid AWBs vanish from "money at carriers", from later "missing" lists and from overdue alerts. When the carrier finally pays, the next match flags them `already` and confirm skips them, so the payment is never recorded. `_compute_missing` does not filter `cod_kind`, so cheque/swap AWBs appear as missing, and adding them overwrites the cheque amount with 0.
- **Fix:** a separate `missing` line state that confirm never settles; filter missing AWBs to `cod_kind = 'cash'`.

### P2-03 🟠 "Return from rep" transfer erases the AWB's vault
- **Where:** `models/awb_transfer.py:186-191` → `models/awb.py:383-384`; view hides `vault_id` for `from_rep` (`views/awb_transfer_views.xml:44-47`); `_check_targets` doesn't require it (`awb_transfer.py:86`).
- **Problem:** `vault=self.vault_id` passes an empty recordset, which is `not None`, so `_fg_set_custody` writes `vault_id = False`.
- **Impact:** every confirmed `from_rep` transfer leaves the AWB "in vault" with no vault → it drops out of audits and vault totals, `_fg_open_transfer` refuses to move it, `action_returned` no longer auto-returns it.
- **Fix:** require `vault_id` for `from_rep` (or default to `awb.vault_id`); in `_fg_set_custody`, write `vault_id` only when `vault` is truthy.

### P2-04 🟠 ⚠️ AWBs scanned during an audit are probably never saved
- **Where:** `views/awb_vault_views.xml:148` (`counted_ids readonly="1"`), filled by onchange at `models/awb_audit.py:141`.
- **Problem:** the Odoo 17+ web client drops changes to readonly fields on save unless `force_save="1"`.
- **Impact:** the auditor scans 50 AWBs, clicks approve; the server sees all of them as missing.
- **Fix:** add `force_save="1"` (or make the field editable with `create="0" delete="0"`). Confirm on staging first.

### P2-05 🟠 Grouped shipments: label, AWB and customer message show different amounts
- **Where:** `models/ship_wizard.py:132-159` (`_apply` only sets `fg_ship_master_id`); `models/stock_picking.py:285-297` (`fg_cod_group_total` = root + members); `models/awb.py:267-282` (`cod_amount` = sum of the AWB's own pickings); `models/fastegy_wa.py:655` (customer message uses the root picking's own amount).
- **Problem:** members never get `fg_awb_id` = the root AWB (only the v4.0 migration ever did that). Each member keeps its own placeholder (`TMP-…`) AWB, which finance lists exclude.
- **Impact:** label prints the group total (e.g. 500); the root AWB, overdue alert and settlement expect the root amount only (e.g. 300); the customer message shows 300; the members' 200 sits on invisible placeholder AWBs.
- **Fix:** in `_apply`, link members to the root AWB and remove their placeholders; use the group total in `_fg_ship_vals`. Decide with the owner whether a data-repair migration is needed for existing grouped shipments.

### P2-06 🟡 AWB amount includes cancelled pickings; cancelling the root overwrites the heir's amount
- **Where:** `models/awb.py:267-282`; `models/stock_picking.py:1311-1343` (`_fg_ship_handle_cancel`, especially `:1334`).
- **Problem:** `cod_amount` sums every linked picking, cancelled included, and cancellation never clears `fg_awb_id`. When the root is cancelled, the heir gets `fg_cod_amount = <cancelled root's amount>`, overwriting its own per-order amount, and is linked to the same AWB through `fg_awb_number`.
- **Scenario:** root A (1,000) + member B (500); A is cancelled → B's amount becomes 1,000 and the AWB total becomes 2,000 instead of 500.
- **Fix:** filter `state != 'cancel'` in the compute (add `picking_ids.state` to `@api.depends`); detach cancelled pickings from the AWB; do not copy `fg_cod_amount` to the heir (keep the heir's own amount).

### P2-07 🟡 Every sale-order picking gets a placeholder AWB and the "غير محدد" carrier
- **Where:** `models/sale_order_ship.py:456-480` (`_fg_push_ship_data`, `always = ('fg_cod_kind', ...)`), `models/stock_picking.py:924-928` (`write` → `_fg_ensure_awb`), `:865-922`, `models/awb_carrier.py:65-76`.
- **Problem:** `fg_cod_kind` defaults to `'cash'` on the SO and is always pushed. On a picking without an AWB the related value is `False`, so it is always written, which triggers `_fg_ensure_awb` → a `TMP-<id>` AWB (with a chatter message) and the carrier "غير محدد" — for every picking of every confirmed SO, including non-shipping orders, pick/pack steps and returns. `_fg_find_or_create` also re-activates that carrier if someone archived it.
- **Fix:** push COD fields only when the picking actually ships via a carrier (`fg_ship_company` set and outgoing); don't reactivate archived carriers.
- **Data:** check `SELECT count(*) FROM fastegy_awb WHERE placeholder;` and propose a cleanup to the owner (do not delete without approval).

### P2-08 🟠 Kill-switch settings turn themselves back on; notification mode defaults to "immediate"
- **Where:** `models/res_config_settings.py:15` (`fg_wa_enabled`), `:136` (`fg_ship_quiet_enabled`), `:189` (`fg_daily_report_enabled`), `:203` (`fg_cod_overdue_enabled`), `:209` (`fg_cod_due_today_in_alert`), `fg_notify_submit`, `fg_show_status_btn`; `:128-132` (`fg_ship_notify_mode`).
- **Problem:** for `Boolean` + `config_parameter` + `default=True`, unchecking deletes the parameter; on the next load `get_values` falls back to the field default (True) so the box shows checked; the next save of any settings page writes `'True'`, and `_icp_bool_sync` then clears the `__off` mirror. WhatsApp, the daily report or the COD alert silently turn back on. Existing tests only toggle the field itself, not this round trip.
- **Also:** `fg_ship_notify_mode` has `default='immediate'` while the runtime default is `'review'`. On a fresh install the first settings save switches customer notifications to "send without finance review".
- **Fix:** override `get_values` to read the default-on keys through `_icp_bool` (or store explicit `'False'`/`'0'` instead of deleting); set the mode default to `'review'`.
- **Test:** uncheck → save → reload → save another setting → the switch must still be off.

### P2-09 🟠 Customers receive duplicate shipment messages when the WAHA session is down
- **Where:** `models/fastegy_wa.py:773-775`, `:864-872` (`_cron_ship_quiet_flush`), fallback session at `:645`.
- **Problem:** a gateway reply `{'queued': True}` sets `fg_notify_state = 'queued'`, the same state the quiet-hours queue uses. The flush cron resends every `queued` picking each hour; each resend is queued again in WAHA (or sent through another healthy session).
- **Impact:** when the session recovers, the customer receives N copies.
- **Fix:** a distinct state (e.g. `gateway_queued`) that the flush ignores, or treat gateway-queued as sent.

### P2-10 🟡 "Returned" / "To accounts" on the picking bypass the AWB lifecycle
- **Where:** `models/stock_picking.py:563-572` → `awb_move._fg_log` (`models/awb_move.py:78-122`); buttons at `views/awb_vault_views.xml:298-303`, no groups.
- **Problem:** the AWB form uses `_fg_set_custody`; the picking buttons don't.
- **Impact:** custody becomes `returned` but `cod_state` stays `out` and `delivery_state` stays `with_courier`, with no auto-return to the vault, and `rep_id` cleared. The AWB stays in overdue WhatsApp alerts (`awb.py:572-577`) and in settlements as money owed.
- **Fix:** route the picking buttons to `fg_awb_id.action_returned()` / `action_to_accounts()`; retire `_fg_log` for state changes.

### P2-11 🟡 "Lost" on the AWB form is broken (duplicate model)
- **Where:** `models/awb.py:682-693` and `models/ship_wizard.py:354-381` both define `_name = 'fastegy.awb.lost.wizard'`; `ship_wizard` loads later and wins.
- **Impact:** `FastegyAwb.action_lost` (`awb.py:536-544`) passes `default_awb_id`, which doesn't exist on the final model; `picking_id` is required and empty → can't save. An AWB without pickings can't be declared lost.
- **Fix:** one class with both `awb_id` and `picking_id`, routed through `_fg_set_custody('lost')`.

### P2-12 🟡 Cancelling a transfer that's "on the way" strands its AWBs
- **Where:** `models/awb_transfer.py:232-239`; button visible in state `sent` (`views/awb_transfer_views.xml:30-32`); `_fg_line_error` `:131-136`.
- **Impact:** AWBs stay `in_transit` with `vault_dest_id` set; no transfer type accepts `in_transit`, so the only exit is "lost" (manager) + receive again.
- **Fix:** from `sent` allow only `action_reject`, or have cancel restore the AWBs to `in_vault` at the source vault.

### P2-13 🟡 Transfers don't check where the AWB is or who is confirming
- **Where:** `models/awb_transfer.py:127-156`, `:159-213`; line domain `views/awb_transfer_views.xml:76-82`.
- **Problems:** `to_rep`/`vault_move`/`receive` don't check the AWB is in `self.vault_id` or the same warehouse (confirm then overwrites `vault_id`); `from_rep` doesn't check `awb.rep_id == self.rep_id`; `action_confirm`/`action_receive`/`action_reject` don't check the user against the vault manager/users (unlike `_fg_is_manager`).
- **Fix:** validate vault, warehouse and rep in `_fg_line_error` and the line domain; add `_fg_is_manager()` checks per action.

### P2-14 🟡 The Finance group can't do its job without supervisor rights
- **Where:** `security/ir.model.access.csv:60-68`; `security/security.xml:38-43`; root menu restricted to operators (`views/menus_root.xml:9-10`); ship/AWB/COD wizards are WH-manager only (`ir.model.access.csv:37-39`).
- **Problem:** Finance has no ACL on `fastegy.awb`, no path to the root menu, and no write on `stock.picking`. `_compute_missing`, `action_match`, `action_confirm` raise `AccessError` for a finance-only user; the finance-only menus ("💵 مبالغ تحت الاعتماد", "🧾 بوالص بانتظار المراجعة") are unreachable.
- **Impact:** finance users must also be WH supervisors, which gives them write on AWBs, transfers and vaults — defeating the separation the group was created for.
- **Fix:** give Finance read on `fastegy.awb` (settlement writes done in `sudo()` inside confirm after a finance check), its own menu path, and the minimal rights its wizards need.

### P2-15 🟡 Per-warehouse AWB visibility rule goes stale
- **Where:** `views/awb_views.xml:9`, `models/res_users_awb.py:21-36`, `models/awb_vault.py:33-39`.
- **Problem:** `ir.rule._compute_domain` is cached per uid; `invalidate_model(['fg_warehouse_ids'])` doesn't clear it, and vault create/unlink invalidate nothing.
- **Impact:** adding/removing a user on a vault has no effect until restart or another rule change; a removed user keeps seeing the AWBs.
- **Fix:** `self.env.registry.clear_cache()` on vault create/write/unlink, or replace with a static related-path domain on vault manager/auditor/users.

### P2-16 🟡 Supervisors can bypass custody rules
- **Where:** `security/ir.model.access.csv:44` (vault write for supervisors); editable fields in `views/awb_views.xml:70-71,98,110,113`; `ir.model.access.csv:60`.
- **Problems:** a supervisor can make themselves `manager_id`/`auditor_id`/member of `user_ids` on a vault (passing `_fg_is_manager`/`_fg_is_auditor` and widening their own record-rule scope); `vault_id`, `rep_id`, `warehouse_id`, `carrier_id`, `date_shipped` are editable with no move log; over RPC they can write `custody`, `cod_state`, `date_settled`, `cod_received` directly.
- **Fix:** vault write for managers only; custody fields readonly and guarded in `write()` unless called from `_fg_set_custody` (use a private method + `sudo()`, not a context flag); money fields restricted to Finance.

### P2-17 🟡 No multi-company record rules on financial models
- **Where:** `security/security.xml` — `fastegy.awb` (manager/finance rule is `1=1`), `fastegy.awb.settlement`, `fastegy.awb.transfer`, `fastegy.awb.vault`, `fastegy.awb.audit`, `fastegy.awb.move`, `fastegy.carrier`. Staging is multi-company (see the v4.3.5 comment in `stock_picking.py`).
- **Fix:** `[('company_id', 'in', company_ids)]` rules; make `fastegy.awb.move.company_id` come from `awb_id` (empty today for AWBs without pickings, `awb_move.py:42-43`). Also scan events have no company rule for supervisors.

### P2-18 🟡 `action_fg_cod_reopen` un-settles with no permission check
- **Where:** `models/stock_picking.py:484-492`; button at `views/cod_views.xml:77-79`.
- **Problem:** public method, no group check; resets `fg_cod_state='out'`, `date_settled=False`, `cod_received=0` on the AWB via related fields, leaves custody `collected`, logs only on the picking.
- **Impact:** an AWB still listed in a closed settlement can be settled again.
- **Fix:** Finance-only check, log on the AWB, and block if the AWB belongs to a closed settlement.

### P2-19 🟡 Legacy COD wizard bypasses the AWB lifecycle
- **Where:** `models/ship_wizard.py:262-317` (`fastegy.cod.wizard`), exposed at `views/cod_views.xml:71`.
- **Problem:** writes `fg_cod_state` directly, skipping `_fg_set_custody` → no custody, no `date_collected`, no `awb.move` log; settlement "missing" check misses these AWBs; compares against the root amount, not the group total.
- **Fix:** route to the AWB actions or remove the legacy buttons.

### P2-20 🟡 Saving the carton wizard clears "prepaid"
- **Where:** `models/ship_wizard.py:97,156-157`; opener `stock_picking.py:1021-1038` passes no `default_prepaid`.
- **Impact:** every save writes `fg_cod_prepaid=False`, so a prepaid shipment switches to the COD template and loses its AWB flag. `_apply` also trusts client values for `cod_amount`/`prepaid` despite the "finance lock" comment.
- **Fix:** pass `default_prepaid`; enforce the finance check server-side in `_apply`.

### P2-21 🟡 Cheque/swap shipments print and notify as "prepaid"
- **Where:** `report/fg_ship_labels.xml:83-99`; `models/fastegy_wa.py:720`.
- **Problem:** with "show COD on label" on, a cheque or swap shipment has `fg_cod_amount=0`, so the label prints "prepaid" and the courier doesn't collect the cheque/parcel. The WhatsApp template is chosen on the `fg_cod_prepaid` flag, not `cod_kind == 'prepaid'`, so a prepaid SO can get the COD template showing "0 ج.م".
- **Fix:** branch on `fg_cod_kind` before the prepaid branch in both places.

### P2-22 🟡 Misleading collection status after re-dispatch
- **Where:** `models/awb.py:385-387`, `:190-191`.
- **Problem:** sending an AWB back to a rep after a return doesn't reset `cod_state` from `returned` to `out` (missing from due-date page and alerts while out again). `cod_due_state` is `settled` for any `cod_state != 'out'`, so `collected` without `date_settled` shows "✅ settled" while settlement still treats it as owed.
- **Fix:** set `cod_state='out'` on `with_rep` when not settled/prepaid; show `settled` only when `date_settled` is set. ⚠️ Confirm with the owner what `collected` means operationally.

### P2-23 🟡 Audits are not a fixed record; ⚠️ possible search loop
- **Where:** `models/awb_audit.py:22,84-113,171`.
- **Problems:** `expected/missing/extra` and counts are non-stored and recomputed from current custody, so a done audit's screen and PDF change later; `_rec_name='display_name'` + `_search_display_name` returning the same leaf may loop in the domain parser (no search view on the model); `action_close` has no `state == 'open'` guard; the vault summary message is never posted because the vault has no `mail.thread`.
- **Fix:** store snapshot sets/counts at close; `_rec_name='vault_id'` or `_rec_names_search`; add the state guard.

### P2-24 🟡 WhatsApp sends happen inside DB transactions
- **Where:** quiet-hours flush cron (`fastegy_wa.py:864-872`, up to 100 customers × 2 images per run), `stock_picking.action_fg_notify_approve` (`:656-671`).
- **Problem:** messages go out, then a later error/serialization failure rolls back the state → resent next run. `action_fg_notify_approve` sends, then may raise on a later record → re-approve sends again. No throttle between sends.
- **Fix:** commit per picking in the cron (or send via `cr.postcommit`), validate all records before sending any, add a small delay between sends.

---

## Phase 3 — Scanner, offline queue, stock integrity

### P3-01 🟠 Session approval destroys warehouse-prepared quantities and can over-deliver
- **Where:** `models/wh_session.py:780-894` (`_apply_to_picking`), especially `:835-845` and `:851-887`.
- **Problems:**
  1. **Delivery, serials:** every lot line not scanned in a session is unlinked (`extra.unlink()`). Lines the warehouse picked manually (the v3.2.0 "prepared" feature: `picked_qty` is deducted from the worker's remaining) are therefore dropped. Example: demand 5, warehouse picks serials A, B manually, worker scans C, D, E → approval removes A and B → the picking ships 3.
  2. **Non-tracked:** only the first non-lot move line is overwritten with the session total (`line.write({'quantity': total})`). A manually prepared quantity is lost (picked 4 + scanned 6 → line becomes 6). If reservation was split across locations/packages (several lines), the other lines keep their reserved quantity → move quantity = total + leftovers → over-delivery.
- **Fix:** add the session's quantities on top of prepared quantities instead of replacing; only remove reserved (not `picked`) lot lines; when overwriting non-tracked quantities, distribute across all lines (or clear the others) so `move.quantity` equals exactly prepared + scanned.
- **Test:** prepared + scanned mixes for both serial and non-tracked products; multi-location reservation.

### P3-02 🟠 ⚠️ Direct receipts of serial products keep Odoo's blank lines
- **Where:** `models/wh_session.py:810-811` (reuse only lines with `not l.lot_id and not l.quantity`), compare `wizard/intake_pull.py:98-101` (which deletes the blanks).
- **Problem:** Odoo 17/18 `_action_assign` on receipts from a supplier (bypass reservation) creates one move line per unit for serial products, with `quantity = 1` and no lot. Those lines never match the reuse filter, so approval adds N lotted lines on top → move quantity doubles, and validation then complains about lines without a serial.
- **Existing test gap:** `tests/test_scan_engine.py` (~line 285) only counts `filtered('lot_id')`, never the total quantity.
- **Fix:** like the intake pull, remove (or reuse) the blank lines of the move before writing lotted ones. Confirm the Odoo 18 behaviour on staging first.

### P3-03 🟡 ⚠️ Delivery move lines are written at the parent location
- **Where:** `models/wh_session.py:784-785,812-828`.
- **Problem:** new lot lines use `location_id = session.source_location_id or picking.location_id` (e.g. WH/Stock), while the scan check accepts quants in any child location (`child_of`). If stock sits in sub-locations/bins, validating moves the serial out of WH/Stock (negative quant) and leaves +1 in the bin.
- **Fix:** use the lot's actual internal quant location under the source. Only relevant if sub-locations are used.

### P3-04 🟡 Cross-session duplicate protection is not concurrency-safe
- **Where:** `models/scan_event.py:1302-1317` (`_lock_lot`, `_lock_serials`), `:1423-1431`, `:1520-1527`, `:1606-1618`.
- **Problem:** the pattern is `pg_advisory_xact_lock` → `search` for a duplicate → create. Odoo cursors run in `REPEATABLE READ`; the snapshot is taken at the first query of the request, long before the lock. The second transaction waits for the lock, then searches its old snapshot and does not see the first transaction's committed event → the same serial is accepted in two open sessions. Intake serials have a partial unique index as a backstop (`models/intake_order.py:269-300`); scan events don't. The window is widest for carton batches (seconds per carton). The code comment at `:1602-1605` ("the second one waits, finds the lot") is not true under RR.
- **Fix options:**
  - (a) Make the first transaction *update* a shared row (e.g. write a claim field on `stock.lot`), so the second gets a serialization failure and Odoo retries the request.
  - (b) A claim table with a unique index on (company, serial) for open sessions, cleared on reverse/cancel/done.
  - In both cases, `process_scan_bulk`/`process_offline_batch`/`_register_batch` must re-raise concurrency errors (`SerializationFailure`, `DeadlockDetected`) instead of swallowing them in `except Exception`, so the whole request is retried.

### P3-05 🟡 Bulk scanning re-orders scans alphabetically
- **Where:** `models/scan_event.py:663` (`sorted(items, key=barcode)`); the client sends a chunk whenever more than one scan is queued (`portal_scanner.js:1336`).
- **Problem:** order matters: a product barcode sets `current_product_id` for the serials after it. Sorting can move serials before/after the wrong product scan → serials attributed to the wrong product on receipts.
- **Fix:** process in received order (Odoo already retries deadlocks).

### P3-06 🟡 Worker slices are not enforced on receipts and quantity entry
- **Where:** `models/scan_event.py:1262-1300` (`_resolve_product`), `:1549-1599` (`_resolve_new_serial`) vs `_expected_qty` `:1013` and `_out_of_scope` `:989`; `controllers/portal.py:460-465` → `scan_event.py:936-945` (`add_quantity`).
- **Problem:** these paths compare against the whole move quantity (`int(move.product_uom_qty)`), not the worker's line, and never call `_out_of_scope`. `_resolve_existing_lot` and `add_quantity` do use the slice. The `/fastegy/wh/add_qty` non-intake path uses the client's `product_id` and has no upper bound when `allow_unexpected_product` is on.
- **Impact:** a worker can receive a colleague's product or more than their share; approval writes cumulative totals → over-receipt.
- **Also:** after selecting a non-serial product, focus goes to the quantity box (`portal_scanner.js:347-352`), so a wedge scan of the product barcode becomes "add 6291234567890".
- **Fix:** use `_expected_qty` + `_out_of_scope` consistently; use `session.current_product_id` / verify the line in `add_qty`; cap quantities; treat long numeric input in the quantity box as a scan.

### P3-07 🟠 Offline queue is not bound to a session or user
- **Where:** `static/src/portal/portal_scanner.js:17,46-55,74-97,1298-1305,1364-1366`; `controllers/portal.py:344-362`; `finish()` at `portal_scanner.js:1727`.
- **Problem:** queued items carry only `{barcode, uuid, at, skew}` under one global `localStorage` key; the server replays them into whatever `_get_user_session()` returns at sync time.
- **Scenarios:** worker queues scans for delivery A, goes 🏠 and opens B → A's serials land on B. Shared handheld: logout/login as another user → the first user's queue syncs under the second account. When the session is no longer active, `res.error` → `break`, and the queue silently waits to be injected into the next active session. Submit doesn't check for a pending queue.
- **Fix:** store `session_id` (and uid) per item; the server rejects items whose session isn't the caller's active session; namespace the storage key per user and clear/warn on logout/user switch; block submit while the queue is non-empty; show stale items to the worker. Two tabs also overwrite each other's queue.

### P3-08 🟠 Session expiry is never detected
- **Where:** `static/src/portal/portal_scanner.js:184-202,718-723`.
- **Problem:** `rpc()` throws `new Error(data.error.data.message)`, dropping `error.code` (100) and `data.name`. Odoo 18 raises `SessionExpiredException("Session expired")`; the regex `/SessionExpired|…|Odoo Session Expired/` doesn't match "Session expired" (space), and "Access Denied" doesn't match either.
- **Impact:** after expiry the page never redirects to login; every scan goes to the offline queue with a success beep and "📥 الشبكة قطعت"; the error lands in the hidden `#nsdiag`. Feeds directly into P3-07.
- **Fix:** keep `err.code`/`err.data` in `rpc()`; treat `code === 100` or a name containing `SessionExpired` as an auth failure → redirect to login, never queue.

### P3-09 🟡 Offline scan timestamps are always discarded
- **Where:** `models/scan_event.py:595-601`; client `portal_scanner.js:40-42` (`toISOString()`).
- **Problem:** `toISOString()` includes milliseconds (`…:00.123Z`). Odoo 18 `fields.Datetime.to_datetime` does `strptime(value, DATETIME_FORMAT[:len(value)-2])`, which raises "unconverted data remains"; the code falls back to sync time. "Earliest scan wins" only holds inside one batch; across devices, whoever syncs first wins, contradicting the docstring and the WhatsApp template.
- **Fix:** parse with `datetime.fromisoformat(...)` and drop tzinfo (convert to UTC); clamp to `[session.start_at, now]`; keep the server receive time in a separate field.

### P3-10 🟠 Cancelling an intake order permanently blocks its serials
- **Where:** `models/intake_order.py:128-137` (`action_cancel`); unique index `fastegy_intake_serial_scanned_uniq (serial) WHERE state='scanned'` at `:281-286`; duplicate checks skip cancelled orders (`scan_event.py:1083-1086`, `1196-1199`).
- **Problem:** cancel leaves the serials `scanned`, still covered by the index; the scan checks ignore cancelled orders and proceed to insert.
- **Impact:** rescanning the same physical serials into a new order hits a raw unique violation (500, or the whole carton fails). Those serials can never be staged again.
- **Fix:** void the scanned serials on cancel; decide what `action_reset_draft` does (restore them or leave them void). A data-repair migration for already-cancelled orders needs owner approval.

### P3-11 🟡 Serials pulled into a receipt that is later cancelled can't be recovered
- **Where:** `wizard/intake_pull.py:76-134`.
- **Problem:** the pull creates `stock.lot` records and marks intake serials `pulled`; nothing reverses this when the receipt is cancelled or its move lines are deleted. `action_void` refuses `pulled`, and rescanning is blocked by the "lot exists" check (`scan_event.py:1208`).
- **Fix:** on picking cancel / move-line unlink, return the serials to `scanned` and remove lots that never got stock.

### P3-12 🟡 Non-serial products break the intake pull
- **Where:** `wizard/intake_pull.py:33-50,193-200,244-247`.
- **Problem:** `available_qty` for untracked/lot products comes from `scanned_manual` (allowed by the wizard with `serial_only=False`), but the pull only looks for serial records → `action_pull` raises "available is only 0" and the whole pull fails; `action_pull_live` marks those lines done (`pulled_done = pull_qty`) without moving stock. When the same product is on two moves, each line is offered the full availability (double count).
- **Fix:** handle quantity products explicitly (write a non-lot move line) or exclude them consistently; allocate availability across moves.

### P3-13 🟡 Intake quantity counting is inconsistent
- **Where:** `controllers/portal.py:434-439` (`scanned_manual` incremented in the controller only); `scan_event.py:275-291` + `add_quantity` (no intake branch).
- **Problem:** reverse/undo/reset never decrement `scanned_manual`, so counts stay inflated and flow into the order and the pull. The product-barcode "+1" path calls `add_quantity`, which has no intake branch (intake sessions have no picking move) → always a conflict, although the UI says "امسح باركود الصنف = +1".
- **Fix:** move intake quantity logic into the model and make reversal adjust `scanned_manual`.

### P3-14 🟡 Undo button can never work
- **Where:** `static/src/portal/portal_scanner.js:1790-1800`.
- **Problem:** looks for `l.id === focusPid`, but line dicts from `get_screen_state` have no `id` key → always "مفيش سكانات للصنف ده".
- **Fix:** `var fl = findLine(focusPid);` (match on `product_id`).

### P3-15 🟡 Torch button is broken
- **Where:** `static/src/portal/portal_scanner.js:2017,2063`.
- **Problem:** `videoTrack()` is not defined anywhere → `ReferenceError` → "مشكلة في الكشاف".
- **Fix:** `camScanner.applyVideoConstraints({advanced: [{torch: on}]})` and `getRunningTrackSettings()`.

### P3-16 🟡 Ship-photo upload has no state/type checks
- **Where:** `controllers/portal.py:720-765`; `models/stock_picking.py:321,828-853`.
- **Problem:** `session_id` from the client is only checked with `_can_open_session` (no `state`/`operation_type` check). A worker can replace the customer-facing `fg_cartons_photo` on done, cancelled or already-notified deliveries and add unlimited extra photos. The field is a plain `Binary`, so non-image bytes are accepted and can be sent to the customer.
- **Fix:** require an active delivery session for workers; validate with `odoo.tools.image` (`base64_to_image` / `image_process`); cap extra photos.

### P3-17 🟡 Scans can be lost on weak Wi-Fi or reload
- **Where:** `portal_scanner.js:849,1234-1236,187,1646-1647`.
- **Problem:** `scanQueue` lives in memory only; `fetch` has no timeout, so a request can hang for tens of seconds and anything scanned meanwhile is lost if the PWA is killed/reloaded; `setCartonBusy` disables `#scan` while a carton registers, so wedge scans in that window go nowhere (no beep).
- **Fix:** persist each scan before sending and remove it on acknowledgement; `AbortController` with ~8 s; buffer wedge input instead of disabling the field.

### P3-18 🟡 Double polling every 5 seconds; refresh race
- **Where:** `portal_scanner.js:2400-2406` (unconditional 5 s `loop()`) alongside `schedulePoll` `:2705-2732`.
- **Problem:** the idle 120 s interval and "silent when hidden" never take effect; every device hits `/fastegy/wh/state` (heavy `get_screen_state`) every 5 s. Overlapping `refresh()` calls have no sequence guard, so an older response can overwrite newer state.
- **Fix:** delete the first loop; add a request sequence number to `refresh()`.

### P3-19 🟡 Offline sync hides rejected scans
- **Where:** `portal_scanner.js:81-90`.
- **Problem:** `results` and `summary.rejected` are discarded; the toast "✅ اتزامن كل اللي اتسكن…" shows even when items were rejected.
- **Fix:** show rejected barcodes and reasons; keep them in a "needs attention" list.

### P3-20 🟡 `/fastegy/wh/damage` validation and notification
- **Where:** `controllers/portal.py:639-666`; `portal_scanner.js:2598-2608`; `models/damage_report.py:29`.
- **Problem:** `product_id` not checked against the session; `qty` unbounded; the photo is sent raw via `readAsDataURL` (many camera JPEGs exceed the 4M-char cap and are rejected); each report sends a synchronous WhatsApp to all supervisors inside the request, with no rate limit.
- **Fix:** validate the product against session lines, cap qty, reuse `compressImage()`, queue the notification.

### P3-21 🟢 Camera and completion-screen JS issues
- `toggleCamera` (`portal_scanner.js:2274-2294`) has no "starting" guard: a double tap starts two `Html5Qrcode` instances and orphans the first stream (light stays on).
- `showComplete` (`:1071-1087`) clears its own auto-back timer when every line is done, so the screen never returns automatically.

---

## Phase 4 — Migrations, crons, settings, dependencies, cleanup

### P4-01 🟠 Migration 18.0.2.8.7 uses a wrong table name and aborts the transaction
- **Where:** `migrations/18.0.2.8.7/pre-migrate.py:33-35`.
- **Problem:** `table = model.replace('.', '_')` gives `ir_actions_act_window`; the real table is `ir_act_window`. The `except` has no savepoint, so the transaction is left aborted and the upgrade dies on DBs that still have `action_fg_ship_exclude`.
- **Fix:** map model → table explicitly (`env[model]._table` is not available in pre-migrate; hard-code `ir_act_window`, `ir_ui_view`, …) and wrap in `cr.savepoint()`.

### P4-02 🟡 Direct upgrade from < 4.0 crashes in 18.0.4.2.0 pre-migrate
- **Where:** `migrations/18.0.4.2.0/pre-migrate.py:16-20` references `fg_awb_id`, which doesn't exist yet on a direct upgrade from below 4.0.
- **Fix:** check column existence first (as other scripts do).

### P4-03 🟡 AWB migration/hook data issues
- The 4.0 snapshot path doesn't carry `fg_cod_received`, `fg_cod_note`, `fg_cod_settled_date` → data lost on that path.
- `hooks.py:126` searches the raw AWB number, but `create` stores the cleaned one → numbers with spaces/Arabic digits hit the unique constraint; an exact duplicate number for a different customer is silently merged into one AWB.
- `hooks.py` `migrate_awbs()` non-snapshot path: `col('p.fg_awb_custody', ...)` checks `'p.fg_awb_custody' in cols`, but `cols` holds names without the `p.` prefix → every column falls back to its default.
- **Fix:** carry all columns; clean the number before searching; fix `col()`.

### P4-04 🟡 Raw-SQL migrations leave stored related columns stale
- **Where:** 4.2.0, 4.6.0, 4.10.0 migrations update `fastegy_awb` without touching the stored related columns on `stock_picking` (`fg_awb_custody`, `fg_cod_prepaid`, `fg_awb_placeholder`, …).
- **Fix:** mirror the update on `stock_picking` or trigger a recompute.

### P4-05 🟡 Daily report window is in UTC
- **Where:** `models/fastegy_wa.py:554-573`.
- **Problem:** `create_date >= 'D 00:00:00'` in UTC = 02:00/03:00 Cairo; sessions created between ~18:00 and 02:00 are never reported. "Done" units are counted by session creation date, not completion date.
- **Fix:** compute the Cairo-day window with `pytz` (Africa/Cairo, DST-aware) and count by `approved_at`/`end_at`.

### P4-06 🟡 Photo clean-up cron stalls after 500 pickings
- **Where:** `models/fastegy_wa.py:885-890`.
- **Problem:** clears the photo but leaves `fg_cartons_photo_at` set; the same 500 oldest pickings match every day.
- **Fix:** add `('fg_cartons_photo', '!=', False)` to the domain or clear the timestamp.

### P4-07 🟢 COD due-date cron and search helpers
- `data/wa_templates.xml:129-137`: the cron has no `nextcall`, so it runs at the install time of day; the stored late/today/week buckets are stale for part of every day. Set `nextcall` ~00:05 Cairo.
- `models/awb.py:216-224`: `_search_days_late` returns `[]` (match everything) for operators other than `>`/`>=`, and `>=` is off by one.
- `models/awb.py:318-326`: `days_out '='` compares exact datetimes.

### P4-08 🟢 ⚠️ WAHA session choice in Settings may not stick
- **Where:** `models/res_config_settings.py:18-20` vs `:57-80`.
- **Problem:** the inverse of `fg_wa_session_sel` sets the parameter during `create`; `set_values` then writes the stale hidden `fg_wa_session_name` back over it.
- **Fix:** confirm on staging; make one field the single source of truth.

### P4-09 🟡 Undeclared dependencies
The code relies on modules not listed in `depends` (some guarded, some hard):
- `sale` / `sale_stock` (`sale.order`, `sale.view_order_form`, `picking.sale_id`, `procurement.group.sale_id`)
- `account` (intake order `bill_ids`, cheque wizard `account.journal`)
- `hr` (`hr.employee`, guarded)
- the WAHA module providing `wa.session` (guarded)
- `sale_order_approvals` (`payment_type`, `action_first_approval`/`action_third_approval`, `group_financial_approve`, states in views). ⚠️ If it loads after this module, the COD/ship-data gates in those overrides never fire.
- `customer_credit_limit` (`check_credit`, guarded)
- `views/sale_order_ship_views.xml:118-119` references `delivery_type` and `show_check_repair` — the view fails to load if the providing module is absent.
- **Fix:** declare the hard ones in `depends`; keep guards for the optional ones.

### P4-10 🟢 ⚠️ Unused primary `stock.picking` list views may become the default Transfers list
- **Where:** `view_fg_cod_list`, `view_fg_custody_list` — primary views, default priority 16, used by no action. "fastegy.cod.list" sorts before "stock.picking.list".
- **Fix:** check `ir.ui.view.default_view('stock.picking', 'list')`; set `priority` 99 or delete them (as was done for the search views).

### P4-11 🟢 `_fg_inject_cod_field` is fragile
- **Where:** `data/fg_cod_post_init.xml:3`.
- **Problem:** install/upgrade fails if any primary `delivery.company` view lacks `field[@name='name']`; the injected field (`fg_cod_days`) has no effect (see low-priority table).

### P4-12 🟢 Test suite gaps
- No `HttpCase` tests for any portal route (`/fastegy/wh/*`) — add at least one per route, including the access checks from Phase 1.
- No tests for bulk/offline paths, prepared+scanned approval, settings round-trip, settlement double-confirm.
- Concurrency (P3-04) can't be covered by `TransactionCase`; document it or add a two-cursor test.

---

## 5. Low-priority items (fix opportunistically)

| Area | Item | Where |
|---|---|---|
| Money | All amounts are `Float` with no currency/rounding; settlement uses `abs(...) < 0.01` | AWB, settlement models |
| Money | Settlement date range compares UTC `date_collected` with local 00:00/23:59; transfer `f_today` filter and report `date_done` use UTC | `awb_settlement.py:114-116`, transfer views |
| AWB | `write()` doesn't normalise the AWB number (create does) → spaces/Arabic digits defeat the unique constraint and lookups | `awb.py:337-343` |
| AWB | `@api.constrains('picking_ids')` never fires (links are made from the picking side) | `awb.py:328-335` |
| AWB | Image-only save in the AWB wizard (notify off) writes `fg_awb_number=False`, blanking required `awb.name` | `ship_wizard.py` AWB wizard |
| Audit trail | Supervisors can write/create `fastegy.awb.move`; delete lines of completed transfers; Finance/managers can delete closed settlements | `ir.model.access.csv:47,59,65-68` |
| Audit trail | `awb.move.amount`/`awb_number` are stored related fields of the first picking → history changes with the picking | `awb_move.py:26-31` |
| Cheques | Catch-all `except` with no savepoint in the cheque wizard | `awb_collect_wizards.py:190-203` |
| Perf | Vault totals load every historical picking and count pickings, not AWBs | `awb_vault.py:70-79` |
| Perf | One search per settlement line | `awb_settlement.py:213-216` |
| Perf | `_compute_fg_cod_group_total` does a search per record | `stock_picking.py:285-297` |
| Perf | `serial_rule.action_check_coverage`: one search per lot for 5,000 lots; callable by portal operators | `serial_rule.py` |
| Perf | `process_scan` writes the device row on every scan (contention on shared devices; keeps `last_seen_at` fresh so telemetry heartbeat never fires) | `scan_event.py:270` |
| Perf | `_fg_apply_credit_rules` / `_fg_credit_bypass_sync` run on every `sale.order` write; a deliberate COD of 0 not yet approved is overwritten with the order total | `sale_order_ship.py` |
| ORM | Non-stored compute writes to the DB (`_compute_scanned` clears shortage via `sudo().write`) | `wh_session_line.py:186-191` |
| ORM | `@api.depends` on non-compute methods (`_search_pending_count`, `_fg_picked_qty`); `_compute_cod_due` mixes stored and non-stored fields; `display_name` depends on undeclared `picking_refs` | `wh_session.py:97`, `wh_session_line.py:130`, `awb.py:292-307` |
| ORM | Blanket `except` without savepoint (DB error leaves the transaction aborted and hides the cause) | `sale.order.write` `:520`, `damage_report.create`, picking create hook |
| Errors | `_wa()` swallows every exception with `pass`, no log | `wh_session.py:362-366` |
| Errors | `touch()` exceptions swallowed without savepoint (int4 overflow on `cpu_cores`/`rtt` → `/state` 500) | `portal.py:213-234` |
| Chatter | Plain-`str` bodies containing `<br/>` render literally (Odoo escapes non-`Markup` bodies) | `scan_event.py:635`, intake-pull chatter |
| Chatter | Literal `'\\n'` shows a backslash-n | `split_picking.py:209` |
| UoM | `int(move.product_uom_qty)` truncates and ignores UoM | many places in `scan_event.py` / `wh_session.py` |
| Settings | Unchecking `fg_ship_guard` deletes the key and the code default `'1'` keeps the guard on; clearing `fg_label_qr` can't hide the QR; hour = 0 deletes the param and falls back to 18/10; `notify_worker_assign` is never read | `res_config_settings.py` |
| Settings | System panel shows times with a fixed +2 offset (wrong during Egyptian DST) and is rebuilt on every Settings open | `res_config_settings.py:278` |
| Test mode | Implicit test mode on staging with no test number: `send()` drops messages without a log row; `_fg_is_nonprod` matches substring `'dev'` anywhere in the DB name | `fastegy_wa.py` |
| ACL | `fastegy.ship.photo`: any internal user can create/write (dispute evidence can be overwritten) | `ir.model.access.csv:40` |
| Server checks | `action_pull_intake` doesn't check the picking is a receipt; ship wizard member domain is UI-only | `stock_picking.py:127`, `ship_wizard.py` |
| Server checks | `action_fastegy_split` picks an arbitrary device (`search([], limit=1)`) | `stock_picking.py:79` |
| Portal | `_page_context` does a `set_param` per user per day from a GET (clears registry caches on all workers, grows `ir_config_parameter` with `last_open_<uid>`); `(user.name or '').split()[0]` raises on a whitespace-only name | `portal.py:182-205` |
| Portal | `wh_lookup` quant/location/intake-serial searches not filtered by company | `portal.py:501-600` |
| Portal | `scans_today` compares local date against UTC midnight; a supervisor opening a worker's session overwrites the worker's device telemetry | `wh_device.py:128` |
| Templates | Two `theme-color` metas; Google Fonts loaded externally (fails offline) although Cairo fonts are bundled; `t-esc` → `t-out`; `user-scalable=no` | `views/portal_templates.xml:8,15,18` |
| Files | `manifest.json`/`sw.js` read with `open()` and no encoding → use `file_open(..., encoding='utf-8')` | `portal.py:142,155` |
| Backend JS | `wa_editor.xml:14-15`: textarea uses a `<t t-esc="value"/>` child, so chips inserted via `record.update` aren't shown and the next keystroke overwrites them; use `t-att-value` and honour `props.readonly` | `static/src/backend/wa_editor.xml` |
| Data | `data/sequence.xml` is not `noupdate` → every upgrade resets prefix/padding | `data/sequence.xml` |
| Dead code | `static/src/scanner/*` is not in any bundle (and broken: `this.rpc` undefined, duplicated methods) → delete | `static/src/scanner/` |
| Dead code | `delivery.company.fg_cod_days`, `_fg_days_for`, `stock.picking._fg_carrier_days`, setting `fg_cod_days`, `_fg_inject_cod_field` — due date actually uses `fastegy.carrier.settlement_days`; `fastegy.carrier.commission_pct` unused despite its help text; `fastegy.awb.custody.wizard` has no view; duplicated `@api.model` at `awb_move.py:63-64`; `fg_wh_review_ok` never set (`portal.py:80`); `bind("__unused_switch")`; `for pick in self: pass` (`stock_picking.py:958`); unused imports flagged by pyflakes | various |
| Library | `html5-qrcode` bundle (~375 KB, 2.x API); upstream unmaintained since 2.3.8 | `static/src/portal/html5-qrcode.min.js` |

---

## 6. Things that already work well — do not break

- UUID idempotency backed by a DB unique constraint; the client retries with the same UUID; bulk and offline batches use a savepoint per item.
- Carton validation in intake happens fully before any write (wrong-product guard rejects without recording anything).
- Intake serials have partial unique indexes created idempotently; intake quantity uses a `FOR UPDATE` row lock; the pull takes advisory locks per order.
- AWB custody changes on the AWB form go through `_fg_set_custody` with an explicit transition map and log; double collection is rejected.
- Transfer confirm re-validates every line and blocks AWBs held in another open transfer.
- Service worker is network-first, caches only versioned static assets, never caches HTML or JSON/POST responses, purges old caches; the page is served `no-store`.
- Most portal rendering uses `textContent` / `esc()`; `fgConfirm` uses `textContent`; the v4.22 key trap stops the wedge's Enter from confirming dangerous dialogs.
- Every session-bound portal route resolves the session server-side (`_get_user_session` / `_can_open_session`); there is no portal path to approve sessions or write `stock.move.line`.
- WhatsApp test mode is honoured on every send path and tagged `[TEST]`; templates render with `str.replace` (no format-string injection).
- Views are Odoo 18 clean: no `attrs`, `states`, `<tree>`, `name_get`; crons have no `numbercall`/`doall`.
- Most migrations check column existence and use `ON CONFLICT`; 4.13.1 preserves customised templates.

---

## 7. Needs runtime confirmation on staging (⚠️)

1. P2-04 — readonly `counted_ids` dropped on save in the 18.0 client.
2. P2-23 — audit `_rec_name='display_name'` search loop.
3. P3-02 — Odoo 18 blank per-unit move lines on serial receipts.
4. P3-03 — only relevant if stock lives in sub-locations/bins.
5. P1-01 / P1-04 — confirm the payloads fire in this deployment's HTML widget (expected: yes).
6. P4-08 — WAHA session setting reverting.
7. P4-09 — load order of `sale_order_approvals` vs this module.
8. P4-10 — default `stock.picking` list view.
9. `wa.session` API assumptions: `_send_image_base64`, `get_waha_status`, queue/resend behaviour (P2-09).
10. Whether `fastegy.wh.device` records are shared between users (affects per-scan `touch()` contention).
11. Design limit to confirm with the owner: offline mode only survives while the page stays open; a reload/relaunch while offline shows the "مفيش اتصال" screen because the page itself is never cached.
