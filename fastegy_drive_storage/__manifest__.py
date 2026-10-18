{
    "name": "FastEgy Google Drive Storage",
    "version": "18.0.0.2.3",
    "summary": "Automatic application discovery, private Drive storage, routing and recovery",
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
        "views/scope_views.xml",
        "views/portal_templates.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": False,
    "development_status": "Beta",
    "uninstall_hook": "uninstall_hook",
}

