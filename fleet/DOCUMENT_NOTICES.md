# Document renderer dependency notices

This separate closure is source-only preparation, not an installed or qualified
image. No wheel or dependency implementation is distributed by this repository.
`document-closure.json` records exact official PyPI wheel URLs, SHA-256 values,
lengths and version-specific metadata URLs. Only mandatory dependencies are
selected; no ReportLab extras are enabled.

- ReportLab 4.4.9: BSD-style toolkit license in
  `reportlab-4.4.9.dist-info/licenses/LICENSE`. Retain the bundled DarkGarden
  `DarkGarden-copying.txt` and `DarkGarden-copying-gpl.txt` and Bitstream Vera
  `bitstream-vera-license.txt` font notices, even when the document generator
  does not select those fonts. Source: https://pypi.org/project/reportlab/4.4.9/.
- Pillow 13.0.0: MIT-CMU and bundled library notices in
  `pillow-13.0.0.dist-info/licenses/LICENSE`. Do not replace the complete wheel
  notice with a shortened SPDX label. Source: https://pypi.org/project/pillow/13.0.0/.
- charset-normalizer 3.5.2: MIT license in
  `charset_normalizer-3.5.2.dist-info/licenses/LICENSE`.
  Source: https://pypi.org/project/charset-normalizer/3.5.2/.

The offline verifier checks archive bytes before reading metadata and notices.
All selected notices must exist and be nonempty. After authorized installation,
it compares every immutable wheel member with the installed bytes and records
notice hashes in the image manifest. The complete wheel notices remain installed
in the environment. Source metadata and local distributions informed this list;
actual selected-wheel inspection and installed-image qualification remain pending.
This record does not relicense third-party software or historical papers.
