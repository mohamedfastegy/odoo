{
    "name": "FastEgy Google Drive Storage",
    "version": "18.0.0.2.0",
    "summary": "Private storage, recovery manifest, job queue, health, routing and coverage audit",
    "author": "FastEgy",
    "license": "AGPL-3",
    "category": "Administration",
    "depends": ["fs_attachment", "web", "portal", "mail"],
    "external_dependencies": {"python": ["requests", "fsspec>=2025.3.0", "cryptography"]},
    "data": [
        "security/ir.model.access.csv",
        "views/fs_storage_views.xml",
        "views/migration_views.xml",
        "views/management_views.xml",
        "views/portal_templates.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": False,
    "development_status": "Beta",
    "uninstall_hook": "uninstall_hook",
}

