# Source and licensing notices

FastEgy's independent trial addon is licensed AGPL-3.0-or-later. Its source is included. No proprietary EBITDA module source was used or bundled.

Unmodified upstream dependencies are included as source under `dependencies/`:

| Addon | Version | Source | License declared by addon |
|---|---|---|---|
| fs_storage | 18.0.2.1.3 | https://github.com/OCA/storage/tree/fb81a6083c6d84d48578ae9490c27f8139290d6e/fs_storage | LGPL-3 |
| fs_attachment | 18.0.2.2.4 | https://github.com/OCA/storage/tree/fb81a6083c6d84d48578ae9490c27f8139290d6e/fs_attachment | AGPL-3 |

Keep the upstream copyright notices, license headers, READMEs, and license texts when redistributing. The repository's license is included in `LICENSES/storage-repository-LICENSE.txt`; individual addon license declarations and file notices also apply.

`base_sparse_field` is an Odoo 18 core addon, not an additional OCA addon. It is not duplicated in this bundle.

Python runtime libraries are listed in requirements files and are not redistributed in the ZIP. The development virtual environment is excluded.

Source code for the trial, including its modifications and tests, is in this package. Deployment operators should review and satisfy the licenses applicable to their use and distribution.
