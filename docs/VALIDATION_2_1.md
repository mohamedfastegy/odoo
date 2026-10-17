# Validation of 18.0.0.2.1

Validated source commit: `a399064d474844d7e46ebb13aa139bf8c837570c`.

## Automated validation

- 63 offline connector and policy tests passed.
- 52 Odoo 18 post-install test methods passed; zero failures and zero errors. Odoo's statistics also count subtests separately.
- The hosting wrapper preserved the configured addon paths. The isolated test server used localhost, no workers and no cron threads. Only QWeb asset pregeneration was skipped in that test process. Authentication and access checks were retained; external transfers in integration cases were mocked.
- Scope regression cases cover automatic routing with no manual mappings, introduced fields on shared models, direct-column exceptions, manual and automatic storage conflicts, pause/readback, unapplied configuration, refresh idempotency, removed applications, admin access, opt-in backfill, stale queued work, explicit local rules, size checks before binary reads, and real registry provenance.
- A migration snapshot now uses the ORM cursor timestamp, including microseconds, so files created within the same transaction are included. A regression case covers that precision boundary.

## Live staging acceptance

The UI selected one installed application and activated discovery. The result was 54 persistent models and 11 attachment fields ready for routing. The 114 inventory rows also contained 49 excluded definitions, including transient wizards and internal services. These are definition counts, not counts of files or a coverage percentage.

An isolated Installation record without a customer, assigned users or followers received a native internal note. Two synthetic files began as temporary composer attachments and were reassigned by the native mail posting flow:

| File | MIME type | Bytes | Result |
| --- | --- | ---: | --- |
| scope-image.png | image/png | 73 | Private Drive storage; exact readback; no db_datas or index_content |
| scope-proof.txt | text/plain | 72 | Private Drive storage; exact readback; no db_datas or index_content |

Drive metadata matched the original names, MIME types and sizes. The note generated no notifications. A synthetic partner attachment outside the chosen application remained local. Two repeated discovery refreshes preserved the same inventory IDs. The 15-minute metadata cron was enabled, while old-file backfill remained disabled and no application migration snapshot existed for that connection.

The live connection retained its earlier manual routes. The independent integration cases establish that automatic routing works without manual model or field mappings. Runtime IDs, account details, tokens and evidence screenshots are kept outside this public repository.

## Limits

This is staging acceptance for the tested flows. Direct database columns, embedded binary content and custom filesystem writes require adapters or source/workflow review. Transient upload steps must be evaluated by their final persistent destination. Ordinary attachment metadata remains in Odoo.

The optional Documents extension was not installed. Shared drives, Picker configuration, encryption-key survival after a hosting rebuild, live recovery and load testing require separate acceptance. The current Google testing-mode OAuth lifecycle must be addressed before production. The public source branch is not automatically connected to the private Odoo.sh build repository.
